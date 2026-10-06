"""查詢層（唯讀）。讀最新快照，於 Python 端算統計。

刻意在 Python 算(不用 SQL 日期運算)：資料量小、邏輯與單機版一致、且轉 MSSQL 時零改動。
逾期/分帶用「今天」現算 → 天生避開跨午夜。
到期時間帶互斥：各帶相加＝未結案(對帳)。
"""
from __future__ import annotations

import datetime as dt
import re
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from collections import defaultdict

from .config import SLA_POLICY_DAYS, lead_days
from .logic import (CLOSE_DONE, CLOSE_OPEN, SEVERITIES, STAGE_EXCEPTION,
                    STAGE_EXTENSION, STAGE_ORIGINAL, _applied_exc, _applied_ext, overdue_days)
from .models import Finding, ImportBatch, SheetColumns

BANDS = ("已逾期", "30天內", "31–90天", "91–180天", "180天以上", "無到期日")
HIGH_RISK = ("Critical", "High")
SOON_DAYS = 30


def latest_batch(session: Session) -> Optional[ImportBatch]:
    return session.execute(
        select(ImportBatch).where(ImportBatch.is_latest.is_(True))
    ).scalars().first()


# 「只看某一張工作表」：由 main 的中介層依網址參數 ?sheet= 設定，這個請求內所有統計都只算那張表。
# 用 contextvar 而不是每個函式加參數：週報、到期倒數、負責人、趨勢…十幾支查詢都經過 _latest_findings，
# 一處生效就不會有哪個面板漏改、數字對不起來。
SHEET_SCOPE: ContextVar[Optional[str]] = ContextVar("SHEET_SCOPE", default=None)


def _latest_findings(session: Session, department: Optional[str] = None) -> list[Finding]:
    b = latest_batch(session)
    if not b:
        return []
    q = select(Finding).where(Finding.batch_id == b.id)
    if department and department != "全部":
        q = q.where(Finding.department == department)
    sheet = SHEET_SCOPE.get()
    if sheet:
        q = q.where(Finding.sheet_key == sheet)
    return list(session.execute(q).scalars().all())


def sheets(session: Session) -> list[dict]:
    """最新快照有哪些工作表（依原本順序）與各自未結筆數；不受 ?sheet= 影響。"""
    b = latest_batch(session)
    if not b:
        return []
    order: list[str] = []
    cnt: dict[str, int] = {}
    for f in session.execute(select(Finding).where(Finding.batch_id == b.id).order_by(Finding.id)).scalars().all():
        k = f.sheet_key or "未分類"
        if k not in cnt:
            cnt[k] = 0
            order.append(k)
        if f.close_status == CLOSE_OPEN:
            cnt[k] += 1
    return [{"sheet_key": k, "open": cnt[k]} for k in order]


def reconcile_check(session: Session, department: Optional[str] = None,
                    today: Optional[dt.date] = None) -> dict:
    """對帳健檢（讓操作者不靠 AI 也能確認數字兜得起來）：跑一組「A 應等於 B」不變式，
    每條回實際數字＋是否相等。全綠＝畫面各數字彼此一致、且對得上匯入總列數。"""
    today = today or dt.date.today()
    b = latest_batch(session)
    allf = _latest_findings(session, None)
    fs = _latest_findings(session, department)
    open_ = [f for f in fs if f.close_status == CLOSE_OPEN]
    closed = [f for f in fs if f.close_status == CLOSE_DONE]
    other = [f for f in fs if f.close_status not in (CLOSE_OPEN, CLOSE_DONE)]
    n = len(open_)

    # 到期桶(互斥)
    due = {"overdue": 0, "d30": 0, "d31_60": 0, "d61_90": 0, "d90plus": 0, "no_due": 0}
    for f in open_:
        if not f.effective_due:
            due["no_due"] += 1; continue
        d = (f.effective_due - today).days
        due["overdue" if d < 0 else "d30" if d <= 30 else "d31_60" if d <= 60
            else "d61_90" if d <= 90 else "d90plus"] += 1
    # 處置階段
    stg = {"original": 0, "extension": 0, "exception": 0, "other": 0}
    for f in open_:
        k = {STAGE_ORIGINAL: "original", STAGE_EXTENSION: "extension",
             STAGE_EXCEPTION: "exception"}.get(f.stage, "other")
        stg[k] += 1
    # 嚴重度(含 Unknown)
    sev = len([f for f in open_ if f.severity in SEVERITIES])
    sev_unknown = n - sev
    # 各部門未結相加(全域)
    dept_open = {}
    for f in allf:
        if f.close_status == CLOSE_OPEN:
            dept_open[f.department or "（未填）"] = dept_open.get(f.department or "（未填）", 0) + 1

    def eq(name, a_label, a, b_label, b):
        return {"name": name, "a_label": a_label, "a": a, "b_label": b_label, "b": b, "ok": a == b}

    checks = [
        eq("未結案 ＝ 到期各桶相加", "未結案", n,
           "+".join(str(due[k]) for k in ["overdue", "d30", "d31_60", "d61_90", "d90plus", "no_due"]),
           sum(due.values())),
        eq("未結案 ＝ 處置階段相加", "未結案", n,
           "原始%d+展延%d+例外%d+其他%d" % (stg["original"], stg["extension"], stg["exception"], stg["other"]),
           sum(stg.values())),
        eq("未結案 ＝ 嚴重度相加", "未結案", n, "四級%d+Unknown%d" % (sev, sev_unknown), sev + sev_unknown),
        eq("本報表全部列 ＝ 未結＋已結＋其他", "全部列", len(fs),
           "未結%d+已結%d+其他%d" % (n, len(closed), len(other)), n + len(closed) + len(other)),
    ]
    if not department or department == "全部":
        checks.append(eq("全部未結 ＝ 各部門未結相加", "全部未結", n,
                         "%d 個部門相加" % len(dept_open), sum(dept_open.values())))

    return {
        "scope": department or "全部",
        "source_file": b.source_file if b else None,
        "imported_at": b.imported_at.isoformat() if b else None,
        "import_rows": b.row_count if b else 0,
        "latest_rows": len(allf),
        "all_ok": all(c["ok"] for c in checks),
        "checks": checks,
    }


def action_line(f: Finding, today: dt.date) -> dt.date | None:
    """行動線＝真正到期日 − 申請提前期（依嚴重度）。"""
    if not f.effective_due:
        return None
    return f.effective_due - dt.timedelta(days=lead_days(f.severity))


def should_apply(f: Finding, today: dt.date) -> bool:
    """應提申請未提：未結案、還在原始修補期限(＝尚未申請的代理)、已過行動線。"""
    al = action_line(f, today)
    return (f.close_status == CLOSE_OPEN and f.stage == STAGE_ORIGINAL
            and al is not None and al <= today)


def _band(f: Finding, today: dt.date) -> str:
    if not f.effective_due:
        return "無到期日"
    d = (f.effective_due - today).days  # 距到期天數，負數＝已逾期
    if d < 0:
        return "已逾期"
    if d <= 30:
        return "30天內"
    if d <= 90:
        return "31–90天"
    if d <= 180:
        return "91–180天"
    return "180天以上"


def departments(session: Session) -> list[str]:
    b = latest_batch(session)
    if not b:
        return []
    rows = session.execute(
        select(Finding.department).where(Finding.batch_id == b.id).distinct()
    ).scalars().all()
    return sorted(d for d in rows if d)


def owners(session: Session) -> list[str]:
    """最新快照的既有負責人清單(去重排序)，供編輯視窗可搜尋下拉。"""
    b = latest_batch(session)
    if not b:
        return []
    rows = session.execute(
        select(Finding.owner).where(Finding.batch_id == b.id).distinct()
    ).scalars().all()
    return sorted(o.strip() for o in rows if o and o.strip())


def summary(session: Session, department: Optional[str] = None,
            lead: int = 0, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    fs = _latest_findings(session, department)
    open_ = [f for f in fs if f.close_status == CLOSE_OPEN]
    done = [f for f in fs if f.close_status == CLOSE_DONE]

    bands = {k: 0 for k in BANDS}
    for f in open_:
        bands[_band(f, today)] += 1

    # 到期倒數桶(互斥，依距到期天數)：已逾期/30天內/31–60/61–90/90天以上/無到期日
    # lead＝申請提前量；給值時用「行動期限＝到期−lead」倒數(該動手倒數)
    due = {"overdue": 0, "d30": 0, "d31_60": 0, "d61_90": 0, "d90plus": 0, "no_due": 0}
    for f in open_:
        if not f.effective_due:
            due["no_due"] += 1; continue
        d = (f.effective_due - today).days - lead
        if d < 0: due["overdue"] += 1
        elif d <= 30: due["d30"] += 1
        elif d <= 60: due["d31_60"] += 1
        elif d <= 90: due["d61_90"] += 1
        else: due["d90plus"] += 1

    sev = {k: 0 for k in SEVERITIES}
    for f in open_:
        if f.severity in sev:
            sev[f.severity] += 1

    total = len(open_) + len(done)
    b = latest_batch(session)
    imported = b.imported_at if b else None
    return {
        "department": department or "全部",
        "unresolved": len(open_),
        "overdue": bands["已逾期"],
        "due_soon": bands["30天內"],
        "high_risk": sum(1 for f in open_ if f.severity in HIGH_RISK),
        "closed": len(done),
        "close_rate": round(len(done) / total * 100, 1) if total else 0.0,
        "bands": bands,          # 互斥；相加＝unresolved（對帳）
        "due_buckets": due,      # 到期倒數桶(互斥)：overdue/d30/d31_60/d61_90/d90plus/no_due
        "severity": sev,
        # 行動線：應提申請未提
        "should_apply": sum(1 for f in open_ if should_apply(f, today)),
        "lead_days": lead_days(None),   # 預設提前期天數
        # 缺口（會被漏掉的洞）
        "gaps": {
            "no_owner": sum(1 for f in open_ if not (f.owner or "").strip()),
            "no_due": sum(1 for f in open_ if not f.effective_due),
        },
        # 資料新鮮度
        "freshness": {
            "imported_at": imported.isoformat() if imported else None,
            "days_ago": (today - imported.date()).days if imported else None,
        },
    }


def display_host(f) -> Optional[str]:
    """畫面顯示用的主機：Excel 沒有主機/IP 的列（如 10-外部威脅情資）退回顯示「資產名稱」。
    只影響顯示；DB 的 host 與承辦疊加層的鍵(sheet|plugin|host)不動，避免重匯後承辦進度對不上。"""
    if f.host:
        return f.host
    raw = f.raw or {}
    a = raw.get("資產名稱")
    return str(a).strip() if a not in (None, "") and str(a).strip() else f.host


def _note_no_progress(c) -> bool:
    """追蹤備註有寫、但處理進度沒設（狀態沒轉，卻以為轉了）。"""
    if c is None:
        return False
    from .logic import PROGRESS_VALUES
    return bool((c.track_note or "").strip()) and c.status not in PROGRESS_VALUES


def _is_reported(c) -> bool:
    """承辦有沒有回報過：處理進度、預計完成日、追蹤備註任一有填（不看 Excel，只看系統內疊加欄）。"""
    if c is None:
        return False
    from .logic import PROGRESS_VALUES
    return (c.status in PROGRESS_VALUES) or bool((c.track_note or "").strip()) or (c.target_date is not None)


def find(session: Session, department: Optional[str] = None, status: str = CLOSE_OPEN,
         owner: Optional[str] = None, severity: Optional[str] = None,
         band: Optional[str] = None, keyword: Optional[str] = None,
         sheet_key: Optional[str] = None, stage: Optional[str] = None,
         only_should_apply: bool = False, applied: bool = False, apply_intent: bool = False,
         progress: Optional[str] = None,
         no_owner: bool = False, no_due: bool = False,
         risk: Optional[str] = None, apply_universe: bool = False, not_apply: bool = False,
         no_target: bool = False, flagged: bool = False, reported: bool = False,
         note_no_progress: bool = False,
         due_min: Optional[int] = None, due_max: Optional[int] = None, lead: int = 0,
         today: Optional[dt.date] = None) -> list[dict]:
    """下鑽明細。status 預設未結案；band 互斥分帶；keyword 多字 AND；
    only_should_apply/no_owner/no_due 為缺口/行動線清單；applied=已申請處置中(例外/展延，官方)；
    apply_intent=管理人標「要申請展延/例外」(送審中，尚未在 Excel 反映)。"""
    today = today or dt.date.today()
    fs = _latest_findings(session, department)

    if status and status != "全部":
        fs = [f for f in fs if f.close_status == status]
    if owner:
        fs = [f for f in fs if f.owner == owner]
    if severity:
        fs = [f for f in fs if f.severity == severity]
    if sheet_key:
        fs = [f for f in fs if f.sheet_key == sheet_key]
    if stage:
        fs = [f for f in fs if f.stage == stage]
    if applied:  # 已申請處置中：備註有申請紀錄→階段已成 例外/展延(備註閘門)
        fs = [f for f in fs if f.stage in (STAGE_EXCEPTION, STAGE_EXTENSION)]
    if apply_intent:  # 管理人標「要申請展延/例外」(不論官方階段，含尚未反映的送審中)
        from .models import Case as _C
        from .logic import PROGRESS_APPLY_EXT, PROGRESS_APPLY_EXC
        _ks = {c.vuln_key for c in session.execute(
            select(_C).where(_C.status.in_((PROGRESS_APPLY_EXT, PROGRESS_APPLY_EXC)))).scalars().all()}
        fs = [f for f in fs if "|".join(vuln_key(f)) in _ks]
    if progress:  # 管理人處理進度精確比對(如 等複掃)；對 Case.status
        from .models import Case as _C2
        _pk = {c.vuln_key for c in session.execute(
            select(_C2).where(_C2.status == progress)).scalars().all()}
        fs = [f for f in fs if "|".join(vuln_key(f)) in _pk]
    if band:
        fs = [f for f in fs if _band(f, today) == band]
    if only_should_apply:
        fs = [f for f in fs if should_apply(f, today)]
    if no_owner:
        fs = [f for f in fs if not (f.owner or "").strip()]
    if no_due:
        fs = [f for f in fs if not f.effective_due]
    # 總覽「每個數字可下鑽」用的組合篩選：
    if risk == "high":   # 高風險 = Critical/High
        fs = [f for f in fs if f.severity in HIGH_RISK]
    _in_universe = lambda f: should_apply(f, today) or f.stage in (STAGE_EXCEPTION, STAGE_EXTENSION)
    if apply_universe:   # 需申請母體 = 應申請未申請 + 已申請處置中
        fs = [f for f in fs if _in_universe(f)]
    if not_apply:        # 還不急 = 未結但不在申請母體(原始階段、未過行動線)
        fs = [f for f in fs if not _in_universe(f)]
    if no_target:        # 母體內「未回報預計完成日」(要催)
        from .models import Case as _Ct
        _tgt = {c.vuln_key for c in session.execute(
            select(_Ct).where(_Ct.target_date.isnot(None))).scalars().all()}
        fs = [f for f in fs if _in_universe(f) and "|".join(vuln_key(f)) not in _tgt]
    if flagged:          # 需追查⚠️(說要申請/做完卻未反映：待查或可疑)
        from .models import Case as _Cf
        from .logic import classify_progress, FLAGGED_STATES, PROGRESS_VALUES, CLOSE_DONE as _CD
        _b2 = latest_batch(session); _imp2 = _b2.imported_at if _b2 else None
        _cm = {c.vuln_key: c for c in session.execute(select(_Cf)).scalars().all()}
        def _flag(f):
            c = _cm.get("|".join(vuln_key(f)))
            p = c.status if (c and c.status in PROGRESS_VALUES) else ""
            return classify_progress(p, f.close_status == _CD, f.stage,
                                     (c.status_changed_at if c else None), _imp2) in FLAGGED_STATES
        fs = [f for f in fs if _flag(f)]
    if note_no_progress:   # ⚠ 待補進度：追蹤備註有寫、處理進度沒設（常見：寫了「修補完畢」卻忘了設等複掃）
        from .models import Case as _Cn
        _nk = {c.vuln_key for c in session.execute(select(_Cn)).scalars().all() if _note_no_progress(c)}
        fs = [f for f in fs if "|".join(vuln_key(f)) in _nk]
    if reported:         # 已回報：處理進度／預計完成日／追蹤備註任一有填
        from .models import Case as _Cr
        _rk = {c.vuln_key for c in session.execute(select(_Cr)).scalars().all() if _is_reported(c)}
        fs = [f for f in fs if "|".join(vuln_key(f)) in _rk]
    if due_min is not None or due_max is not None:  # 距到期天數範圍(到期倒數)；lead=申請提前量(行動期限=到期−lead)
        def _dd(f):
            return (f.effective_due - today).days - lead if f.effective_due else None
        fs = [f for f in fs if _dd(f) is not None
              and (due_min is None or _dd(f) >= due_min)
              and (due_max is None or _dd(f) <= due_max)]
    if keyword:
        terms = [t.lower() for t in keyword.split() if t.strip()]
        def hit(f: Finding) -> bool:
            hay = " ".join(str(x or "").lower() for x in (f.host, f.owner, f.name, f.plugin_id))
            return all(t in hay for t in terms)
        fs = [f for f in fs if hit(f)]

    # 系統內寫的疊加欄(追蹤備註/預計完成日/處理進度)：依穩定鍵對 Case 帶進每列(非 Excel 原值)
    from .models import Attachment, Case
    from .logic import PROGRESS_VALUES, classify_progress, CLOSE_DONE
    ov = {c.vuln_key: c for c in session.execute(
        select(Case).where((Case.track_note.isnot(None)) | (Case.target_date.isnot(None))
                           | (Case.status.in_(PROGRESS_VALUES)))).scalars().all()}
    # 申請佐證文件數(依穩定鍵)：清單顯示 📎N
    att_count: dict = {}
    for (vk,) in session.execute(select(Attachment.vuln_key)).all():
        att_count[vk] = att_count.get(vk, 0) + 1
    _b = latest_batch(session)
    _imp = _b.imported_at if _b else None

    def row(f: Finding) -> dict:
        c = ov.get("|".join(vuln_key(f)))   # Case.vuln_key 是字串(| 接)
        progress = (c.status if (c and c.status in PROGRESS_VALUES) else "")
        pstate = classify_progress(progress, f.close_status == CLOSE_DONE, f.stage,
                                   (c.status_changed_at if c else None), _imp)
        return {
            "id": f.id, "sheet_key": f.sheet_key, "plugin_id": f.plugin_id, "name": f.name,
            "host": display_host(f), "severity": f.severity, "department": f.department, "owner": f.owner,
            "effective_due": f.effective_due.isoformat() if f.effective_due else None,
            "remediation_due": f.remediation_due.isoformat() if f.remediation_due else None,  # 原始/應計修補期限(展延前)
            "first_extension_due": f.first_extension_due.isoformat() if f.first_extension_due else None,  # Excel 首次展延上限(原值)
            "exception_due": f.exception_due.isoformat() if f.exception_due else None,  # Excel 例外核准期限(原值)
            # 日期有填但備註沒有對應申請紀錄＝不算數（compute_effective_due 的規則）；畫面用來把那格標灰
            "ext_applied": _applied_ext(f.remark), "exc_applied": _applied_exc(f.remark),
            "overdue_days": overdue_days(f.effective_due, today),
            "action_line": action_line(f, today).isoformat() if action_line(f, today) else None,
            "should_apply": should_apply(f, today),
            "stage": f.stage, "close_status": f.close_status, "remark": f.remark,
            "track_note": c.track_note if c else None,
            "target_date": (c.target_date.isoformat() if (c and c.target_date) else None),
            "progress": progress,          # 管理人手動標(處理中/要申請展延/要申請例外/等複掃/'')
            "progress_state": pstate,      # 進度對帳(等複掃→複掃三態；要申請→申請三態；否則 None)
            "att_count": att_count.get("|".join(vuln_key(f)), 0),   # 申請佐證文件數(📎)
            "raw": f.raw or {},   # 原始整列(原欄名→原值)，供「匯出此清單」帶出全部原始欄位
        }

    return [row(f) for f in fs]


# 單機版解析器(sheets.js parseCounts)的「計數字串」，如「中*4 低*2」：一列會展開成 4+2 筆紀錄。
_COUNT_RE = re.compile(r"(嚴重|critical|高|high|中|medium|低|low|info)\s*[\*xX×]\s*(\d+)", re.I)


def _count_total(raw: dict) -> Optional[int]:
    """這列是「計數字串」列的話，回傳它會展開成幾筆；不是就回 None。"""
    for v in raw.values():
        if isinstance(v, str):
            ms = _COUNT_RE.findall(v)
            if ms:
                return sum(int(n) for _, n in ms)
    return None


def _collapse_counted(rows: list) -> list:
    """把「計數字串」展開出來的重複列收回原本那一列。

    為什麼：匯入時「中*4 低*2」這種列被(刻意)展開成 6 筆、每筆都帶同一份原始列。
    快照若照存回前端，前端重組 Excel 再解析時每一列又會再展開一次（6 列 × 6 = 36），
    2026-10-05 實測第 9 表：Excel 28 列 → DB 136 筆 → 畫面 1042 筆。
    規則：連續且內容完全相同的 L 列，若該列計數總和為 n(>1) 且 L 是 n 的倍數，收成 L/n 列
    （Excel 本來就有兩列一模一樣時也保得住）；對不上就原樣不動，寧可不收也不要收錯。"""
    out: list = []
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1] == rows[i]:
            j += 1
        run = j - i + 1
        n = _count_total(rows[i]) if isinstance(rows[i], dict) else None
        keep = run // n if (n and n > 1 and run % n == 0) else run
        out.extend(rows[i] for _ in range(keep))
        i = j + 1
    return out


def _drop_blank_columns(columns: list, rows: list) -> tuple[list, list]:
    """去掉「沒有欄名、而且整欄都沒值」的欄。原始 Excel 的使用範圍常被拉到最後一欄(XFD)，
    2026-10-05 實測第 3 表 16,377 欄只有 33 欄有值，前端重組 Excel 光這張就多花 1 秒以上。
    有欄名的欄一律保留（就算整欄空白），不改原封匯出(export 用 sheet_columns，不經這裡)。"""
    def blank(v):
        return v is None or (isinstance(v, str) and v.strip() == "")
    used = set()
    for r in rows:
        for k, v in r.items():
            if not blank(v):
                used.add(k)
    keep = [c for c in columns if str(c).strip() or c in used]
    keep_set = set(keep)
    slim = [{k: v for k, v in r.items() if k in keep_set or not blank(v)} for r in rows]
    return keep, slim


# 開頁輕量版：平均每格超過這麼多字的欄先不送（Description、Plugin Output 之類，約占快照七成大小）。
# 原畫面不用這些欄；要看完整內容的「原始資料」與「完整匯出」另外向伺服器要，不經快照。
HEAVY_AVG_CHARS = 150
# 解析器會用到的欄（profiles.js fieldAliases），再長也一律保留，不然畫面會少欄位
_PROTECT_COLS = ["負責單位", "部門", "負責人", "負責人員", "Host", "內部Host IP", "標的IP", "IP", "網址", "資產名稱",
                 "Name", "風險項目", "發現", "標的", "Audit Name", "外部情資", "Plugin ID",
                 "發現嚴重性", "Finding Severity", "Risk Severity", "風險等級", "Grade", "風險",
                 "修補期限", "改善期限", "預計完成日期", "改善完成日", "首次展延上限", "首次展延期限", "例外核准期限",
                 "預計完成日", "修補完成日", "結案狀態", "改善狀況", "結案日期", "備註"]


def _nk(v) -> str:
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(v))).lower()


_PROTECT_NK = [_nk(c) for c in _PROTECT_COLS]


def _drop_heavy_columns(columns: list, rows: list) -> tuple[list, list, list]:
    """去掉長文字欄（輕量版快照用）。回傳 (保留欄, 瘦身後的列, 被拿掉的欄)。"""
    heavy = []
    for c in columns:
        k = _nk(c)
        if any(k == p or k.startswith(p) for p in _PROTECT_NK):
            continue
        vals = [r.get(c) for r in rows if r.get(c) not in (None, "")]
        if vals and sum(len(str(v)) for v in vals) / len(vals) > HEAVY_AVG_CHARS:
            heavy.append(c)
    if not heavy:
        return columns, rows, []
    hs = set(heavy)
    return ([c for c in columns if c not in hs],
            [{k: v for k, v in r.items() if k not in hs} for r in rows], heavy)


# 伺服器端快取：同一批資料只算一次（開機後、匯入後先算好），之後開頁直接回傳 bytes。
# 2026-10-06 實測 221：每次重算 0.44 秒；快照只跟匯入批次有關（不含承辦疊加層），批次沒換就不會變。
_SNAP_CACHE: dict = {}


def snapshot_bytes(session: Session, light: bool = True) -> bytes:
    import json as _json
    b = latest_batch(session)
    key = (b.id if b else None, b.imported_at.isoformat() if (b and b.imported_at) else None, light)
    hit = _SNAP_CACHE.get(key)
    if hit is not None:
        return hit
    data = _json.dumps(snapshot(session, light=light), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    _SNAP_CACHE.clear()          # 只留最新一批，舊的丟掉
    _SNAP_CACHE[key] = data
    return data


def warm_snapshot_cache() -> None:
    """背景預先算好輕量快照（開機、匯入後呼叫）；失敗不影響服務，下次有人開頁再算。"""
    from .db import SessionLocal
    try:
        with SessionLocal() as s:
            snapshot_bytes(s, light=True)
    except Exception as e:  # noqa: BLE001
        print(f"[snapshot] 預熱失敗：{e!r}")


def snapshot(session: Session, light: bool = False) -> dict:
    """回傳最新快照的『原封』內容（各表欄序＋每列 raw），供網頁前端重建 workbook、
    餵回單機版原本的解析/render pipeline，畫面與單機版一模一樣。
    送出前做兩件事（都不改 DB）：收回計數字串展開的重複列、去掉沒名字的空白欄。"""
    b = latest_batch(session)
    if not b:
        return {"source_file": None, "imported_at": None, "sheets": []}
    scs = {sc.sheet_key: sc.columns for sc in session.execute(
        select(SheetColumns).where(SheetColumns.batch_id == b.id)).scalars().all()}
    findings = session.execute(
        select(Finding).where(Finding.batch_id == b.id).order_by(Finding.id)).scalars().all()
    order: list[str] = []
    by: dict[str, list] = {}
    for f in findings:
        k = f.sheet_key or "未分類"
        if k not in by:
            by[k] = []
            order.append(k)
        by[k].append(f.raw or {})
    sheets = []
    for k in order:
        rows = _collapse_counted(by[k])
        cols = scs.get(k) or (list(rows[0].keys()) if rows else [])
        cols, rows = _drop_blank_columns(cols, rows)
        dropped: list = []
        if light:
            cols, rows, dropped = _drop_heavy_columns(cols, rows)
        sheets.append({"name": k, "columns": cols, "rows": rows, "omitted": dropped})
    return {"source_file": b.source_file,
            "imported_at": b.imported_at.isoformat() if b.imported_at else None,
            "light": light, "sheets": sheets}


def matrix(session: Session, department: Optional[str] = None,
           today: Optional[dt.date] = None) -> dict:
    """交叉分析：嚴重度 × 到期時間帶（未結案）。列/欄總和皆可對帳到未結案總數。"""
    today = today or dt.date.today()
    open_ = [f for f in _latest_findings(session, department) if f.close_status == CLOSE_OPEN]
    grid: dict = {s: {b: 0 for b in BANDS} for s in SEVERITIES}
    unknown = {b: 0 for b in BANDS}
    has_unknown = False
    for f in open_:
        b = _band(f, today)
        if f.severity in grid:
            grid[f.severity][b] += 1
        else:
            unknown[b] += 1
            has_unknown = True
    order = list(SEVERITIES) + (["Unknown"] if has_unknown else [])
    if has_unknown:
        grid["Unknown"] = unknown
    return {
        "severities": order,
        "bands": list(BANDS),
        "cells": grid,  # cells[severity][band] = 數
        "row_totals": {s: sum(grid[s].values()) for s in order},
        "col_totals": {b: sum(grid[s][b] for s in order) for b in BANDS},
        "total": len(open_),
    }


def stage_stats(session: Session, department: Optional[str] = None,
                today: Optional[dt.date] = None) -> dict:
    """例外／展延階段統計（未結案）：各處置階段計數＋占比＋『例外核准未到期』安全名單。"""
    today = today or dt.date.today()
    open_ = [f for f in _latest_findings(session, department) if f.close_status == CLOSE_OPEN]
    total = len(open_)
    order = [STAGE_EXCEPTION, STAGE_EXTENSION, STAGE_ORIGINAL]
    cnt = {k: 0 for k in order}
    other = 0
    for f in open_:
        if f.stage in cnt:
            cnt[f.stage] += 1
        else:
            other += 1
    stages = [{"key": k, "count": cnt[k],
               "pct": round(cnt[k] / total * 100, 1) if total else 0.0} for k in order]
    if other:
        stages.append({"key": "未定期限", "count": other,
                       "pct": round(other / total * 100, 1) if total else 0.0})
    # 安全名單：例外管理中 且 真正到期日尚未到（例外核准未到期）
    safe = sum(1 for f in open_ if f.stage == STAGE_EXCEPTION
               and f.effective_due and f.effective_due > today)
    return {"total": total, "stages": stages, "safe_count": safe}


def _is_overdue(f: Finding, today: dt.date) -> bool:
    od = overdue_days(f.effective_due, today)
    return od is not None and od > 0


def owner_summary(session: Session, department: Optional[str] = None,
                  due_max: Optional[int] = None, lead: int = 0,
                  today: Optional[dt.date] = None) -> list[dict]:
    """負責人角度（主管要的『誰還有幾隻、各自什麼狀態』）：
    每位負責人未結案 總數 ＋ 處置階段分佈(原始/首次展延/例外管理) ＋ 等複掃(自行結案請複審) ＋ 逾期。
    stage 合計＝total；rescan/overdue 為疊加標記(子集)。依逾期、總數排序。
    due_max 給值時只算『距到期 ≤ due_max 天』(含已逾期；無到期日排除)，供近期到期彙總。"""
    from collections import defaultdict
    from .models import Case
    from .logic import PROGRESS_RESCAN, PROGRESS_VALUES
    today = today or dt.date.today()
    fs = [f for f in _latest_findings(session, department) if f.close_status == CLOSE_OPEN]
    if due_max is not None:
        fs = [f for f in fs if f.effective_due and (f.effective_due - today).days - lead <= due_max]
    prog = {c.vuln_key: c.status for c in session.execute(
        select(Case).where(Case.status.in_(PROGRESS_VALUES))).scalars().all()}

    agg: dict = defaultdict(lambda: {"total": 0, "original": 0, "extension": 0, "exception": 0,
                                     "other": 0, "rescan": 0, "overdue": 0, "_depts": set()})
    for f in fs:
        name = (f.owner or "").strip() or "— 未指派"
        a = agg[name]
        a["total"] += 1
        if f.stage == STAGE_ORIGINAL: a["original"] += 1
        elif f.stage == STAGE_EXTENSION: a["extension"] += 1
        elif f.stage == STAGE_EXCEPTION: a["exception"] += 1
        else: a["other"] += 1
        if _is_overdue(f, today): a["overdue"] += 1
        if prog.get("|".join(vuln_key(f))) == PROGRESS_RESCAN: a["rescan"] += 1
        if f.department: a["_depts"].add(f.department)
    rows = []
    for name, a in agg.items():
        depts = sorted(a.pop("_depts"))
        rows.append({"owner": name, "department": "、".join(depts), **a})
    rows.sort(key=lambda r: (-r["overdue"], -r["total"]))
    return rows


def _ranking(session: Session, key_fn, department: Optional[str], today: dt.date) -> list[dict]:
    fs = _latest_findings(session, department)
    agg: dict = defaultdict(lambda: {"unresolved": 0, "overdue": 0, "should_apply": 0,
                                     "high_risk": 0, "closed": 0})
    for f in fs:
        a = agg[key_fn(f)]
        if f.close_status == CLOSE_OPEN:
            a["unresolved"] += 1
            if _is_overdue(f, today):
                a["overdue"] += 1
            if should_apply(f, today):
                a["should_apply"] += 1
            if f.severity in HIGH_RISK:
                a["high_risk"] += 1
        elif f.close_status == CLOSE_DONE:
            a["closed"] += 1
    rows = []
    for name, a in agg.items():
        total = a["unresolved"] + a["closed"]
        rows.append({"name": name, **a,
                     "close_rate": round(a["closed"] / total * 100, 1) if total else 0.0})
    rows.sort(key=lambda r: (-r["overdue"], -r["unresolved"]))
    return rows


def ranking_by_owner(session: Session, department: Optional[str] = None,
                     today: Optional[dt.date] = None) -> list[dict]:
    """負責人數量排行榜（依逾期多寡）。"""
    return _ranking(session, lambda f: (f.owner or "").strip() or "— 未指派",
                    department, today or dt.date.today())


def ranking_by_department(session: Session, today: Optional[dt.date] = None) -> list[dict]:
    return _ranking(session, lambda f: (f.department or "").strip() or "— 未填",
                    None, today or dt.date.today())


def vuln_key(f: Finding) -> tuple:
    """穩定識別鍵。有 row_key(新制，見 rowkey.py)就用它；沒有(轉換前或轉換失敗)才用舊的
    sheet+plugin+正規化 host。轉換是全庫一次做完，所以同一時間不會新舊混用。"""
    rk = getattr(f, "row_key", None)
    if rk:
        return (rk,)
    host = (f.host or "").strip().lower()
    return ((f.sheet_key or ""), (f.plugin_id or ""), host)


def _two_latest_batches(session: Session):
    bs = session.execute(
        select(ImportBatch).order_by(ImportBatch.imported_at.desc(), ImportBatch.id.desc()).limit(2)
    ).scalars().all()
    return (bs[0] if bs else None, bs[1] if len(bs) > 1 else None)


def trend(session: Session, department: Optional[str] = None,
          today: Optional[dt.date] = None, limit: int = 12) -> list[dict]:
    """未結趨勢：每次匯入(批)當下的未結數與其中逾期數，依時間排序。供主管週報折線。

    歷史批留著(is_latest 只標最新；舊批的 finding 仍在)，所以能回看每批的未結量。
    """
    today = today or dt.date.today()
    batches = session.execute(
        select(ImportBatch).order_by(ImportBatch.imported_at.asc(), ImportBatch.id.asc())
    ).scalars().all()
    if limit and len(batches) > limit:
        batches = batches[-limit:]        # 只看最近 N 批
    out = []
    for b in batches:
        q = select(Finding).where(Finding.batch_id == b.id, Finding.close_status == CLOSE_OPEN)
        if department and department != "全部":
            q = q.where(Finding.department == department)
        rows = session.execute(q).scalars().all()
        out.append({
            "imported_at": b.imported_at.isoformat() if b.imported_at else None,
            "date": b.imported_at.date().isoformat() if b.imported_at else None,
            "open": len(rows),
            "overdue": sum(1 for f in rows if _is_overdue(f, today)),
        })
    return out


def close_stats(session: Session, department: Optional[str] = None,
                today: Optional[dt.date] = None) -> dict:
    """結案統計：本期新結案（上期未結、這期已結）＋依結案人。來源(Excel)確認為準。

    「承辦聲稱但來源未確認(可疑)」需承辦疊加層(W3)才算得出，這裡先回 0/空並標註。
    """
    latest, prev = _two_latest_batches(session)
    if not latest:
        return {"new_closed": 0, "by_closer": [], "source_confirmed": 0,
                "claimed_unconfirmed": 0, "note": "尚無匯入"}

    def _rows(b):
        if not b:
            return {}
        q = select(Finding).where(Finding.batch_id == b.id)
        if department and department != "全部":
            q = q.where(Finding.department == department)
        return {vuln_key(f): f for f in session.execute(q).scalars().all()}

    cur = _rows(latest)
    old = _rows(prev)

    newly_closed = []
    for k, f in cur.items():
        was_open = (k in old) and (old[k].close_status == CLOSE_OPEN)
        if f.close_status == CLOSE_DONE and (was_open or (k not in old)):
            # 上期未結→這期已結，或這期才出現就已結案
            newly_closed.append(f)

    by_closer: dict = defaultdict(int)
    for f in newly_closed:
        by_closer[(f.owner or "").strip() or "— 未指派"] += 1

    from . import cases  # 延後匯入避免循環
    claimed = cases.suspect_count(session)  # 承辦聲稱完成、來源未確認(可疑)

    return {
        "new_closed": len(newly_closed),
        "source_confirmed": len(newly_closed),   # 皆為來源 Excel 確認
        "claimed_unconfirmed": claimed,          # 承辦聲稱完成但來源仍未結案(W3)
        "by_closer": sorted(({"name": k, "closed": v} for k, v in by_closer.items()),
                            key=lambda r: -r["closed"]),
        "prev_batch": prev.id if prev else None,
        "latest_batch": latest.id,
    }


def weekly_report(session: Session, department: Optional[str] = None,
                  owner: Optional[str] = None, today: Optional[dt.date] = None) -> dict:
    """主管週報：一份快照回答『要申請的有幾支、申請了沒、預計何時完成、落後多少』。

    口徑(皆取未結案)：
      - 應申請未申請(need_apply)：尚在原始修補期限、已過行動線→催承辦去提例外/展延(iForm)。
      - 已申請處置中(applied)：備註有申請紀錄→stage 已成 例外/展延(備註閘門，見 logic)。
      - 預計完成(target_date)：承辦回報的日期；彙總已填/未填、逾預計、近 30 天到期。
      - 落後(overdue)：已過真正到期日；如期(on_track)：未逾期。
    清單只回『應申請未申請』與『落後』(主管最需要催的兩類)，各帶預計完成日與追蹤備註。
    """
    today = today or dt.date.today()
    fs = _latest_findings(session, department)
    if owner:
        fs = [f for f in fs if (f.owner or "").strip() == owner]
    open_ = [f for f in fs if f.close_status == CLOSE_OPEN]
    _b = latest_batch(session)
    imported = _b.imported_at if _b else None

    # 本週變化(週對週)：用「穩定鍵的多重集合(保留重複列)」比對。
    # 計列數(非去重鍵)，讓「本批未結」＝未結案(302)、與對帳健檢一致；
    # 用 Counter 相減算新增/解決，淨變化＝新增−解決 精準兜得起來(重複列也算對)。
    from collections import Counter
    _latest_b, _prev_b = _two_latest_batches(session)
    def _open_keys(batch):
        if not batch:
            return Counter()
        q = select(Finding).where(Finding.batch_id == batch.id, Finding.close_status == CLOSE_OPEN)
        if department and department != "全部":
            q = q.where(Finding.department == department)
        rows = session.execute(q).scalars().all()
        if owner:
            rows = [f for f in rows if (f.owner or "").strip() == owner]
        return Counter("|".join(vuln_key(f)) for f in rows)   # 多重集合：同鍵重複列各算一次
    if _prev_b:
        _cur_k = _open_keys(_latest_b); _prv_k = _open_keys(_prev_b)
        _now = sum(_cur_k.values()); _prev = sum(_prv_k.values())   # 列數(與未結案同口徑)
        change = {"has_prev": True, "prev": _prev, "now": _now,
                  "delta": _now - _prev,
                  "new": sum((_cur_k - _prv_k).values()),          # 這批多出來的列
                  "resolved": sum((_prv_k - _cur_k).values())}      # 上批有、這批沒了的列
    else:
        change = {"has_prev": False}

    # 疊加欄(預計完成日/追蹤備註/處理進度)對照
    from .models import Case
    from .logic import (PROGRESS_VALUES, PROGRESS_WIP, PROGRESS_APPLY_EXT, PROGRESS_APPLY_EXC,
                        PROGRESS_RESCAN, classify_progress, FLAGGED_STATES, CLOSE_DONE)
    ov = {c.vuln_key: c for c in session.execute(
        select(Case).where((Case.track_note.isnot(None)) | (Case.target_date.isnot(None))
                           | (Case.status.in_(PROGRESS_VALUES)))).scalars().all()}

    def _c(f):
        return ov.get("|".join(vuln_key(f)))

    def _target(f):
        c = _c(f)
        return c.target_date if c else None

    def _progress(f):
        c = _c(f)
        return c.status if (c and c.status in PROGRESS_VALUES) else ""

    def _detail(f) -> dict:
        c = _c(f)
        td = c.target_date if c else None
        prog = c.status if (c and c.status in PROGRESS_VALUES) else ""
        pstate = classify_progress(prog, f.close_status == CLOSE_DONE, f.stage,
                                   (c.status_changed_at if c else None), imported)
        return {
            "id": f.id, "host": display_host(f), "owner": f.owner, "department": f.department,
            "severity": f.severity, "name": f.name, "plugin_id": f.plugin_id,
            "stage": f.stage, "progress": prog, "progress_state": pstate,
            "effective_due": f.effective_due.isoformat() if f.effective_due else None,
            "overdue_days": overdue_days(f.effective_due, today),
            "action_line": action_line(f, today).isoformat() if action_line(f, today) else None,
            "target_date": td.isoformat() if td else None,
            "target_overdue": bool(td and td < today),   # 已過自己承諾的完成日
            "track_note": c.track_note if c else None,
            "raw": f.raw or {},   # 原始整列，供「匯出此清單」帶出全部原始欄位
        }

    need_apply = [f for f in open_ if should_apply(f, today)]
    applied = [f for f in open_ if f.stage in (STAGE_EXCEPTION, STAGE_EXTENSION)]
    overdue = [f for f in open_ if _is_overdue(f, today)]
    on_track = [f for f in open_ if not _is_overdue(f, today)]

    # 處置落點：每筆目前「真正到期日」是落在哪一關(原始修補 / 首次展延 / 例外管理)。
    # 落點日期＝effective_due(已套備註閘門)；這裡給各關計數與日期區間,細節可下鑽 stage 看。
    def _stage_block(stg):
        items = [f for f in open_ if f.stage == stg]
        dues = sorted(f.effective_due for f in items if f.effective_due)
        return {
            "count": len(items),
            "overdue": sum(1 for f in items if _is_overdue(f, today)),
            "earliest_due": dues[0].isoformat() if dues else None,
            "latest_due": dues[-1].isoformat() if dues else None,
        }
    stages = {
        "original": _stage_block(STAGE_ORIGINAL),
        "extension": _stage_block(STAGE_EXTENSION),
        "exception": _stage_block(STAGE_EXCEPTION),
    }
    stage_known = sum(stages[k]["count"] for k in stages)
    stages["other"] = {"count": len(open_) - stage_known, "overdue": None,
                       "earliest_due": None, "latest_due": None}

    # 嚴重度分佈(未結案)＋到期倒數桶：供主管週報「總覽」畫圓餅/長條(一眼看風險結構與近期壓力)
    sev_dist = {k: 0 for k in SEVERITIES}
    for f in open_:
        if f.severity in sev_dist:
            sev_dist[f.severity] += 1
    due_buckets = {"overdue": 0, "d30": 0, "d31_60": 0, "d61_90": 0, "d90plus": 0, "no_due": 0}
    for f in open_:
        if not f.effective_due:
            due_buckets["no_due"] += 1; continue
        d = (f.effective_due - today).days
        if d < 0: due_buckets["overdue"] += 1
        elif d <= 30: due_buckets["d30"] += 1
        elif d <= 60: due_buckets["d31_60"] += 1
        elif d <= 90: due_buckets["d61_90"] += 1
        else: due_buckets["d90plus"] += 1

    # 需申請母體＝應申請未申請 + 已申請(都曾需要申請決策)
    universe = need_apply + applied
    with_target = [f for f in universe if _target(f)]
    no_target = [f for f in universe if not _target(f)]
    target_overdue = [f for f in with_target if _target(f) < today]
    target_soon = [f for f in with_target
                   if 0 <= (_target(f) - today).days <= SOON_DAYS]

    # 處理進度分佈(管理人手動標的)：各類計數 + 需追查(⚠️待查/可疑)總數
    pcount = {PROGRESS_WIP: 0, PROGRESS_APPLY_EXT: 0, PROGRESS_APPLY_EXC: 0, PROGRESS_RESCAN: 0}
    flagged = 0
    for f in open_:
        c = _c(f)
        p = c.status if (c and c.status in PROGRESS_VALUES) else ""
        if p in pcount:
            pcount[p] += 1
        st = classify_progress(p, f.close_status == CLOSE_DONE, f.stage,
                               (c.status_changed_at if c else None), imported)
        if st in FLAGGED_STATES:
            flagged += 1

    # 要申請·送審中清單(管理人標了要申請展延/例外的)；需追查清單(⚠️的，主管要盯)
    apply_intent = [f for f in open_ if _progress(f) in (PROGRESS_APPLY_EXT, PROGRESS_APPLY_EXC)]
    flagged_rows = [d for d in (_detail(f) for f in open_) if d["progress_state"] in FLAGGED_STATES]

    return {
        "department": department or "全部",
        "owner": owner,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "freshness": {
            "imported_at": imported.isoformat() if imported else None,
            "days_ago": (today - imported.date()).days if imported else None,
        },
        "unresolved": len(open_),
        "overdue": len(overdue),
        "on_track": len(on_track),
        "high_risk": sum(1 for f in open_ if f.severity in HIGH_RISK),
        "high_risk_overdue": sum(1 for f in overdue if f.severity in HIGH_RISK),  # 主管最在意:高風險且逾期
        "change": change,                          # 本週變化(週對週):prev/now/delta/new/resolved

        # 申請面
        "need_apply_count": len(need_apply),      # 應申請未申請(要催)
        "applied_count": len(applied),            # 已申請處置中
        "apply_universe": len(universe),          # 需申請母體
        "stages": stages,                         # 處置落點：原始/首次展延/例外管理各計數與到期區間
        "severity": sev_dist,                     # 嚴重度分佈(未結案)：供圓餅
        "due_buckets": due_buckets,               # 到期倒數桶(未結案,真正到期日)：供長條
        # 處理進度分佈(管理人手動標)：要申請展延/例外、處理中、等複掃，及需追查(⚠️)總數
        "progress": {
            "wip": pcount[PROGRESS_WIP],
            "apply_ext": pcount[PROGRESS_APPLY_EXT],
            "apply_exc": pcount[PROGRESS_APPLY_EXC],
            "rescan": pcount[PROGRESS_RESCAN],
            "flagged": flagged,
            "reported": sum(1 for f in open_ if _is_reported(_c(f))),
            "note_no_progress": sum(1 for f in open_ if _note_no_progress(_c(f))),   # ⚠ 備註有寫、進度未設   # 已回報(進度/預計完成日/備註任一)
        },
        # 預計完成彙總(僅母體)
        "target": {
            "with_target": len(with_target),
            "no_target": len(no_target),          # 未回報預計完成日(要催)
            "target_overdue": len(target_overdue),  # 已過自己承諾的完成日
            "target_soon": len(target_soon),      # 預計 30 天內完成
        },
        # 清單(主管要催/要盯的幾類)
        "need_apply_list": sorted(
            (_detail(f) for f in need_apply),
            key=lambda r: ((r["overdue_days"] is None), -(r["overdue_days"] or 0))),
        "overdue_list": sorted(
            (_detail(f) for f in overdue),
            key=lambda r: -(r["overdue_days"] or 0)),
        "apply_intent_list": sorted(
            (_detail(f) for f in apply_intent),
            key=lambda r: ((r["overdue_days"] is None), -(r["overdue_days"] or 0))),
        "flagged_list": sorted(
            flagged_rows, key=lambda r: ((r["overdue_days"] is None), -(r["overdue_days"] or 0))),
    }


def sla(session: Session, department: Optional[str] = None,
        today: Optional[dt.date] = None) -> list[dict]:
    """各嚴重度 SLA 達成率（未結案中未逾期比率，政策天數見設定）。"""
    today = today or dt.date.today()
    open_ = [f for f in _latest_findings(session, department) if f.close_status == CLOSE_OPEN]
    out = []
    for sev in SEVERITIES:
        items = [f for f in open_ if f.severity == sev]
        od = [f for f in items if _is_overdue(f, today)]
        out.append({
            "severity": sev,
            "policy_days": SLA_POLICY_DAYS.get(sev),
            "unresolved": len(items),
            "overdue": len(od),
            "met_rate": round((len(items) - len(od)) / len(items) * 100, 1) if items else 100.0,
        })
    return out

"""匯入間的變化：每次匯入新 Excel，跟上一批逐筆比對（使用者 2026-10-07：5 天後套新 Excel，要知道變了什麼）。

三件事：
1. 逐筆逐欄記錄（FindingChange）：給「這一筆的歷程」用。匯入時寫；舊批開機時補算一次（冪等）。
2. 主管看的數字（compare）：上次／本次／差額＋本期變動，可依部門、工作表、登入範圍篩。
   「上次」＝上一批被換掉那一刻的狀態（承辦進度在那一刻拍下來，ProgressSnap）；「本次」＝現在（跟週報同一套數字）。
3. 點數字看清單（compare_rows）：跟下鑽同一個欄位格式，多一欄「變了什麼」。

配對規則：穩定鍵（row_key）相同＝同一筆；同鍵多列依 id 順序一對一配。
等式（對帳）：本次未結 ＝ 上次未結 − 離開未結 − 移出／消失 ＋ 新出現／移入 ＋ 重新開啟。
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import query
from .logic import CLOSE_OPEN, PROGRESS_VALUES, STAGE_EXCEPTION, STAGE_EXTENSION, STAGE_ORIGINAL
from .models import ChangeRun, Finding, FindingChange, ImportBatch, ProgressSnap

# 只記會影響追蹤的欄位（Plugin Output 之類每次掃描都會變，記了只是雜訊）
TRACK = [
    ("close_status", "結案狀態"), ("close_date", "結案日期"), ("retest", "複測狀態"), ("stage", "處置階段"),
    ("effective_due", "到期日"), ("remediation_due", "修補期限"), ("first_extension_due", "首次展延上限"),
    ("exception_due", "例外核准期限"), ("owner", "負責人"), ("department", "部門"), ("severity", "嚴重度"),
    ("remark", "備註"),
]
LABEL = dict(TRACK)
_RETEST_NK = query._nk("複測狀態")
_FIXED_RE = re.compile(r"已通過|已修復|已修補|無發現")
_NOT_FIXED_RE = re.compile(r"未通過|未修")


def key(f: Finding) -> str:
    return "|".join(query.vuln_key(f))


def retest(f: Finding) -> str:
    for k, v in (f.raw or {}).items():
        if query._nk(k) == _RETEST_NK:
            return "" if v is None else str(v).strip()
    return ""


def retest_fixed(f: Finding) -> bool:
    t = retest(f)
    return bool(t) and bool(_FIXED_RE.search(t)) and not _NOT_FIXED_RE.search(t)


_OV: dict = {}   # 這次比較用的換人紀錄 {"owner": {key: 值}, "department": {...}}（compute_run／_context 開頭載入）


def _load_ov(session: Session) -> None:
    o, d = query._overrides(session)
    _OV.clear(); _OV.update({"owner": o, "department": d})


def eff(f: Finding, field: str):
    """負責人／部門以系統上的換人為準（兩批都套同一份，比出來的才是 Excel 真的變了什麼）。"""
    if field in ("owner", "department"):
        v = (_OV.get(field) or {}).get(key(f))
        if v:
            return v
    return getattr(f, field, None)


def _val(f: Finding, field: str) -> str:
    v = retest(f) if field == "retest" else eff(f, field)
    if v is None:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    return str(v).strip()


def diff_fields(p: Finding, c: Finding) -> list[tuple[str, str, str]]:
    return [(fld, _val(p, fld), _val(c, fld)) for fld, _ in TRACK if _val(p, fld) != _val(c, fld)]


def pair(prev_fs: list, cur_fs: list):
    """回 (matched[(p,c)], prev_only, cur_only)。同鍵多列依 id 一對一配。"""
    pk: dict = defaultdict(list)
    for f in sorted(prev_fs, key=lambda x: x.id):
        pk[key(f)].append(f)
    matched, cur_only = [], []
    for c in sorted(cur_fs, key=lambda x: x.id):
        lst = pk.get(key(c))
        if lst:
            matched.append((lst.pop(0), c))
        else:
            cur_only.append(c)
    prev_only = [f for lst in pk.values() for f in lst]
    return matched, prev_only, cur_only


def _batches(session: Session) -> list[ImportBatch]:
    return session.execute(select(ImportBatch).order_by(ImportBatch.imported_at, ImportBatch.id)).scalars().all()


def _fs(session: Session, batch_id: int) -> list[Finding]:
    return session.execute(select(Finding).where(Finding.batch_id == batch_id).order_by(Finding.id)).scalars().all()


# ── 寫入：匯入時、補算、刪批 ──
def capture_progress(session: Session, batch: ImportBatch) -> None:
    """新批進來前，把舊批當下的承辦進度拍下來（之後「上次的展延送審幾筆」才算得出來）。"""
    from .models import Case
    session.execute(delete(ProgressSnap).where(ProgressSnap.batch_id == batch.id))
    for c in session.execute(select(Case).where(Case.status.in_(PROGRESS_VALUES))).scalars().all():
        session.add(ProgressSnap(batch_id=batch.id, vuln_key=c.vuln_key, progress=c.status))


def compute_run(session: Session, batch: ImportBatch, prev: Optional[ImportBatch],
                prev_asof: Optional[dt.datetime] = None, progress_captured: bool = False) -> int:
    """這批 vs 前一批，寫逐筆逐欄紀錄（先清掉同批舊紀錄＝冪等）。回寫入筆數。"""
    session.execute(delete(FindingChange).where(FindingChange.batch_id == batch.id))
    session.execute(delete(ChangeRun).where(ChangeRun.batch_id == batch.id))
    _load_ov(session)
    n = 0
    if prev is not None:
        matched, prev_only, cur_only = pair(_fs(session, prev.id), _fs(session, batch.id))
        at = batch.imported_at
        for p, c in matched:
            for fld, old, new in diff_fields(p, c):
                session.add(FindingChange(batch_id=batch.id, prev_batch_id=prev.id, vuln_key=key(c), kind="field",
                                          field=fld, old=old, new=new, at=at)); n += 1
        for c in cur_only:
            session.add(FindingChange(batch_id=batch.id, prev_batch_id=prev.id, vuln_key=key(c), kind="new", at=at)); n += 1
        for p in prev_only:
            session.add(FindingChange(batch_id=batch.id, prev_batch_id=prev.id, vuln_key=key(p), kind="gone", at=at)); n += 1
    session.add(ChangeRun(batch_id=batch.id, prev_batch_id=prev.id if prev else None,
                          prev_asof=prev_asof or batch.imported_at, progress_captured=progress_captured, n_changes=n))
    return n


def backfill(session: Session) -> int:
    """開機時補算：還沒比對過的批次（含第一批，記成「沒有前一批」）。冪等。"""
    done = {r for (r,) in session.execute(select(ChangeRun.batch_id)).all()}
    bs = _batches(session)
    k = 0
    for i, b in enumerate(bs):
        if b.id in done:
            continue
        compute_run(session, b, bs[i - 1] if i else None)
        k += 1
    if k:
        session.commit()
    return k


def delete_batch(session: Session, batch_id: int) -> dict:
    """刪一批匯入（匯錯檔用），連同它造成的變化紀錄；刪的是最新批就讓前一批回到最新。
    之後那一批（如果有）改跟新的前一批重比。呼叫端負責先備份 DB。"""
    from . import cases
    bs = _batches(session)
    ids = [b.id for b in bs]
    if batch_id not in ids:
        raise ValueError("找不到這批匯入")
    if len(bs) < 2:
        raise ValueError("只剩這一批，不能刪（刪了就沒有資料）")
    i = ids.index(batch_id)
    b = bs[i]
    prev = bs[i - 1] if i > 0 else None
    nxt = bs[i + 1] if i + 1 < len(bs) else None
    was_latest = bool(b.is_latest)
    session.execute(delete(FindingChange).where((FindingChange.batch_id == b.id) | (FindingChange.prev_batch_id == b.id)))
    session.execute(delete(ChangeRun).where((ChangeRun.batch_id == b.id) | (ChangeRun.prev_batch_id == b.id)))
    session.execute(delete(ProgressSnap).where(ProgressSnap.batch_id == b.id))
    from .models import SheetColumns
    # 明確刪子表（SQLite 外鍵串刪不一定有開，不能靠 cascade）
    session.execute(delete(Finding).where(Finding.batch_id == b.id))
    session.execute(delete(SheetColumns).where(SheetColumns.batch_id == b.id))
    session.execute(delete(ImportBatch).where(ImportBatch.id == b.id))
    session.flush()
    if was_latest and prev is not None:
        prev.is_latest = True
    if nxt is not None:
        compute_run(session, nxt, prev)
    session.commit()
    if was_latest and prev is not None:
        cases.reconcile(session, prev)
        cases.apply_owner_overrides(session, prev)
    query._SNAP_CACHE.clear()
    return {"deleted": batch_id, "latest": (prev.id if was_latest and prev else None),
            "rows": b.row_count, "source_file": b.source_file}


# ── 讀：主管看的數字 ──
def _in_scope(f: Finding, department: Optional[str]) -> bool:
    if department and department != "全部" and (eff(f, "department") or "") != department:
        return False
    sh = query.SHEET_SCOPE.get()
    if sh and f.sheet_key != sh:
        return False
    sc = query.VIEW_SCOPE.get()   # 登入範圍（同 query._in_view，但負責人／部門用套過換人的值）
    if not sc:
        return True
    kind, val = sc
    if kind == "owner":
        return bool(val) and (eff(f, "owner") or "").strip() == val
    if kind == "dept":
        return bool(val) and (eff(f, "department") or "") == val
    return False


def _progress_cat(prog: str) -> str:
    return {"處理中": "wip", "等複掃": "rescan", "要申請展延": "subext", "要申請例外": "subexc"}.get(prog or "", "todo")


METRICS = [
    ("unresolved", "未結"), ("overdue", "已逾期"), ("soon", "近期到期（30 天內）"),
    ("high_only", "高風險未結（不含逾期、近期）"), ("apply_universe", "需申請"),
    ("need_apply", "應申請未申請"), ("applied", "已核准展延／例外"), ("not_apply", "暫無需申請（期限內）"),
    ("todo", "　尚未修補"), ("wip", "　修補中"), ("subext", "　展延送審"), ("subexc", "　例外送審"), ("rescan", "　結案申請中"),
]
PROGRESS_METRICS = ("todo", "wip", "subext", "subexc", "rescan")


def _metric_sets(fs: list, asof: dt.date, prog: Optional[dict]) -> dict:
    """每個指標＝哪些 Finding（以 id 存），差額清單要用。prog=None＝這批沒拍到進度。"""
    out = {k: set() for k, _ in METRICS}
    for f in fs:
        if f.close_status != CLOSE_OPEN:
            continue
        out["unresolved"].add(f.id)
        d = (f.effective_due - asof).days if f.effective_due else None
        if d is not None and d < 0:
            out["overdue"].add(f.id)
        elif d is not None and d <= query.SOON_DAYS:
            out["soon"].add(f.id)
        if query._is_high_only(f, asof):
            out["high_only"].add(f.id)
        sa = query.should_apply(f, asof)
        ap = f.stage in (STAGE_EXCEPTION, STAGE_EXTENSION)
        if sa or ap:
            out["apply_universe"].add(f.id)
        else:
            out["not_apply"].add(f.id)
        if ap:
            out["applied"].add(f.id)
        if sa:
            out["need_apply"].add(f.id)
            if prog is not None:
                out[_progress_cat(prog.get(key(f), ""))].add(f.id)
    return out


def _current_progress(session: Session) -> dict:
    from .models import Case
    return {c.vuln_key: c.status for c in session.execute(
        select(Case).where(Case.status.in_(PROGRESS_VALUES))).scalars().all()}


def _snap_progress(session: Session, batch_id: int) -> Optional[dict]:
    rows = session.execute(select(ProgressSnap).where(ProgressSnap.batch_id == batch_id)).scalars().all()
    run = session.execute(select(ChangeRun).where(ChangeRun.prev_batch_id == batch_id,
                                                  ChangeRun.progress_captured.is_(True))).scalars().first()
    if run is None:
        return None          # 沒拍到（補算的舊批）→ 承辦進度那幾格顯示「未記錄」，不要假裝是 0
    return {r.vuln_key: r.progress for r in rows}


def batches_info(session: Session) -> list[dict]:
    out = []
    for b in reversed(_batches(session)):
        out.append({"id": b.id, "imported_at": b.imported_at.isoformat(timespec="minutes") if b.imported_at else None,
                    "source_file": b.source_file, "row_count": b.row_count, "is_latest": bool(b.is_latest)})
    return out


def _context(session: Session, prev_id: Optional[int], department: Optional[str], today: dt.date):
    cur = query.latest_batch(session)
    if cur is None:
        return None
    _load_ov(session)
    bs = _batches(session)
    if prev_id is None:
        earlier = [b for b in bs if (b.imported_at, b.id) < (cur.imported_at, cur.id)]
        prev = earlier[-1] if earlier else None
    else:
        prev = next((b for b in bs if b.id == prev_id and b.id != cur.id), None)
    if prev is None:
        return {"cur": cur, "prev": None}
    # 「上次」的時間點：上一批被換掉那一刻（有拍就用拍的時間，否則用下一批的匯入時間）
    nxt = next((b for b in bs if (b.imported_at, b.id) > (prev.imported_at, prev.id)), cur)
    run = session.execute(select(ChangeRun).where(ChangeRun.batch_id == nxt.id)).scalars().first()
    prev_asof_dt = (run.prev_asof if run and run.prev_asof else nxt.imported_at)
    prev_asof = prev_asof_dt.date() if prev_asof_dt else today
    pf = [f for f in _fs(session, prev.id) if _in_scope(f, department)]
    cf = [f for f in _fs(session, cur.id) if _in_scope(f, department)]
    p_prog = _snap_progress(session, prev.id)
    c_prog = _current_progress(session)
    pall = {f.id for f in _fs(session, prev.id)}   # 判斷「移出範圍」vs「從來源消失」要看整批
    return {"cur": cur, "prev": prev, "prev_asof": prev_asof, "prev_asof_dt": prev_asof_dt,
            "pf": pf, "cf": cf, "p_prog": p_prog, "c_prog": c_prog, "pall": pall}


def _events(session: Session, ctx: dict, today: dt.date) -> dict:
    """本期變動（互斥分類跟等式用），值＝(prev Finding 或 None, cur Finding 或 None) 清單。"""
    pf, cf, pa = ctx["pf"], ctx["cf"], ctx["prev_asof"]
    matched, p_only, c_only = pair(pf, cf)
    # 範圍外的配對：看整批，分出「移出／移入範圍」與「真的消失／新出現」
    allm, allp_only, allc_only = pair(_fs(session, ctx["prev"].id), _fs(session, ctx["cur"].id))
    gone_ids = {f.id for f in allp_only}
    new_ids = {f.id for f in allc_only}
    ev = {k: [] for k in ("closed", "reopened", "new", "moved_in", "gone", "moved_out", "approved",
                          "new_overdue", "retest_fixed", "due_changed", "owner_changed")}

    def od(f, asof):
        return f.close_status == CLOSE_OPEN and f.effective_due is not None and f.effective_due < asof

    for p, c in matched:
        po, co = p.close_status == CLOSE_OPEN, c.close_status == CLOSE_OPEN
        if po and not co:
            ev["closed"].append((p, c))
        elif co and not po:
            ev["reopened"].append((p, c))
        if po and co:
            if p.stage == STAGE_ORIGINAL and c.stage in (STAGE_EXCEPTION, STAGE_EXTENSION):
                ev["approved"].append((p, c))
            if p.effective_due != c.effective_due:
                ev["due_changed"].append((p, c))
            if (eff(p, "owner") or "") != (eff(c, "owner") or "") or (eff(p, "department") or "") != (eff(c, "department") or ""):
                ev["owner_changed"].append((p, c))
        if co and od(c, today) and not od(p, pa):
            ev["new_overdue"].append((p, c))
        if co and retest_fixed(c) and retest(c) != retest(p):
            ev["retest_fixed"].append((p, c))
    for c in c_only:
        if c.close_status == CLOSE_OPEN:
            ev["new" if c.id in new_ids else "moved_in"].append((None, c))
    for p in p_only:
        if p.close_status == CLOSE_OPEN:
            ev["gone" if p.id in gone_ids else "moved_out"].append((p, None))
    return ev


EVENTS = [("closed", "結案（離開未結）", "good"), ("approved", "送審 → 核准（展延／例外）", "good"),
          ("retest_fixed", "資安複測已修復、尚未結案", "good"), ("new", "新出現", "bad"),
          ("new_overdue", "新增逾期", "bad"), ("reopened", "重新開啟", "bad"), ("gone", "從來源消失", "info"),
          ("moved_in", "移入範圍（換部門／負責人）", "info"), ("moved_out", "移出範圍（換部門／負責人）", "info"),
          ("due_changed", "到期日變了", "info"), ("owner_changed", "負責人／部門變了", "info")]


def compare(session: Session, prev_id: Optional[int] = None, department: Optional[str] = None,
            today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    ctx = _context(session, prev_id, department, today)
    if ctx is None:
        return {"has_prev": False, "reason": "尚無匯入"}
    cur = ctx["cur"]
    out = {"batches": batches_info(session),
           "cur": {"id": cur.id, "imported_at": cur.imported_at.isoformat(timespec="minutes") if cur.imported_at else None,
                   "source_file": cur.source_file}}
    if ctx["prev"] is None:
        out.update(has_prev=False, reason="只有一批匯入，無法比較")
        return out
    prev = ctx["prev"]
    ps = _metric_sets(ctx["pf"], ctx["prev_asof"], ctx["p_prog"])
    cs = _metric_sets(ctx["cf"], today, ctx["c_prog"])
    metrics = []
    for k, label in METRICS:
        unk = k in PROGRESS_METRICS and ctx["p_prog"] is None
        metrics.append({"key": k, "label": label, "prev": None if unk else len(ps[k]), "cur": len(cs[k]),
                        "delta": None if unk else len(cs[k]) - len(ps[k])})
    ev = _events(session, ctx, today)
    events = [{"key": k, "label": lb, "tone": tone, "n": len(ev[k])} for k, lb, tone in EVENTS]
    # 等式：本次未結 ＝ 上次未結 − 結案 − 消失 − 移出 ＋ 新出現 ＋ 移入 ＋ 重新開啟
    pu, cu = len(ps["unresolved"]), len(cs["unresolved"])
    calc = pu - len(ev["closed"]) - len(ev["gone"]) - len(ev["moved_out"]) + len(ev["new"]) + len(ev["moved_in"]) + len(ev["reopened"])
    out.update(
        has_prev=True,
        prev={"id": prev.id, "imported_at": prev.imported_at.isoformat(timespec="minutes") if prev.imported_at else None,
              "source_file": prev.source_file, "asof": ctx["prev_asof_dt"].isoformat(timespec="minutes") if ctx["prev_asof_dt"] else None},
        progress_recorded=ctx["p_prog"] is not None,
        metrics=metrics, events=events,
        equation={"prev_open": pu, "cur_open": cu, "calc": calc, "ok": calc == cu,
                  "text": f"{pu} − 結案{len(ev['closed'])} − 消失{len(ev['gone'])} − 移出{len(ev['moved_out'])} ＋ 新出現{len(ev['new'])} ＋ 移入{len(ev['moved_in'])} ＋ 重開{len(ev['reopened'])} ＝ {calc}"},
        summary=_summary(ev),
    )
    return out


def _summary(ev: dict) -> str:
    parts = []
    for k, word in (("closed", "結案"), ("approved", "核准展延／例外"), ("retest_fixed", "複測已修復待結案"),
                    ("new", "新出現"), ("new_overdue", "新增逾期")):
        if ev[k]:
            parts.append(f"{word} {len(ev[k])} 筆")
    return "本期" + "、".join(parts) + "。" if parts else "本期沒有變動。"


def _note(p: Optional[Finding], c: Optional[Finding], kind: str) -> str:
    if p is None:
        return "移入範圍" if kind == "moved_in" else "新出現"
    if c is None:
        return "移出範圍（部門／負責人改了）" if kind == "moved_out" else "從來源消失（不一定是修好，請確認）"
    ds = diff_fields(p, c)
    return "；".join(f"{LABEL[f]} {o or '（空）'} → {n or '（空）'}" for f, o, n in ds if f != "remark") or "（欄位沒變，狀態因時間改變）"


def compare_rows(session: Session, prev_id: Optional[int] = None, department: Optional[str] = None,
                 metric: Optional[str] = None, side: str = "cur", event: Optional[str] = None,
                 today: Optional[dt.date] = None) -> list[dict]:
    """點數字看清單。metric+side：prev／cur＝那時的清單；plus＝這次才在、minus＝上次在這次不在。event：本期變動那類。
    每列多 change_note（變了什麼）；只存在上一批的列標 readonly（目前資料裡沒有它，不能編輯）。"""
    today = today or dt.date.today()
    ctx = _context(session, prev_id, department, today)
    if not ctx or ctx["prev"] is None:
        return []
    cur_row = query._row_builder(session, today)
    prev_row = query._row_builder(session, ctx["prev_asof"], progress_map=ctx["p_prog"] or {})
    matched, _, _ = pair(ctx["pf"], ctx["cf"])
    p2c = {p.id: c for p, c in matched}
    c2p = {c.id: p for p, c in matched}
    pf = {f.id: f for f in ctx["pf"]}
    cf = {f.id: f for f in ctx["cf"]}

    def as_cur(c, note):
        d = cur_row(c); d["change_note"] = note; return d

    def as_prev(p, note):
        d = prev_row(p); d["change_note"] = note; d["readonly"] = True; return d

    if event:
        ev = _events(session, ctx, today).get(event, [])
        return [as_cur(c, _note(p, c, event)) if c is not None else as_prev(p, _note(p, None, event)) for p, c in ev]
    if not metric:
        return []
    ps = _metric_sets(ctx["pf"], ctx["prev_asof"], ctx["p_prog"])[metric]
    cs = _metric_sets(ctx["cf"], today, ctx["c_prog"])[metric]
    if side == "cur":
        return [as_cur(cf[i], _note(c2p.get(i), cf[i], "")) for i in sorted(cs)]
    if side == "prev":
        return [as_prev(pf[i], "") for i in sorted(ps)]
    if side == "both":    # 差額：進來的＋離開的
        return (compare_rows(session, prev_id, department, metric, "plus", None, today)
                + compare_rows(session, prev_id, department, metric, "minus", None, today))
    if side == "plus":    # 這次在、上次不在
        out = []
        for i in sorted(cs):
            p = c2p.get(i)
            if p is None or p.id not in ps:
                out.append(as_cur(cf[i], "這次新進這一類：" + _note(p, cf[i], "")))
        return out
    if side == "minus":   # 上次在、這次不在
        out = []
        for i in sorted(ps):
            c = p2c.get(i)
            if c is None or c.id not in cs:
                out.append(as_cur(c, "離開這一類：" + _note(pf[i], c, "")) if c is not None
                           else as_prev(pf[i], "離開這一類：" + _note(pf[i], None, "gone")))
        return out
    return []


def history(session: Session, finding_id: int) -> dict:
    """一筆弱點的歷程：第一次出現、每次匯入的欄位變化、系統上的操作紀錄，依時間排。"""
    from .models import AuditLog
    f = session.get(Finding, finding_id)
    if f is None:
        raise ValueError("弱點不存在")
    k = key(f)
    items = []
    first = None
    q = select(ImportBatch).join(Finding, Finding.batch_id == ImportBatch.id)
    if f.row_key:
        q = q.where(Finding.row_key == f.row_key)
    else:   # 舊鍵（尚未轉 row_key）：sheet＋plugin＋host
        q = q.where(Finding.sheet_key == f.sheet_key, Finding.plugin_id == f.plugin_id, Finding.host == f.host)
    first = session.execute(q.order_by(ImportBatch.imported_at, ImportBatch.id)).scalars().first()
    if first is not None:
        items.append({"at": first.imported_at.isoformat(timespec="minutes"), "src": "匯入",
                      "text": "首次出現（%s）" % (first.source_file or "")})
    for ch in session.execute(select(FindingChange).where(FindingChange.vuln_key == k)
                              .order_by(FindingChange.at, FindingChange.id)).scalars().all():
        if ch.kind == "new" and first is not None and ch.batch_id == first.id:
            continue
        txt = {"new": "重新出現在來源", "gone": "從來源消失"}.get(ch.kind) or \
            f"{LABEL.get(ch.field, ch.field)}：{ch.old or '（空）'} → {ch.new or '（空）'}"
        items.append({"at": ch.at.isoformat(timespec="minutes") if ch.at else "", "src": "匯入", "text": txt})
    for a in session.execute(select(AuditLog).where(AuditLog.target == f"vuln:{k}")
                             .order_by(AuditLog.at)).scalars().all():
        items.append({"at": a.at.isoformat(timespec="minutes"), "src": "系統",
                      "text": f"{a.username or ''}：{_audit_text(a.detail)}"})
    items.sort(key=lambda x: x["at"])
    return {"key": k, "items": items}


_AUDIT_LABEL = {"progress": "處理進度", "note": "追蹤備註", "target_date": "預計完成日", "owner": "負責人",
                "department": "部門", "track_note": "追蹤備註"}


def _audit_text(detail: Optional[str]) -> str:
    """操作紀錄的 detail 是 dict 字串，轉成人看得懂的「處理進度 → 等複掃」。解析不了就原樣。"""
    import ast
    try:
        d = ast.literal_eval(detail or "")
        if isinstance(d, dict):
            def show(v):
                return "結案申請中" if v == "等複掃" else ("（清空）" if v in (None, "") else str(v))
            return "；".join(f"{_AUDIT_LABEL.get(k, k)} → {show(v)}" for k, v in d.items())
    except (ValueError, SyntaxError):
        pass
    return detail or ""

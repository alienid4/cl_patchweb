"""查詢層（唯讀）。讀最新快照，於 Python 端算統計。

刻意在 Python 算(不用 SQL 日期運算)：資料量小、邏輯與單機版一致、且轉 MSSQL 時零改動。
逾期/分帶用「今天」現算 → 天生避開跨午夜。
到期時間帶互斥：各帶相加＝未結案(對帳)。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from collections import defaultdict

from .config import SLA_POLICY_DAYS, lead_days
from .logic import (CLOSE_DONE, CLOSE_OPEN, SEVERITIES, STAGE_EXCEPTION,
                    STAGE_EXTENSION, STAGE_ORIGINAL, overdue_days)
from .models import Finding, ImportBatch, SheetColumns

BANDS = ("已逾期", "30天內", "31–90天", "91–180天", "180天以上", "無到期日")
HIGH_RISK = ("Critical", "High")
SOON_DAYS = 30


def latest_batch(session: Session) -> Optional[ImportBatch]:
    return session.execute(
        select(ImportBatch).where(ImportBatch.is_latest.is_(True))
    ).scalars().first()


def _latest_findings(session: Session, department: Optional[str] = None) -> list[Finding]:
    b = latest_batch(session)
    if not b:
        return []
    q = select(Finding).where(Finding.batch_id == b.id)
    if department and department != "全部":
        q = q.where(Finding.department == department)
    return list(session.execute(q).scalars().all())


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
            today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    fs = _latest_findings(session, department)
    open_ = [f for f in fs if f.close_status == CLOSE_OPEN]
    done = [f for f in fs if f.close_status == CLOSE_DONE]

    bands = {k: 0 for k in BANDS}
    for f in open_:
        bands[_band(f, today)] += 1

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


def find(session: Session, department: Optional[str] = None, status: str = CLOSE_OPEN,
         owner: Optional[str] = None, severity: Optional[str] = None,
         band: Optional[str] = None, keyword: Optional[str] = None,
         sheet_key: Optional[str] = None, stage: Optional[str] = None,
         only_should_apply: bool = False, applied: bool = False, apply_intent: bool = False,
         no_owner: bool = False, no_due: bool = False,
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
    if band:
        fs = [f for f in fs if _band(f, today) == band]
    if only_should_apply:
        fs = [f for f in fs if should_apply(f, today)]
    if no_owner:
        fs = [f for f in fs if not (f.owner or "").strip()]
    if no_due:
        fs = [f for f in fs if not f.effective_due]
    if keyword:
        terms = [t.lower() for t in keyword.split() if t.strip()]
        def hit(f: Finding) -> bool:
            hay = " ".join(str(x or "").lower() for x in (f.host, f.owner, f.name, f.plugin_id))
            return all(t in hay for t in terms)
        fs = [f for f in fs if hit(f)]

    # 系統內寫的疊加欄(追蹤備註/預計完成日/處理進度)：依穩定鍵對 Case 帶進每列(非 Excel 原值)
    from .models import Case
    from .logic import PROGRESS_VALUES, classify_progress, CLOSE_DONE
    ov = {c.vuln_key: c for c in session.execute(
        select(Case).where((Case.track_note.isnot(None)) | (Case.target_date.isnot(None))
                           | (Case.status.in_(PROGRESS_VALUES)))).scalars().all()}
    _b = latest_batch(session)
    _imp = _b.imported_at if _b else None

    def row(f: Finding) -> dict:
        c = ov.get("|".join(vuln_key(f)))   # Case.vuln_key 是字串(| 接)
        progress = (c.status if (c and c.status in PROGRESS_VALUES) else "")
        pstate = classify_progress(progress, f.close_status == CLOSE_DONE, f.stage,
                                   (c.status_changed_at if c else None), _imp)
        return {
            "id": f.id, "sheet_key": f.sheet_key, "plugin_id": f.plugin_id, "name": f.name,
            "host": f.host, "severity": f.severity, "department": f.department, "owner": f.owner,
            "effective_due": f.effective_due.isoformat() if f.effective_due else None,
            "overdue_days": overdue_days(f.effective_due, today),
            "action_line": action_line(f, today).isoformat() if action_line(f, today) else None,
            "should_apply": should_apply(f, today),
            "stage": f.stage, "close_status": f.close_status, "remark": f.remark,
            "track_note": c.track_note if c else None,
            "target_date": (c.target_date.isoformat() if (c and c.target_date) else None),
            "progress": progress,          # 管理人手動標(處理中/要申請展延/要申請例外/等複掃/'')
            "progress_state": pstate,      # 進度對帳(等複掃→複掃三態；要申請→申請三態；否則 None)
            "raw": f.raw or {},   # 原始整列(原欄名→原值)，供「匯出此清單」帶出全部原始欄位
        }

    return [row(f) for f in fs]


def snapshot(session: Session) -> dict:
    """回傳最新快照的『原封』內容（各表欄序＋每列 raw），供網頁前端重建 workbook、
    餵回單機版原本的解析/render pipeline，畫面與單機版一模一樣。"""
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
    sheets = [{
        "name": k,
        "columns": scs.get(k) or (list(by[k][0].keys()) if by[k] else []),
        "rows": by[k],
    } for k in order]
    return {"source_file": b.source_file,
            "imported_at": b.imported_at.isoformat() if b.imported_at else None,
            "sheets": sheets}


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
    """穩定識別鍵：sheet+plugin+正規化 host（跨快照追同一弱點；host 去空白轉小寫）。"""
    host = (f.host or "").strip().lower()
    return ((f.sheet_key or ""), (f.plugin_id or ""), host)


def _two_latest_batches(session: Session):
    bs = session.execute(
        select(ImportBatch).order_by(ImportBatch.imported_at.desc(), ImportBatch.id.desc()).limit(2)
    ).scalars().all()
    return (bs[0] if bs else None, bs[1] if len(bs) > 1 else None)


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

    # 疊加欄(預計完成日/追蹤備註)對照
    from .models import Case
    from .logic import PROGRESS_VALUES, PROGRESS_WIP, PROGRESS_APPLY_EXT, PROGRESS_APPLY_EXC, \
        PROGRESS_RESCAN, classify_progress, FLAGGED_STATES
    ov = {c.vuln_key: c for c in session.execute(
        select(Case).where((Case.track_note.isnot(None)) | (Case.target_date.isnot(None))
                           | (Case.status.in_(PROGRESS_VALUES)))).scalars().all()}

    def _c(f):
        return ov.get("|".join(vuln_key(f)))

    def _target(f):
        c = _c(f)
        return c.target_date if c else None

    def _detail(f) -> dict:
        c = _c(f)
        td = c.target_date if c else None
        return {
            "id": f.id, "host": f.host, "owner": f.owner, "department": f.department,
            "severity": f.severity, "name": f.name, "plugin_id": f.plugin_id,
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

    # 需申請母體＝應申請未申請 + 已申請(都曾需要申請決策)
    universe = need_apply + applied
    with_target = [f for f in universe if _target(f)]
    no_target = [f for f in universe if not _target(f)]
    target_overdue = [f for f in with_target if _target(f) < today]
    target_soon = [f for f in with_target
                   if 0 <= (_target(f) - today).days <= SOON_DAYS]

    b = latest_batch(session)
    imported = b.imported_at if b else None

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
        # 申請面
        "need_apply_count": len(need_apply),      # 應申請未申請(要催)
        "applied_count": len(applied),            # 已申請處置中
        "apply_universe": len(universe),          # 需申請母體
        "stages": stages,                         # 處置落點：原始/首次展延/例外管理各計數與到期區間
        # 處理進度分佈(管理人手動標)：要申請展延/例外、處理中、等複掃，及需追查(⚠️)總數
        "progress": {
            "wip": pcount[PROGRESS_WIP],
            "apply_ext": pcount[PROGRESS_APPLY_EXT],
            "apply_exc": pcount[PROGRESS_APPLY_EXC],
            "rescan": pcount[PROGRESS_RESCAN],
            "flagged": flagged,
        },
        # 預計完成彙總(僅母體)
        "target": {
            "with_target": len(with_target),
            "no_target": len(no_target),          # 未回報預計完成日(要催)
            "target_overdue": len(target_overdue),  # 已過自己承諾的完成日
            "target_soon": len(target_soon),      # 預計 30 天內完成
        },
        # 清單(主管要催的兩類)
        "need_apply_list": sorted(
            (_detail(f) for f in need_apply),
            key=lambda r: ((r["overdue_days"] is None), -(r["overdue_days"] or 0))),
        "overdue_list": sorted(
            (_detail(f) for f in overdue),
            key=lambda r: -(r["overdue_days"] or 0)),
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

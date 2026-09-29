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
from .logic import CLOSE_DONE, CLOSE_OPEN, SEVERITIES, STAGE_ORIGINAL, overdue_days
from .models import Finding, ImportBatch

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
         sheet_key: Optional[str] = None, only_should_apply: bool = False,
         no_owner: bool = False, no_due: bool = False,
         today: Optional[dt.date] = None) -> list[dict]:
    """下鑽明細。status 預設未結案；band 互斥分帶；keyword 多字 AND；
    only_should_apply/no_owner/no_due 為缺口/行動線清單。"""
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

    def row(f: Finding) -> dict:
        return {
            "id": f.id, "sheet_key": f.sheet_key, "plugin_id": f.plugin_id, "name": f.name,
            "host": f.host, "severity": f.severity, "department": f.department, "owner": f.owner,
            "effective_due": f.effective_due.isoformat() if f.effective_due else None,
            "overdue_days": overdue_days(f.effective_due, today),
            "action_line": action_line(f, today).isoformat() if action_line(f, today) else None,
            "should_apply": should_apply(f, today),
            "stage": f.stage, "close_status": f.close_status, "remark": f.remark,
        }

    return [row(f) for f in fs]


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

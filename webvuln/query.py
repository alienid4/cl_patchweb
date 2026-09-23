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

from .logic import CLOSE_DONE, CLOSE_OPEN, SEVERITIES, overdue_days
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
    }


def find(session: Session, department: Optional[str] = None, status: str = CLOSE_OPEN,
         owner: Optional[str] = None, severity: Optional[str] = None,
         band: Optional[str] = None, keyword: Optional[str] = None,
         sheet_key: Optional[str] = None, today: Optional[dt.date] = None) -> list[dict]:
    """下鑽明細。status 預設未結案；band 用互斥分帶過濾；keyword 多字 AND(host/owner/name/plugin)。"""
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
            "stage": f.stage, "close_status": f.close_status, "remark": f.remark,
        }

    return [row(f) for f in fs]

"""承辦疊加層服務：reconcile(隨匯入)、查詢、狀態轉移、可疑聲稱偵測。

穩定鍵沿用 query.vuln_key(finding) 的正規化，串成字串當 Case.vuln_key，確保 finding↔case 對得上。
寫入(狀態轉移)之權限控管屬 W4(登入+audit)；此層只保證規則正確與可測，端點串接見 main。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import logic, query
from .models import Case, Finding, ImportBatch


def key_str(f: Finding) -> str:
    return "|".join(query.vuln_key(f))


def reconcile(session: Session, batch: ImportBatch) -> dict:
    """把最新快照對到 case：見到的 upsert 並更新去正規化欄／source_closed；沒見到的標 orphan。

    回傳 {created, updated, orphaned} 計數。冪等：同一批重跑結果一致。
    """
    findings = session.execute(
        select(Finding).where(Finding.batch_id == batch.id)
    ).scalars().all()

    existing = {c.vuln_key: c for c in session.execute(select(Case)).scalars().all()}
    created = updated = 0
    seen: set[str] = set()

    for f in findings:
        k = key_str(f)
        if k in seen:
            continue  # 同一弱點在單批可能多列；一鍵一案
        seen.add(k)
        closed = (f.close_status == logic.CLOSE_DONE)
        c = existing.get(k)
        if c is None:
            session.add(Case(
                vuln_key=k, sheet_key=f.sheet_key, plugin_id=f.plugin_id, host=f.host,
                department=f.department, owner=f.owner, status=logic.CASE_NEW,
                last_seen_batch_id=batch.id, is_orphan=False, source_closed=closed,
            ))
            created += 1
        else:
            c.department = f.department
            c.owner = f.owner
            c.host = f.host
            c.last_seen_batch_id = batch.id
            c.is_orphan = False
            c.source_closed = closed
            updated += 1

    orphaned = 0
    for k, c in existing.items():
        if k not in seen and not c.is_orphan:
            c.is_orphan = True
            orphaned += 1

    session.commit()
    return {"created": created, "updated": updated, "orphaned": orphaned}


def apply_owner_overrides(session: Session, batch: ImportBatch) -> int:
    """把管理員改過的負責人(Case.owner_override)套回本批 finding。重匯後呼叫→不被 Excel 洗掉。"""
    overrides = {c.vuln_key: c.owner_override for c in
                 session.execute(select(Case).where(Case.owner_override.isnot(None))).scalars().all()
                 if c.owner_override}
    if not overrides:
        return 0
    n = 0
    for f in session.execute(select(Finding).where(Finding.batch_id == batch.id)).scalars().all():
        ov = overrides.get(key_str(f))
        if ov and f.owner != ov:
            f.owner = ov
            n += 1
    if n:
        session.commit()
    return n


def _get_or_create_case(session: Session, f0: Finding) -> Case:
    c = session.execute(select(Case).where(Case.vuln_key == key_str(f0))).scalars().first()
    if c is None:
        c = Case(vuln_key=key_str(f0), sheet_key=f0.sheet_key, plugin_id=f0.plugin_id, host=f0.host,
                 department=f0.department, owner=f0.owner, status=logic.CASE_NEW,
                 last_seen_batch_id=f0.batch_id, is_orphan=False)
        session.add(c)
    return c


def set_overlay(session: Session, finding_id: int, fields: dict) -> dict:
    """管理員在系統內改一筆弱點的可寫欄位（存疊加層、重匯不洗掉、不動 Excel）。
    fields 只改有給的鍵：
      - 'owner' → Case.owner_override，並即時套到最新快照同鍵 finding 的 owner
      - 'note'  → Case.track_note（管理追蹤備註，純系統、不碰 Excel 原備註）
    空字串＝清除該覆蓋。"""
    f0 = session.get(Finding, finding_id)
    if f0 is None:
        raise ValueError("弱點不存在")
    vk = key_str(f0)
    c = _get_or_create_case(session, f0)
    out = {"vuln_key": vk, "updated": 0}

    if "owner" in fields:
        ov = (fields["owner"] or "").strip() or None
        c.owner_override = ov
        latest = query.latest_batch(session)
        if latest and ov:
            for f in session.execute(select(Finding).where(Finding.batch_id == latest.id)).scalars().all():
                if key_str(f) == vk:
                    f.owner = ov
                    out["updated"] += 1
        out["owner"] = ov
    if "note" in fields:
        c.track_note = (fields["note"] or "").strip() or None
        out["note"] = c.track_note
    if "target_date" in fields:
        c.target_date = logic.parse_iso_date(fields["target_date"])
        out["target_date"] = c.target_date.isoformat() if c.target_date else None
    session.commit()
    return out


def set_owner(session: Session, finding_id: int, owner: Optional[str]) -> dict:
    """相容舊呼叫：只改負責人。"""
    return set_overlay(session, finding_id, {"owner": owner})


def transition(session: Session, case_id: int, to: str, note: Optional[str] = None) -> Case:
    """推進申請管線狀態；非法轉移丟 ValueError（呼叫端轉 400）。"""
    if to not in logic.CASE_STATUSES:
        raise ValueError(f"未知狀態：{to}")
    c = session.get(Case, case_id)
    if c is None:
        raise ValueError("案件不存在")
    if not logic.can_transition(c.status, to):
        raise ValueError(f"不允許的轉移：{c.status} → {to}")
    c.status = to
    c.status_changed_at = dt.datetime.now()
    if note is not None:
        c.note = note
    session.commit()
    session.refresh(c)
    return c


def _is_suspect(c: Case, latest: Optional[ImportBatch]) -> bool:
    """承辦聲稱完成、但來源仍未結案，且『聲稱早於最新匯入』(排除資料還沒更新的寬限)。"""
    if not (c.status == logic.CASE_DONE and not c.source_closed):
        return False
    if latest is None:
        return False
    # 承辦聲稱完成早於最新匯入 → 資料本應反映卻仍未結 → 可疑；聲稱晚於匯入＝資料還沒更新(寬限)
    return c.status_changed_at <= latest.imported_at


def _row(c: Case, latest: Optional[ImportBatch]) -> dict:
    return {
        "id": c.id, "vuln_key": c.vuln_key, "sheet_key": c.sheet_key,
        "plugin_id": c.plugin_id, "host": c.host, "department": c.department,
        "owner": c.owner, "status": c.status, "note": c.note,
        "is_orphan": c.is_orphan, "source_closed": c.source_closed,
        "suspect": _is_suspect(c, latest),
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "status_changed_at": c.status_changed_at.isoformat() if c.status_changed_at else None,
    }


def list_cases(session: Session, status: Optional[str] = None, department: Optional[str] = None,
               orphan: Optional[bool] = None, suspect: Optional[bool] = None) -> list[dict]:
    latest = query.latest_batch(session)
    cs = session.execute(select(Case)).scalars().all()
    rows = [_row(c, latest) for c in cs]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if department and department != "全部":
        rows = [r for r in rows if r["department"] == department]
    if orphan is not None:
        rows = [r for r in rows if r["is_orphan"] == orphan]
    if suspect is not None:
        rows = [r for r in rows if r["suspect"] == suspect]
    rows.sort(key=lambda r: (not r["suspect"], not r["is_orphan"], r["status"]))
    return rows


def purge_orphans(session: Session) -> int:
    """刪除所有『已消失』(is_orphan) 案件：來源快照已無此弱點的舊案件紀錄。
    回傳刪除筆數。不影響現有弱點(finding)與仍在來源的案件。"""
    orphans = session.execute(select(Case).where(Case.is_orphan.is_(True))).scalars().all()
    n = len(orphans)
    for c in orphans:
        session.delete(c)
    if n:
        session.commit()
    return n


def suspect_count(session: Session) -> int:
    latest = query.latest_batch(session)
    return sum(1 for c in session.execute(select(Case)).scalars().all()
               if _is_suspect(c, latest))

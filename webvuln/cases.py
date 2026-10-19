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
                department=f.department, owner=f.owner, status=logic.PROGRESS_NONE,
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
    """把管理員改過的負責人/部門(Case.owner_override/department_override)套回本批 finding。
    重匯後呼叫→不被 Excel 洗掉。回傳套用筆數(欄位計)。"""
    cs = session.execute(select(Case).where(
        (Case.owner_override.isnot(None)) | (Case.department_override.isnot(None)))).scalars().all()
    owners = {c.vuln_key: c.owner_override for c in cs if c.owner_override}
    depts = {c.vuln_key: c.department_override for c in cs if c.department_override}
    if not owners and not depts:
        return 0
    n = 0
    for f in session.execute(select(Finding).where(Finding.batch_id == batch.id)).scalars().all():
        k = key_str(f)
        ov = owners.get(k)
        if ov and f.owner != ov:
            f.owner = ov
            n += 1
        dv = depts.get(k)
        if dv and f.department != dv:
            f.department = dv
            n += 1
    if n:
        session.commit()
    return n


AUTO_RESOLVE_ACTION = "auto_resolve_submit"


def auto_resolve_submitted(session: Session, batch: ImportBatch) -> int:
    """匯入新批後，自動解除已有結論的「送審中」（2026-10-19）。冪等：解除過的就不是送審中，不會再處理。
      - 展延送審中：Excel 處置階段變成 首次展延中／例外管理中 → 資安已核准，清掉標記
      - 例外送審中：Excel 處置階段變成 例外管理中 → 資安已核准，清掉標記
      - 已結案：結案就不需要等核准，清掉標記
    到期日／階段一律照 Excel（匯入時已算好），所以清掉後這筆自然落到「已核准」並用資安的日期。
    每筆解除都寫一筆操作紀錄（username＝系統），這一筆的歷程看得到。回傳解除筆數。"""
    from .models import AuditLog
    cs = session.execute(select(Case).where(Case.status.in_(logic.PROGRESS_SUBMITTED))).scalars().all()
    if not cs:
        return 0
    first: dict = {}
    for f in session.execute(select(Finding).where(Finding.batch_id == batch.id)
                             .order_by(Finding.id)).scalars().all():
        first.setdefault(key_str(f), f)   # 同鍵多列取第一列（同 reconcile：一鍵一案）
    now = dt.datetime.now()
    n = 0
    for c in cs:
        f = first.get(c.vuln_key)
        if f is None:
            continue   # 這批沒有這筆（來源消失）→ 標記留著，不猜
        closed = f.close_status == logic.CLOSE_DONE
        # 核准要「還有效」才算（2026-10-19）：例外／展延今天已過期＝核准已失效，承辦送的是續申請，標記留著
        valid = f.effective_due is None or f.effective_due >= now.date()
        if not closed and not (logic.submit_reflected(c.status, f.stage) and valid):
            continue
        old = c.status
        if closed:
            detail = {"progress": f"{old}→（已結案，自動解除）", "close_status": f.close_status}
        else:
            detail = {"progress": f"{old}→（資安已核准，自動解除）", "stage": f.stage,
                      "due": f.effective_due.isoformat() if f.effective_due else ""}
        c.status = logic.PROGRESS_NONE
        c.status_changed_at = now
        session.add(AuditLog(username="系統", action=AUTO_RESOLVE_ACTION, target=f"vuln:{c.vuln_key}",
                             detail=str(detail), at=now))
        n += 1
    if n:
        session.commit()
    return n


def _get_or_create_case(session: Session, f0: Finding) -> Case:
    c = session.execute(select(Case).where(Case.vuln_key == key_str(f0))).scalars().first()
    if c is None:
        c = Case(vuln_key=key_str(f0), sheet_key=f0.sheet_key, plugin_id=f0.plugin_id, host=f0.host,
                 department=f0.department, owner=f0.owner, status=logic.PROGRESS_NONE,
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
    # 先驗證、再動任何欄位（2026-10-19）：驗不過就整筆不改，批次迴圈接著 commit 也不會把別的欄位偷偷寫進去
    new_prog = None
    if "progress" in fields:
        new_prog = (fields["progress"] or "").strip()
        if new_prog and new_prog not in logic.PROGRESS_VALUES:
            raise ValueError("處理進度不認得：%s" % new_prog)
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
    if "department" in fields:
        dv = (fields["department"] or "").strip() or None
        c.department_override = dv
        c.department = dv or c.department
        latest = query.latest_batch(session)
        if latest and dv:
            for f in session.execute(select(Finding).where(Finding.batch_id == latest.id)).scalars().all():
                if key_str(f) == vk:
                    f.department = dv
        out["department"] = dv
    if "note" in fields:
        c.track_note = (fields["note"] or "").strip() or None
        out["note"] = c.track_note
    if "target_date" in fields:
        c.target_date = logic.parse_iso_date(fields["target_date"])
        out["target_date"] = c.target_date.isoformat() if c.target_date else None
    if "progress" in fields:
        # 管理人手動處理進度(處理中/等複掃/送審中/清空)；更新標記時間供複掃三態／送審逾時對帳
        new = new_prog   # 已在最前面驗證過
        # 進度沒變就不動標記時間（2026-10-19）：編輯視窗每次存檔都會帶進度，
        # 只改備註就重設時間，「送審逾時」永遠不會到。
        if new != (c.status or "") or c.status_changed_at is None:
            c.status_changed_at = dt.datetime.now()
        c.status = new
        out["progress"] = c.status
    session.commit()
    return out


def set_owner(session: Session, finding_id: int, owner: Optional[str]) -> dict:
    """相容舊呼叫：只改負責人。"""
    return set_overlay(session, finding_id, {"owner": owner})


def _is_suspect(c: Case, latest: Optional[ImportBatch]) -> bool:
    """標『等複掃』但來源仍未結，且已跨過一次新匯入仍未結 → 可疑(見 logic.classify_rescan)。"""
    return logic.classify_rescan(
        c.status, c.source_closed, c.status_changed_at,
        latest.imported_at if latest else None) == logic.RESCAN_SUSPECT


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

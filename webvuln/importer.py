"""匯入：把一批 finding 存成一個新快照(batch)，並把舊快照的 is_latest 設 false。

入庫時算好 effective_due / stage；close_status / severity 以 logic 正規化（對已正規化的值是冪等的，
所以前端有帶就原樣、沒帶或帶原文也接得住）。
"""
from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import logic
from .models import Finding, ImportBatch, SheetColumns
from .schemas import ImportIn


def create_batch(session: Session, data: ImportIn) -> ImportBatch:
    # 變化紀錄：舊的最新版被換掉前，先把它當下的承辦進度拍下來（「上次展延送審幾筆」要用）
    from . import changes
    from .models import ImportBatch as _IB
    from sqlalchemy import select as _sel
    import datetime as _dt
    old = session.execute(_sel(_IB).where(_IB.is_latest.is_(True)).order_by(_IB.id.desc())).scalars().first()
    old_asof = _dt.datetime.now()
    if old is not None:
        changes.capture_progress(session, old)
    # 舊的最新版全部退位
    session.execute(update(ImportBatch).where(ImportBatch.is_latest.is_(True)).values(is_latest=False))

    batch = ImportBatch(
        source_file=data.source_file,
        note=data.note,
        is_latest=True,
        row_count=len(data.findings),
    )

    for f in data.findings:
        rem = logic.parse_iso_date(f.remediation_due)
        fext = logic.parse_iso_date(f.first_extension_due)
        exc = logic.parse_iso_date(f.exception_due)
        batch.findings.append(
            Finding(
                sheet_key=f.sheet_key,
                plugin_id=f.plugin_id,
                name=f.name,
                host=f.host,
                severity=logic.normalize_severity(f.severity) or logic.normalize_severity(f.severity_raw),
                severity_raw=f.severity_raw,
                department=f.department,
                owner=f.owner,
                remediation_due=rem,
                first_extension_due=fext,
                exception_due=exc,
                effective_due=logic.compute_effective_due(exc, fext, rem, f.remark),
                stage=logic.compute_stage(exc, fext, rem, f.remark),
                close_status=logic.classify_close(f.close_status),
                close_date=logic.parse_iso_date(f.close_date),
                remark=f.remark,
                raw=f.raw,
            )
        )

    # 各表欄序（供原封匯出）
    for sheet_key, columns in (data.sheet_columns or {}).items():
        batch.sheets.append(SheetColumns(sheet_key=sheet_key, columns=list(columns)))

    # 每一列的穩定識別碼（轉換過才用新制；沒轉過就維持舊鍵，避免新舊混用）
    from . import rowkey
    if rowkey.migrated(session):
        rowkey.assign(batch.findings)

    session.add(batch)
    session.commit()
    session.refresh(batch)

    # 承辦疊加層：把新快照對到既有 case（見 W3）。import 內部呼叫，避免循環匯入放這。
    from . import cases
    cases.reconcile(session, batch)
    # 管理員改過的負責人（owner_override）在重匯時套回本批 finding（Excel 值被覆蓋，不被洗掉）
    cases.apply_owner_overrides(session, batch)
    # 跟上一批逐筆比對，寫變化紀錄（失敗不擋匯入；開機補算會再補）
    try:
        changes.compute_run(session, batch, old, prev_asof=old_asof, progress_captured=old is not None)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[changes] 比對失敗（開機會補算）：{e!r}")
    return batch

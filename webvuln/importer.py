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

    session.add(batch)
    session.commit()
    session.refresh(batch)

    # 承辦疊加層：把新快照對到既有 case（見 W3）。import 內部呼叫，避免循環匯入放這。
    from . import cases
    cases.reconcile(session, batch)
    # 管理員改過的負責人（owner_override）在重匯時套回本批 finding（Excel 值被覆蓋，不被洗掉）
    cases.apply_owner_overrides(session, batch)
    return batch

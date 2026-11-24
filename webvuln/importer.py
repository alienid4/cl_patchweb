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


_UNBOUND = object()


def create_batch(session: Session, data: ImportIn, older: dict | None = None,
                 expected_latest=_UNBOUND, sheets_missing: list | None = None) -> ImportBatch:
    """older＝使用者已確認「用舊檔覆蓋」時的衝突內容；sheets_missing＝已確認「工作表不見」的清單（都記進匯入檢核報告）。
    expected_latest＝使用者確認當下看到的最新批 id（None＝當時沒有資料）：退位那一步原子地再驗一次，
    不是同一批（有人剛匯入／刪批）→ 丟 importcheck.LatestChanged，什麼都不寫。"""
    from .importcheck import LatestChanged
    # 變化紀錄：舊的最新版被換掉前，先把它當下的承辦進度拍下來（「上次展延送審幾筆」要用）
    from . import changes
    from .models import ImportBatch as _IB
    from sqlalchemy import select as _sel
    import datetime as _dt
    old = session.execute(_sel(_IB).where(_IB.is_latest.is_(True)).order_by(_IB.id.desc())).scalars().first()
    old_asof = _dt.datetime.now()
    if expected_latest is not _UNBOUND and (old.id if old else None) != expected_latest:
        raise LatestChanged()
    if old is not None:
        changes.capture_progress(session, old)
    if expected_latest is not _UNBOUND and expected_latest is not None:
        # 條件式退位：確認的那一批此刻仍是最新才成立（同一個寫入交易內，別人插隊就 0 列）
        res = session.execute(update(ImportBatch).where(ImportBatch.id == expected_latest,
                                                        ImportBatch.is_latest.is_(True)).values(is_latest=False))
        if res.rowcount != 1:
            session.rollback()
            raise LatestChanged()
    # 舊的最新版全部退位
    session.execute(update(ImportBatch).where(ImportBatch.is_latest.is_(True)).values(is_latest=False))

    batch = ImportBatch(
        source_file=data.source_file,
        note=data.note,
        is_latest=True,
        row_count=len(data.findings),
        # 前端用哪一版規則解析的（舊版前端沒帶＝None → 之後管理者開頁會就地重算，V2.110）
        rules_version=data.rules_version,
    )

    _rows = []   # (Finding, Excel 列號)：匯入檢核報告用
    for f in data.findings:
        rem = logic.parse_iso_date(f.remediation_due)
        fext = logic.parse_iso_date(f.first_extension_due)
        exc = logic.parse_iso_date(f.exception_due)
        _fo = Finding(
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
            effective_due=logic.compute_effective_due(exc, fext, rem, f.remark, f.raw),
            stage=logic.compute_stage(exc, fext, rem, f.remark, f.raw),
            close_status=logic.classify_close(f.close_status),
            close_date=logic.parse_iso_date(f.close_date),
            remark=f.remark,
            raw=f.raw,
        )
        batch.findings.append(_fo)
        _rows.append((_fo, f.row_no))

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

    # 匯入檢核報告（V2.110）：失敗不擋匯入，報告裡記錯誤
    from . import importcheck
    try:
        batch.import_report = importcheck.build_report(
            session, batch, old, {fo.id: rn for fo, rn in _rows}, data.sheet_columns, data.client_checks,
            data.rules_version, older=older, sheets_missing_confirmed=sheets_missing)
    except Exception as e:  # noqa: BLE001
        session.rollback()
        batch.import_report = {"version": importcheck.REPORT_VERSION, "error": f"檢核報告產生失敗：{e!r}"}
    session.commit()

    # 承辦疊加層：把新快照對到既有 case（見 W3）。import 內部呼叫，避免循環匯入放這。
    from . import cases
    cases.reconcile(session, batch)
    # 管理員改過的負責人（owner_override）在重匯時套回本批 finding（Excel 值被覆蓋，不被洗掉）
    cases.apply_owner_overrides(session, batch)
    # 送審中（等核准）自動解除：資安已核准(階段已反映)或已結案的，清掉標記並留紀錄（2026-10-19）
    cases.auto_resolve_submitted(session, batch)
    # 資安已確認結案自動解除：Excel 這次已不是未結案（終於更新了）→ 清狀態與證據並留紀錄（2026-10-28）
    cases.auto_resolve_confirmed(session, batch)
    # 跟上一批逐筆比對，寫變化紀錄（失敗不擋匯入；開機補算會再補）
    try:
        changes.compute_run(session, batch, old, prev_asof=old_asof, progress_captured=old is not None)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[changes] 比對失敗（開機會補算）：{e!r}")
    return batch

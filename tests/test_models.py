import datetime as dt

from sqlalchemy import select

from webvuln.models import Finding, ImportBatch


def test_batch_finding_roundtrip(session):
    b = ImportBatch(source_file="report_v1.xlsx", row_count=2, is_latest=True)
    b.findings = [
        Finding(sheet_key="1-系統弱點掃描弱點", plugin_id="10420", host="10.30.1.11",
                severity="Critical", department="資訊架構部", owner="玄慈",
                effective_due=dt.date(2026, 4, 20), stage="原始修補期限", close_status="未結案"),
        Finding(sheet_key="1-系統弱點掃描弱點", plugin_id="10070", host="10.21.1.2",
                severity="Medium", department="資訊架構部", owner="喬峰",
                effective_due=dt.date(2026, 5, 30), stage="例外管理中", close_status="未結案"),
    ]
    session.add(b)
    session.commit()

    got = session.execute(select(ImportBatch)).scalar_one()
    assert got.row_count == 2
    assert got.is_latest is True
    assert len(got.findings) == 2
    assert {f.owner for f in got.findings} == {"玄慈", "喬峰"}


def test_cascade_delete(session):
    b = ImportBatch(source_file="x.xlsx", row_count=1)
    b.findings = [Finding(host="h1", severity="Low")]
    session.add(b)
    session.commit()
    assert session.execute(select(Finding)).scalars().all()

    session.delete(b)
    session.commit()
    # ORM cascade：批次刪掉，明細跟著走
    assert session.execute(select(Finding)).scalars().all() == []


def test_is_latest_flag(session):
    b1 = ImportBatch(source_file="a", is_latest=True)
    b2 = ImportBatch(source_file="b", is_latest=False)
    session.add_all([b1, b2])
    session.commit()
    latest = session.execute(select(ImportBatch).where(ImportBatch.is_latest.is_(True))).scalars().all()
    assert len(latest) == 1 and latest[0].source_file == "a"

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from webvuln import importer
from webvuln.models import Finding, ImportBatch
from webvuln.schemas import FindingIn, ImportIn


def _sample(n_exc=True):
    findings = [
        FindingIn(sheet_key="1-系統弱點掃描弱點", plugin_id="10420", host="10.30.1.11",
                  severity="Critical", department="資訊架構部", owner="玄慈",
                  remediation_due="2026-05-01", first_extension_due="2026-06-01",
                  exception_due="2026-04-20" if n_exc else None, close_status="未結案",
                  remark="例外管理(iForm_1)"),  # 有申請紀錄→例外日期才算數
        FindingIn(sheet_key="1-系統弱點掃描弱點", plugin_id="10070", host="10.21.1.2",
                  severity="中", department="資訊架構部", owner="喬峰",
                  remediation_due="2026-05-30", close_status="已修補"),
    ]
    return ImportIn(source_file="report.xlsx", findings=findings)


def test_create_batch_computes_derived(session):
    batch = importer.create_batch(session, _sample())
    assert batch.row_count == 2 and batch.is_latest is True
    fs = {f.owner: f for f in batch.findings}
    # 例外核准期限優先 → effective_due 取 exception_due
    assert fs["玄慈"].effective_due == dt.date(2026, 4, 20)
    assert fs["玄慈"].stage == "例外管理中"
    # 無例外/展延 → 取修補期限；stage 原始
    assert fs["喬峰"].effective_due == dt.date(2026, 5, 30)
    assert fs["喬峰"].stage == "原始修補期限"
    # 嚴重度後備正規化：中 → Medium
    assert fs["喬峰"].severity == "Medium"
    # 結案分類：已修補 → 已結案
    assert fs["喬峰"].close_status == "已結案"


def test_second_import_flips_latest(session):
    b1 = importer.create_batch(session, _sample())
    b2 = importer.create_batch(session, _sample())
    latest = session.execute(select(ImportBatch).where(ImportBatch.is_latest.is_(True))).scalars().all()
    assert len(latest) == 1 and latest[0].id == b2.id
    assert session.get(ImportBatch, b1.id).is_latest is False
    # 兩版快照都在（歷史留著供趨勢）
    assert session.execute(select(func.count(ImportBatch.id))).scalar_one() == 2


def test_import_api(client, engine):
    payload = _sample().model_dump()
    r = client.post("/api/import", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 2 and body["is_latest"] is True

    factory = sessionmaker(bind=engine)
    with factory() as s:
        assert s.execute(select(func.count(Finding.id))).scalar_one() == 2
        glory = s.execute(select(Finding).where(Finding.owner == "玄慈")).scalar_one()
        assert glory.effective_due == dt.date(2026, 4, 20)

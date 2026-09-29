from sqlalchemy import select

from webvuln import importer
from webvuln.models import Finding, SheetColumns
from webvuln.schemas import FindingIn, ImportIn


def test_raw_and_sheet_columns_roundtrip(session):
    data = ImportIn(
        source_file="r.xlsx",
        sheet_columns={"1-系統弱點掃描弱點": ["主機", "弱點名稱", "嚴重度", "負責人", "修補期限", "備註"]},
        findings=[
            FindingIn(
                sheet_key="1-系統弱點掃描弱點", host="10.30.1.11", severity="Critical",
                owner="玄慈", remediation_due="2026-06-01", close_status="未結案",
                raw={"主機": "10.30.1.11", "弱點名稱": "SMBv1 啟用", "嚴重度": "Critical",
                     "負責人": "玄慈", "修補期限": "114/5/1", "備註": ""},
            ),
        ],
    )
    batch = importer.create_batch(session, data)

    f = session.execute(select(Finding)).scalar_one()
    # 整列原始資料原封存下（含來源原始的民國年字串、空備註）
    assert f.raw["弱點名稱"] == "SMBv1 啟用"
    assert f.raw["修補期限"] == "114/5/1"      # 原封保留來源原值
    assert f.raw["備註"] == ""                 # 匯出時才轉「無原始資料」

    sc = session.execute(select(SheetColumns)).scalar_one()
    assert sc.sheet_key == "1-系統弱點掃描弱點"
    assert sc.columns[0] == "主機" and sc.columns[-1] == "備註"   # 欄序 1:1 保留


def test_import_without_raw_still_ok(session):
    # 舊契約（沒帶 raw）仍相容
    batch = importer.create_batch(session, ImportIn(findings=[FindingIn(host="h", severity="Low")]))
    assert batch.row_count == 1
    assert session.execute(select(Finding)).scalar_one().raw is None

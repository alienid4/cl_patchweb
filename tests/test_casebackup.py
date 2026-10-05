"""承辦狀態備份／還原：A 機標的狀態帶到 B 機（2026-10-05 使用者要把公司狀態拿到 221 校準）。"""
import pytest
from sqlalchemy.orm import Session, sessionmaker

from webvuln import casebackup, cases, importer, rowkey
from webvuln.db import _make_engine
from webvuln.models import Base, Case, Finding
from webvuln.schemas import FindingIn, ImportIn


def _db(tmp_path, name):
    eng = _make_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False, class_=Session)()
    rowkey.migrate(s)                       # 兩台都升到新識別碼
    rows = [FindingIn(sheet_key="8-BAS演練", name=n, close_status="未結案", raw={"Audit Name": n}) for n in ("A", "B", "C")]
    importer.create_batch(s, ImportIn(source_file="t.xlsx", findings=rows))
    return s


def _case(s, name):
    f = s.query(Finding).filter(Finding.name == name).one()
    return s.query(Case).filter(Case.vuln_key == cases.key_str(f)).one()


def test_backup_and_restore(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    ca = _case(a, "A"); ca.status, ca.track_note = "處理中", "公司這邊在修"
    cb_ = _case(a, "B"); cb_.status = "等複掃"
    a.commit()
    # B 機上 B 這列已經有不同的狀態 → 衝突
    bb = _case(b, "B"); bb.status, bb.track_note = "處理中", "221 自己標的"; b.commit()

    data = casebackup.export(a)
    assert data["count"] == 2 and data["key_scheme"] == "v2"

    pv = casebackup.restore(b, data)                 # 只預覽
    assert (pv["new"], pv["conflict"], pv["unmatched"], pv["applied"]) == (1, 1, 0, False)
    assert _case(b, "A").status in ("", "未申請")       # 預覽不寫

    r = casebackup.restore(b, data, apply=True)      # 衝突預設保留現有
    assert r["applied"] and r["skipped_conflict"] == 1
    assert _case(b, "A").status == "處理中" and _case(b, "A").track_note == "公司這邊在修"
    assert _case(b, "B").track_note == "221 自己標的"

    r2 = casebackup.restore(b, data, overwrite=True, apply=True)
    assert r2["overwritten"] == 1 and _case(b, "B").status == "等複掃"
    assert casebackup.restore(b, data, apply=True)["same"] == 2     # 重跑：都一樣，不動


def test_refuse_different_key_scheme(tmp_path):
    a = _db(tmp_path, "a.db")
    data = casebackup.export(a)
    data["key_scheme"] = "v1"
    with pytest.raises(ValueError):
        casebackup.restore(a, data)


def test_api_requires_super(client):
    from webvuln import config
    config.NO_AUTH = False
    try:
        assert client.get("/api/cases/backup").status_code == 401
        assert client.post("/api/cases/restore", json={"data": {}}).status_code == 401
    finally:
        config.NO_AUTH = True

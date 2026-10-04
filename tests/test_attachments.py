"""申請佐證文件：上傳／列出／去重／重匯不洗／刪除（最後參照才刪實體檔）／副檔名白名單。"""
import pytest

from webvuln import attachments, config, importer, query
from webvuln.models import Finding
from webvuln.schemas import FindingIn, ImportIn


def _one_finding(session):
    importer.create_batch(session, ImportIn(findings=[
        FindingIn(host="h1", plugin_id="p1", sheet_key="s", owner="甲", close_status="未結案"),
    ]))
    return session.query(Finding).one().id


def test_attachment_flow(session, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "up")
    fid = _one_finding(session)

    r = attachments.save_for_finding(session, fid, b"hello wbs", "wbs.pdf", "WBS", "tester")
    assert r["kind"] == "WBS" and r["orig_name"] == "wbs.pdf" and r["size"] == 9
    assert attachments.list_for_finding(session, fid)[0]["uploaded_by"] == "tester"

    # findings 清單帶回 att_count（供 📎N）
    assert query.find(session, status="未結案")[0]["att_count"] == 1

    # 去重：同內容再傳一次 → 共用一個實體檔，但 2 筆 metadata
    attachments.save_for_finding(session, fid, b"hello wbs", "copy.pdf", "佐證", "t2")
    assert len(list((tmp_path / "up").iterdir())) == 1
    assert len(attachments.list_for_finding(session, fid)) == 2

    # 重匯 Excel（同穩定鍵）→ 附件還在
    importer.create_batch(session, ImportIn(findings=[
        FindingIn(host="h1", plugin_id="p1", sheet_key="s", owner="甲", close_status="未結案"),
    ]))
    fid2 = session.query(Finding).order_by(Finding.id.desc()).first().id
    lst = attachments.list_for_finding(session, fid2)
    assert len(lst) == 2   # 重匯不洗

    # 刪第一筆 → 實體檔仍在（另一筆還參照）；刪第二筆 → 實體檔才消失
    attachments.delete(session, lst[0]["id"])
    assert len(list((tmp_path / "up").iterdir())) == 1
    attachments.delete(session, lst[1]["id"])
    assert len(list((tmp_path / "up").iterdir())) == 0


def test_attachment_rejects_bad_ext_and_big(session, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "up")
    monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 10)
    fid = _one_finding(session)
    with pytest.raises(ValueError):
        attachments.save_for_finding(session, fid, b"x", "evil.exe", "其他", "t")   # 副檔名不允許
    with pytest.raises(ValueError):
        attachments.save_for_finding(session, fid, b"x" * 11, "big.pdf", "其他", "t")  # 超過上限

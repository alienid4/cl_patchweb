import datetime as dt

import pytest

from webvuln import cases, importer, logic, query
from webvuln.models import Case, ImportBatch
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _imp(session, findings):
    return importer.create_batch(session, ImportIn(findings=findings))


def test_reconcile_create_update_orphan(session):
    f = FindingIn
    # 第一批：兩個弱點 → 建兩案
    _imp(session, [
        f(host="H1 ", plugin_id="p1", sheet_key="s", owner="玄慈", close_status="未結案"),
        f(host="h2", plugin_id="p2", sheet_key="s", owner="喬峰", close_status="未結案"),
    ])
    assert session.query(Case).count() == 2
    c1 = session.query(Case).filter_by(vuln_key="s|p1|h1").one()  # host 正規化(去空白小寫)
    assert c1.status == logic.CASE_NEW and c1.is_orphan is False

    # 第二批：p1 換承辦名＋已結案；p2 消失(→orphan)；新增 p3
    _imp(session, [
        f(host="h1", plugin_id="p1", sheet_key="s", owner="虛竹", close_status="已結案"),
        f(host="h3", plugin_id="p3", sheet_key="s", owner="段譽", close_status="未結案"),
    ])
    session.expire_all()
    c1 = session.query(Case).filter_by(vuln_key="s|p1|h1").one()
    assert c1.owner == "虛竹" and c1.source_closed is True and c1.is_orphan is False
    c2 = session.query(Case).filter_by(vuln_key="s|p2|h2").one()
    assert c2.is_orphan is True                    # 來源消失
    assert session.query(Case).count() == 3        # p3 新建，總數不減


def test_reconcile_idempotent(session):
    f = FindingIn
    batch = _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", close_status="未結案")])
    r = cases.reconcile(session, batch)   # 同批重跑
    assert r["created"] == 0 and r["orphaned"] == 0
    assert session.query(Case).count() == 1


def test_transition_valid_and_invalid(session):
    f = FindingIn
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", close_status="未結案")])
    c = session.query(Case).one()
    c = cases.transition(session, c.id, logic.CASE_WAIT_MGR)
    assert c.status == logic.CASE_WAIT_MGR
    cases.transition(session, c.id, logic.CASE_WAIT_SEC)
    with pytest.raises(ValueError):
        cases.transition(session, c.id, logic.CASE_DONE)   # 待資安 不能直接跳完成


def test_suspect_and_close_stats(session):
    f = FindingIn
    # 第一批未結
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", owner="玄慈", close_status="未結案")])
    c = session.query(Case).one()
    # 承辦一路推到完成
    for to in (logic.CASE_WAIT_MGR, logic.CASE_WAIT_SEC, logic.CASE_APPROVED, logic.CASE_DONE):
        cases.transition(session, c.id, to)
    # 把聲稱時間壓到很早，之後再匯入一批仍未結 → 聲稱早於匯入 = 可疑
    session.query(Case).update({Case.status_changed_at: dt.datetime(2020, 1, 1)})
    session.commit()
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", owner="玄慈", close_status="未結案")])

    assert cases.suspect_count(session) == 1
    stats = query.close_stats(session, today=TODAY)
    assert stats["claimed_unconfirmed"] == 1


def test_endpoint_write_gated(client, monkeypatch):
    from webvuln import config
    # 先建一案
    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}
    ]})
    cid = client.get("/api/cases").json()[0]["id"]

    monkeypatch.setattr(config, "ALLOW_WRITE", False)
    r = client.post(f"/api/cases/{cid}/transition", json={"to": "待主管"})
    assert r.status_code == 403

    monkeypatch.setattr(config, "ALLOW_WRITE", True)
    r = client.post(f"/api/cases/{cid}/transition", json={"to": "待主管"})
    assert r.status_code == 200 and r.json()["status"] == "待主管"
    r = client.post(f"/api/cases/{cid}/transition", json={"to": "完成"})
    assert r.status_code == 400   # 非法轉移

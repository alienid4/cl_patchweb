import datetime as dt

import pytest

from webvuln import cases, importer, logic, query
from webvuln.models import Case, Finding, ImportBatch
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
    assert c1.status == logic.PROGRESS_NONE and c1.is_orphan is False

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


def test_suspect_and_close_stats(session):
    f = FindingIn
    # 第一批未結
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", owner="玄慈", close_status="未結案")])
    fid = session.query(Finding).one().id
    # 管理人標「等複掃」(承辦聲稱做完、等資安複掃)
    cases.set_overlay(session, fid, {"progress": logic.PROGRESS_RESCAN})
    # 把標記時間壓到很早，之後再匯入一批仍未結 → 跨過新匯入仍未結 = 可疑
    session.query(Case).update({Case.status_changed_at: dt.datetime(2020, 1, 1)})
    session.commit()
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", owner="玄慈", close_status="未結案")])

    assert cases.suspect_count(session) == 1
    stats = query.close_stats(session, today=TODAY)
    assert stats["claimed_unconfirmed"] == 1


def test_endpoint_read_and_write_requires_login(client, engine, monkeypatch):
    from sqlalchemy.orm import Session, sessionmaker
    from webvuln import security, config
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "staff", "pw12345", role="承辦")

    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}
    ]})
    fid = client.get("/api/findings").json()[0]["id"]   # 讀取免登入

    monkeypatch.setattr(config, "NO_AUTH", False)    # 關免登入，測權限閘門
    body = {"set_progress": True, "progress": "處理中"}
    # 未登入寫入 → 401；登入後 → 200（細節見 test_auth）
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 401
    client.post("/api/login", json={"username": "staff", "password": "pw12345"})
    r = client.post(f"/api/findings/{fid}/overlay", json=body)
    assert r.status_code == 200 and r.json()["progress"] == "處理中"


def test_owner_override_survives_reimport(session):
    f = FindingIn
    importer.create_batch(session, ImportIn(findings=[
        f(host="h1", plugin_id="p1", sheet_key="s", owner="網路組", close_status="未結案"),
    ]))
    fid = session.query(__import__("webvuln.models", fromlist=["Finding"]).Finding).one().id
    # 改負責人
    r = cases.set_owner(session, fid, "張三")
    assert r["owner"] == "張三" and r["updated"] == 1
    from webvuln.models import Finding as F
    assert session.query(F).filter_by(batch_id=session.query(F).one().batch_id).one().owner == "張三"
    # 重匯(Excel 仍寫網路組)→ 覆蓋套回,不被洗掉
    importer.create_batch(session, ImportIn(findings=[
        f(host="h1", plugin_id="p1", sheet_key="s", owner="網路組", close_status="未結案"),
    ]))
    latest = query.latest_batch(session)
    newf = session.query(F).filter_by(batch_id=latest.id).one()
    assert newf.owner == "張三"   # 重匯後仍是改過的名字


def test_progress_and_rescan_states(session):
    f = FindingIn
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", close_status="未結案")])
    fid = session.query(Finding).one().id
    # 標等複掃(現在) + 來源未結 + 標記在最新匯入之後 → 等複掃確認(正常在途)
    cases.set_overlay(session, fid, {"progress": logic.PROGRESS_RESCAN})
    rows = query.find(session, status="未結案")
    assert rows[0]["progress"] == logic.PROGRESS_RESCAN
    assert rows[0]["progress_state"] == logic.RESCAN_WAITING
    # 下鑽：progress 精確篩選只回該進度(供負責人追蹤「等複掃」格下鑽)
    assert len(query.find(session, progress="等複掃", status="未結案")) == 1
    assert query.find(session, progress="要申請展延", status="未結案") == []
    # 壓早標記 + 又匯入一次仍未結 → 跨過新匯入仍未結 = 可疑待查
    session.query(Case).update({Case.status_changed_at: dt.datetime(2020, 1, 1)})
    session.commit()
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", close_status="未結案")])
    rows = query.find(session, status="未結案")
    assert rows[0]["progress_state"] == logic.RESCAN_SUSPECT
    # 來源(Excel)變已結 → 已確認結案(以 Excel 為主)
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", close_status="已結案")])
    rows = query.find(session, status="全部")
    assert rows[0]["progress_state"] == logic.RESCAN_CONFIRMED


def test_apply_intent_reconcile(session):
    f = FindingIn
    # 原始階段(備註空)的弱點，管理人標「要申請展延」
    _imp(session, [f(host="h2", plugin_id="p2", sheet_key="s", remediation_due="2026-04-01", close_status="未結案")])
    fid = session.query(Finding).filter_by(host="h2").one().id
    cases.set_overlay(session, fid, {"progress": logic.PROGRESS_APPLY_EXT})
    rows = query.find(session, status="未結案")
    assert rows[0]["progress_state"] == logic.APPLY_SUBMITTING   # 官方還原始、標在匯入後 → 送審中
    # 壓早 + 重匯仍原始(備註還是空) → 跨過新匯入沒反映 = 待查
    session.query(Case).update({Case.status_changed_at: dt.datetime(2020, 1, 1)}); session.commit()
    _imp(session, [f(host="h2", plugin_id="p2", sheet_key="s", remediation_due="2026-04-01", close_status="未結案")])
    rows = query.find(session, status="未結案")
    assert rows[0]["progress_state"] == logic.APPLY_PENDING
    # 資安 Excel 備註出現展延 → 官方階段變首次展延中 → 已反映
    _imp(session, [f(host="h2", plugin_id="p2", sheet_key="s", remediation_due="2026-04-01",
                     first_extension_due="2026-11-15", remark="首次展延(iForm_1)", close_status="未結案")])
    rows = query.find(session, status="未結案")
    assert rows[0]["stage"] == logic.STAGE_EXTENSION
    assert rows[0]["progress_state"] == logic.APPLY_REFLECTED


def test_purge_orphans(session):
    f = FindingIn
    # 第一批：p1、p2 → 兩案
    _imp(session, [
        f(host="h1", plugin_id="p1", sheet_key="s", close_status="未結案"),
        f(host="h2", plugin_id="p2", sheet_key="s", close_status="未結案"),
    ])
    # 第二批：只剩 p1 → p2 變 orphan(已消失)
    _imp(session, [f(host="h1", plugin_id="p1", sheet_key="s", close_status="未結案")])
    assert session.query(Case).count() == 2
    n = cases.purge_orphans(session)
    assert n == 1                                   # 清掉 p2
    assert session.query(Case).count() == 1
    assert session.query(Case).filter_by(vuln_key="s|p1|h1").one().is_orphan is False  # 仍在的不動


def test_department_override_survives_reimport(session):
    f = FindingIn
    importer.create_batch(session, ImportIn(findings=[
        f(host="h5", plugin_id="p5", sheet_key="s", owner="林楚諺", department="網路組", close_status="未結案"),
    ]))
    fid = session.query(Finding).one().id
    # 改部門(此負責人其實屬資訊架構部)
    r = cases.set_overlay(session, fid, {"department": "資訊架構部"})
    assert r["department"] == "資訊架構部"
    rows = query.find(session, status="未結案")
    assert rows[0]["department"] == "資訊架構部"
    # 重匯(Excel 仍寫網路組) → 覆蓋套回，不被洗掉
    importer.create_batch(session, ImportIn(findings=[
        f(host="h5", plugin_id="p5", sheet_key="s", owner="林楚諺", department="網路組", close_status="未結案"),
    ]))
    rows2 = query.find(session, status="未結案")
    assert rows2[0]["department"] == "資訊架構部"


def test_track_note_overlay(session):
    from webvuln.models import Finding as F
    f = FindingIn
    importer.create_batch(session, ImportIn(findings=[
        f(host="h9", plugin_id="p9", sheet_key="s", owner="網路組", close_status="未結案"),
    ]))
    fid = session.query(F).one().id
    # 改負責人＋追蹤備註
    cases.set_overlay(session, fid, {"owner": "李四", "note": "承辦回報 10/20 前完成修補"})
    rows = query.find(session, status="未結案")
    assert rows[0]["owner"] == "李四"
    assert rows[0]["track_note"] == "承辦回報 10/20 前完成修補"
    # 重匯(Excel 原值)→ 覆蓋與備註都還在
    importer.create_batch(session, ImportIn(findings=[
        f(host="h9", plugin_id="p9", sheet_key="s", owner="網路組", close_status="未結案"),
    ]))
    rows2 = query.find(session, status="未結案")
    assert rows2[0]["owner"] == "李四"
    assert rows2[0]["track_note"] == "承辦回報 10/20 前完成修補"

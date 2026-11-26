"""V2.114 名錄的出路：AD 登入自動加入名錄、最高管理員手動新增、名錄過舊提醒（虛構資料、corp.test）。
含獨立檢核找到的問題（探測測試改成回歸測試）。"""
import datetime as dt

import pytest
from sqlalchemy.orm import Session, sessionmaker

from webvuln import ad, adimport, appsettings, config, directory, importer, mailer, security
from webvuln.models import AuditLog, DirectoryEntry
from webvuln.schemas import FindingIn, ImportIn

MON = dt.datetime(2026, 10, 26, 8, 0)
DEPT = "資訊架構部"


def _r(emp, name, mail, dept="資訊處資訊架構部(1)", enabled="TRUE"):
    return {"employeeID": emp, "SamAccountName": "s" + emp, "DisplayName": name, "mail": mail,
            "department": dept, "manager": "", "Enabled": enabled}


def _rows():
    return [_r("90000001", "陳小美 AMY", "amy@corp.test"), _r("90000005", "趙主管", "boss@corp.test")]


def _batch(session):
    importer.create_batch(session, ImportIn(findings=[FindingIn(sheet_key="s", close_status="未結案", department=d,
                                                                host="h%d" % i, plugin_id="p", owner="陳小美")
                                                      for i, d in enumerate((DEPT, "網路部"))]))
    session.commit()


def _apply(session, rows, now=MON, **kw):
    pv = adimport.run_import(session, rows, "ad.csv", "admin", now=now, **kw)
    return adimport.run_import(session, rows, "ad.csv", "admin", apply=True, token=pv["token"], now=now, **kw)


class _Conn:
    def unbind(self):
        pass


def _ad(monkeypatch, attrs, matches=1):
    monkeypatch.setattr(appsettings, "get_ad_config", lambda s: {"enabled": True, "base_dn": "dc=corp,dc=test"})
    monkeypatch.setattr(ad, "_bind", lambda cfg, login, pw: (True, _Conn(), None))
    a = dict(attrs)
    if matches is not None:
        a["_matches"] = matches
    monkeypatch.setattr(ad, "_lookup_attrs", lambda cfg, conn, login: dict(a))


def _login(client, monkeypatch, username, attrs, matches=1):
    monkeypatch.setattr(config, "NO_AUTH", False)
    _ad(monkeypatch, attrs, matches)
    assert client.post("/api/login", json={"username": username, "password": "pw"}).status_code == 200


def _person(session, emp):
    session.expire_all()
    return session.query(DirectoryEntry).filter_by(employee_id=emp).one_or_none()


@pytest.fixture
def real(client, engine):
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with f() as s:
        security.create_user(s, "sa1", "pw12345", role="super_admin", display_name="管理甲")
    assert client.post("/api/login", json={"username": "sa1", "password": "pw12345"}).status_code == 200
    return client


NEW = {"name": "林新人 NEW", "department": "資訊處資訊架構部(1)", "email": "new@corp.test", "employee_id": "90000099"}


# ── AD 登入自動加入名錄 ──
def test_login_upserts_new_hire_with_audit(client, session, monkeypatch):
    _batch(session)
    _apply(session, _rows())
    _login(client, monkeypatch, "snew", NEW)
    e = _person(session, "90000099")
    assert (e.chinese_name, e.department, e.email, e.enabled, e.source) == ("林新人", DEPT, "new@corp.test", True, "ad_login")
    a = session.query(AuditLog).filter_by(action="directory_login_sync").all()
    assert a and "AD 登入加入名錄" in a[0].detail and a[0].ip


def test_first_login_does_not_activate_directory(client, session, monkeypatch):
    """探測：只有一個人 AD 登入 → 名錄不算啟用（不擋自由輸入、不提醒過舊）。"""
    _batch(session)
    _login(client, monkeypatch, "snew", NEW)
    assert _person(session, "90000099") is not None
    assert not directory.active(session) and directory.status(session)["stale_warning"] == ""
    assert directory.check_owner(session, "王大同", "陳小美", None)["ok"]


def test_login_without_employee_id_attr_not_written(client, session, monkeypatch):
    """探測：AD 沒有員編屬性時，不能用登入帳號當員編造出第二筆同名。"""
    _batch(session)
    _apply(session, _rows())
    _login(client, monkeypatch, "s90000001", {"name": "陳小美 AMY", "department": "資訊處資訊架構部(1)", "email": "amy@corp.test"})
    assert session.query(DirectoryEntry).filter_by(chinese_name="陳小美").count() == 1
    assert mailer.resolve_owner(session, "陳小美")["status"] == "ok"


def test_login_multiple_ad_matches_not_written(client, session, monkeypatch):
    _batch(session)
    _login(client, monkeypatch, "snew", NEW, matches=2)
    assert _person(session, "90000099") is None


def test_ldap_filter_escaped():
    assert ad._esc("a*)(uid=*") == "a\\2a\\29\\28uid=\\2a"


def test_login_never_downgrades_ad_import_row(client, session, monkeypatch):
    _batch(session)
    _apply(session, _rows())
    _login(client, monkeypatch, "s90000001", {"name": "陳小美", "department": "別處別部(9)", "email": "old@corp.test",
                                             "employee_id": "90000001"})
    e = _person(session, "90000001")
    assert (e.email, e.department, e.source) == ("amy@corp.test", DEPT, "ad_import") and e.last_seen is not None


def test_login_write_failure_logged_and_counted(client, session, monkeypatch):
    _batch(session)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(directory, "sync_login", boom)
    _login(client, monkeypatch, "snew", NEW)
    assert (appsettings.get_json(session, directory.FAIL_KEY) or {}).get("count") == 1
    assert directory.status(session, is_super=True)["login_write_failures"] == 1


def test_login_does_not_overwrite_manual_person(real, session, monkeypatch):
    """探測：手動新增的人，AD 登入名字不同 → 不合併；管理者停用的不會被登入恢復。"""
    _batch(session)
    _apply(session, _rows())
    r = real.post("/api/directory/persons", json={"name": "王大同", "employee_id": "90000077", "department": DEPT,
                                                 "email": "wang@corp.test", "enabled": False})
    assert r.status_code == 200
    _login(real, monkeypatch, "s77", {"name": "李四", "department": "資訊處資訊架構部(1)", "email": "li@corp.test",
                                      "employee_id": "90000077"})
    e = _person(session, "90000077")
    assert (e.chinese_name, e.enabled, e.source, e.email) == ("王大同", False, "manual", "wang@corp.test")
    assert e.login_conflict


def test_login_reenable_only_when_ad_disabled(client, session, monkeypatch):
    _batch(session)
    _apply(session, _rows() + [_r("90000007", "李回任", "back@corp.test", enabled="FALSE")])
    assert _person(session, "90000007").disabled_reason == "ad_disabled"
    _login(client, monkeypatch, "s7", {"name": "李回任", "email": "back@corp.test", "employee_id": "90000007"})
    e = _person(session, "90000007")
    assert e.enabled is True and e.source == "ad_import"
    assert any("AD 登入成功，恢復在職" in a.detail for a in session.query(AuditLog).filter_by(action="directory_login_sync"))
    _apply(session, _rows() + [_r("90000007", "李回任", "back@corp.test", enabled="FALSE")], now=dt.datetime.now())
    assert _person(session, "90000007").enabled is False


def test_login_does_not_reenable_absent(client, session, monkeypatch):
    _batch(session)
    _apply(session, _rows() + [_r("90000008", "吳缺席", "absent@corp.test")])
    _apply(session, _rows(), now=MON + dt.timedelta(days=1))
    e = _person(session, "90000008")
    assert (e.enabled, e.disabled_reason) == (False, "absent")
    _login(client, monkeypatch, "s8", {"name": "吳缺席", "email": "absent@corp.test", "employee_id": "90000008"})
    e = _person(session, "90000008")
    assert e.enabled is False and "名單缺席" in (e.login_conflict or "")


def test_login_dept_mapping_uses_collision_rules(client, session, monkeypatch):
    importer.create_batch(session, ImportIn(findings=[FindingIn(sheet_key="s", close_status="未結案", department="營運部",
                                                                host="h", plugin_id="p", owner="陳小美")]))
    session.commit()
    _apply(session, [_r("90000001", "陳小美", "amy@corp.test", dept="甲處營運部(1)")])
    _login(client, monkeypatch, "snew", dict(NEW, department="乙處營運部(2)"))
    assert _person(session, "90000099").department is None    # 兩個不同 AD 部門撞同一短名 → 不對應


# ── 手動新增 ──
OK = {"name": "林新人", "employee_id": "N0001", "department": DEPT, "email": "new@corp.test"}


def test_manual_add_validation_uniqueness_audit(real, session):
    _batch(session)
    _apply(session, _rows())
    url = "/api/directory/persons"
    for bad in (dict(OK, employee_id="N-0001"), dict(OK, employee_id=""), dict(OK, email="a@corp.test; b@corp.test"),
                dict(OK, department="亂打部"), dict(OK, employee_id="90000001"), dict(OK, name="林新人abc"),
                dict(OK, email="x@gmail.com")):
        assert real.post(url, json=bad).status_code == 400, bad
    assert real.post(url, json=dict(OK, phone="1")).status_code == 422
    assert real.post(url, json=dict(OK, name="甲" * 101)).status_code == 422          # 長度＝欄位大小
    r = real.post(url, json=OK)
    assert r.status_code == 200 and r.json()["source"] == "manual"
    pid = r.json()["id"]
    assert real.post(url, json=dict(OK, id=pid, employee_id="N0009")).status_code == 400   # 員編不可改
    assert real.post(url, json=dict(OK, id=pid, enabled=False)).status_code == 200
    assert _person(session, "N0001").disabled_reason == "manual"
    ad_id = _person(session, "90000001").id
    assert real.post(url, json=dict(OK, id=ad_id, employee_id="90000001")).status_code == 400
    a = session.query(AuditLog).filter_by(action="directory_person_save").all()
    assert len(a) == 2 and "before=" in a[1].detail and "after=" in a[1].detail


def test_manual_add_english_name_and_long_name(real, session):
    """探測：中文夾英文要求修正；純英文名照原樣。"""
    _batch(session)
    _apply(session, _rows())
    r = real.post("/api/directory/persons", json=dict(OK, name="陳小美abc", employee_id="x123"))
    assert r.status_code == 400 and "陳小美" in r.json()["detail"]
    r = real.post("/api/directory/persons", json=dict(OK, name="Amy Chen", employee_id="x124"))
    assert r.status_code == 200 and r.json()["name"] == "Amy Chen"


def test_manual_same_name_requires_confirmation(real, session):
    """探測：手動新增跟在職同名 → 要確認，並講清楚影響。"""
    _batch(session)
    _apply(session, _rows())
    body = dict(OK, name="陳小美", employee_id="X1")
    r = real.post("/api/directory/persons", json=body)
    assert r.status_code == 409 and "既有 1 位同名，存檔後催辦會轉部門窗口" in r.json()["detail"]
    assert _person(session, "X1") is None
    assert real.post("/api/directory/persons", json=dict(body, confirm_same_name=True)).status_code == 200
    assert mailer.resolve_owner(session, "陳小美")["status"] == "conflict"


def test_allowed_domains_setting(real, session):
    _batch(session)
    _apply(session, _rows())
    g = real.get("/api/general-settings").json()
    assert g["allowed_email_domains"] is None and g["allowed_email_domains_effective"] == ["corp.test"]
    assert real.post("/api/directory/groups", json={"name": "DBA", "email": "dba@gmail.com"}).status_code == 400
    assert real.post("/api/general-settings", json={"allowed_email_domains": "bad domain!"}).status_code == 400
    assert real.post("/api/general-settings", json={"allowed_email_domains": ""}).json()["allowed_email_domains"] == []
    assert real.post("/api/directory/groups", json={"name": "DBA", "email": "dba@gmail.com"}).status_code == 200


def test_manual_add_super_admin_only(client, session, engine, monkeypatch):
    _batch(session)
    _apply(session, _rows())
    assert client.post("/api/directory/persons", json=OK).status_code == 401
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with f() as s:
        security.create_user(s, "d1", "pw12345", role="dept_admin", display_name="窗口", department=DEPT)
    monkeypatch.setattr(config, "NO_AUTH", False)
    client.post("/api/login", json={"username": "d1", "password": "pw12345"})
    assert client.post("/api/directory/persons", json=OK).status_code == 403


# ── 匯入合併／缺席 ──
def test_import_merges_manual_and_identity_conflict(real, session):
    _batch(session)
    _apply(session, _rows())
    real.post("/api/directory/persons", json=OK)
    real.post("/api/directory/persons", json=dict(OK, name="張臨時", employee_id="N0002", email="tmp@corp.test"))
    rows = _rows() + [_r("N0001", "林新人 NEW", "new.ad@corp.test"), _r("N0002", "張正式", "tmp@corp.test")]
    pv = adimport.run_import(session, rows, "ad.csv", "admin", now=MON)
    d = pv["directory"]
    assert d["identity_conflicts"] == [{"employee_id": "N0002", "old_name": "張臨時", "new_name": "張正式", "source": "manual"}]
    assert d["identity_needs_confirm"]
    rep = adimport.run_import(session, rows, "ad.csv", "admin", apply=True, token=pv["token"], now=MON)
    assert rep["directory"]["manual_confirmed"][0]["employee_id"] == "N0001"
    assert (_person(session, "N0001").source, _person(session, "N0001").email) == ("ad_import", "new.ad@corp.test")
    assert (_person(session, "N0002").source, _person(session, "N0002").chinese_name) == ("manual", "張臨時")   # 沒確認不合併
    _apply(session, rows, confirm_identity=True)
    assert (_person(session, "N0002").source, _person(session, "N0002").chinese_name) == ("ad_import", "張正式")
    assert session.query(AuditLog).filter_by(action="directory_source_convert").count() == 2


def test_manual_absent_exempt_once_then_candidate(real, session):
    _batch(session)
    _apply(session, _rows(), now=MON)
    real.post("/api/directory/persons", json=OK)          # 建立時間＝現在（晚於檔案時間）
    ft = dt.datetime.now() - dt.timedelta(days=1)
    rep = _apply(session, _rows(), now=dt.datetime.now(), file_time=ft)
    assert rep["directory"]["manual_absent"] == [{"name": "林新人", "employee_id": "N0001"}]
    assert _person(session, "N0001").enabled is True
    rep = _apply(session, _rows(), now=dt.datetime.now(), file_time=ft)   # 連續第 2 次缺席 → 一般停用候選
    assert any(x["employee_id"] == "N0001" for x in rep["directory"]["to_disable"])
    assert _person(session, "N0001").enabled is False and _person(session, "N0001").disabled_reason == "absent"


def test_manual_older_than_file_not_exempt(real, session):
    _batch(session)
    _apply(session, _rows(), now=MON)
    real.post("/api/directory/persons", json=OK)
    rep = _apply(session, _rows(), now=dt.datetime.now(), file_time=dt.datetime.now() + dt.timedelta(hours=1))
    assert any(x["employee_id"] == "N0001" for x in rep["directory"]["to_disable"])


# ── 名錄過舊提醒 ──
def test_staleness_message_and_setting(real, session, monkeypatch):
    _batch(session)
    st = real.get("/api/directory/status").json()
    assert st["stale_warning"] == "" and st["last_import_label"] == "尚未匯入"
    _apply(session, _rows(), now=dt.datetime.now() - dt.timedelta(days=40))
    st = real.get("/api/directory/status").json()
    assert st["days_since_import"] == 40 and st["stale_days_limit"] == 35
    assert st["stale_warning"].startswith("名錄已 40 天未更新") and st["stale_warning"].endswith("請重新匯入 AD 名單")
    assert "新進同仁請由管理員手動新增" in st["stale_warning"]                         # AD 登入沒開
    monkeypatch.setattr(directory, "_ad_login_on", lambda db: True)
    assert "AD 登入一次" in directory.status(session)["stale_warning"]
    assert "請重新匯入" not in directory.status(session)["stale_warning"]            # 非管理者不叫他匯入
    assert real.post("/api/general-settings", json={"directory_stale_days": 60}).json()["directory_stale_days"] == 60
    assert real.get("/api/directory/status").json()["stale_warning"] == ""
    assert real.post("/api/general-settings", json={"directory_stale_days": 9999}).json()["directory_stale_days"] == 365


def test_general_settings_need_real_super(client, session, monkeypatch):
    assert client.get("/api/general-settings").status_code == 401
    assert client.post("/api/general-settings", json={"directory_stale_days": 10}).status_code == 401
    monkeypatch.setattr(config, "FORCE_OPEN", True)
    assert client.post("/api/general-settings", json={"directory_stale_days": 10}).status_code == 401

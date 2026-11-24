"""V2.113 員工名錄：負責人只能從 AD 名錄挑；群組信箱；部門窗口（收件人）從名錄挑、跟角色脫鉤。
全部虛構姓名、corp.test、假部門代碼。"""
import datetime as dt
import email
import os

import pytest
from sqlalchemy.orm import Session, sessionmaker

from webvuln import adimport, appsettings, automail, cases, config, directory, importer, mailer, security
from webvuln.models import AuditLog, Case, DirectoryEntry, Finding, User
from webvuln.schemas import FindingIn, ImportIn

MON = dt.datetime(2026, 10, 26, 8, 0)
TODAY = MON.date()
DEPT = "資訊架構部"
COLS = list(adimport.FIELDS)


def _r(emp, name, mail, dept="資訊處資訊架構部(1)", enabled="TRUE", mgr=""):
    return {"employeeID": emp, "SamAccountName": "s" + emp, "DisplayName": name, "mail": mail,
            "department": dept, "manager": mgr, "Enabled": enabled}


def _rows():
    return [_r("90000001", "陳小美 AMY", "amy@corp.test", mgr="CN=趙主管,OU=x"),
            _r("90000002", "王小明", "ming1@corp.test", dept="資訊處網路部(2)"),
            _r("90000003", "王小明 MING", "ming2@corp.test", dept="資訊處系統部(3)"),
            _r("90000004", "李停用", "gone@corp.test", enabled="FALSE"),
            _r("90000005", "趙主管", "boss@corp.test"),
            _r("90000006", "林窗口", "win@corp.test")]


def _batch(session, owners=("陳小美",), dept=DEPT, extra=("網路部", "系統部")):
    fs = [FindingIn(sheet_key="s", close_status="未結案", department=dept, host="h%d" % i, plugin_id="p%d" % i,
                    severity="High", owner=o, remediation_due=(TODAY - dt.timedelta(days=5)).isoformat())
          for i, o in enumerate(owners)]
    fs += [FindingIn(sheet_key="s", close_status="未結案", department=d, host="x%d" % i, plugin_id="q", severity="Low",
                     owner="某人", remediation_due="2026-12-01") for i, d in enumerate(extra)]
    importer.create_batch(session, ImportIn(findings=fs))
    session.commit()


def _apply(session, rows, confirm=False):
    pv = adimport.run_import(session, rows, "ad.csv", "admin", now=MON, confirm_disable=confirm)
    return adimport.run_import(session, rows, "ad.csv", "admin", apply=True, token=pv["token"], now=MON,
                               confirm_disable=confirm)


def _ids(session):
    return {f.host: f.id for f in session.query(Finding).all()}


def _pick(session, emp):
    return "d:%d" % session.query(DirectoryEntry).filter_by(employee_id=emp).one().id


@pytest.fixture
def real(client, engine):
    """真的登入的最高管理員（名錄搜尋／群組／窗口設定不接受免登入匿名）。"""
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with f() as s:
        security.create_user(s, "sa1", "pw12345", role="super_admin", display_name="管理甲")
    assert client.post("/api/login", json={"username": "sa1", "password": "pw12345"}).status_code == 200
    return client


# ── 名錄刷新 ──
def test_directory_refresh_from_import_incl_disabled(session):
    _batch(session)
    adimport.run_import(session, _rows(), "ad.csv", "admin", now=MON)
    assert session.query(DirectoryEntry).count() == 0          # 預覽不寫
    rep = _apply(session, _rows())
    assert rep["directory"]["added"] == 6
    d = {e.employee_id: e for e in session.query(DirectoryEntry).all()}
    assert d["90000004"].enabled is False and d["90000001"].enabled is True
    assert d["90000001"].chinese_name == "陳小美" and d["90000001"].department == "資訊架構部"
    assert d["90000001"].manager_email == "boss@corp.test"
    rep = _apply(session, [x for x in _rows() if x["employeeID"] != "90000006"])   # 少一人（≤5）→ 標停用，不刪
    session.expire_all()
    assert session.query(DirectoryEntry).filter_by(employee_id="90000006").one().enabled is False
    assert rep["directory"]["disabled"] == 1 and rep["directory"]["to_disable"][0]["name"] == "林窗口"


def test_disable_guard_needs_confirmation_bound_into_token(session):
    _batch(session)
    rows = _rows() + [_r("800000%02d" % i, "測員" + chr(0x4E00 + i), "t%d@corp.test" % i) for i in range(10)]
    _apply(session, rows)
    part = rows[:2]
    pv = adimport.run_import(session, part, "ad.csv", "admin", now=MON)
    assert pv["directory"]["needs_confirm"] and len(pv["directory"]["to_disable"]) > 5
    rep = adimport.run_import(session, part, "ad.csv", "admin", apply=True, token=pv["token"], now=MON)
    session.expire_all()
    assert session.query(DirectoryEntry).filter_by(employee_id="80000005").one().enabled is True and rep["warnings"]
    pv = adimport.run_import(session, part, "ad.csv", "admin", now=MON)          # 沒勾的 token 不能拿來「勾了」套用
    with pytest.raises(adimport.ImportError_, match="STALE"):
        adimport.run_import(session, part, "ad.csv", "admin", apply=True, token=pv["token"], confirm_disable=True)
    _apply(session, part, confirm=True)
    session.expire_all()
    assert session.query(DirectoryEntry).filter_by(employee_id="80000005").one().enabled is False


def test_duplicate_conflicting_ids_not_refreshed(session):
    _batch(session)
    _apply(session, _rows())
    rows = _rows() + [_r("90000001", "陳小美", "other@corp.test")]           # 同員編、內容不同
    rep = _apply(session, rows)
    session.expire_all()
    e = session.query(DirectoryEntry).filter_by(employee_id="90000001").one()
    assert e.email == "amy@corp.test" and e.enabled is True and "90000001" in rep["directory"]["conflicts"]


def test_directory_never_stores_phone(client, session, engine):
    _batch(session)
    pv = client.post("/api/users/ad-import", json={"columns": COLS, "rows": _rows()}).json()
    assert client.post("/api/users/ad-import", json={"columns": COLS, "rows": _rows(), "apply": True,
                                                     "token": pv["token"]}).status_code == 200
    assert client.post("/api/users/ad-import", json={"columns": COLS + ["Phone"],
                                                     "rows": [dict(x, Phone="0912-000-111") for x in _rows()]}).status_code == 422
    engine.dispose()
    for f in (engine.url.database, engine.url.database + "-wal"):
        if os.path.exists(f):
            assert b"0912-000-111" not in open(f, "rb").read()


# ── 搜尋 ──
def test_search_ranking_limit_and_fields(real, session):
    _batch(session)
    _apply(session, _rows() + [_r("700000%02d" % i, "王小" + chr(0x4E00 + i), "w%d@corp.test" % i) for i in range(22)],
           confirm=True)
    items = real.get("/api/directory/search", params={"q": "王小明"}).json()["items"]
    assert [i["name"] for i in items[:2]] == ["王小明", "王小明"]          # 同名兩位都列、附部門
    assert {i["department"] for i in items[:2]} == {"網路部", "系統部"}
    assert all(set(i) == {"kind", "name", "department", "employee_id", "pick_id"} for i in items)   # 不給信箱／主管
    assert all(i["pick_id"].startswith("d:") for i in items)
    assert len(real.get("/api/directory/search", params={"q": "王小"}).json()["items"]) == 20
    assert real.get("/api/directory/search", params={"q": "王"}).json()["items"] == []            # 至少 2 字
    assert all(i["name"] != "李停用" for i in real.get("/api/directory/search", params={"q": "李停用"}).json()["items"])


def test_search_masking_permissions_and_open_mode(client, session, engine, monkeypatch):
    _batch(session)
    _apply(session, _rows())
    assert client.get("/api/directory/search", params={"q": "陳小美"}).status_code == 401   # 免登入匿名
    assert client.get("/api/directory").status_code == 401
    assert client.post("/api/directory/groups", json={"name": "DBA", "email": "dba@corp.test"}).status_code == 401
    monkeypatch.setattr(config, "FORCE_OPEN", True)                                         # 緊急強制免登入也不行
    assert client.get("/api/directory/search", params={"q": "陳小美"}).status_code == 401
    monkeypatch.setattr(config, "FORCE_OPEN", False)
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with f() as s:
        security.create_user(s, "u1", "pw12345", role="user", display_name="甲")
        security.create_user(s, "s1", "pw12345", role="super_admin", display_name="乙")
    monkeypatch.setattr(config, "NO_AUTH", False)
    client.post("/api/login", json={"username": "u1", "password": "pw12345"})
    it = client.get("/api/directory/search", params={"q": "陳小美"}).json()["items"][0]
    assert it["employee_id"] == "****0001" and "90000001" not in str(it)
    assert client.get("/api/directory").status_code == 403
    client.post("/api/logout")
    client.post("/api/login", json={"username": "s1", "password": "pw12345"})
    assert client.get("/api/directory/search", params={"q": "陳小美"}).json()["items"][0]["employee_id"] == "90000001"
    assert client.get("/api/directory").status_code == 200


def test_search_rate_limit(real, session, monkeypatch):
    from webvuln import main as m
    _batch(session)
    _apply(session, _rows())
    monkeypatch.setattr(m, "SEARCH_RATE", 3)
    m._SEARCH_HITS.clear()
    codes = [real.get("/api/directory/search", params={"q": "陳小美"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    m._SEARCH_HITS.clear()


def test_masking_rules():
    assert directory._mask("12345678", False) == "****5678" and directory._mask("12345678", True) == "12345678"
    assert directory._mask("1234", False) == "" and directory._mask("12", False) == ""


# ── 群組 ──
def test_group_mail_resolution_and_validation(real, session):
    _batch(session, owners=("DBA",))
    _apply(session, _rows())
    assert real.post("/api/directory/groups", json={"name": "DBA", "email": "bad"}).status_code == 400
    assert real.post("/api/directory/groups", json={"name": "陳小美", "email": "g@corp.test"}).status_code == 400   # 跟人同名
    r = real.post("/api/directory/groups", json={"name": "DBA", "email": "dba@corp.test", "department": DEPT})
    assert r.status_code == 200
    assert real.post("/api/directory/groups", json={"name": " DBA ", "email": "x@corp.test"}).status_code == 400    # 重複
    assert session.query(AuditLog).filter_by(action="directory_group_save").count() == 1
    rr = mailer.resolve_recipients(session, "DBA", DEPT, {})
    assert rr["to"] == ["dba@corp.test"] and rr["mode"] == "send" and rr["rule"] == "群組"
    assert [i["kind"] for i in real.get("/api/directory/search", params={"q": "DBA"}).json()["items"]] == ["group"]


# ── 收件人：名錄停用一律排除、名錄同名 ──
def test_disabled_directory_person_excluded_everywhere(session):
    _batch(session, owners=("陳小美",))
    _apply(session, _rows())
    session.add_all([User(username="win", display_name="舊窗口", email="win.old@corp.test", department=DEPT,
                          role=config.ROLE_DEPT_ADMIN, employee_id="90000004", is_active=True),
                     User(username="wk", display_name="週報者", email="wk@corp.test", department=DEPT, weekly_report=True,
                          role=config.ROLE_USER, employee_id="90000004", is_active=True),
                     User(username="win2", display_name="窗口乙", email="win2@corp.test", department=DEPT,
                          role=config.ROLE_DEPT_ADMIN, is_active=True)])          # 沒綁員編 → 不受影響
    session.commit()
    wins, rule, notes = directory.dept_windows(session, DEPT, [])
    assert wins == ["win2@corp.test"] and rule == "角色窗口" and any("名錄顯示已停用" in n for n in notes)
    to, rule, notes = automail._inventory_recipients_ex(session, DEPT, [])
    assert "wk@corp.test" not in to
    # 負責人本人綁的員編停用 → 不寄本人
    u = session.query(User).filter_by(username="mail:陳小美").one()
    u.employee_id = "90000004"
    session.commit()
    rr = mailer.resolve_recipients(session, "陳小美", DEPT, {})
    assert rr["mode"] == "fallback" and "名錄顯示已停用" in rr["reason"]


def test_ad_manager_disabled_not_cc(session):
    _batch(session)
    _apply(session, _rows())
    rows = _rows()
    rows[4]["Enabled"] = "FALSE"      # 主管停用
    _apply(session, rows)
    u = session.query(User).filter_by(username="mail:陳小美").one()
    u.manager_email = "boss@corp.test"
    session.commit()
    ident = mailer.resolve_owner(session, "陳小美")
    assert ident["status"] == "ok" and ident["manager_email"] is None


def test_directory_same_name_conflict(session):
    _batch(session, owners=("王小明",))
    _apply(session, _rows())
    session.add(User(username="m1", display_name="王小明", email="ming1@corp.test", employee_id="90000002",
                     role=config.ROLE_USER, is_active=True))
    session.commit()
    rr = mailer.resolve_recipients(session, "王小明", DEPT, {"global_fallback": "gf@corp.test"})
    assert rr["mode"] == "fallback" and "名錄同名 2 人" in rr["reason"]


# ── 後端驗證 ──
def test_overlay_rejects_unknown_owner_and_department(client, session):
    _batch(session)
    _apply(session, _rows())
    fid = _ids(session)["h0"]
    url = "/api/findings/%d/overlay" % fid
    r = client.post(url, json={"set_owner": True, "owner": "陳小美X"})
    assert r.status_code == 400 and r.json()["detail"] == directory.OWNER_REQUIRED
    assert client.post(url, json={"set_owner": True, "owner": "李停用"}).status_code == 400        # 停用不行
    assert client.post(url, json={"set_owner": True, "owner": "陳小美"}).status_code == 200        # 不變
    assert client.post(url, json={"set_owner": True, "owner": "王小明"}).status_code == 400        # 同名多人要挑
    assert client.post(url, json={"set_owner": True, "owner": "王小明", "owner_pick": "90000003"}).status_code == 400  # 員編不能當挑選 id
    r = client.post(url, json={"set_owner": True, "owner": "王小明", "owner_pick": _pick(session, "90000003")})
    assert r.status_code == 200 and r.json()["owner_link"] == "已建只收信帳號"
    u = session.query(User).filter_by(username="mail:王小明").one()
    assert u.employee_id == "90000003" and u.email == "ming2@corp.test"
    assert client.post(url, json={"set_department": True, "department": "亂打部"}).status_code == 400
    assert client.post(url, json={"set_department": True, "department": "網路部"}).status_code == 200


def test_clearing_overlay_restores_excel_values(client, session):
    _batch(session)
    _apply(session, _rows())
    fid = _ids(session)["h0"]
    url = "/api/findings/%d/overlay" % fid
    assert client.post(url, json={"set_owner": True, "owner": "趙主管", "set_department": True, "department": "網路部"}).status_code == 200
    session.expire_all()
    f = session.get(Finding, fid)
    assert (f.owner, f.department) == ("趙主管", "網路部")
    assert client.post(url, json={"set_owner": True, "owner": "", "set_department": True, "department": ""}).status_code == 200
    session.expire_all()
    f = session.get(Finding, fid)
    assert (f.owner, f.department) == ("陳小美", DEPT)


def test_overlay_uses_latest_batch_row_for_scope(client, session):
    _batch(session)
    old_id = _ids(session)["h0"]
    _batch(session)                                   # 新一批：同身分的列換了 id
    r = client.post("/api/findings/%d/overlay" % old_id, json={"set_note": True, "note": "x"})
    assert r.status_code == 200
    latest = max(f.id for f in session.query(Finding).filter_by(host="h0").all())
    session.expire_all()
    c = session.query(Case).filter_by(vuln_key=cases.key_str(session.get(Finding, latest))).one()
    assert c.track_note == "x"
    # 最新一批沒有這個身分 → 拒絕
    importer.create_batch(session, ImportIn(findings=[FindingIn(sheet_key="s", close_status="未結案", department=DEPT,
                                                                host="zz", plugin_id="z", owner="某人")]))
    session.commit()
    assert client.post("/api/findings/%d/overlay" % old_id, json={"set_note": True, "note": "y"}).status_code == 400


def test_bulk_overlay_counts_and_rejects(client, session, engine, monkeypatch):
    _batch(session, owners=("陳小美", "陳小美"))
    _apply(session, _rows())
    ids = [_ids(session)["h0"], _ids(session)["h1"]]
    r = client.post("/api/findings/bulk-overlay", json={"ids": ids, "set_owner": True, "owner": "陳小美X"}).json()
    assert r["applied"] == 0 and all(f["error"] == directory.OWNER_REQUIRED for f in r["failed"])
    r = client.post("/api/findings/bulk-overlay", json={"ids": ids + ids[:1], "set_owner": True, "owner": "趙主管"}).json()
    assert (r["requested"], r["unique"], r["changed"], r["unchanged"]) == (3, 2, 2, 0) and r["owner_link"] == "已建只收信帳號"
    r = client.post("/api/findings/bulk-overlay", json={"ids": ids, "set_owner": True, "owner": "趙主管"}).json()
    assert (r["changed"], r["unchanged"]) == (0, 2)
    # 無權限：只回 id、不回主機
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with f() as s:
        security.create_user(s, "u9", "pw12345", role="user", display_name="別人")
    monkeypatch.setattr(config, "NO_AUTH", False)
    client.post("/api/login", json={"username": "u9", "password": "pw12345"})
    r = client.post("/api/findings/bulk-overlay", json={"ids": ids, "set_note": True, "note": "x"}).json()
    assert len(r["skipped"]) == 2 and all(set(x) == {"id", "error"} for x in r["skipped"])


def test_free_text_when_directory_empty(client, session):
    _batch(session)
    assert client.get("/api/directory/status").json() == {"persons": 0, "groups": 0, "active": False,
                                                          "warning": "尚未匯入 AD 名單，負責人無法驗證"}
    fid = _ids(session)["h0"]
    assert client.post("/api/findings/%d/overlay" % fid, json={"set_owner": True, "owner": "隨便打"}).status_code == 200
    assert client.post("/api/findings/%d/overlay" % fid, json={"set_department": True, "department": "新部門"}).status_code == 200


# ── 部門窗口（收件人）──
@pytest.fixture
def smtp(monkeypatch):
    sent = []

    class _Fake:
        def __init__(self, *a, **k):
            pass

        def starttls(self):
            pass

        def sendmail(self, frm, to, msg):
            sent.append({"env": list(to), "msg": email.message_from_string(msg)})
            return {}

        def quit(self):
            pass

    monkeypatch.setattr(automail.smtplib, "SMTP", _Fake)
    return sent


def _mail(session, windows=None, test_mode=False, keep=False):
    appsettings.set_email_config(session, {"enabled": True, "smtp_host": "relay.test", "from_default": "sys@corp.test"})
    dep = {"department": DEPT, "enabled": True, "cc": "", "escalate_cc": "", "send_inventory": True, "send_dunning": True}
    if not keep:
        dep["windows"] = windows or []
    kw = {"enabled": True, "departments": [dep]}
    if test_mode:
        kw.update(test_mode=True, test_addresses="t1@corp.test")
    return automail.save_settings(session, kw, now=MON - dt.timedelta(days=7))


def _jobs(session):
    return automail.preview(session, MON)["jobs"]


def _win(session, emp):
    return [{"dir_id": int(_pick(session, emp)[2:])}]


def test_configured_window_used_for_inventory_and_fallback(session, smtp):
    _batch(session, owners=("無信箱者",))
    _apply(session, _rows())
    session.add(User(username="legacy", display_name="舊窗口", email="legacy@corp.test", department=DEPT,
                     role=config.ROLE_DEPT_ADMIN, is_active=True))
    session.add(User(username="admin", display_name="系統管理", email="admin@corp.test", role=config.ROLE_SUPER, is_active=True))
    session.commit()
    cfg = _mail(session, windows=_win(session, "90000006"))
    assert cfg["departments"][0]["windows"] == [{"employee_id": "90000006", "name": "林窗口"}]   # 只存員編＋姓名
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    dun = [j for j in _jobs(session) if j["kind"] == "dunning"][0]
    assert inv["to"] == ["win@corp.test"] and inv["to_rule"] == "設定窗口"
    assert dun["to"] == ["win@corp.test"] and dun["mode"] == "fallback" and dun["to_rule"] == "設定窗口"
    assert session.query(User).filter_by(username="admin").one().role == config.ROLE_SUPER    # 不改角色
    automail.tick(session, MON)
    assert any("win@corp.test" in m["env"] for m in smtp) and not any("legacy@corp.test" in m["env"] for m in smtp)


def test_legacy_role_window_when_none_configured(session):
    _batch(session, owners=("無信箱者",))
    _apply(session, _rows())
    session.add(User(username="legacy", display_name="舊窗口", email="legacy@corp.test", department=DEPT,
                     role=config.ROLE_DEPT_ADMIN, is_active=True))
    session.commit()
    _mail(session)
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    dun = [j for j in _jobs(session) if j["kind"] == "dunning"][0]
    assert inv["to"] == ["legacy@corp.test"] and inv["to_rule"] == "角色窗口"
    assert dun["to"] == ["legacy@corp.test"] and dun["to_rule"] == "角色窗口"


def test_disabled_window_kept_skipped_and_new_validated(session):
    _batch(session, owners=("無信箱者",))
    _apply(session, _rows())
    _mail(session, windows=_win(session, "90000006"))
    _apply(session, [x for x in _rows() if x["employeeID"] != "90000006"] + [_r("90000006", "林窗口", "win@corp.test", enabled="FALSE")])
    session.add(User(username="legacy", display_name="舊窗口", email="legacy@corp.test", department=DEPT,
                     role=config.ROLE_DEPT_ADMIN, is_active=True))
    session.commit()
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    assert inv["to"] == ["legacy@corp.test"] and "AD 已停用，略過" in inv["final_to_reason"]
    # 既有窗口停用了也能照常存檔（保留、標示）；windows 沒帶＝沿用
    cfg = _mail(session, windows=[{"employee_id": "90000006"}])
    assert cfg["departments"][0]["windows"][0]["employee_id"] == "90000006"
    cfg = _mail(session, keep=True)
    assert cfg["departments"][0]["windows"][0]["employee_id"] == "90000006"
    cfg = _mail(session, windows=[])                     # 明確空清單＝清掉
    assert cfg["departments"][0]["windows"] == []
    with pytest.raises(ValueError):                      # 新加停用者 → 擋
        _mail(session, windows=_win(session, "90000006"))


def test_window_email_resolved_at_send_time(session):
    _batch(session, owners=("無信箱者",))
    _apply(session, _rows())
    _mail(session, windows=_win(session, "90000006"))
    before = appsettings.get_json(session, automail.KEY)
    rows = _rows()
    rows[-1]["mail"] = "win.new@corp.test"
    _apply(session, rows)
    assert appsettings.get_json(session, automail.KEY) == before       # 匯入不改自動寄信設定
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    assert inv["to"] == ["win.new@corp.test"]


def test_window_settings_need_real_login(client, session):
    _batch(session)
    _apply(session, _rows())
    body = {"departments": [{"department": DEPT, "enabled": True, "windows": [{"employee_id": "90000006"}]}]}
    assert client.put("/api/auto-mail", json=body).status_code == 401          # 免登入匿名不行
    bad = {"departments": [{"department": DEPT, "windows": [{"employee_id": "1", "email": "x@corp.test"}]}]}
    assert client.put("/api/auto-mail", json=bad).status_code == 401


def test_window_test_mode_redirects(session, smtp):
    _batch(session, owners=("無信箱者",))
    _apply(session, _rows())
    _mail(session, windows=_win(session, "90000006"), test_mode=True)
    automail.tick(session, MON)
    assert smtp and all(set(m["env"]) == {"t1@corp.test"} and not m["msg"].get("Cc") for m in smtp)
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    assert inv["final_to"] == ["t1@corp.test"] and "設定窗口" in inv["final_to_reason"]

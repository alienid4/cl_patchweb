"""V2.112 AD 部門 → Excel 部門短名自動對應（虛構部門／人名，網域 corp.test）。"""
import datetime as dt

import pytest

from webvuln import ad, adimport, appsettings, config, deptmap, importer, mailer
from webvuln.models import User
from webvuln.schemas import FindingIn, ImportIn

KNOWN = ["資訊架構部", "架構部", "網路部", "系統部"]


@pytest.mark.parametrize("raw,want", [
    ("甲處資訊架構部(1234567)", "甲處資訊架構部"),
    ("甲處資訊架構部（1234567）", "甲處資訊架構部"),
    ("甲處資訊架構部（６１１）", "甲處資訊架構部"),          # 全形數字
    ("  甲處 資訊架構部 （611）  ", "甲處資訊架構部"),
    ("海外處營運部（海外）", "海外處營運部（海外）"),                  # 非數字說明保留
    ("營運部(A12)", "營運部(A12)"),                                  # 非純數字保留
    ("營運部(1)(2)", "營運部(1)"),                                   # 只去結尾一組
    ("營運部(123）", "營運部(123）"),                                # 括號不成對不去
    ("網路部", "網路部"), ("", ""), (None, ""),
])
def test_norm_ad_dept(raw, want):
    assert deptmap.norm_ad_dept(raw) == want


def test_excel_side_keeps_parentheses():
    assert deptmap.norm_excel_dept("營運部（海外） ") == "營運部（海外）"
    assert deptmap.map_ad_department("總處營運部（海外）(123)", ["營運部", "營運部（海外）"]) == ("營運部（海外）", "結尾相符")
    assert deptmap.map_ad_department("總處營運部(123)", ["營運部", "營運部（海外）"]) == ("營運部", "結尾相符")


def test_longest_match_and_exact():
    assert deptmap.map_ad_department("甲處資訊架構部(1234567)", KNOWN) == ("資訊架構部", "結尾相符")
    assert deptmap.map_ad_department("網路部（123）", KNOWN) == ("網路部", "相同")


def test_exact_finding_string_returned_and_space_variants_ambiguous():
    # 回傳 Finding.department 原字串（含它自己的空白），不是正規化後的
    assert deptmap.map_ad_department("資訊處甲乙部(1)", ["甲乙部 "]) == ("甲乙部 ", "結尾相符")
    # 同一個部門在弱點裡有兩種寫法 → 不知道該寫哪個 → 多於一個
    assert deptmap.map_ad_department("資訊處甲乙部(1)", ["甲乙部", " 甲乙部 "]) == (None, "多於一個")


def test_suffix_requires_org_unit_boundary():
    assert deptmap.map_ad_department("資安營運部", ["營運部"]) == (None, "對不到")          # 前綴「資安」不是單位字
    assert deptmap.map_ad_department("資訊處營運部(1)", ["營運部"])[0] == "營運部"
    assert deptmap.map_ad_department("數位中心營運部", ["營運部"])[0] == "營運部"
    assert deptmap.map_ad_department("台北分公司營運組", ["營運組"])[0] == "營運組"


def test_clean_strips_bom_zero_width():
    assert deptmap.clean("\ufeff 資訊處\u200b資訊架構部(1) \u3000") == "資訊處 資訊架構部(1)"
    assert deptmap.map_ad_department("\ufeff資訊處\u200b資訊架構部(1)", ["資訊架構部"])[0] == "資訊架構部"


def test_collisions_helper():
    c = deptmap.collisions(["甲處營運部(1)", "乙處營運部(2)", "甲處營運部(1)", "資訊處網路部(3)"], ["營運部", "網路部"])
    assert c == {"營運部": ["乙處營運部(2)", "甲處營運部(1)"]}


def test_raw_change_unmappable_clears_auto_dept():
    from webvuln.models import AuditLog  # noqa: F401
    u = User(username="x", display_name="陳小美", role="user")
    deptmap.apply_department(u, "資訊處資訊架構部(1)", KNOWN)
    assert u.department == "資訊架構部"
    r = deptmap.apply_department(u, "資訊處資訊架構部(1)", ["別的部"])     # 原始值沒變、對不到 → 不動
    assert u.department == "資訊架構部" and not r["cleared"]
    r = deptmap.apply_department(u, "人資處招募部(9)", KNOWN)              # 原始值變了、對不到 → 清空、留原始值
    assert r["cleared"] and u.department is None and u.department_source is None and u.ad_department == "人資處招募部(9)"
    deptmap.apply_department(u, "資訊處資訊架構部(1)", KNOWN)
    r = deptmap.apply_department(u, "資訊處網路部(2)", None)               # 候選讀不到 → 不清
    assert u.department == "資訊架構部" and not r["cleared"]


def test_ambiguous_two_candidates():
    # 兩個不同的部門名，去空白後一樣、都符合 → 不猜
    d = deptmap.map_detail("總處營運部", ["營運部", "營運 部"])
    assert d["mapped"] is None and d["reason"] == "多於一個" and len(d["candidates"]) == 2


def test_no_match_and_read_failure():
    assert deptmap.map_ad_department("人資處招募部(9)", KNOWN) == (None, "對不到")
    assert deptmap.map_ad_department("", KNOWN)[0] is None
    assert deptmap.map_ad_department("資訊處網路部", None) == (None, deptmap.READ_FAIL)


def test_apply_department_rules():
    u = User(username="x", display_name="陳小美", role="user")
    assert deptmap.apply_department(u, "甲處資訊架構部(1234567)", KNOWN)["changed"]
    assert u.department == "資訊架構部" and u.department_source == "ad_auto" and u.ad_department.endswith("(1234567)")
    deptmap.apply_department(u, "甲處網路部(1)", KNOWN)      # 上次是自動 → 跟著改
    assert u.department == "網路部"
    m = User(username="m", display_name="王大同", role="user", department="手填部", department_source="manual")
    deptmap.apply_department(m, "甲處網路部(1)", KNOWN)
    assert m.department == "手填部" and m.ad_department == "甲處網路部(1)"
    legacy = User(username="l", display_name="李小華", role="user", department="舊部門")   # 來源不明 → 視同手填
    deptmap.apply_department(legacy, "甲處網路部(1)", KNOWN)
    assert legacy.department == "舊部門"
    empty = User(username="e", display_name="趙主管", role="user")
    deptmap.apply_department(empty, "人資處招募部(9)", KNOWN)           # 對不到 → 留空
    assert empty.department is None and empty.ad_department == "人資處招募部(9)"


@pytest.mark.parametrize("role", ["dept_admin", "super_admin"])
def test_privileged_never_changed_only_suggested(role):
    for dep, src in ((None, None), ("網路部", "ad_auto")):
        u = User(username="w", display_name="窗口甲", role=role, department=dep, department_source=src)
        r = deptmap.apply_department(u, "甲處資訊架構部(1234567)", KNOWN)
        assert u.department == dep and u.department_source == src and not r["changed"]
        assert r["suggestion"] == "資訊架構部" and u.ad_department == "甲處資訊架構部(1234567)"


def _batch(session, dept="資訊架構部", owner="陳小美", extra_depts=()):
    fs = [FindingIn(sheet_key="s", close_status="未結案", department=dept, host="h0", plugin_id="p0",
                    severity="High", owner=owner, remediation_due="2026-10-01")]
    fs += [FindingIn(sheet_key="s", close_status="未結案", department=d, host="h%d" % (i + 1), plugin_id="p",
                     severity="Low", owner="某人", remediation_due="2026-10-01") for i, d in enumerate(extra_depts)]
    importer.create_batch(session, ImportIn(findings=fs))
    session.commit()


def test_known_departments_include_auto_mail_settings(session, monkeypatch):
    from webvuln import automail
    _batch(session)
    monkeypatch.setattr(automail, "get_settings", lambda db: {"departments": [{"department": "設定部"}, {"department": ""}]})
    assert set(deptmap.known_departments(session)) == {"資訊架構部", "設定部"}


class _FakeConn:
    def unbind(self):
        pass


def _ad(monkeypatch, dept):
    monkeypatch.setattr(appsettings, "get_ad_config", lambda s: {"enabled": True, "base_dn": "dc=corp,dc=test"})
    monkeypatch.setattr(ad, "_bind", lambda cfg, login, pw: (True, _FakeConn(), None))
    monkeypatch.setattr(ad, "_lookup_attrs", lambda cfg, conn, login: {"name": "陳小美", "department": dept,
                                                                       "email": "amy@corp.test"})


def test_login_sets_dept_when_empty_and_keeps_manual(client, session, monkeypatch):
    _batch(session)
    monkeypatch.setattr(config, "NO_AUTH", False)
    _ad(monkeypatch, "甲處資訊架構部(1234567)")
    assert client.post("/api/login", json={"username": "A001", "password": "pw"}).status_code == 200
    session.expire_all()
    u = session.query(User).filter_by(username="A001").one()
    assert u.department == "資訊架構部" and u.department_source == "ad_auto"
    assert u.ad_department == "甲處資訊架構部(1234567)"
    # 帳號與權限手填 → 之後登入不覆蓋
    from webvuln import security
    security.create_user(session, "s1", "pw12345", role="super_admin", display_name="管理員")
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": "s1", "password": "pw12345"}).status_code == 200
    assert client.post("/api/users/%d/role" % u.id, json={"department": "手填部"}).status_code == 200
    client.post("/api/logout")
    client.post("/api/login", json={"username": "A001", "password": "pw"})
    session.expire_all()
    u = session.query(User).filter_by(username="A001").one()
    assert u.department == "手填部" and u.department_source == "manual"
    assert config.canon_role(u.role) == config.ROLE_USER   # 不會自動給窗口角色


def test_import_mismatch_uses_mapped_dept(session):
    # 弱點部門「架構部」：舊的互相包含會誤判符合；對應結果是更長的「資訊架構部」→ 部門不符
    _batch(session, dept="架構部", extra_depts=("資訊架構部",))
    rows = [{"employeeID": "1001", "SamAccountName": "s1", "DisplayName": "陳小美 AMY", "mail": "amy@corp.test",
             "department": "甲處資訊架構部(1234567)", "manager": "", "Enabled": "TRUE"}]
    pv = adimport.run_import(session, rows, "ad.csv", "admin")
    assert [a["name"] for a in pv["ambiguous"]] == ["陳小美"] and "部門不符" in pv["ambiguous"][0]["reason"]
    assert pv["dept_map"][0]["mapped"] == "資訊架構部" and pv["dept_map"][0]["count"] == 1
    # 部門對得上 → 自動對上，只收信帳號的部門也寫成短名
    session.query(User).delete()
    session.commit()
    _batch(session, dept="資訊架構部")
    rep = adimport.run_import(session, rows, "ad.csv", "admin", apply=True,
                              token=adimport.run_import(session, rows, "ad.csv", "admin")["token"])
    assert [m["name"] for m in rep["matched"]] == ["陳小美"]
    u = session.query(User).filter_by(username="mail:陳小美").one()
    assert u.department == "資訊架構部" and u.department_source == "ad_auto"


def test_import_unmappable_falls_back_to_containment(session):
    _batch(session, dept="財務部")
    rows = [{"employeeID": "1001", "SamAccountName": "s1", "DisplayName": "陳小美", "mail": "amy@corp.test",
             "department": "總處財務部第一科", "manager": "", "Enabled": "TRUE"}]
    pv = adimport.run_import(session, rows, "ad.csv", "admin")
    assert [m["name"] for m in pv["matched"]] == ["陳小美"]       # 對不到短名 → 互相包含仍算符合
    assert pv["dept_map"][0]["mapped"] == "" and pv["dept_map"][0]["reason"] == "對不到"


def test_window_resolution_after_mapping(session):
    _batch(session, owner="無信箱者")
    # 先以一般帳號自動對應部門，管理者之後再指派窗口角色（自動流程不給角色、也不改窗口的部門）
    w = User(username="win", display_name="王大同", email="win@corp.test", role=config.ROLE_USER, is_active=True)
    deptmap.apply_department(w, "甲處資訊架構部(1234567)", deptmap.known_departments(session))
    w.role = config.ROLE_DEPT_ADMIN
    session.add(w)
    session.commit()
    to, mode, reason = mailer._resolve_recipient(session, "無信箱者", "資訊架構部", {})
    assert to == "win@corp.test" and mode == "fallback"


def test_legacy_cleanup_idempotent(engine, session):
    from webvuln import db as dbmod
    from webvuln.models import AuditLog
    _batch(session)
    session.add_all([
        User(username="a", display_name="甲", role="user", department="甲處資訊架構部(1234567)"),   # 長名、來源不明 → 改
        User(username="b", display_name="乙", role="user", department="資訊處資訊架構部", ad_department="資訊處資訊架構部"),  # ＝原始 AD → 改
        User(username="c", display_name="丙", role="user", department="甲處資訊架構部(1234567)", department_source="manual"),  # 手填 → 不碰
        User(username="d", display_name="丁", role="user", department="資訊架構部"),                       # 短名 → 不碰
        User(username="e", display_name="戊", role="user", department="人資處招募部(9)"),                   # 對不到 → 不動
        User(username="f", display_name="己", role="dept_admin", department="甲處資訊架構部(1234567)"),  # 窗口 → 不碰
        User(username="g", display_name="庚", role="user", department="甲處資訊架構部(1234567)",
             ad_department="甲處網路部(7654321)"),                                                   # 原始值不一致 → 衝突
    ])
    session.commit()
    dbmod.init_db(engine)
    dbmod.init_db(engine)   # 第二次不再改
    session.expire_all()
    u = {x.username: x for x in session.query(User).all()}
    assert (u["a"].department, u["a"].department_source, u["a"].ad_department) == ("資訊架構部", "ad_auto", "甲處資訊架構部(1234567)")
    assert (u["b"].department, u["b"].department_source) == ("資訊架構部", "ad_auto")
    assert u["c"].department == "甲處資訊架構部(1234567)" and u["c"].department_source == "manual"
    assert u["d"].department == "資訊架構部" and u["d"].department_source is None
    assert u["e"].department == "人資處招募部(9)" and u["e"].department_source is None
    assert u["f"].department == "甲處資訊架構部(1234567)" and u["f"].department_source is None
    assert u["g"].department == "甲處資訊架構部(1234567)" and u["g"].department_source is None
    logs = session.query(AuditLog).filter_by(action="dept_legacy_cleanup").all()
    assert logs and "2 筆" in logs[0].detail and "g" in logs[0].detail
    per = session.query(AuditLog).filter_by(action="dept_auto_change").all()
    assert sorted(x.target for x in per) == ["user:a", "user:b"] and all("trigger=cleanup" in x.detail for x in per)


def test_cleanup_skips_when_candidates_unreadable_or_empty(session, monkeypatch):
    from webvuln import query
    session.add(User(username="a", display_name="甲", role="user", department="甲處資訊架構部(1234567)"))
    session.commit()
    res = deptmap.cleanup_legacy(session)            # 沒有任何批次 → 候選是空的
    assert res["changed"] == 0 and res["skipped_reason"]
    _batch(session)

    def boom(db):
        raise RuntimeError("db locked")
    monkeypatch.setattr(query, "latest_batch", boom)
    res = deptmap.cleanup_legacy(session)
    assert res["changed"] == 0 and deptmap.READ_FAIL in res["skipped_reason"]
    session.expire_all()
    assert session.query(User).filter_by(username="a").one().department_source is None


def test_login_survives_candidate_read_failure(client, session, monkeypatch):
    from webvuln import query
    monkeypatch.setattr(config, "NO_AUTH", False)
    _ad(monkeypatch, "甲處資訊架構部(1234567)")

    def boom(db):
        raise RuntimeError("db locked")
    monkeypatch.setattr(query, "latest_batch", boom)
    assert client.post("/api/login", json={"username": "A002", "password": "pw"}).status_code == 200
    session.expire_all()
    u = session.query(User).filter_by(username="A002").one()
    assert u.department is None and u.ad_department == "甲處資訊架構部(1234567)"


def test_login_audit_and_privileged_not_changed(client, session, monkeypatch):
    from webvuln.models import AuditLog
    _batch(session)
    monkeypatch.setattr(config, "NO_AUTH", False)
    _ad(monkeypatch, "甲處資訊架構部(1234567)")
    session.add(User(username="W01", display_name="窗口甲", role="dept_admin", department="網路部", department_source="ad_auto"))
    session.commit()
    assert client.post("/api/login", json={"username": "A003", "password": "pw"}).status_code == 200
    assert client.post("/api/login", json={"username": "W01", "password": "pw"}).status_code == 200
    session.expire_all()
    assert session.query(User).filter_by(username="W01").one().department == "網路部"
    a = session.query(AuditLog).filter_by(action="dept_auto_change").all()
    assert [x.target for x in a] == ["user:A003"]
    assert "trigger=login" in a[0].detail and "(1234567)" in a[0].detail and "→ 資訊架構部" in a[0].detail


def test_import_privileged_suggestion_only(session):
    _batch(session)
    session.add(User(username="mail:陳小美", display_name="陳小美", role="dept_admin", department="網路部",
                     department_source="ad_auto"))
    session.commit()
    rows = [{"employeeID": "1001", "SamAccountName": "s1", "DisplayName": "陳小美", "mail": "amy@corp.test",
             "department": "甲處資訊架構部(1234567)", "manager": "", "Enabled": "TRUE"}]
    pv = adimport.run_import(session, rows, "ad.csv", "admin")
    rep = adimport.run_import(session, rows, "ad.csv", "admin", apply=True, token=pv["token"])
    session.expire_all()
    assert session.query(User).filter_by(username="mail:陳小美").one().department == "網路部"
    assert rep["dept_suggestions"][0]["suggestion"] == "資訊架構部"


def test_import_ambiguous_mapping_held(session):
    _batch(session, dept="營運部", owner="陳小美", extra_depts=("營運 部",))
    rows = [{"employeeID": "1001", "SamAccountName": "s1", "DisplayName": "陳小美", "mail": "amy@corp.test",
             "department": "總處營運部(1)", "manager": "", "Enabled": "TRUE"}]
    pv = adimport.run_import(session, rows, "ad.csv", "admin")
    assert pv["ambiguous"] and pv["ambiguous"][0]["reason"].startswith("部門對應不明確")


def test_dept_lock_endpoint(client, session):
    from webvuln.models import AuditLog
    session.add_all([User(username="a", display_name="甲", role="user", department="資訊架構部", department_source="ad_auto"),
                     User(username="b", display_name="乙", role="user")])
    session.commit()
    ids = {u.username: u.id for u in session.query(User).all()}
    r = client.post("/api/users/%d/dept-lock" % ids["a"])
    assert r.status_code == 200 and r.json()["department_source"] == "manual" and r.json()["department"] == "資訊架構部"
    assert client.post("/api/users/%d/dept-lock" % ids["b"]).status_code == 400
    assert session.query(AuditLog).filter_by(action="dept_lock").count() == 1


def _rows(*depts):
    return [{"employeeID": "10%02d" % i, "SamAccountName": "s%d" % i, "DisplayName": n, "mail": "u%d@corp.test" % i,
             "department": d, "manager": "", "Enabled": "TRUE"} for i, (n, d) in enumerate(depts)]


def test_import_collision_blocks_all(session):
    _batch(session, dept="營運部", owner="陳小美")
    rows = _rows(("陳小美", "甲處營運部(1)"), ("王大同", "乙處營運部(2)"))
    session.add(User(username="x", display_name="王大同", role="user", employee_id="1001"))
    session.commit()
    pv = adimport.run_import(session, rows, "ad.csv", "admin")
    assert pv["dept_collisions"] == [{"mapped": "營運部", "ad_departments": ["乙處營運部(2)", "甲處營運部(1)"]}]
    assert all(e["mapped"] == "" for e in pv["dept_map"])
    assert pv["ambiguous"] and pv["ambiguous"][0]["reason"].startswith("部門對應不明確")
    rep = adimport.run_import(session, rows, "ad.csv", "admin", apply=True, token=pv["token"])
    session.expire_all()
    assert session.query(User).filter_by(username="x").one().department is None


def test_token_covers_department_role_and_candidates(session):
    _batch(session)
    session.add(User(username="x", display_name="王大同", role="user", department="資訊架構部", department_source="ad_auto"))
    session.commit()
    rows = _rows(("陳小美", "資訊處資訊架構部(1)"))
    for change in ("department", "department_source", "role", "ad_department", "candidates"):
        pv = adimport.run_import(session, rows, "ad.csv", "admin")
        u = session.query(User).filter_by(username="x").one()
        if change == "department":
            u.department = "網路部"
        elif change == "department_source":
            u.department_source = "manual"
        elif change == "role":
            u.role = "dept_admin"
        elif change == "ad_department":
            u.ad_department = "某處某部(9)"
        else:
            _batch(session, dept="新部門")
        session.commit()
        with pytest.raises(adimport.ImportError_, match="STALE"):
            adimport.run_import(session, rows, "ad.csv", "admin", apply=True, token=pv["token"])


def test_weekly_report_dept_change_listed(session):
    _batch(session)
    session.add(User(username="x", display_name="王大同", role="user", employee_id="1000", weekly_report=True,
                     department="網路部", department_source="ad_auto", email="x@corp.test", email_source="ad_import"))
    session.commit()
    rows = _rows(("王大同", "資訊處資訊架構部(1)"))
    pv = adimport.run_import(session, rows, "ad.csv", "admin")
    assert pv["weekly_dept_changes"] == [{"username": "x", "name": "王大同", "before": "網路部", "after": "資訊架構部"}]
    session.expire_all()
    assert session.query(User).filter_by(username="x").one().department == "網路部"   # 預覽不寫入

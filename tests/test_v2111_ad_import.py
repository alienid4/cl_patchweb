"""V2.111 匯入 AD 名單：寄錯人比沒寄到更糟——名字只找候選人，綁定以員編為準，不確定就轉窗口。"""
import datetime as dt
import email
import json
import os

import pytest
from sqlalchemy.orm import Session, sessionmaker

from webvuln import adimport, appsettings, automail, config, importer, mailer, names, security
from webvuln.models import AuditLog, User
from webvuln.schemas import FindingIn, ImportIn

MON = dt.datetime(2026, 10, 26, 8, 0)
TODAY = MON.date()
DEPT = "資訊架構部"
MGR_DN = "CN=趙主管,OU=ou1,OU=Corp,DC=corp,DC=test"
COLS = list(adimport.FIELDS)


def _row(emp, name, mail, dept="資訊處資訊架構部", mgr=MGR_DN, enabled="True", sam=None):
    return {"employeeID": emp, "SamAccountName": sam or ("s" + str(emp)), "DisplayName": name, "mail": mail,
            "department": dept, "manager": mgr, "Enabled": enabled}


def _base_rows():
    return [
        _row("1001", "陳小美 AMY", "miyuki@corp.test"),
        _row("1002", "李小華　EVA", "evelyn@corp.test"),          # 全形空白
        _row("1003", "王大同", "yusen@corp.test"),
        _row("1004", "王小明 MING", "ming1@corp.test", dept="網路部"),   # 同名 1
        _row("1005", "王小明", "ming2@corp.test", dept="系統部"),         # 同名 2
        _row("2000", "趙主管 JOHN", "boss@corp.test", mgr=""),           # 主管
        _row("", "svc_backup", ""),                                    # 服務帳號
        _row("1006", "李停用", "gone@corp.test", enabled="FALSE"),       # 停用
        _row("1007", "張無信", ""),                                     # 在職沒信箱
    ]


def _load(session, owners=("陳小美", "李小華", "王大同", "王小明", "張無信", "DBA"), dept=DEPT):
    fs = [FindingIn(sheet_key="s", close_status="未結案", department=dept, host="h%d" % i, plugin_id="p%d" % i,
                    severity="High", owner=o, remediation_due=(TODAY - dt.timedelta(days=5)).isoformat())
          for i, o in enumerate(owners)]
    importer.create_batch(session, ImportIn(findings=fs))
    session.commit()


def _apply(session, rows, file_name="ad.csv"):
    pv = adimport.run_import(session, rows, file_name, "admin", apply=False, now=MON)
    return adimport.run_import(session, rows, file_name, "admin", apply=True, token=pv["token"], now=MON)


def _users(session):
    session.expire_all()
    return {u.display_name: u for u in session.query(User).all()}


def _post(client, rows, apply=False, token=None, columns=COLS):
    return client.post("/api/users/ad-import", json={"file_name": "ad.csv", "sheet": "Sheet1", "columns": columns,
                                                     "rows": rows, "apply": apply, "token": token})


# ── 名字正規化（共用）──
@pytest.mark.parametrize("raw,want", [
    ("陳小美 AMY", "陳小美"), ("李小華 EVA", "李小華"), ("王大同", "王大同"), ("  王大同  ", "王大同"),
    ("李小華　EVA", "李小華"), ("陳小美AMY", "陳小美"), ("　陳大文　", "陳大文"), ("周 志明", "周志明"),
    ("周　志明 ABC", "周志明"), ("svc_backup", ""), ("", ""), (None, ""), ("EVA 李小華", ""),
])
def test_norm_name(raw, want):
    assert names.norm_name(raw) == want


def test_owner_key_non_cjk():
    assert names.owner_key("DBA") == "DBA" and names.owner_key(" 主機組 ") == "主機組"
    assert names.owner_key("John  Smith") == "John Smith"


def test_manager_cn_and_enabled_strict():
    assert adimport.manager_cn(MGR_DN) == "趙主管"
    assert adimport.manager_cn(r"CN=Lin\, Amy,OU=x") == "Lin, Amy"
    assert adimport.parse_enabled("FALSE") is False and adimport.parse_enabled("true") is True
    assert adimport.parse_enabled(True) is True
    for bad in ("", "0", "1", "是", "yes", None):
        assert adimport.parse_enabled(bad) is None


def test_row_validation_errors():
    rows = [_row("1001", "甲", "a@corp.test", enabled="maybe"), _row(1002, "乙", "b@corp.test"),
            _row("1003", "丙", "c@corp.test; d@corp.test"), _row("1004", "丁", "x y@corp.test"),
            _row("1005", "戊", "e@@corp.test"), _row("1006", "己", "f@corp.test")]
    idx = adimport.build_index(rows)
    err = {e["display_name"]: e["error"] for e in idx["errors"]}
    assert "TRUE／FALSE" in err["甲"] and err["乙"] == "員編格式需為文字"
    assert all("信箱格式不正確" in err[n] for n in ("丙", "丁", "戊"))
    assert idx["counts"]["ok"] == 1


def test_duplicate_employee_id_conflict_not_first_wins():
    rows = [_row("1001", "甲", "a@corp.test"), _row("1001", "甲", "a@corp.test"),     # 完全相同 → 算一列
            _row("1002", "乙", "b@corp.test"), _row("1002", "乙", "b2@corp.test")]   # 內容不同 → 都錯
    idx = adimport.build_index(rows)
    assert idx["counts"]["duplicate_same"] == 1 and "1001" in idx["by_emp"] and "1002" not in idx["by_emp"]
    assert sum(1 for e in idx["errors"] if "員編重複" in e["error"]) == 2


def test_zero_valid_rows_rejected(session):
    _load(session)
    with pytest.raises(adimport.ImportError_):
        adimport.run_import(session, [_row("", "svc", ""), _row("1", "乙", "b@corp.test", enabled="FALSE")], "x", "a")


# ── 預覽／套用 ──
def test_dry_run_writes_nothing_then_apply(session):
    _load(session)
    pv = adimport.run_import(session, _base_rows(), "ad.csv", "admin", apply=False, now=MON)
    assert pv["token"] and not pv["applied"] and len(pv["matched"]) == 3
    assert any(c["name"] == "陳小美" and c["after_to"] == "miyuki@corp.test" for c in pv["recipient_changes"])
    assert session.query(User).count() == 0 and adimport.last_report(session) is None
    with pytest.raises(adimport.ImportError_):
        adimport.run_import(session, _base_rows(), "ad.csv", "admin", apply=True, token="bad")
    rep = adimport.run_import(session, _base_rows(), "ad.csv", "admin", apply=True, token=pv["token"], now=MON)
    assert rep["batch_id"] == 1 and "token" not in rep
    assert _users(session)["陳小美"].email == "miyuki@corp.test"


def test_apply_rejected_if_state_changed(session):
    _load(session)
    pv = adimport.run_import(session, _base_rows(), "ad.csv", "admin", apply=False, now=MON)
    session.add(User(username="x", display_name="王大同", email="y@corp.test", role=config.ROLE_USER, is_active=True))
    session.commit()
    with pytest.raises(adimport.ImportError_, match="STALE"):
        adimport.run_import(session, _base_rows(), "ad.csv", "admin", apply=True, token=pv["token"])
    rows = _base_rows()
    rows[0]["mail"] = "other@corp.test"
    pv2 = adimport.run_import(session, _base_rows(), "ad.csv", "admin", apply=False)
    with pytest.raises(adimport.ImportError_, match="STALE"):   # 名單換了
        adimport.run_import(session, rows, "ad.csv", "admin", apply=True, token=pv2["token"])


def test_import_unique_ambiguous_notfound_nomail(session):
    _load(session)
    rep = _apply(session, _base_rows())
    assert {r["name"] for r in rep["matched"]} == {"陳小美", "李小華", "王大同"}
    u = _users(session)["陳小美"]
    assert u.username == "mail:陳小美" and u.password_hash is None and u.employee_id == "1001"
    assert u.manager_name == "趙主管" and u.manager_email == "boss@corp.test" and u.email_source == "ad_import"
    assert [a["name"] for a in rep["ambiguous"]] == ["王小明"] and "王小明" not in _users(session)
    assert {n["name"] for n in rep["notfound"]} == {"DBA"}
    assert [n["name"] for n in rep["nomail"]] == ["張無信"]
    assert security.authenticate(session, "mail:陳小美", "x") is None


def test_same_name_counted_over_all_rows_including_disabled(session):
    _load(session, owners=("王大同",))
    rows = _base_rows() + [_row("1099", "王大同", "", enabled="FALSE")]   # 離職的同名者
    rep = _apply(session, rows)
    assert [a["name"] for a in rep["ambiguous"]] == ["王大同"]
    cands = rep["ambiguous"][0]["candidates"]
    assert {c["employee_id"]: c["pickable"] for c in cands} == {"1003": True, "1099": False}
    rows2 = _base_rows() + [_row("", "王大同 SVC", "")]                   # 沒員編的同名列也算
    _load(session, owners=("王大同",))
    assert [a["name"] for a in _apply(session, rows2)["ambiguous"]] == ["王大同"]


def test_department_mismatch_held(session):
    _load(session, owners=("王大同",), dept="財務部")
    rep = _apply(session, _base_rows())
    assert rep["ambiguous"] and "部門不符" in rep["ambiguous"][0]["reason"]
    assert "王大同" not in _users(session)
    # 已綁員編的帳號（之前確認過）不再擋
    session.add(User(username="u3", display_name="王大同", employee_id="1003", role=config.ROLE_USER, is_active=True))
    session.commit()
    rep = _apply(session, _base_rows())
    assert {m["name"] for m in rep["matched"]} == {"王大同"}


def test_identity_conflict_not_overwritten_and_falls_back(session):
    _load(session, owners=("王大同",))
    session.add_all([User(username="u_old", display_name="王大同", email="old@corp.test", employee_id="0777",
                          email_source="ad_import", role=config.ROLE_USER, is_active=True),
                     User(username="win", display_name="窗口", email="win@corp.test", department=DEPT,
                          role=config.ROLE_DEPT_ADMIN, is_active=True)])
    session.commit()
    rep = _apply(session, _base_rows())
    assert [c["name"] for c in rep["conflicts"]] == ["王大同"]
    u = _users(session)["王大同"]
    assert u.email == "old@corp.test" and u.employee_id == "0777" and u.ad_conflict
    to, mode, reason = mailer._resolve_recipient(session, "王大同", DEPT, {})
    assert to == "win@corp.test" and mode == "fallback" and "身分衝突" in reason


def test_confirm_pick_binds_emp_with_who_when(client, session):
    _load(session)
    pv = _post(client, _base_rows()).json()
    rep = _post(client, _base_rows(), apply=True, token=pv["token"]).json()
    bid = rep["batch_id"]
    assert client.post("/api/users/ad-import/confirm", json={"name": "王小明", "employee_id": "9999", "batch_id": bid}).status_code == 400
    assert client.post("/api/users/ad-import/confirm", json={"name": "王小明", "employee_id": "1005", "batch_id": bid + 1}).status_code == 400
    r = client.post("/api/users/ad-import/confirm", json={"name": "王小明", "employee_id": "1005", "batch_id": bid})
    assert r.status_code == 200
    rp = r.json()["report"]
    assert not rp["ambiguous"] and rp["confirmations"][0]["employee_id"] == "1005"
    u = _users(session)["王小明"]
    assert u.email == "ming2@corp.test" and u.ad_bound_by and u.ad_bound_at and u.employee_id == "1005"
    pv = _post(client, _base_rows()).json()
    rep = _post(client, _base_rows(), apply=True, token=pv["token"]).json()
    assert not rep["ambiguous"] and {m["name"]: m for m in rep["matched"]}["王小明"]["via"].startswith("人工指定")
    acts = [a.action for a in session.query(AuditLog).all()]
    assert acts.count("ad_import") == 2 and acts.count("ad_import_pick") == 1


def test_disabled_pick_rejected(session):
    _load(session, owners=("王大同",))
    rep = _apply(session, _base_rows() + [_row("1099", "王大同", "x@corp.test", enabled="FALSE")])
    with pytest.raises(adimport.ImportError_):
        adimport.confirm_pick(session, "王大同", "1099", rep["batch_id"], "admin")


def test_same_name_auto_account_not_treated_as_pick(session):
    """同名帳號（例如 AD 登入的）有員編，不能被當成「上次人工指定」。"""
    _load(session, owners=("王小明",))
    session.add(User(username="1004", display_name="王小明 MING", employee_id="1004", email="ming1@corp.test",
                     role=config.ROLE_USER, is_active=True))
    session.commit()
    rep = _apply(session, _base_rows())
    assert [a["name"] for a in rep["ambiguous"]] == ["王小明"]
    assert not any(m["name"] == "王小明" for m in rep["matched"])


def test_manual_and_legacy_email_never_overwritten_or_cleared(session):
    _load(session, owners=("王大同", "陳小美"))
    session.add_all([User(username="u_m", display_name="王大同", email="hand@corp.test", email_source="manual",
                          role=config.ROLE_USER, is_active=True),
                     User(username="u_l", display_name="陳小美", email="legacy@corp.test", role=config.ROLE_USER, is_active=True)])
    session.commit()
    _apply(session, _base_rows())
    us = _users(session)
    assert us["王大同"].email == "hand@corp.test" and us["陳小美"].email == "legacy@corp.test"
    rows = _base_rows()
    rows[2]["Enabled"] = "FALSE"
    rows[0]["Enabled"] = "FALSE"
    rep = _apply(session, rows)
    us = _users(session)
    assert us["王大同"].email == "hand@corp.test" and us["陳小美"].email == "legacy@corp.test"
    assert all(not d["cleared"] for d in rep["departed"])


def test_manual_email_via_role_endpoint_marked(client, session):
    session.add(User(username="u", display_name="甲", role=config.ROLE_USER, is_active=True))
    session.commit()
    uid = session.query(User).filter_by(username="u").one().id
    client.post("/api/users/%d/role" % uid, json={"email": "hand@corp.test"})
    assert _users(session)["甲"].email_source == "manual"


def test_clear_only_when_verified_disabled(session):
    _load(session)
    _apply(session, _base_rows())
    rows = _base_rows()
    rows[0]["mail"] = "miyuki.new@corp.test"            # 信箱變了 → 更新
    rows[1]["Enabled"] = "FALSE"                         # 李小華停用 → 清
    rows = [r for r in rows if r["employeeID"] != "1003"]  # 王大同缺席 → 只回報
    rep = _apply(session, rows)
    us = _users(session)
    assert us["陳小美"].email == "miyuki.new@corp.test"
    assert us["李小華"].email is None and "AD 已停用" in us["李小華"].email_note and us["李小華"].manager_email is None
    assert us["王大同"].email == "yusen@corp.test"
    assert [a["employee_id"] for a in rep["absent"]] == ["1003"]
    assert {d["name"] for d in rep["departed"] if d["cleared"]} == {"李小華"}
    # 在職但沒信箱 → 只回報不清
    rows2 = _base_rows()
    rows2[0]["mail"] = ""
    rep = _apply(session, rows2)
    assert _users(session)["陳小美"].email == "miyuki.new@corp.test"
    assert any(n.get("employee_id") == "1001" for n in rep["nomail"])


def test_manager_mapping_rules(session):
    _load(session, owners=("陳小美", "李小華", "王大同"))
    rows = _base_rows() + [_row("3001", "周主管", "", mgr=""), _row("3002", "吳主管", "w@corp.test", mgr="", enabled="FALSE"),
                           _row("3003", "陳主管", "c1@corp.test", mgr=""), _row("3004", "陳主管", "", mgr="", enabled="FALSE")]
    rows[1]["manager"] = "CN=周主管,OU=x"    # 主管沒信箱
    rows[2]["manager"] = "CN=吳主管,OU=x"    # 主管停用
    rows.append(_row("1010", "測試甲", "t@corp.test", mgr="CN=陳主管,OU=x"))   # 主管同名（含停用列）
    rep = _apply(session, rows)
    mg = {m["name"]: m for m in rep["managers"]}
    assert mg["陳小美"]["manager_email"] == "boss@corp.test"
    assert mg["李小華"]["manager_email"] == "" and "沒有信箱" in mg["李小華"]["status"]
    assert mg["王大同"]["manager_email"] == "" and "停用" in mg["王大同"]["status"]
    idx = adimport.build_index(rows)
    assert "同名" in adimport.resolve_manager(idx, idx["by_emp"]["1010"])["status"]
    # 主管是自己 → 排除本人
    idx = adimport.build_index([_row("2000", "趙主管", "boss@corp.test", mgr=MGR_DN)])
    assert adimport.resolve_manager(idx, idx["by_emp"]["2000"])["status"] == "名單裡查無主管"


def test_history_keeps_last_10(session):
    _load(session, owners=("王大同",))
    for i in range(12):
        _apply(session, _base_rows(), file_name="ad%d.csv" % i)
    h = adimport.history(session)
    assert len(h) == 10 and h[-1]["batch_id"] == 12 and h[-1]["file"] == "ad11.csv"
    assert h[-1]["by"] == "admin" and h[-1]["rows_hash"] and h[-1]["at"]


# ── 端點驗證 ──
def test_endpoint_validation(client, session):
    _load(session)
    rows = _base_rows()
    assert _post(client, [dict(r, Phone="0912") for r in rows]).status_code == 422          # 未知欄位拒收
    assert _post(client, rows, columns=["DisplayName", "mail", "Enabled"]).status_code == 400   # 缺 employeeID
    assert _post(client, rows, columns=COLS + ["mail"]).status_code == 400                    # 重複標題
    assert _post(client, [dict(r, department="x" * 257) for r in rows]).status_code == 422   # 欄位超長
    bad = [{k: v for k, v in r.items() if k != "Enabled"} for r in rows]
    assert _post(client, bad).status_code == 422                                               # 列缺必要鍵
    assert _post(client, []).status_code == 400
    assert _post(client, _base_rows(), apply=True, token="x").status_code == 409


def test_request_size_cap(client, session, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 1000)
    assert _post(client, _base_rows() * 10).status_code == 413


def test_phone_ext_never_stored(client, session, engine):
    _load(session)
    r = client.post("/api/users/ad-import", json={"columns": COLS + ["Phone"], "rows": [dict(x, Phone="0912-345-678") for x in _base_rows()]})
    assert r.status_code == 422
    pv = _post(client, _base_rows()).json()
    assert _post(client, _base_rows(), apply=True, token=pv["token"]).status_code == 200
    engine.dispose()
    for f in (engine.url.database, engine.url.database + "-wal"):
        if os.path.exists(f):
            assert b"0912-345-678" not in open(f, "rb").read()


def test_super_admin_only(client, engine, monkeypatch):
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with f() as s:
        security.create_user(s, "u1", "pw12345", role="user", display_name="甲")
        security.create_user(s, "d1", "pw12345", role="dept_admin", display_name="乙", department=DEPT)
        security.create_user(s, "s1", "pw12345", role="super_admin", display_name="丙")
    monkeypatch.setattr(config, "NO_AUTH", False)
    assert _post(client, _base_rows()).status_code == 401
    for u in ("u1", "d1"):
        client.post("/api/logout")
        assert client.post("/api/login", json={"username": u, "password": "pw12345"}).status_code == 200
        assert _post(client, _base_rows()).status_code == 403
        assert client.get("/api/users/ad-import/last").status_code == 403
        assert client.post("/api/users/ad-import/confirm", json={"name": "王小明", "employee_id": "1005"}).status_code == 403
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": "s1", "password": "pw12345"}).status_code == 200
    assert _post(client, _base_rows()).status_code == 200


# ── 收件身分統一：To／CC 同一筆 ──
def test_resolve_owner_same_name_different_mails_conflict(session):
    session.add_all([User(username="a", display_name="王小明", email="m1@corp.test", role=config.ROLE_USER, is_active=True),
                     User(username="b", display_name="王小明 MING", email="m2@corp.test", manager_email="boss@corp.test",
                          role=config.ROLE_USER, is_active=True)])
    session.commit()
    r = mailer.resolve_owner(session, "王小明")
    assert r["status"] == "conflict" and r["email"] is None and r["manager_email"] is None


# ── 升級副本組合：開關 × 升級 × AD 主管 × 部門設定 × 測試模式 ──
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


def _mail_setup(session, esc, test_mode=False, dept_esc="chief@corp.test"):
    appsettings.set_email_config(session, {"enabled": True, "smtp_host": "relay.test", "from_default": "sys@corp.test"})
    session.add(User(username="win", display_name="窗口", email="win@corp.test", department=DEPT,
                     role=config.ROLE_DEPT_ADMIN, is_active=True))
    session.commit()
    kw = {"enabled": True, "departments": [{"department": DEPT, "enabled": True, "cc": "", "escalate_cc": dept_esc,
                                            "send_inventory": False, "send_dunning": True}]}
    if test_mode:
        kw.update(test_mode=True, test_addresses="t1@corp.test")
    automail.save_settings(session, kw, now=MON - dt.timedelta(days=7))
    automail.set_escalate_enabled(session, esc)


def _dun(session):
    return {j["label"]: j for j in automail.preview(session, MON)["jobs"] if j["kind"] == "dunning"}


def _setup_owners(session, overdue=True):
    owners = ("陳小美", "張無信")
    due = (TODAY - dt.timedelta(days=5)) if overdue else (TODAY + dt.timedelta(days=45))
    fs = [FindingIn(sheet_key="s", close_status="未結案", department=DEPT, host="h%d" % i, plugin_id="p%d" % i,
                    severity="High", owner=o, remediation_due=due.isoformat()) for i, o in enumerate(owners)]
    importer.create_batch(session, ImportIn(findings=fs))
    session.commit()
    _apply(session, _base_rows())


@pytest.mark.parametrize("esc_on", [True, False])
@pytest.mark.parametrize("overdue", [True, False])
@pytest.mark.parametrize("test_mode", [True, False])
def test_escalation_matrix(session, smtp, esc_on, overdue, test_mode):
    _setup_owners(session, overdue=overdue)
    _mail_setup(session, esc=esc_on, test_mode=test_mode)
    d = _dun(session)
    ad, nm = d["陳小美"], d["張無信"]          # 有 AD 主管／沒 AD 主管（To 轉窗口 → 用部門設定）
    assert ad["ad_manager"] == {"name": "趙主管", "email": "boss@corp.test"} and nm["ad_manager"] is None
    assert nm["final_to_reason"].startswith("測試模式") if test_mode else nm["final_to_reason"].startswith("轉窗口")
    if not overdue:
        assert "無升級項目" in ad["final_cc_reason"] and ad["escalate_cc"] == []
    elif esc_on:
        assert ad["escalate_cc"] == ["boss@corp.test"] and nm["escalate_cc"] == ["chief@corp.test"]
        assert ad["final_cc_reason"].rstrip("）").endswith("AD 主管") and nm["final_cc_reason"].rstrip("）").endswith("部門設定")
    else:
        assert ad["escalate_cc"] == [] and "開關關閉僅預覽（AD 主管" in ad["final_cc_reason"]
        assert "開關關閉僅預覽（部門設定" in nm["final_cc_reason"]
    if test_mode:
        assert ad["final_to"] == ["t1@corp.test"] and ad["final_cc"] == []
    elif overdue and esc_on:
        assert ad["final_cc"] == ["boss@corp.test"]
    automail.tick(session, MON)
    automail.test_run(session, MON, "admin", ["t2@corp.test"])
    assert smtp
    for x in smtp:
        envs = set(x["env"])
        if test_mode or envs <= {"t1@corp.test", "t2@corp.test"}:
            assert envs <= {"t1@corp.test", "t2@corp.test"} and not x["msg"].get("Cc")
            continue
        mgr_mailed = "boss@corp.test" in envs or "chief@corp.test" in envs
        assert mgr_mailed == (esc_on and overdue)
        if "miyuki@corp.test" in envs and esc_on and overdue:
            assert "boss@corp.test" in envs and "chief@corp.test" not in envs


def test_fallback_to_window_never_uses_ad_manager(session, smtp):
    """To 轉窗口（身分衝突）時，不能從名字推 AD 主管當 CC。"""
    _setup_owners(session)
    u = session.query(User).filter_by(display_name="陳小美").one()
    u.ad_conflict = "測試"
    session.commit()
    _mail_setup(session, esc=True)
    d = _dun(session)["陳小美"]
    assert d["mode"] == "fallback" and d["ad_manager"] is None and "boss@corp.test" not in d["cc"]
    assert d["escalate_cc"] == ["chief@corp.test"]


def test_no_ad_manager_and_no_dept_setting(session):
    _setup_owners(session)
    _mail_setup(session, esc=True, dept_esc="")
    d = _dun(session)
    assert d["張無信"]["escalate_missing"] and d["張無信"]["final_cc_reason"] == "應升級但無主管可副本"
    assert d["陳小美"]["escalate_cc"] == ["boss@corp.test"]

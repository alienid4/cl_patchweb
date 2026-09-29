from webvuln import security
from webvuln.models import AuditLog


def test_password_hash_roundtrip():
    h = security.hash_password("s3cret!")
    assert h.startswith("pbkdf2_sha256$")
    assert security.verify_password("s3cret!", h) is True
    assert security.verify_password("wrong", h) is False
    assert security.verify_password("x", None) is False
    assert security.verify_password("x", "garbage") is False


def test_two_hashes_differ_by_salt():
    assert security.hash_password("same") != security.hash_password("same")


def _mkuser(client_db, username, role, pwd="pw12345"):
    from webvuln import security as sec
    sec.create_user(client_db, username, pwd, role=role)


def test_login_logout_me_flow(client, engine):
    from sqlalchemy.orm import Session, sessionmaker
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "amy", "pw12345", role="承辦", display_name="Amy")

    # 未登入
    assert client.get("/api/me").json() == {"authenticated": False}
    # 密碼錯
    assert client.post("/api/login", json={"username": "amy", "password": "bad"}).status_code == 401
    # 正確登入
    r = client.post("/api/login", json={"username": "amy", "password": "pw12345"})
    assert r.status_code == 200 and r.json()["role"] == "承辦"
    # me 已登入（TestClient 會保存 cookie）
    me = client.get("/api/me").json()
    assert me["authenticated"] is True and me["username"] == "amy"
    # 登出
    assert client.post("/api/logout").json()["ok"] is True
    assert client.get("/api/me").json()["authenticated"] is False


def test_transition_requires_login_and_role(client, engine):
    from sqlalchemy.orm import Session, sessionmaker
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "staff", "pw12345", role="承辦")
        security.create_user(s, "bob", "pw12345", role="viewer")

    # 建一個 case
    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}
    ]})
    cid = client.get("/api/cases").json()[0]["id"]

    # 未登入 → 401
    r = client.post(f"/api/cases/{cid}/transition", json={"to": "待主管"})
    assert r.status_code == 401

    # viewer 登入 → 403
    client.post("/api/login", json={"username": "bob", "password": "pw12345"})
    r = client.post(f"/api/cases/{cid}/transition", json={"to": "待主管"})
    assert r.status_code == 403
    client.post("/api/logout")

    # 承辦登入 → 成功 + 稽核留痕
    client.post("/api/login", json={"username": "staff", "password": "pw12345"})
    r = client.post(f"/api/cases/{cid}/transition", json={"to": "待主管"})
    assert r.status_code == 200 and r.json()["status"] == "待主管"
    # 非法轉移 → 400
    assert client.post(f"/api/cases/{cid}/transition", json={"to": "完成"}).status_code == 400

    with factory() as s:
        acts = {a.action for a in s.query(AuditLog).all()}
    assert "login" in acts and "case_transition" in acts and "case_transition_rejected" in acts


def test_disable_write_killswitch(client, engine, monkeypatch):
    from sqlalchemy.orm import Session, sessionmaker
    from webvuln import config
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "staff", "pw12345", role="承辦")
    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}
    ]})
    cid = client.get("/api/cases").json()[0]["id"]
    client.post("/api/login", json={"username": "staff", "password": "pw12345"})

    monkeypatch.setattr(config, "DISABLE_WRITE", True)
    r = client.post(f"/api/cases/{cid}/transition", json={"to": "待主管"})
    assert r.status_code == 503

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
    assert client.get("/api/me").json()["authenticated"] is False
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


def _first_fid(client):
    return client.get("/api/findings").json()[0]["id"]


def test_write_requires_login_and_role(client, engine, monkeypatch):
    from sqlalchemy.orm import Session, sessionmaker
    from webvuln import config
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "staff", "pw12345", role="承辦")
        security.create_user(s, "bob", "pw12345", role="viewer")

    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}
    ]})
    fid = _first_fid(client)
    body = {"set_progress": True, "progress": "處理中"}   # 寫入類：改處理進度

    monkeypatch.setattr(config, "NO_AUTH", False)   # 關免登入，測真正的權限閘門
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 401   # 未登入

    client.post("/api/login", json={"username": "bob", "password": "pw12345"})
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 403   # viewer 無權
    client.post("/api/logout")

    client.post("/api/login", json={"username": "staff", "password": "pw12345"})
    r = client.post(f"/api/findings/{fid}/overlay", json=body)
    assert r.status_code == 200 and r.json()["progress"] == "處理中"   # 承辦可寫

    with factory() as s:
        acts = {a.action for a in s.query(AuditLog).all()}
    assert "login" in acts and "set_overlay" in acts


def test_no_auth_mode_allows_write(client, engine, monkeypatch):
    from webvuln import config
    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}
    ]})
    fid = _first_fid(client)
    body = {"set_progress": True, "progress": "等複掃"}
    monkeypatch.setattr(config, "NO_AUTH", False)
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 401
    monkeypatch.setattr(config, "NO_AUTH", True)
    assert client.get("/api/me").json()["open_write"] is True
    r = client.post(f"/api/findings/{fid}/overlay", json=body)
    assert r.status_code == 200 and r.json()["progress"] == "等複掃"


def test_import_requires_write(client, engine, monkeypatch):
    """P5：匯入屬寫入。關免登入後未登入不能匯入(擋 401)；登入承辦可匯入且留稽核。"""
    from sqlalchemy.orm import Session, sessionmaker
    from webvuln import config
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "staff", "pw12345", role="承辦")
    body = {"findings": [{"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}]}

    monkeypatch.setattr(config, "NO_AUTH", False)
    assert client.post("/api/import", json=body).status_code == 401      # 未登入擋
    client.post("/api/login", json={"username": "staff", "password": "pw12345"})
    assert client.post("/api/import", json=body).status_code == 200      # 登入可匯入
    with factory() as s:
        assert "import" in {a.action for a in s.query(AuditLog).all()}   # 留稽核


def test_disable_write_killswitch(client, engine, monkeypatch):
    from sqlalchemy.orm import Session, sessionmaker
    from webvuln import config
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "staff", "pw12345", role="承辦")
    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}
    ]})
    fid = _first_fid(client)
    client.post("/api/login", json={"username": "staff", "password": "pw12345"})

    monkeypatch.setattr(config, "DISABLE_WRITE", True)
    r = client.post(f"/api/findings/{fid}/overlay", json={"set_progress": True, "progress": "處理中"})
    assert r.status_code == 503

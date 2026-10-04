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
    """三級權限範圍：super 全部；dept_admin 限自己部門；user 限自己的 owner。"""
    from sqlalchemy.orm import Session, sessionmaker
    from webvuln import config
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "super", "pw12345", role=config.ROLE_SUPER)
        security.create_user(s, "deptA", "pw12345", role=config.ROLE_DEPT_ADMIN, department="資訊架構部")
        security.create_user(s, "wang", "pw12345", role=config.ROLE_USER, display_name="王五")
        security.create_user(s, "other", "pw12345", role=config.ROLE_USER, display_name="他人")

    # 一筆：部門=資訊架構部、負責人=王五
    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s",
         "department": "資訊架構部", "owner": "王五", "close_status": "未結案"}
    ]})
    fid = _first_fid(client)
    body = {"set_progress": True, "progress": "處理中"}

    monkeypatch.setattr(config, "NO_AUTH", False)
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 401   # 未登入

    # user 他人：owner 不是他 → 403
    client.post("/api/login", json={"username": "other", "password": "pw12345"})
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 403
    client.post("/api/logout")
    # user 王五：owner＝王五 → 200
    client.post("/api/login", json={"username": "wang", "password": "pw12345"})
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 200
    client.post("/api/logout")
    # dept_admin 同部門 → 200
    client.post("/api/login", json={"username": "deptA", "password": "pw12345"})
    assert client.post(f"/api/findings/{fid}/overlay", json=body).status_code == 200
    client.post("/api/logout")
    # super → 200
    client.post("/api/login", json={"username": "super", "password": "pw12345"})
    r = client.post(f"/api/findings/{fid}/overlay", json=body)
    assert r.status_code == 200 and r.json()["progress"] == "處理中"

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
    """匯入屬系統級動作：未登入擋 401；非 Super Admin 擋 403；Super Admin 可匯入且留稽核。"""
    from sqlalchemy.orm import Session, sessionmaker
    from webvuln import config
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "super", "pw12345", role=config.ROLE_SUPER)
        security.create_user(s, "deptA", "pw12345", role=config.ROLE_DEPT_ADMIN, department="X")
    body = {"findings": [{"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"}]}

    monkeypatch.setattr(config, "NO_AUTH", False)
    assert client.post("/api/import", json=body).status_code == 401      # 未登入擋
    client.post("/api/login", json={"username": "deptA", "password": "pw12345"})
    assert client.post("/api/import", json=body).status_code == 403      # 非 super 擋
    client.post("/api/logout")
    client.post("/api/login", json={"username": "super", "password": "pw12345"})
    assert client.post("/api/import", json=body).status_code == 200      # super 可匯入
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

"""本地測試帳號：AD 啟用時仍能用本地帳號登入（security.authenticate 本地優先）＋ 建立端點。"""
from webvuln import appsettings, config, security
from webvuln.models import User


def test_local_account_works_even_when_ad_enabled(session, monkeypatch):
    # 啟用 AD（但不給可連的伺服器）；本地帳號應仍能登入、不被導去 AD
    appsettings.set_ad_config(session, {"enabled": True, "servers": ["10.30.0.1"], "base_dn": "DC=x"})
    security.create_user(session, "superadmin", "test-1234", role=config.ROLE_SUPER)
    u = security.authenticate(session, "superadmin", "test-1234")
    assert u is not None and config.canon_role(u.role) == config.ROLE_SUPER
    # 密碼錯 → 不會回傳（也不會因 AD 分支誤放行）
    assert security.authenticate(session, "superadmin", "wrong") is None


def test_ad_user_has_no_local_hash(session):
    # AD 帳號 password_hash 為空 → 不會誤中本地分支（本地優先只認有 hash 的）
    session.add(User(username="00000001", display_name="測試員", password_hash=None,
                     role=config.ROLE_SUPER, is_active=True))
    session.commit()
    # 未啟用 AD、也非 AD 後端 → 走本地，但此帳號無 hash → None
    assert security.authenticate(session, "00000001", "anything") is None


def test_create_local_user_endpoint(client):
    r = client.post("/api/users/local", json={"username": "user", "password": "test-1234",
                                             "role": "user", "display_name": "王小明"})
    assert r.status_code == 200 and r.json()["created"] is True
    assert r.json()["role"] == "user" and r.json()["display_name"] == "王小明"
    # 再存一次＝更新（冪等），改成窗口
    r2 = client.post("/api/users/local", json={"username": "user", "password": "test-5678",
                                              "role": "dept_admin", "department": "資訊架構部"})
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r2.json()["role"] == "dept_admin" and r2.json()["department"] == "資訊架構部"


def test_create_local_user_rejects_short_password(client):
    assert client.post("/api/users/local", json={"username": "x", "password": "12"}).status_code == 400

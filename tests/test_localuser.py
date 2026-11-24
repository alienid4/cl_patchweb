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


def test_seed_test_skips_existing(client):
    r1 = client.post("/api/users/seed-test", json={"password": "test-1234"}).json()
    assert r1["created"] == 3
    # 再按一次：全部已存在 → 不再建立
    r2 = client.post("/api/users/seed-test", json={"password": "other-9999"}).json()
    assert r2["created"] == 0
    assert all(a["status"] == "exists" for a in r2["accounts"])


def test_delete_user_guards(client, engine):
    from sqlalchemy.orm import Session, sessionmaker
    fac = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    # 建兩個 super + 一個 user
    client.post("/api/users/local", json={"username": "s1", "password": "test-1234", "role": "super_admin"})
    client.post("/api/users/local", json={"username": "s2", "password": "test-1234", "role": "super_admin"})
    client.post("/api/users/local", json={"username": "u1", "password": "test-1234", "role": "user"})
    with fac() as s:
        from webvuln.models import User
        ids = {u.username: u.id for u in s.query(User).all()}
    # 刪 user 可以
    assert client.delete("/api/users/%d" % ids["u1"]).status_code == 200
    # 刪一個 super（還有另一個）可以
    assert client.delete("/api/users/%d" % ids["s1"]).status_code == 200
    # 剩最後一個 super → 擋下
    r = client.delete("/api/users/%d" % ids["s2"])
    assert r.status_code == 400 and "最後一個" in r.json()["detail"]


def test_login_error_messages(client, engine, monkeypatch):
    from webvuln import config
    client.post("/api/users/local", json={"username": "u", "password": "test-1234", "role": "user"})
    monkeypatch.setattr(config, "NO_AUTH", False)
    # 密碼錯
    r = client.post("/api/login", json={"username": "u", "password": "nope"})
    assert r.status_code == 401 and r.json()["detail"] == "密碼錯誤"
    # 查無此帳號（本地模式、無 AD）
    r2 = client.post("/api/login", json={"username": "ghost", "password": "x"})
    assert r2.status_code == 401 and "查無" in r2.json()["detail"]

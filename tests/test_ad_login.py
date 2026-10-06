"""AD 登入流程（不連真 LDAP：把 ad._bind／_lookup_attrs 換成假的）。

迴歸：V2.62 起 authenticate_ad_ex 改回 (User, 原因)，但「綁定成功」與「帳號停用」兩條路
仍回單一值 → api_login 解包失敗 → 500，所有 AD 帳號都登不進來（2026-10-06 同事回報）。
"""
from webvuln import ad, appsettings, config
from webvuln.models import User


class _FakeConn:
    def unbind(self):
        pass


def _enable_ad(monkeypatch, bind_ok=True):
    monkeypatch.setattr(appsettings, "get_ad_config", lambda s: {"enabled": True, "base_dn": "dc=x"})
    monkeypatch.setattr(ad, "_bind", lambda cfg, login, pw: (True, _FakeConn(), None) if bind_ok
                        else (False, None, "綁定失敗（帳號／密碼或伺服器設定）"))
    monkeypatch.setattr(ad, "_lookup_attrs", lambda cfg, conn, login: {"name": "王小明", "department": "資訊部"})


def test_ad_login_success_first_time(client, session, monkeypatch):
    monkeypatch.setattr(config, "NO_AUTH", False)
    _enable_ad(monkeypatch)
    r = client.post("/api/login", json={"username": "A12345", "password": "pw"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "王小明"


def test_ad_login_success_existing_user(client, session, monkeypatch):
    monkeypatch.setattr(config, "NO_AUTH", False)
    _enable_ad(monkeypatch)
    assert client.post("/api/login", json={"username": "A12345", "password": "pw"}).status_code == 200
    r = client.post("/api/login", json={"username": "A12345", "password": "pw"})   # 第二次＝既有帳號那條路
    assert r.status_code == 200, r.text


def test_ad_login_disabled_user_gets_reason_not_500(client, session, monkeypatch):
    monkeypatch.setattr(config, "NO_AUTH", False)
    _enable_ad(monkeypatch)
    session.add(User(username="B999", password_hash=None, display_name="停用者", role=config.ROLE_USER, is_active=False))
    session.commit()
    r = client.post("/api/login", json={"username": "B999", "password": "pw"})
    assert r.status_code == 401
    assert "停用" in r.json()["detail"]


def test_ad_login_bind_fail_reason(client, monkeypatch):
    monkeypatch.setattr(config, "NO_AUTH", False)
    _enable_ad(monkeypatch, bind_ok=False)
    r = client.post("/api/login", json={"username": "A12345", "password": "bad"})
    assert r.status_code == 401
    assert "綁定失敗" in r.json()["detail"]

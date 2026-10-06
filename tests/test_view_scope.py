"""登入者能看的範圍（2026-10-06：AD 登入看週報，應該只看到自己的弱點）。
一般使用者＝負責人是自己；部門窗口＝自己部門；Super Admin＝全部。"""
from sqlalchemy.orm import Session, sessionmaker

from webvuln import config, security


def _import(client):
    fs = [dict(sheet_key="s1", plugin_id=str(100 + i), name=f"弱點{i}", host=f"10.30.5.{i}",
               severity="High", department="資訊架構部" if i < 4 else "網路部",
               owner="段譽" if i < 2 else ("喬峰" if i < 4 else "虛竹"),
               remediation_due="2026-07-19", close_status="未結案") for i in range(5)]
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": fs}).status_code == 200


def _users(engine):
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with f() as s:
        security.create_user(s, "u1", "pw12345", role="user", display_name="段譽")
        security.create_user(s, "d1", "pw12345", role="dept_admin", display_name="窗口", department="資訊架構部")
        security.create_user(s, "s1", "pw12345", role="super_admin", display_name="管理員")
        security.create_user(s, "u2", "pw12345", role="user", display_name="沒負責任何弱點的人")


def _login(client, u):
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": u, "password": "pw12345"}).status_code == 200


def test_view_scope_by_role(client, engine, monkeypatch):
    _import(client)
    _users(engine)
    monkeypatch.setattr(config, "NO_AUTH", False)

    _login(client, "u1")   # 一般使用者：只看自己負責的 2 筆
    rows = client.get("/api/findings").json()
    assert sorted(r["owner"] for r in rows) == ["段譽", "段譽"]
    rep = client.get("/api/report").json()
    assert rep["unresolved"] == 2

    _login(client, "d1")   # 部門窗口：自己部門 4 筆
    assert len(client.get("/api/findings").json()) == 4

    _login(client, "s1")   # Super Admin：全部 5 筆
    assert len(client.get("/api/findings").json()) == 5

    _login(client, "u2")   # 名下沒有弱點：0 筆，不是看到全部
    assert client.get("/api/findings").json() == []


def test_open_mode_not_scoped(client):
    _import(client)        # 免登入模式（測試預設）：不限範圍
    assert len(client.get("/api/findings").json()) == 5

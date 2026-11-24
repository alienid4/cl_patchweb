import datetime as dt

from webvuln import appsettings, config, importer, mailer, security
from webvuln.models import User
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)
CFG = {**appsettings.EMAIL_DEFAULTS, "enabled": True, "smtp_host": "relay.test",
       "include_overdue": True, "include_soon": False, "from_default": "sys@corp.test"}


def _load(session):
    findings = [
        # 喬峰：逾期(到期 04-01)→ 入清單；有帳號有信箱 → mode=send
        FindingIn(host="a", severity="High", owner="喬峰", department="資訊架構部",
                  remediation_due="2026-04-01", close_status="未結案"),
        FindingIn(host="b", severity="High", owner="喬峰", department="資訊架構部",
                  remediation_due="2026-04-05", close_status="未結案"),
        # 段譽：逾期；無帳號 → 轉部門窗口(資訊架構部的 dept_admin)
        FindingIn(host="c", severity="High", owner="段譽", department="資訊架構部",
                  remediation_due="2026-04-01", close_status="未結案"),
        # 阿朱：逾期；無帳號且其部門無窗口 → skip（無 global_fallback）
        FindingIn(host="d", severity="Low", owner="阿朱", department="資安部",
                  remediation_due="2026-04-01", close_status="未結案"),
        # 虛竹：未到期(到期 12-01) → 不入清單(只納逾期)
        FindingIn(host="e", severity="Low", owner="虛竹", department="資訊架構部",
                  remediation_due="2026-12-01", close_status="未結案"),
    ]
    return importer.create_batch(session, ImportIn(findings=findings))


def _users(session):
    session.add(User(username="01000001", display_name="喬峰", email="qiaofeng@corp.test",
                     department="資訊架構部", role=config.ROLE_USER, is_active=True))
    session.add(User(username="01000002", display_name="王語嫣", email="boss@corp.test",
                     department="資訊架構部", role=config.ROLE_DEPT_ADMIN, is_active=True))
    session.commit()


def test_build_plan_recipient_resolution(session):
    _load(session)
    _users(session)
    plan = mailer.build_plan(session, CFG, today=TODAY)
    by = {p["owner"]: p for p in plan}
    # 虛竹未逾期 → 不在計畫
    assert "虛竹" not in by
    # 喬峰：本人信箱命中
    assert by["喬峰"]["mode"] == "send"
    assert by["喬峰"]["to"] == "qiaofeng@corp.test"
    assert by["喬峰"]["count"] == 2
    # 段譽：查無本人 → 轉部門窗口
    assert by["段譽"]["mode"] == "fallback"
    assert by["段譽"]["to"] == "boss@corp.test"
    # 阿朱：查無本人、資安部無窗口、無系統預設 → skip
    assert by["阿朱"]["mode"] == "skip"
    assert by["阿朱"]["to"] is None


def test_build_plan_department_scope(session):
    _load(session)
    _users(session)
    plan = mailer.build_plan(session, CFG, department="資安部", today=TODAY)
    owners = {p["owner"] for p in plan}
    assert owners == {"阿朱"}   # 只算該部門


def test_send_plan_blocked_when_not_enabled(session):
    _load(session)
    cfg = {**CFG, "enabled": False}

    class _S:
        email = "me@corp.test"

    try:
        mailer.send_plan(session, cfg, _S(), today=TODAY)
        assert False, "should raise"
    except ValueError as e:
        assert "啟用" in str(e)


def test_send_plan_blocked_without_smtp(session):
    cfg = {**CFG, "smtp_host": ""}

    class _S:
        email = "me@corp.test"

    try:
        mailer.send_plan(session, cfg, _S(), today=TODAY)
        assert False, "should raise"
    except ValueError as e:
        assert "SMTP" in str(e)


def test_sender_from_falls_back_to_default(session):
    class _NoEmail:
        email = None

    assert mailer.sender_from(CFG, _NoEmail()) == "sys@corp.test"

    class _HasEmail:
        email = "me@corp.test"

    assert mailer.sender_from(CFG, _HasEmail()) == "me@corp.test"


def test_send_plan_sends_via_smtp(session, monkeypatch):
    _load(session)
    _users(session)
    sent = []

    class _FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent.append(("connect", host, port))

        def starttls(self):
            sent.append(("starttls",))

        def sendmail(self, frm, to, msg):
            sent.append(("sendmail", frm, tuple(to)))

        def quit(self):
            sent.append(("quit",))

    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)

    class _Sender:
        email = "operator@corp.test"

    summary = mailer.send_plan(session, {**CFG, "cc_self": True}, _Sender(), today=TODAY)
    assert summary["sent"] == 1        # 喬峰
    assert summary["fallback"] == 1    # 段譽→窗口
    assert summary["skipped"] == 1     # 阿朱
    # 寄出的信封寄件人＝操作者
    sendmails = [s for s in sent if s[0] == "sendmail"]
    assert all(s[1] == "operator@corp.test" for s in sendmails)


# ── 端點層（HTTP 接線＋權限閘門）──
def test_email_settings_roundtrip(client):
    """開放模式(＝super)可讀寫 Email 設定。"""
    got = client.get("/api/email-settings").json()
    assert got["enabled"] is False and got["smtp_port"] == 25
    saved = client.post("/api/email-settings", json={
        "enabled": True, "smtp_host": "relay.test", "smtp_port": "2525",
        "subject_prefix": "【催】"}).json()
    assert saved["enabled"] is True and saved["smtp_host"] == "relay.test"
    assert saved["smtp_port"] == 2525                      # 字串收斂成 int
    assert client.get("/api/email-settings").json()["subject_prefix"] == "【催】"


def test_send_preview_and_send_gate(client):
    client.post("/api/import", json={"findings": [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "severity": "High",
         "department": "資訊架構部", "owner": "喬峰",
         "remediation_due": "2020-01-01", "close_status": "未結案"}
    ]})
    # 未設定也能預覽（看得到計畫）
    pv = client.get("/api/send-reminders/preview").json()
    assert pv["enabled"] is False
    assert any(p["owner"] == "喬峰" for p in pv["plan"])
    # 未啟用 → 送出擋下 400
    client.post("/api/email-settings", json={"enabled": False, "smtp_host": "relay.test"})
    r = client.post("/api/send-reminders", json={})
    assert r.status_code == 400 and "啟用" in r.json()["detail"]


def test_send_role_gate(client, engine, monkeypatch):
    """非開放模式：一般 user 不能發送；dept_admin 只看自己部門。"""
    from sqlalchemy.orm import Session, sessionmaker
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "wang", "pw12345", role=config.ROLE_USER, display_name="王五")
        security.create_user(s, "deptA", "pw12345", role=config.ROLE_DEPT_ADMIN, department="甲部")
    client.post("/api/import", json={"findings": [
        {"host": "a", "plugin_id": "p", "sheet_key": "s", "department": "甲部", "owner": "張三",
         "remediation_due": "2020-01-01", "close_status": "未結案"},
        {"host": "b", "plugin_id": "p", "sheet_key": "s", "department": "乙部", "owner": "李四",
         "remediation_due": "2020-01-01", "close_status": "未結案"},
    ]})

    monkeypatch.setattr(config, "NO_AUTH", False)
    # 一般 user → 403
    client.post("/api/login", json={"username": "wang", "password": "pw12345"})
    assert client.get("/api/send-reminders/preview").status_code == 403
    client.post("/api/logout")
    # dept_admin → 只看自己部門(甲部→張三)，看不到乙部(李四)
    client.post("/api/login", json={"username": "deptA", "password": "pw12345"})
    pv = client.get("/api/send-reminders/preview").json()
    owners = {p["owner"] for p in pv["plan"]}
    assert owners == {"張三"}
    client.post("/api/logout")

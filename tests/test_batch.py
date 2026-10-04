import datetime as dt

from webvuln import appsettings, config, importer, mailer, security
from webvuln.models import Attachment, MailLog, User
from webvuln.schemas import FindingIn, ImportIn

TODAY = dt.date(2026, 5, 10)


def _import(client, rows):
    return client.post("/api/import", json={"findings": rows})


def _findings(client, **q):
    return client.get("/api/findings", params=q).json()


# ── 批次改狀態 ──
def test_bulk_overlay_applies_to_selected(client):
    _import(client, [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "owner": "林楚彥",
         "department": "資訊架構部", "remediation_due": "2026-04-01", "close_status": "未結案"},
        {"host": "h2", "plugin_id": "p1", "sheet_key": "s", "owner": "林楚彥",
         "department": "資訊架構部", "remediation_due": "2026-04-01", "close_status": "未結案"},
    ])
    ids = [f["id"] for f in _findings(client)]
    r = client.post("/api/findings/bulk-overlay",
                    json={"ids": ids, "set_progress": True, "progress": "要申請展延"})
    assert r.status_code == 200
    j = r.json()
    assert j["applied"] == 2 and not j["skipped"] and not j["failed"]
    # 兩筆都標上了
    got = _findings(client)
    assert all(f["progress"] == "要申請展延" for f in got)


def test_bulk_overlay_needs_fields_and_ids(client):
    _import(client, [{"host": "h", "plugin_id": "p", "sheet_key": "s", "close_status": "未結案"}])
    ids = [f["id"] for f in _findings(client)]
    assert client.post("/api/findings/bulk-overlay", json={"ids": ids}).status_code == 400  # 無欄位
    assert client.post("/api/findings/bulk-overlay",
                       json={"ids": [], "set_progress": True, "progress": "處理中"}).status_code == 400


# ── 批次上傳：一份檔案掛多筆，實體檔去重只一份 ──
def test_bulk_attachment_one_file_many_findings(client, engine):
    _import(client, [
        {"host": "h1", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"},
        {"host": "h2", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"},
        {"host": "h3", "plugin_id": "p1", "sheet_key": "s", "close_status": "未結案"},
    ])
    ids = [f["id"] for f in _findings(client)]
    r = client.post("/api/attachments/bulk?ids=%s&name=WBS.xlsx&kind=WBS" % ",".join(map(str, ids)),
                    content=b"PK\x03\x04 fake xlsx bytes")
    assert r.status_code == 200
    assert r.json()["applied"] == 3
    # 3 筆 metadata、但實體檔（stored_name）只 1 個（sha256 去重）
    from sqlalchemy.orm import Session, sessionmaker
    fac = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with fac() as s:
        atts = s.query(Attachment).all()
        assert len(atts) == 3
        assert len({a.stored_name for a in atts}) == 1


# ── 我的操作紀錄 ──
def test_my_activity_records_actions(client):
    _import(client, [{"host": "h", "plugin_id": "p", "sheet_key": "s",
                      "owner": "林楚彥", "close_status": "未結案"}])
    fid = _findings(client)[0]["id"]
    client.post(f"/api/findings/{fid}/overlay", json={"set_progress": True, "progress": "處理中"})
    acts = client.get("/api/my-activity").json()
    labels = [a["action_label"] for a in acts["items"]]
    assert "匯入彙總表" in labels
    assert "變更弱點（負責人／備註／預計完成／進度）" in labels


# ── 一般設定：session 時數影響新登入的到期 ──
def test_general_settings_session_ttl(client, engine, monkeypatch):
    r = client.post("/api/general-settings", json={"session_ttl_hours": 8})
    assert r.status_code == 200 and r.json()["session_ttl_hours"] == 8
    from sqlalchemy.orm import Session, sessionmaker
    fac = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with fac() as s:
        assert security.session_ttl_hours(s) == 8
    # 範圍夾限：0 → 夾成 1；9999 → 夾成 720
    assert client.post("/api/general-settings", json={"session_ttl_hours": 0}).json()["session_ttl_hours"] == 1
    assert client.post("/api/general-settings", json={"session_ttl_hours": 9999}).json()["session_ttl_hours"] == 720


# ── 發信紀錄：寄送後 MailLog 有資料 ──
def test_mail_log_populated_after_send(client, engine, monkeypatch):
    _import(client, [{"host": "h", "plugin_id": "p", "sheet_key": "s", "owner": "張三",
                      "department": "甲部", "remediation_due": "2020-01-01", "close_status": "未結案"}])
    client.post("/api/email-settings", json={"enabled": True, "smtp_host": "relay.local",
                                             "from_default": "sys@corp.test"})

    class FakeSMTP:
        def __init__(s, h, p, timeout=0): pass
        def starttls(s): pass
        def sendmail(s, f, t, m): pass
        def quit(s): pass
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    client.post("/api/send-reminders", json={})
    log = client.get("/api/mail-log").json()
    assert len(log) >= 1
    assert log[0]["owner"] == "張三"


# ── #8 每週排程：只寄給有開啟且有信箱的部門窗口 ──
def test_send_weekly_targets_optin_admins(session, monkeypatch):
    importer.create_batch(session, ImportIn(findings=[
        FindingIn(host="a", owner="員A", department="甲部",
                  remediation_due="2020-01-01", close_status="未結案"),
    ]))
    session.add(User(username="w1", display_name="窗口甲", email="admin-a@corp.test",
                     department="甲部", role=config.ROLE_DEPT_ADMIN, is_active=True,
                     weekly_report=True))
    session.add(User(username="w2", display_name="窗口乙", email="admin-b@corp.test",
                     department="乙部", role=config.ROLE_DEPT_ADMIN, is_active=True,
                     weekly_report=False))   # 沒開 → 不寄
    session.add(User(username="w3", display_name="窗口丙", email=None,
                     department="丙部", role=config.ROLE_DEPT_ADMIN, is_active=True,
                     weekly_report=True))    # 開了但沒信箱 → 略過
    session.commit()
    sent = []

    class FakeSMTP:
        def __init__(s, h, p, timeout=0): pass
        def starttls(s): pass
        def sendmail(s, f, t, m): sent.append(tuple(t))
        def quit(s): pass
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    cfg = {**appsettings.EMAIL_DEFAULTS, "enabled": True, "smtp_host": "relay",
           "from_default": "sys@corp.test"}
    summary = mailer.send_weekly(session, cfg, today=TODAY)
    assert summary["sent"] == 1 and summary["skipped"] == 1
    assert sent == [("admin-a@corp.test",)]


def test_copy_to_bcc_on_send(session, monkeypatch):
    importer.create_batch(session, ImportIn(findings=[
        FindingIn(host="a", owner="員A", department="甲部",
                  remediation_due="2020-01-01", close_status="未結案"),
    ]))
    session.add(User(username="u", display_name="員A", email="a@corp.test",
                     department="甲部", role=config.ROLE_USER, is_active=True))
    session.commit()
    env = []

    class FakeSMTP:
        def __init__(s, h, p, timeout=0): pass
        def starttls(s): pass
        def sendmail(s, f, t, m): env.append(tuple(t))
        def quit(s): pass
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    cfg = {**appsettings.EMAIL_DEFAULTS, "enabled": True, "smtp_host": "relay",
           "from_default": "sys@corp.test", "cc_self": False, "copy_to": "backup@corp.test"}

    class S:
        username = "op"
        email = "op@corp.test"
    mailer.send_plan(session, cfg, S(), today=TODAY)
    # 信封應含收件人 + 總備份信箱
    assert any("backup@corp.test" in t for t in env)

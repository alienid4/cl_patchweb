"""每週自動寄信排程（2026-10-27）：設定驗證、組信內容、排程時間判斷、防重寄、立即寄出、測試寄送。"""
import datetime as dt

import pytest

from webvuln import appsettings, automail, config, importer
from webvuln.models import MailLog, User
from webvuln.schemas import FindingIn, ImportIn

MON = dt.datetime(2026, 10, 26, 8, 0)   # 2026-W44 週一
TODAY = MON.date()
DEPT = "資訊架構部"


def _d(base, n):
    return (base + dt.timedelta(days=n)).isoformat()


def _load(session, base=TODAY):
    """到期日以 base 為準（API 測試用真實今天，避免日期漂移）。"""
    findings = [
        # 喬峰（有信箱）：已逾期＋45 天內 → 入；>60 天、已結案 → 不入
        FindingIn(host="a1", plugin_id="p1", sheet_key="s", severity="High", owner="喬峰", department=DEPT,
                  remediation_due=_d(base, -25), close_status="未結案"),
        FindingIn(host="a2", plugin_id="p2", sheet_key="s", severity="Medium", owner="喬峰", department=DEPT,
                  remediation_due=_d(base, 45), close_status="未結案"),
        FindingIn(host="a3", plugin_id="p3", sheet_key="s", severity="Low", owner="喬峰", department=DEPT,
                  remediation_due=_d(base, 126), close_status="未結案"),
        FindingIn(host="a4", plugin_id="p4", sheet_key="s", severity="High", owner="喬峰", department=DEPT,
                  remediation_due=_d(base, -16), close_status="已結案"),
        # 段譽：無帳號 → 轉部門窗口
        FindingIn(host="b1", plugin_id="p1", sheet_key="s", severity="High", owner="段譽", department=DEPT,
                  remediation_due=_d(base, 20), close_status="未結案"),
        # 虛竹：只有 >60 天 → 不寄
        FindingIn(host="c1", plugin_id="p1", sheet_key="s", severity="Low", owner="虛竹", department=DEPT,
                  remediation_due=_d(base, 218), close_status="未結案"),
        # 資安部：沒勾選 → 什麼都不寄
        FindingIn(host="d1", plugin_id="p1", sheet_key="s", severity="High", owner="阿朱", department="資安部",
                  remediation_due=_d(base, -25), close_status="未結案"),
    ]
    b = importer.create_batch(session, ImportIn(findings=findings))
    b.imported_at = dt.datetime.combine(base, dt.time(7, 0))
    session.commit()
    session.add(User(username="u1", display_name="喬峰", email="qiaofeng@corp.test", department=DEPT,
                     role=config.ROLE_USER, is_active=True))
    session.add(User(username="u2", display_name="王語嫣", email="boss@corp.test", department=DEPT,
                     role=config.ROLE_DEPT_ADMIN, is_active=True))
    session.add(User(username="u3", display_name="阿紫", email="sec@corp.test", department="資安部",
                     role=config.ROLE_DEPT_ADMIN, is_active=True))
    session.commit()
    appsettings.set_email_config(session, {"enabled": True, "smtp_host": "relay.test",
                                           "from_default": "sys@corp.test", "copy_to": "archive@corp.test"})


def _settings(session, **kw):
    base = {"enabled": True, "departments": [
        {"department": DEPT, "enabled": True, "cc": "lead@corp.test, mgr@corp.test",
         "send_inventory": True, "send_dunning": True},
        {"department": "資安部", "enabled": False, "cc": [], "send_inventory": True, "send_dunning": True},
    ]}
    base.update(kw)
    return automail.save_settings(session, base, now=MON - dt.timedelta(days=7))


@pytest.fixture
def smtp(monkeypatch):
    sent = []

    class _Fake:
        def __init__(self, host, port, timeout=0):
            pass

        def starttls(self):
            pass

        def sendmail(self, frm, to, msg):
            sent.append({"from": frm, "to": list(to), "msg": msg})

        def quit(self):
            pass

    monkeypatch.setattr(automail.smtplib, "SMTP", _Fake)
    return sent


def _subject(m):
    import email
    from email.header import decode_header, make_header
    return str(make_header(decode_header(email.message_from_string(m["msg"])["Subject"])))


def _body(m):
    import email
    return email.message_from_string(m["msg"]).get_payload(decode=True).decode("utf-8")


# ── 設定驗證 ──
def test_settings_validation(session):
    _load(session)
    cfg = _settings(session)
    assert cfg["departments"][0]["cc"] == ["lead@corp.test", "mgr@corp.test"]
    assert cfg["enabled_at"]
    with pytest.raises(ValueError, match="CC"):
        automail.save_settings(session, {"departments": [{"department": DEPT, "enabled": True, "cc": "not-an-email"}]})
    with pytest.raises(ValueError, match="部門不存在"):
        automail.save_settings(session, {"departments": [{"department": "不存在部", "enabled": True}]})
    with pytest.raises(ValueError, match="HH:MM"):
        automail.save_settings(session, {"time": "25:00"})
    with pytest.raises(ValueError, match="補寄"):
        automail.save_settings(session, {"time": "09:00", "catchup_until": "08:00"})
    with pytest.raises(ValueError, match="測試"):
        automail.save_settings(session, {"test_mode": True, "test_addresses": ""})
    with pytest.raises(ValueError, match="測試"):
        automail.save_settings(session, {"test_mode": True, "test_addresses": "a@b.test, bad"})
    ok = automail.save_settings(session, {"test_mode": True, "test_addresses": "a@b.test\nc@d.test"})
    assert ok["test_addresses"] == ["a@b.test", "c@d.test"]
    # 驗證失敗不改到已存的設定
    assert automail.get_settings(session)["time"] == "08:00"


# ── 組信內容 ──
def test_preview_content(session):
    _load(session)
    _settings(session)
    pv = automail.preview(session, MON)
    jobs = pv["jobs"]
    assert {j["department"] for j in jobs} == {DEPT}            # 資安部沒勾 → 沒有
    inv = [j for j in jobs if j["kind"] == "inventory"]
    assert len(inv) == 1 and inv[0]["to"] == ["boss@corp.test"]
    assert inv[0]["cc"] == ["lead@corp.test", "mgr@corp.test"]
    assert "需申請" in inv[0]["body"] and "暫無需申請" in inv[0]["body"] and "待追查" in inv[0]["body"]
    dun = {j["label"]: j for j in jobs if j["kind"] == "dunning"}
    assert set(dun) == {"喬峰", "段譽"}                          # 虛竹 >60 天、無項目 → 無信
    q = dun["喬峰"]
    assert q["to"] == ["qiaofeng@corp.test"] and q["count"] == 2   # 逾期＋45 天；>60 天、已結案不算
    assert [i["host"] for i in q["items"]] == ["a1", "a2"]          # 依到期日
    assert q["items"][0]["due_text"].startswith("逾期")
    assert q["cc"] == ["lead@corp.test", "mgr@corp.test"]
    d = dun["段譽"]
    assert d["mode"] == "fallback" and d["to"] == ["boss@corp.test"]
    assert d["cc"] == ["lead@corp.test", "mgr@corp.test"]
    assert pv["totals"]["mails"] == 3


def test_inventory_stale_data_warning(session):
    _load(session)
    _settings(session)
    later = MON + dt.timedelta(days=20)
    inv = [j for j in automail.preview(session, later)["jobs"] if j["kind"] == "inventory"][0]
    assert "超過 14 天未更新" in inv["body"]


# ── 排程時間判斷 ──
def test_decide_times():
    cfg = {"enabled": True, "weekday": 0, "time": "08:00", "catchup_until": "12:00", "paused_week": ""}
    at = lambda h, m, day=26: dt.datetime(2026, 10, day, h, m)
    assert automail.decide(cfg, at(7, 59), None)[0] == "idle"
    assert automail.decide(cfg, at(8, 0), None)[0] == "send"
    assert automail.decide(cfg, at(11, 59), None)[0] == "send"
    assert automail.decide(cfg, at(12, 1), None)[0] == "missed"
    assert automail.decide(cfg, at(9, 0, day=27), None)[0] != "send"            # 週二
    assert automail.decide({**cfg, "paused_week": "2026-W44"}, at(8, 0), None)[0] == "idle"
    assert automail.decide(cfg, at(8, 30), "sent")[0] == "idle"                  # 本週已寄
    assert automail.decide({**cfg, "enabled": False}, at(8, 0), None)[0] == "idle"
    # 排程時間後才開啟 → 不算漏寄
    assert automail.decide({**cfg, "enabled_at": "2026-10-26T13:00:00"}, at(14, 0), None)[0] == "idle"


def test_tick_missed_recorded_once(session, smtp):
    _load(session)
    _settings(session)
    r1 = automail.tick(session, MON.replace(hour=12, minute=1))
    r2 = automail.tick(session, MON.replace(hour=12, minute=2))
    r3 = automail.tick(session, MON + dt.timedelta(days=1))
    assert r1["action"] == "missed" and r2["action"] == "idle" and r3["action"] == "idle"
    st = automail.get_state(session)
    assert len(st["missed"]) == 1 and st["missed"][0]["week"] == "2026-W44"
    assert not smtp
    from webvuln.models import AuditLog
    assert session.query(AuditLog).filter_by(action="auto_mail_missed").count() == 1


def test_tick_sends_once_and_logs(session, smtp):
    _load(session)
    _settings(session)
    assert automail.tick(session, MON.replace(hour=7, minute=59))["action"] == "idle"
    r = automail.tick(session, MON)
    assert r["action"] == "send" and r["result"]["sent"] == 3
    # 同週再跑（另一執行緒／重啟後）→ 不再寄
    assert automail.tick(session, MON.replace(hour=8, minute=1))["action"] == "idle"
    assert len(smtp) == 3
    # 收件人＋CC＋總備份（bcc）都在信封
    q = [m for m in smtp if "qiaofeng@corp.test" in m["to"]][0]
    assert set(q["to"]) == {"qiaofeng@corp.test", "lead@corp.test", "mgr@corp.test", "archive@corp.test"}
    assert "60 天內到期" in _subject(q)
    logs = session.query(MailLog).all()
    assert len(logs) == 3 and {m.mode for m in logs} == {"inventory", "dunning"}
    st = automail.get_state(session)
    assert st["last_run"]["week"] == "2026-W44" and st["last_run"]["sent"] == 3
    assert automail.claim_status(session, "2026-W44") == "sent"


def test_double_claim_protection(engine, session, smtp):
    """兩個連線（模擬兩個執行緒／程序）同一週各跑一次 → 只有一個寄出。"""
    from sqlalchemy.orm import Session, sessionmaker
    _load(session)
    _settings(session)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as a, factory() as b:
        assert automail.try_claim(a, "2026-W44", "running") is True
        assert automail.try_claim(b, "2026-W44", "running") is False
    # 已認領 → tick 不寄
    assert automail.tick(session, MON)["action"] == "idle"
    assert not smtp


# ── 立即寄出 ──
def test_run_now_confirm_and_force(client, session, smtp):
    _load(session, dt.date.today())
    _settings(session)
    assert client.post("/api/auto-mail/run-now", json={}).status_code == 400
    r = client.post("/api/auto-mail/run-now", json={"confirm": True})
    assert r.status_code == 200 and r.json()["sent"] == 3
    # 本週已寄 → 再按要 force
    r2 = client.post("/api/auto-mail/run-now", json={"confirm": True})
    assert r2.status_code == 409
    r3 = client.post("/api/auto-mail/run-now", json={"confirm": True, "force": True})
    assert r3.status_code == 200 and len(smtp) == 6
    # 立即寄出佔用本週 → 排程本週不再寄
    week = automail.iso_week(dt.date.today())
    assert automail.claim_status(session, week) == "sent"


def test_run_now_after_missed_allowed(session, smtp):
    _load(session)
    _settings(session)
    automail.tick(session, MON.replace(hour=13))
    assert automail.claim_status(session, "2026-W44") == "missed"
    s = automail.run_now(session, MON.replace(hour=14), "admin")
    assert s["sent"] == 3 and automail.claim_status(session, "2026-W44") == "sent"


# ── 測試寄送 ──
def test_sample_test_only_to_given_addresses(client, session, smtp):
    _load(session, dt.date.today())
    _settings(session)
    r = client.post("/api/auto-mail/test", json={"to": "me@corp.test, you@corp.test", "department": DEPT})
    assert r.status_code == 200 and r.json()["sent"] == 2      # 盤點週報＋一封催辦
    for m in smtp:
        assert m["to"] == ["me@corp.test", "you@corp.test"]
        assert _subject(m).startswith("[測試]")
        assert "[測試] 正式寄送時：收件人" in _body(m)
    assert {m.mode for m in session.query(MailLog).all()} == {"test"}
    assert client.post("/api/auto-mail/test", json={"to": "bad", "department": DEPT}).status_code == 400
    assert client.post("/api/auto-mail/test", json={"department": DEPT}).status_code == 400   # 免登入無本人信箱


def test_test_run_redirects_all(client, session, smtp):
    _load(session, dt.date.today())
    _settings(session)
    r = client.post("/api/auto-mail/test-run", json={"to": "tester@corp.test"})
    assert r.status_code == 200 and r.json()["sent"] == 3
    real = {"boss@corp.test", "qiaofeng@corp.test", "lead@corp.test", "mgr@corp.test", "archive@corp.test"}
    for m in smtp:
        assert m["to"] == ["tester@corp.test"] and not (set(m["to"]) & real)
        assert _subject(m).startswith("[測試]")
    assert any("CC lead@corp.test、mgr@corp.test" in _body(m) for m in smtp)
    # 不佔用本週、不改 last_run → 週一正式寄送照常
    st = automail.get_state(session)
    assert st["last_run"] is None and st["history"][0]["trigger"] == "test_run"
    assert automail.claim_status(session, automail.iso_week(dt.date.today())) is None


def test_scheduled_run_in_test_mode_redirects(session, smtp):
    _load(session)
    _settings(session, test_mode=True, test_addresses="t1@corp.test\nt2@corp.test")
    r = automail.tick(session, MON)
    assert r["action"] == "send" and r["result"]["sent"] == 3
    for m in smtp:
        assert m["to"] == ["t1@corp.test", "t2@corp.test"]
        assert _subject(m).startswith("[測試]")
    assert automail.get_state(session)["last_run"]["test_mode"] is True
    assert automail.claim_status(session, "2026-W44") == "test"


# ── 端點 ──
def test_endpoints_get_put_preview_pause(client, session):
    _load(session, dt.date.today())
    v = client.get("/api/auto-mail").json()
    assert v["settings"]["enabled"] is False and v["next_run"] is None and "server_now" in v
    r = client.put("/api/auto-mail", json={"enabled": True, "departments": [
        {"department": DEPT, "enabled": True, "cc": "x@corp.test", "send_inventory": True, "send_dunning": False}]})
    assert r.status_code == 200 and r.json()["next_run"]
    assert client.put("/api/auto-mail", json={"dunning_days": 0}).status_code == 400
    pv = client.post("/api/auto-mail/preview", json={}).json()
    assert [j["kind"] for j in pv["jobs"]] == ["inventory"]
    p = client.post("/api/auto-mail/pause-week", json={}).json()
    assert p["settings"]["paused_week"] == p["current_week"]
    assert client.post("/api/auto-mail/pause-week", json={"week": ""}).json()["settings"]["paused_week"] == ""

# ── 整批寄不出 → 補寄截止前重試；寄送中卡住 → 標中斷（2026-10-27 獨立測試回饋）──
def test_smtp_down_retries_until_window_then_error(session, monkeypatch):
    _load(session)
    _settings(session)
    state = {"down": True, "sent": 0}

    class _Fake:
        def __init__(self, *a, **k):
            if state["down"]:
                raise OSError("connection refused")

        def sendmail(self, *a, **k):
            state["sent"] += 1

        def quit(self):
            pass

    monkeypatch.setattr(automail.smtplib, "SMTP", _Fake)
    r = automail.tick(session, MON)
    assert r["result"]["sent"] == 0 and automail.claim_status(session, "2026-W44") == "retry"
    assert automail.tick(session, MON.replace(minute=5))["action"] == "idle"          # 未滿 10 分鐘
    assert automail.tick(session, MON.replace(minute=10))["action"] == "send"         # 重試，仍失敗
    state["down"] = False
    r = automail.tick(session, MON.replace(minute=20))
    assert r["action"] == "send" and r["result"]["sent"] == 3 and state["sent"] == 3
    assert automail.claim_status(session, "2026-W44") == "sent"
    assert automail.tick(session, MON.replace(minute=40))["action"] == "idle"


def test_smtp_down_past_window_records_once(session, monkeypatch):
    _load(session)
    _settings(session)

    class _Down:
        def __init__(self, *a, **k):
            raise OSError("down")

    monkeypatch.setattr(automail.smtplib, "SMTP", _Down)
    automail.tick(session, MON.replace(hour=11, minute=55))
    assert automail.tick(session, MON.replace(hour=12, minute=5))["action"] == "missed"
    assert automail.tick(session, MON.replace(hour=13))["action"] == "idle"
    assert automail.claim_status(session, "2026-W44") == "error"
    assert len(automail.get_state(session)["missed"]) == 1


def test_stale_running_marked_interrupted_once(session, smtp):
    _load(session)
    _settings(session)
    automail.try_claim(session, "2026-W44", "running", MON)
    assert automail.tick(session, MON.replace(minute=20))["action"] == "idle"
    assert automail.tick(session, MON.replace(minute=31))["action"] == "interrupted"
    assert automail.tick(session, MON.replace(minute=40))["action"] == "idle"
    assert automail.claim_status(session, "2026-W44") == "interrupted"
    assert len(automail.get_state(session)["missed"]) == 1 and not smtp
    with pytest.raises(ValueError):
        automail.run_now(session, MON.replace(hour=9), "admin")   # 不自動重寄，要強制

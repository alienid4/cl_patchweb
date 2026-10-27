"""獨立測試：每週自動寄信排程（2026-10-27，由非作者撰寫）。

全程用假 SMTP（smtplib.SMTP / SMTP_SSL 全域替換），並把 socket.create_connection 擋掉，
確保任何路徑都不可能連到真的郵件主機。發現的 bug 以 xfail(strict=True) 標記。
"""
import datetime as dt
import email
import threading
from email.header import decode_header, make_header

import pytest
from sqlalchemy.orm import Session, sessionmaker

from webvuln import appsettings, automail, cases, config, importer, query, security
from webvuln.models import AuditLog, Finding, MailLog, User
from webvuln.schemas import FindingIn, ImportIn

MON = dt.datetime(2026, 10, 26, 8, 0)      # 2026-W44 週一
DEPT = "資訊架構部"
DEPT2 = "資安部"
DEPT3 = "網路部"

REAL = {"boss@corp.test", "qiaofeng@corp.test", "lead@corp.test", "mgr@corp.test",
        "archive@corp.test", "sec@corp.test", "seccc@corp.test", "fallback@corp.test",
        "weekly@corp.test", "net@corp.test", "netcc@corp.test"}
TESTS = ["t1@corp.test", "t2@corp.test"]


# ── 假 SMTP ＋ 全面封鎖真網路 ──
@pytest.fixture
def smtp(monkeypatch):
    import smtplib
    import socket
    sent = _Rec()
    state = {"fail_on": None, "connect_error": None, "n": 0}

    class _Fake:
        def __init__(self, host="", port=0, *a, **kw):
            if state["connect_error"]:
                raise state["connect_error"]
            self.host = host

        def starttls(self, *a, **kw):
            pass

        def login(self, *a, **kw):
            pass

        def ehlo(self, *a, **kw):
            pass

        def _rec(self, frm, to, raw):
            state["n"] += 1
            if state["fail_on"] and state["n"] == state["fail_on"]:
                raise smtplib.SMTPRecipientsRefused({to[0]: (550, b"boom")})
            m = email.message_from_string(raw)
            sent.append({"from": frm, "env": list(to), "msg": m, "raw": raw})

        def sendmail(self, frm, to, msg, *a, **kw):
            self._rec(frm, list(to) if not isinstance(to, str) else [to], msg)

        def send_message(self, msg, from_addr=None, to_addrs=None, *a, **kw):
            to = to_addrs or [x for h in ("To", "Cc", "Bcc") for x in (msg.get_all(h) or [])]
            self._rec(from_addr, list(to), msg.as_string())

        def quit(self):
            pass

        def close(self):
            pass

    def _no_net(*a, **kw):
        raise AssertionError("real network connection attempted: %r" % (a,))

    monkeypatch.setattr(smtplib, "SMTP", _Fake)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Fake)
    monkeypatch.setattr(socket, "create_connection", _no_net)
    sent.state = state
    return sent


class _Rec(list):
    state: dict


def _hdr(m, name):
    v = m["msg"].get(name)
    if v is None:
        return ""
    return str(make_header(decode_header(v)))


def _addrs_in_headers(m):
    out = set()
    for h in ("To", "Cc", "Bcc"):
        for part in _hdr(m, h).replace(";", ",").split(","):
            if part.strip():
                out.add(part.strip())
    return out


def _body(m):
    return m["msg"].get_payload(decode=True).decode("utf-8")


def _assert_only_test(smtp, allowed):
    assert len(smtp) > 0
    for m in smtp:
        assert set(m["env"]) <= set(allowed), m["env"]
        assert _addrs_in_headers(m) <= set(allowed), _addrs_in_headers(m)
        assert not m["msg"].get("Cc") and not m["msg"].get("Bcc")
        assert _hdr(m, "Subject").startswith("[測試]")
        assert not (set(m["env"]) & REAL)


# ── 資料 ──
def _d(base, n):
    return (base + dt.timedelta(days=n)).isoformat()


def _load(session, base=MON.date(), extra=()):
    F = lambda **k: FindingIn(sheet_key="s", close_status=k.pop("close_status", "未結案"), **k)  # noqa: E731
    findings = [
        F(host="a1", plugin_id="p1", severity="High", owner="喬峰", department=DEPT, remediation_due=_d(base, -100)),
        F(host="a2", plugin_id="p2", severity="Medium", owner="喬峰", department=DEPT, remediation_due=_d(base, 60)),
        F(host="a3", plugin_id="p3", severity="Low", owner="喬峰", department=DEPT, remediation_due=_d(base, 61)),
        F(host="a4", plugin_id="p4", severity="High", owner="喬峰", department=DEPT, remediation_due=_d(base, -5),
          close_status="已結案"),
        F(host="a5", plugin_id="p5", severity="High", owner="喬峰", department=DEPT, remediation_due=_d(base, 0)),
        F(host="a6", plugin_id="p6", severity="High", owner="喬峰", department=DEPT),   # 無到期日
        F(host="b1", plugin_id="p1", severity="High", owner="段譽", department=DEPT, remediation_due=_d(base, 20)),
        F(host="n1", plugin_id="p1", severity="High", owner="", department=DEPT, remediation_due=_d(base, 3)),
        F(host="d1", plugin_id="p1", severity="High", owner="阿朱", department=DEPT2, remediation_due=_d(base, -25)),
        F(host="x1", plugin_id="p1", severity="High", owner="蕭遠山", department=DEPT3, remediation_due=_d(base, -1)),
    ] + list(extra)
    b = importer.create_batch(session, ImportIn(findings=findings))
    b.imported_at = dt.datetime.combine(base, dt.time(7, 0))
    session.commit()
    U = lambda **k: User(is_active=k.pop("is_active", True), **k)  # noqa: E731
    session.add_all([
        U(username="u1", display_name="喬峰", email="qiaofeng@corp.test", department=DEPT, role=config.ROLE_USER),
        U(username="u2", display_name="王語嫣", email="boss@corp.test", department=DEPT, role=config.ROLE_DEPT_ADMIN),
        U(username="u3", display_name="阿紫", email="sec@corp.test", department=DEPT2, role=config.ROLE_DEPT_ADMIN),
        U(username="u4", display_name="週報人", email="weekly@corp.test", department=DEPT, role=config.ROLE_USER,
          weekly_report=True),
        U(username="u5", display_name="離職窗口", email="gone@corp.test", department=DEPT,
          role=config.ROLE_DEPT_ADMIN, is_active=False),
        U(username="u6", display_name="網管", email="net@corp.test", department=DEPT3, role=config.ROLE_DEPT_ADMIN),
    ])
    session.commit()
    appsettings.set_email_config(session, {"enabled": True, "smtp_host": "relay.invalid",
                                           "from_default": "sys@corp.test", "copy_to": "archive@corp.test",
                                           "global_fallback": "fallback@corp.test"})


def _settings(session, depts=None, **kw):
    base = {"enabled": True, "departments": depts if depts is not None else [
        {"department": DEPT, "enabled": True, "cc": "lead@corp.test, mgr@corp.test",
         "send_inventory": True, "send_dunning": True},
        {"department": DEPT2, "enabled": False, "cc": "seccc@corp.test", "send_inventory": True,
         "send_dunning": True},
    ]}
    base.update(kw)
    return automail.save_settings(session, base, now=MON - dt.timedelta(days=7))


# ═════ 1. 洩漏檢查 ═════
def test_leak_sample_test_endpoint(client, session, smtp):
    _load(session, dt.date.today())
    _settings(session)
    for dept in (DEPT, DEPT2, DEPT3):       # 含未勾選、未設定的部門
        r = client.post("/api/auto-mail/test", json={"to": "t1@corp.test;t2@corp.test", "department": dept})
        assert r.status_code == 200, r.text
    _assert_only_test(smtp, TESTS)
    # body 顯示正式收件人
    assert any("boss@corp.test" in _body(m) for m in smtp)


def test_leak_test_run_endpoint_including_disabled_dept(client, session, smtp):
    _load(session, dt.date.today())
    _settings(session)
    r = client.post("/api/auto-mail/test-run", json={"to": ["t1@corp.test", "t2@corp.test"],
                                                     "departments": [DEPT, DEPT2, DEPT3]})
    assert r.status_code == 200, r.text
    _assert_only_test(smtp, TESTS)
    # 未勾選部門不會進完整測試
    assert not any(DEPT2 in _hdr(m, "Subject") or "阿朱" in _hdr(m, "Subject") for m in smtp)
    assert automail.claim_status(session, automail.iso_week(dt.date.today())) is None


def test_leak_scheduled_test_mode(session, smtp):
    _load(session)
    _settings(session, test_mode=True, test_addresses=TESTS)
    r = automail.tick(session, MON)
    assert r["action"] == "send" and r["result"]["sent"] >= 3
    _assert_only_test(smtp, TESTS)


def test_leak_run_now_test_mode(session, smtp):
    _load(session)
    _settings(session, test_mode=True, test_addresses="t1@corp.test")
    automail.run_now(session, MON.replace(hour=9), "admin")
    _assert_only_test(smtp, ["t1@corp.test"])


def test_leak_test_mode_with_fallback_and_skipped(session, smtp):
    """負責人轉寄 global_fallback、部門無窗口時，測試模式仍只寄測試信箱。"""
    _load(session)
    _settings(session, depts=[{"department": DEPT3, "enabled": True, "cc": "netcc@corp.test"}],
              test_mode=True, test_addresses=TESTS)
    session.query(User).filter_by(username="u6").update({"email": None})
    session.commit()
    automail.tick(session, MON)
    _assert_only_test(smtp, TESTS)


def test_leak_test_mode_empty_addresses_never_real(session, smtp):
    _load(session)
    _settings(session, test_mode=True, test_addresses=TESTS)
    cfg = appsettings.get_json(session, automail.KEY)
    cfg["test_addresses"] = []
    appsettings.set_json(session, automail.KEY, cfg)
    automail.tick(session, MON)
    assert not any(set(m["env"]) & REAL for m in smtp)


def test_real_run_recipients_exact(session, smtp):
    _load(session)
    _settings(session)
    r = automail.tick(session, MON)
    assert r["action"] == "send"
    allowed_cc = {"lead@corp.test", "mgr@corp.test", "archive@corp.test"}
    for m in smtp:
        assert not m["msg"].get("Bcc")          # BCC 不能出現在標頭
        assert "archive@corp.test" not in _addrs_in_headers(m)
        assert "seccc@corp.test" not in m["env"] and "sec@corp.test" not in m["env"]   # 未勾選部門
        assert "net@corp.test" not in m["env"]
        to = set(_addrs_in_headers(m)) - allowed_cc
        assert set(m["env"]) - allowed_cc == to
    envs = [set(m["env"]) for m in smtp]
    assert {"boss@corp.test", "weekly@corp.test", "lead@corp.test", "mgr@corp.test", "archive@corp.test"} in envs
    assert all("gone@corp.test" not in e for e in envs)   # 停用帳號不收


def test_copy_to_multiple_addresses_split(session, smtp):
    _load(session)
    appsettings.set_email_config(session, {"copy_to": "arc1@corp.test, arc2@corp.test"})
    _settings(session)
    automail.tick(session, MON)
    for m in smtp:
        assert "arc1@corp.test, arc2@corp.test" not in m["env"]


# ═════ 2. 重寄 ═════
def test_two_threads_same_minute_single_send(engine, session, smtp):
    _load(session)
    _settings(session)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    res, errs = [], []
    barrier = threading.Barrier(4)

    def worker():
        try:
            with factory() as s:
                barrier.wait()
                res.append(automail.tick(s, MON)["action"])
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert res.count("send") == 1, (res, errs)
    n = len(smtp)
    # 重啟（新 session）後同週
    with factory() as s:
        assert automail.tick(s, MON.replace(minute=1))["action"] == "idle"
    assert len(smtp) == n


def test_run_now_then_schedule_same_week(session, smtp):
    _load(session)
    _settings(session)
    automail.run_now(session, MON - dt.timedelta(hours=1), "admin")   # 07:00 立即寄
    n = len(smtp)
    assert automail.tick(session, MON)["action"] == "idle"
    assert automail.tick(session, MON.replace(hour=12, minute=30))["action"] == "idle"   # 不記漏寄
    assert len(smtp) == n and automail.get_state(session)["missed"] == []
    with pytest.raises(ValueError):
        automail.run_now(session, MON.replace(hour=9), "admin")
    automail.run_now(session, MON.replace(hour=9), "admin", force=True)
    assert len(smtp) == 2 * n


def test_smtp_fails_third_mail_no_resend(session, smtp):
    _load(session)
    _settings(session)
    smtp.state["fail_on"] = 3
    r = automail.tick(session, MON)
    assert r["result"]["failed"] == 1 and r["result"]["sent"] >= 2
    n = len(smtp)
    for minute in range(1, 6):
        assert automail.tick(session, MON.replace(minute=minute))["action"] == "idle"
    assert automail.tick(session, MON + dt.timedelta(days=1))["action"] == "idle"
    assert len(smtp) == n
    assert session.query(MailLog).filter_by(status="failed").count() == 1


def test_exception_mid_build_marks_error_no_loop(session, smtp, monkeypatch):
    _load(session)
    _settings(session)

    def boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(automail.query, "find", boom)
    r = automail.tick(session, MON)
    assert r["action"] == "send" and "db exploded" in r["result"]["error"]
    assert automail.claim_status(session, "2026-W44") == "error"
    assert automail.tick(session, MON.replace(minute=1))["action"] == "idle"
    assert automail.tick(session, MON.replace(hour=13))["action"] == "idle"     # 不另記漏寄
    assert automail.get_state(session)["missed"] == []
    assert not smtp


def test_smtp_down_not_marked_sent(session, smtp):
    _load(session)
    _settings(session)
    smtp.state["connect_error"] = OSError("connection refused")
    r = automail.tick(session, MON)
    assert r["result"]["sent"] == 0 and r["result"]["error"]
    assert automail.claim_status(session, "2026-W44") != "sent"


def test_killed_midrun_running_claim_never_resends(session, smtp):
    _load(session)
    _settings(session)
    automail.try_claim(session, "2026-W44", "running")      # 模擬程序被砍，認領卡在 running
    assert automail.tick(session, MON.replace(minute=5))["action"] == "idle"
    assert automail.tick(session, MON.replace(hour=13))["action"] == "idle"
    assert not smtp
    with pytest.raises(ValueError):
        automail.run_now(session, MON.replace(hour=14), "admin")


def test_missed_once_across_restart_and_days(engine, session, smtp):
    _load(session)
    _settings(session)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    acts = []
    for t in [MON.replace(hour=12, minute=1), MON.replace(hour=15), MON + dt.timedelta(days=3),
              MON + dt.timedelta(days=6, hours=15)]:
        with factory() as s:
            acts.append(automail.tick(s, t)["action"])
    assert acts == ["missed", "idle", "idle", "idle"]
    with factory() as s:
        assert len(automail.get_state(s)["missed"]) == 1
        assert s.query(AuditLog).filter_by(action="auto_mail_missed").count() == 1
    # 下週正常寄
    assert automail.tick(session, MON + dt.timedelta(days=7))["action"] == "send"
    assert not any(set(m["env"]) & {"x"} for m in smtp)


# ═════ 3. 時間 ═════
CFG = {"enabled": True, "weekday": 0, "time": "08:00", "catchup_until": "12:00", "paused_week": ""}


@pytest.mark.parametrize("now,exp", [
    (dt.datetime(2026, 10, 26, 7, 59, 59), "idle"),
    (dt.datetime(2026, 10, 26, 8, 0), "send"),
    (dt.datetime(2026, 10, 26, 11, 59), "send"),
    (dt.datetime(2026, 10, 26, 12, 0), "send"),
    (dt.datetime(2026, 10, 26, 12, 0, 59), "send"),
    (dt.datetime(2026, 10, 26, 12, 1), "missed"),
    (dt.datetime(2026, 10, 25, 9, 0), "missed"),     # 週日＝上一週（W43）的尾巴 → 上週沒寄就記漏寄
    (dt.datetime(2026, 10, 27, 9, 0), "missed"),     # 週二
])
def test_decide_matrix(now, exp):
    assert automail.decide(CFG, now, None)[0] == exp


def test_decide_paused_week_no_missed():
    c = {**CFG, "paused_week": "2026-W44"}
    for h in (8, 12, 23):
        assert automail.decide(c, dt.datetime(2026, 10, 26, h, 30), None)[0] == "idle"
    assert automail.decide(c, dt.datetime(2026, 11, 2, 8, 0), None)[0] == "send"


def test_iso_week_boundary(session, smtp):
    assert automail.iso_week(dt.date(2029, 12, 31)) == "2030-W01"
    assert automail.iso_week(dt.date(2029, 12, 30)) == "2029-W52"
    assert automail.iso_week(dt.date(2026, 12, 31)) == "2026-W53"
    assert automail.iso_week(dt.date(2027, 1, 4)) == "2027-W01"
    _load(session, dt.date(2029, 12, 31))
    _settings(session)
    m = dt.datetime(2029, 12, 31, 8, 0)
    assert automail.tick(session, m)["action"] == "send"
    assert automail.claim_status(session, "2030-W01") == "sent"
    assert automail.tick(session, dt.datetime(2030, 1, 1, 9))["action"] == "idle"
    # 2026-12-31（週四）屬 W53，週一 12-28 08:00 排程
    assert automail._sched_dt(CFG, dt.datetime(2026, 12, 31, 9)) == dt.datetime(2026, 12, 28, 8, 0)
    assert automail._sched_dt(CFG, dt.datetime(2027, 1, 3, 9)) == dt.datetime(2026, 12, 28, 8, 0)


def test_enable_at_0900_monday_sends_immediately(session, smtp):
    """報告行為：週一 09:00 才開啟 → 仍在補寄窗口內，下一次 tick 就寄出。"""
    _load(session)
    automail.save_settings(session, {"enabled": True, "departments": [
        {"department": DEPT, "enabled": True, "cc": ""}]}, now=MON.replace(hour=9))
    r = automail.tick(session, MON.replace(hour=9, minute=1))
    assert r["action"] == "send"


def test_enable_after_window_no_missed_then_next_week(session, smtp):
    _load(session)
    automail.save_settings(session, {"enabled": True, "departments": [
        {"department": DEPT, "enabled": True, "cc": ""}]}, now=MON.replace(hour=13))
    assert automail.tick(session, MON.replace(hour=13, minute=1))["action"] == "idle"
    assert automail.tick(session, MON + dt.timedelta(days=2))["action"] == "idle"
    assert automail.get_state(session)["missed"] == []
    assert automail.tick(session, MON + dt.timedelta(days=7))["action"] == "send"


# ═════ 4. 內容 ═════
def _jobs(session, now=MON):
    return automail.preview(session, now)["jobs"]


def test_dunning_items_exact(session):
    _load(session)
    _settings(session)
    dun = {j["label"]: j for j in _jobs(session) if j["kind"] == "dunning"}
    hosts = [i["host"] for i in dun["喬峰"]["items"]]
    assert hosts == ["a1", "a5", "a2"]        # -100（逾期）、0（今天）、+60；+61／已結案／無到期日不入
    assert dun["喬峰"]["count"] == 3
    # 無負責人 → 轉部門窗口，不遺漏
    assert "（未指定）" in dun and dun["（未指定）"]["to"] == ["boss@corp.test"]
    assert set(dun) == {"喬峰", "段譽", "（未指定）"}


def test_dunning_submitted_label(session):
    _load(session)
    _settings(session)
    f = session.query(Finding).filter_by(host="a2").one()
    cases.set_overlay(session, f.id, {"progress": "展延送審中"})
    session.commit()
    it = [i for j in _jobs(session) if j["label"] == "喬峰" for i in j["items"] if i["host"] == "a2"][0]
    assert "送審" in it["progress_text"]


def test_background_run_not_restricted_by_view_scope(session, smtp):
    """排程執行緒沒有請求 context；即使呼叫端 context 有 VIEW_SCOPE，新執行緒也不繼承。"""
    _load(session)
    _settings(session)
    tok = query.VIEW_SCOPE.set(("deny", ""))
    try:
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", automail.tick(session, MON)))
        t.start()
        t.join(30)
    finally:
        query.VIEW_SCOPE.reset(tok)
    assert out["r"]["result"]["sent"] >= 4


def test_run_now_ignores_sheet_query_param(client, session, smtp):
    _load(session, dt.date.today(), extra=[FindingIn(host="z1", plugin_id="pz", sheet_key="other", severity="High",
                                                    owner="喬峰", department=DEPT,
                                                    remediation_due=_d(dt.date.today(), 1),
                                                    close_status="未結案")])
    _settings(session)
    pv_all = client.post("/api/auto-mail/preview", json={}).json()
    pv_sheet = client.post("/api/auto-mail/preview?sheet=other", json={}).json()
    assert pv_all["totals"] == pv_sheet["totals"]
    assert [j["count"] for j in pv_all["jobs"]] == [j["count"] for j in pv_sheet["jobs"]]


def test_department_override_routes_to_new_department(session, smtp):
    _load(session)
    _settings(session, depts=[
        {"department": DEPT, "enabled": True, "cc": "lead@corp.test"},
        {"department": DEPT2, "enabled": True, "cc": "seccc@corp.test"},
    ])
    f = session.query(Finding).filter_by(host="a1").one()
    cases.set_overlay(session, f.id, {"department": DEPT2})
    session.commit()
    jobs = _jobs(session)
    q = {(j["department"], j["label"]): j for j in jobs if j["kind"] == "dunning"}
    assert "a1" not in [i["host"] for i in q[(DEPT, "喬峰")]["items"]]
    moved = q[(DEPT2, "喬峰")]
    assert [i["host"] for i in moved["items"]] == ["a1"] and moved["cc"] == ["seccc@corp.test"]
    assert moved["to"] == ["qiaofeng@corp.test"]


def test_owner_override_routes_to_new_owner(session):
    _load(session)
    _settings(session)
    f = session.query(Finding).filter_by(host="a1").one()
    cases.set_overlay(session, f.id, {"owner": "段譽"})
    session.commit()
    dun = {j["label"]: j for j in _jobs(session) if j["kind"] == "dunning"}
    assert "a1" in [i["host"] for i in dun["段譽"]["items"]]
    assert "a1" not in [i["host"] for i in dun["喬峰"]["items"]]


def test_department_override_survives_reimport(session):
    _load(session)
    _settings(session, depts=[{"department": DEPT2, "enabled": True, "cc": ""},
                              {"department": DEPT, "enabled": True, "cc": ""}])
    f = session.query(Finding).filter_by(host="a1").one()
    cases.set_overlay(session, f.id, {"department": DEPT2})
    session.commit()
    # 重匯同一份 Excel（部門仍是舊的）
    findings = [FindingIn(host="a1", plugin_id="p1", sheet_key="s", severity="High", owner="喬峰",
                          department=DEPT, remediation_due=_d(MON.date(), -100), close_status="未結案"),
                FindingIn(host="d1", plugin_id="p1", sheet_key="s", severity="High", owner="阿朱",
                          department=DEPT2, remediation_due=_d(MON.date(), -25), close_status="未結案")]
    importer.create_batch(session, ImportIn(findings=findings))
    session.commit()
    q = {(j["department"], j["label"]) for j in _jobs(session) if j["kind"] == "dunning"}
    assert (DEPT2, "喬峰") in q and (DEPT, "喬峰") not in q


# ═════ 5. 權限 ═════
EPS = [("get", "/api/auto-mail", None), ("put", "/api/auto-mail", {"enabled": True}),
       ("post", "/api/auto-mail/preview", {}), ("post", "/api/auto-mail/test", {"to": "t1@corp.test", "department": DEPT}),
       ("post", "/api/auto-mail/test-run", {"to": "t1@corp.test"}),
       ("post", "/api/auto-mail/run-now", {"confirm": True, "force": True}),
       ("post", "/api/auto-mail/pause-week", {})]


def _call(client, meth, url, body):
    return client.get(url) if meth == "get" else getattr(client, meth)(url, json=body)


def test_permissions_non_super_forbidden(client, engine, session, smtp, monkeypatch):
    _load(session, dt.date.today())
    _settings(session)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "sup", "pw12345", role=config.ROLE_SUPER, email="sup@corp.test")
        security.create_user(s, "dadm", "pw12345", role=config.ROLE_DEPT_ADMIN, department=DEPT)
        security.create_user(s, "usr", "pw12345", role=config.ROLE_USER, display_name="喬峰2")
    monkeypatch.setattr(config, "NO_AUTH", False)
    for meth, url, body in EPS:
        assert _call(client, meth, url, body).status_code == 401, url
    for who in ("dadm", "usr"):
        assert client.post("/api/login", json={"username": who, "password": "pw12345"}).status_code == 200
        for meth, url, body in EPS:
            assert _call(client, meth, url, body).status_code == 403, (who, url)
        client.post("/api/logout")
    assert not smtp
    assert automail.get_settings(session)["paused_week"] == ""
    client.post("/api/login", json={"username": "sup", "password": "pw12345"})
    assert client.get("/api/auto-mail").status_code == 200
    # 不給 to → 用登入者本人信箱
    r = client.post("/api/auto-mail/test", json={"department": DEPT})
    assert r.status_code == 200
    _assert_only_test(smtp, ["sup@corp.test"])
    acts = {a.action for a in session.query(AuditLog).all()}
    assert "auto_mail_test" in acts


def test_settings_validation_endpoint(client, session):
    _load(session, dt.date.today())
    bad = [
        {"test_addresses": "ok@corp.test, not-mail"},
        {"departments": [{"department": "不存在部", "enabled": True}]},
        {"departments": [{"department": DEPT, "enabled": True, "cc": "a@b.test, x@"}]},
        {"weekday": 9}, {"time": "8"}, {"catchup_until": "07:00"}, {"dunning_days": "abc"},
        {"paused_week": "2026-44"}, {"test_mode": True, "test_addresses": ""},
    ]
    for b in bad:
        assert client.put("/api/auto-mail", json=b).status_code == 400, b
    for b in ({"to": "bad", "department": DEPT}, {"to": "t1@corp.test", "department": "不存在部"}):
        assert client.post("/api/auto-mail/test", json=b).status_code == 400
    assert client.post("/api/auto-mail/test-run", json={"to": "x@"}).status_code == 400


def test_enabled_string_false_not_enabling(client, session):
    _load(session, dt.date.today())
    client.put("/api/auto-mail", json={"enabled": "false"})
    assert automail.get_settings(session)["enabled"] is False


def test_audit_logged_for_all_actions(client, session, smtp):
    _load(session, dt.date.today())
    _settings(session)
    client.put("/api/auto-mail", json={"dunning_days": 30})
    client.post("/api/auto-mail/test", json={"to": "t1@corp.test", "department": DEPT})
    client.post("/api/auto-mail/test-run", json={"to": "t1@corp.test"})
    client.post("/api/auto-mail/run-now", json={"confirm": True})
    client.post("/api/auto-mail/pause-week", json={})
    acts = {a.action for a in session.query(AuditLog).all()}
    assert {"auto_mail_settings", "auto_mail_test", "auto_mail_test_run", "auto_mail_run_now",
            "auto_mail_pause"} <= acts


# ═════ 6. 健壯性 ═════
def test_smtp_not_configured_records_error(session, smtp):
    _load(session)
    appsettings.set_email_config(session, {"smtp_host": ""})
    _settings(session)
    r = automail.tick(session, MON)
    assert r["action"] == "send" and "SMTP" in r["result"]["error"]
    assert "SMTP" in automail.get_state(session)["last_run"]["error"]
    assert not smtp


def test_smtp_disabled_not_sent(session, smtp):
    _load(session)
    appsettings.set_email_config(session, {"enabled": False})
    _settings(session)
    automail.tick(session, MON)
    assert not smtp


def test_empty_departments(session, smtp):
    _load(session)
    _settings(session, depts=[])
    r = automail.tick(session, MON)
    assert r["result"]["total"] == 0 and not smtp


def test_huge_cc_and_unicode(session, smtp):
    _load(session)
    cc = ["人員%03d@corp.test" % i for i in range(300)]
    _settings(session, depts=[{"department": DEPT, "enabled": True, "cc": ",".join(cc)}])
    r = automail.tick(session, MON)
    assert r["result"]["failed"] == 0
    for m in smtp:
        assert set(cc) <= set(m["env"])
    assert "喬峰" in "".join(_hdr(m, "Subject") for m in smtp)


def test_scheduler_disabled_in_tests(client):
    assert automail.thread_disabled() is True
    assert automail.start_scheduler(lambda: None) is None
    assert not any(t.name == "auto-mail" for t in threading.enumerate())


def test_loop_survives_tick_exception(monkeypatch):
    calls = {"n": 0}

    class Stop(BaseException):
        pass

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > 3:
            raise Stop

    def bad_factory():
        raise RuntimeError("db down")
    monkeypatch.setattr(automail.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        automail._loop(bad_factory, 60)
    assert calls["n"] == 4

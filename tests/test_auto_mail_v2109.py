"""V2.109 自動寄信「不掉球、提前通知」：
  · 送審中（等資安處理）不催負責人、不升級；窗口盤點週報另列「等資安處理（我方已送件）」僅供參考
  · 缺期限・待確認：催辦信另段、盤點週報列數字＋清單
  · 盤點週報：我方已逾期（不含送審中）、缺期限、等資安處理三段
  · 結案申請中（等複掃）照催，加提示
  · 升級副本（主管）：已逾期／N 天內到期且沒填或已過預計完成日
  · 失敗告警信＋畫面紅色橫幅
  · 測試模式：升級副本、告警收件人一律改寄測試信箱
"""
import datetime as dt
import email
from email.header import decode_header, make_header

import pytest

from webvuln import appsettings, automail, cases, config, importer, query
from webvuln.models import Finding, MailLog, User
from webvuln.schemas import FindingIn, ImportIn

MON = dt.datetime(2026, 10, 26, 8, 0)   # 2026-W44 週一
TODAY = MON.date()
DEPT = "資訊架構部"
REAL = {"qiaofeng@corp.test", "boss@corp.test", "lead@corp.test", "chief@corp.test", "ops@corp.test",
        "archive@corp.test"}


def _d(n):
    return (TODAY + dt.timedelta(days=n)).isoformat()


def _load(session, extra=()):
    F = lambda **k: FindingIn(sheet_key="s", close_status="未結案", department=DEPT, **k)  # noqa: E731
    findings = [
        F(host="h-od", plugin_id="p1", severity="High", owner="喬峰", remediation_due=_d(-10)),      # 已逾期
        F(host="h-sub", plugin_id="p2", severity="High", owner="喬峰", remediation_due=_d(-5)),     # 送審中＋已逾期
        F(host="h-sub2", plugin_id="p3", severity="Low", owner="喬峰", remediation_due=_d(30)),     # 送審中、未逾期
        F(host="h-nodue", plugin_id="p4", severity="Medium", owner="喬峰"),                         # 缺期限
        F(host="h-d10", plugin_id="p5", severity="Medium", owner="喬峰", remediation_due=_d(10)),   # 10 天內、沒填預計完成
        F(host="h-d10t", plugin_id="p6", severity="Medium", owner="喬峰", remediation_due=_d(10)),  # 10 天內、有填未來預計完成
        F(host="h-d20", plugin_id="p7", severity="Medium", owner="喬峰", remediation_due=_d(20)),   # 20 天、沒填 → 不升級
        F(host="h-res", plugin_id="p8", severity="Low", owner="喬峰", remediation_due=_d(40)),     # 等複掃
        F(host="g-d40", plugin_id="p1", severity="Low", owner="段譽", remediation_due=_d(40)),     # 段譽：沒有升級項目
    ] + list(extra)
    b = importer.create_batch(session, ImportIn(findings=findings))
    b.imported_at = dt.datetime.combine(TODAY, dt.time(7, 0))
    session.commit()
    session.add_all([
        User(username="u1", display_name="喬峰", email="qiaofeng@corp.test", department=DEPT,
             role=config.ROLE_USER, is_active=True),
        User(username="u2", display_name="段譽", email="duanyu@corp.test", department=DEPT,
             role=config.ROLE_USER, is_active=True),
        User(username="u3", display_name="王語嫣", email="boss@corp.test", department=DEPT,
             role=config.ROLE_DEPT_ADMIN, is_active=True),
    ])
    session.commit()

    def _f(host):
        return session.query(Finding).filter_by(host=host).one()
    cases.set_overlay(session, _f("h-sub").id, {"progress": "展延送審中"})
    cases.set_overlay(session, _f("h-sub2").id, {"progress": "例外送審中"})
    cases.set_overlay(session, _f("h-d10t").id, {"target_date": _d(5)})
    cases.set_overlay(session, _f("h-res").id, {"progress": "等複掃"})
    appsettings.set_email_config(session, {"enabled": True, "smtp_host": "relay.test",
                                           "from_default": "sys@corp.test", "copy_to": "archive@corp.test"})


def _settings(session, esc=True, **kw):
    """esc＝「啟用主管副本」總開關（預設關；這裡的升級測試大多要開著測，關著的另有專門測試）。"""
    base = {"enabled": True, "departments": [
        {"department": DEPT, "enabled": True, "cc": "lead@corp.test", "escalate_cc": "chief@corp.test",
         "send_inventory": True, "send_dunning": True}]}
    base.update(kw)
    automail.save_settings(session, base, now=MON - dt.timedelta(days=7))
    return automail.set_escalate_enabled(session, esc)


@pytest.fixture
def smtp(monkeypatch):
    sent = []
    state = {"down": False, "fail_on": None, "n": 0}

    class _Fake:
        def __init__(self, *a, **k):
            if state["down"]:
                raise OSError("connection refused")

        def starttls(self):
            pass

        def sendmail(self, frm, to, msg):
            state["n"] += 1
            if state["fail_on"] and state["n"] == state["fail_on"]:
                raise OSError("boom")
            m = email.message_from_string(msg)
            sent.append({"env": list(to), "msg": m})
            return {}

        def quit(self):
            pass

    monkeypatch.setattr(automail.smtplib, "SMTP", _Fake)
    sent_state = state
    return sent, sent_state


def _subj(m):
    return str(make_header(decode_header(m["msg"]["Subject"])))


def _body(m):
    return m["msg"].get_payload(decode=True).decode("utf-8")


def _jobs(session, now=MON):
    return automail.preview(session, now)["jobs"]


def _dun(session):
    return {j["label"]: j for j in _jobs(session) if j["kind"] == "dunning"}


# ── 3a 送審中＝球在資安 ──
def test_waiting_on_security_rule():
    assert automail.waiting_on_security({"submitted": True}) is True
    assert automail.waiting_on_security({"submitted": False, "progress": "等複掃"}) is False


def test_dunning_excludes_all_submitted_even_overdue(session):
    _load(session)
    _settings(session)
    q = _dun(session)["喬峰"]
    hosts = [i["host"] for i in q["items"]]
    assert "h-sub" not in hosts and "h-sub2" not in hosts
    assert "送審" not in q["body"].split("已送展延")[0]       # 清單內沒有送審中的列


def test_submitted_overdue_alone_does_not_escalate(session):
    """只有「送審中已逾期」的負責人：不寄催辦、也不副本主管。"""
    _load(session, extra=[FindingIn(sheet_key="s", close_status="未結案", department=DEPT, host="z1",
                                    plugin_id="p9", severity="High", owner="虛竹", remediation_due=_d(-30))])
    cases.set_overlay(session, session.query(Finding).filter_by(host="z1").one().id, {"progress": "例外送審中"})
    _settings(session)
    assert "虛竹" not in _dun(session)


def test_inventory_waiting_section_informational_and_counts_unchanged(session):
    _load(session)
    _settings(session)
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    body = inv["body"]
    assert "等資安處理（我方已送件）" in body and "不需處理" in body
    seg = body.split("【等資安處理（我方已送件）】")[1].split("二、盤點數字")[0]
    assert "h-sub" in seg and "h-sub2" in seg and "已送審" in seg and "已逾期 5 天" in seg
    m = dict(inv["metrics"])
    assert m["等資安處理（我方已送件）"] == 2 and m["　其中已逾期"] == 1
    # 未結照算（含送審中，跟資安 Excel 一致）
    assert m["未結"] == 9 == query.weekly_report(session, department=DEPT, today=TODAY)["unresolved"]
    assert m["已逾期"] == 2 and m["　我方已逾期（不含送審中）"] == 1


# ── 3b 缺期限 ──
def test_nodue_in_owner_mail_own_section(session):
    _load(session)
    _settings(session)
    q = _dun(session)["喬峰"]
    it = [i for i in q["items"] if i["host"] == "h-nodue"][0]
    assert it["section"] == "nodue" and not it["escalate"]
    assert q["items"][-1]["host"] == "h-nodue"           # 放最後一段
    assert "期限待確認（請向資安確認到期日）" in q["body"]
    assert "期限待確認 1 筆" in q["subject"] and "60 天內到期（含已逾期）未修補" in q["subject"]


def test_owner_with_only_nodue_gets_mail(session):
    _load(session, extra=[FindingIn(sheet_key="s", close_status="未結案", department=DEPT, host="y1",
                                    plugin_id="p9", severity="High", owner="阿紫")])
    _settings(session)
    j = _dun(session)["阿紫"]
    assert j["count"] == 1 and j["nodue_count"] == 1 and "天內到期未修補" not in j["subject"]


# ── 3c 盤點週報三段 ──
def test_inventory_attention_lists(session):
    _load(session)
    _settings(session)
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    body = inv["body"]
    head = body.split("【等資安處理")[0]
    assert "已逾期（不含送審中）：1 筆" in head and "h-od" in head and "h-sub" not in head
    assert "缺期限・待確認：1 筆" in head and "h-nodue" in head
    assert dict(inv["metrics"])["缺期限・待確認（請向資安確認到期日）"] == 1


def test_inventory_list_capped(session, monkeypatch):
    monkeypatch.setattr(automail, "LIST_CAP", 1)
    extra = [FindingIn(sheet_key="s", close_status="未結案", department=DEPT, host="od%d" % i, plugin_id="q%d" % i,
                       severity="Low", owner="喬峰", remediation_due=_d(-3)) for i in range(3)]
    _load(session, extra=extra)
    _settings(session)
    inv = [j for j in _jobs(session) if j["kind"] == "inventory"][0]
    assert "…另 3 筆" in inv["body"]


# ── 結案申請中（等複掃）照催＋提示 ──
def test_rescan_still_dunned_with_hint(session):
    _load(session)
    _settings(session)
    it = [i for i in _dun(session)["喬峰"]["items"] if i["host"] == "h-res"][0]
    assert automail.RESCAN_HINT in it["progress_text"]


# ── 5 升級副本（主管）──
def test_escalation_rules_and_cc(session):
    _load(session)
    _settings(session)
    d = _dun(session)
    esc = {i["host"]: i["escalate"] for i in d["喬峰"]["items"]}
    assert esc == {"h-od": True, "h-d10": True, "h-d10t": False, "h-d20": False, "h-res": False, "h-nodue": False}
    q = d["喬峰"]
    assert q["escalate_count"] == 2 and q["escalate_cc"] == ["chief@corp.test"]
    assert q["cc"] == ["lead@corp.test", "chief@corp.test"]
    assert "★ h-od" in q["body"] and "★ h-d10" in q["body"] and "已副本主管：chief@corp.test" in q["body"]
    # 段譽沒有升級項目 → 不加主管副本
    assert d["段譽"]["escalate_cc"] == [] and d["段譽"]["cc"] == ["lead@corp.test"]


def test_escalation_target_passed_and_days_config(session):
    _load(session)
    cases.set_overlay(session, session.query(Finding).filter_by(host="h-d10t").one().id, {"target_date": _d(-1)})
    _settings(session)
    assert {i["host"]: i["escalate"] for i in _dun(session)["喬峰"]["items"]}["h-d10t"] is True
    _settings(session, escalate_days=0)            # 0＝只有已逾期
    esc = [i["host"] for i in _dun(session)["喬峰"]["items"] if i["escalate"]]
    assert esc == ["h-od"]
    _settings(session, escalate_days=30)
    assert "h-d20" in [i["host"] for i in _dun(session)["喬峰"]["items"] if i["escalate"]]


def test_real_send_envelope_has_escalation_cc(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session)
    automail.tick(session, MON)
    q = [m for m in sent if "qiaofeng@corp.test" in m["env"]][0]
    assert "chief@corp.test" in q["env"] and "chief@corp.test" in q["msg"]["Cc"]
    dy = [m for m in sent if "duanyu@corp.test" in m["env"]][0]
    assert "chief@corp.test" not in dy["env"]


def test_test_mode_never_mails_escalation_or_alert_recipients(session, smtp):
    sent, st = smtp
    _load(session)
    _settings(session, test_mode=True, test_addresses="t1@corp.test", alert_recipients="ops@corp.test")
    st["fail_on"] = 3                               # 讓一封（段譽那封）失敗 → 觸發告警
    automail.tick(session, MON)
    assert sent
    for m in sent:
        assert m["env"] == ["t1@corp.test"] and not m["msg"].get("Cc")
        assert _subj(m).startswith("[測試]")
    alert = [m for m in sent if "告警" in _subj(m)]
    assert alert and "ops@corp.test" in _body(alert[0])          # 正式告警收件人只寫在信件開頭
    assert any("chief@corp.test" in _body(m) for m in sent)    # 升級副本只寫在測試信開頭


def test_test_run_and_sample_never_mail_escalation(client, session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test")
    automail.test_run(session, MON, "admin", ["t1@corp.test"])
    automail.sample_test(session, MON, "admin", ["t1@corp.test"], DEPT)
    assert sent and all(set(m["env"]) == {"t1@corp.test"} for m in sent)
    assert not any("告警" in _subj(m) for m in sent)       # 測試寄送不發告警


# ── 4 告警 ──
def test_alert_on_failed_mail_once(session, smtp):
    sent, st = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test, ops2@corp.test")
    st["fail_on"] = 2
    r = automail.tick(session, MON)
    assert r["result"]["failed"] == 1
    alerts = [m for m in sent if "告警" in _subj(m)]
    assert len(alerts) == 1 and set(alerts[0]["env"]) == {"ops@corp.test", "ops2@corp.test"}
    assert "失敗" in _body(alerts[0])
    assert session.query(MailLog).filter_by(mode="alert", status="ok").count() == 1
    automail.tick(session, MON.replace(minute=5))
    assert len([m for m in sent if "告警" in _subj(m)]) == 1
    a = automail.list_alerts(session)[0][1]
    assert a["status"] == "ok" and a["week"] == "2026-W44"
    assert automail.alert_banner(session, MON.replace(minute=10)) == []   # 告警已寄到 → 不出橫幅


def test_alert_on_skipped_no_recipient(session, smtp):
    sent, _ = smtp
    _load(session)
    session.query(User).filter_by(username="u3").update({"email": None})   # 窗口沒信箱 → 盤點週報略過
    session.commit()
    _settings(session, alert_recipients="ops@corp.test")
    automail.tick(session, MON)
    alert = [m for m in sent if "告警" in _subj(m)]
    assert len(alert) == 1 and "略過" in _body(alert[0]) and "此部門無窗口信箱" in _body(alert[0])


def test_no_alert_when_all_ok(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test")
    automail.tick(session, MON)
    assert sent and not any("告警" in _subj(m) for m in sent)
    assert automail.list_alerts(session) == []


def test_missed_week_alert_and_banner(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test")
    assert automail.alert_banner(session, MON.replace(hour=11)) == []          # 補寄截止前沒有橫幅
    # 服務整個早上沒開：過截止後畫面就有橫幅（唯讀，不寄信）
    b = automail.alert_banner(session, MON.replace(hour=13))
    assert b and "沒有完成" in b[0]["text"] and not sent
    # 背景排程 tick 才記漏寄＋寄告警；之後的 tick 不重寄
    automail.tick(session, MON.replace(hour=13, minute=1))
    automail.tick(session, MON.replace(hour=14, minute=1))
    alerts = [m for m in sent if "告警" in _subj(m)]
    assert len(alerts) == 1 and alerts[0]["env"] == ["ops@corp.test"]
    assert automail.alert_banner(session, MON.replace(hour=15))      # 仍未完成 → 橫幅還在
    automail.run_now(session, MON.replace(hour=15), "admin")          # 手動補寄完成 → 橫幅消失
    assert automail.alert_banner(session, MON.replace(hour=16)) == []
    assert automail.health_status(session, MON.replace(hour=16))["last_formal_sent_at"].startswith("2026-10-26T15")


def test_banner_and_alerts_api_are_read_only(client, session, smtp):
    sent, _ = smtp
    _load(session)
    automail.save_settings(session, {"enabled": True, "alert_recipients": "ops@corp.test", "departments": [
        {"department": DEPT, "enabled": True}]}, now=dt.datetime.now() - dt.timedelta(days=30))
    for _ in range(3):
        assert client.get("/api/auto-mail/alerts").status_code == 200
        client.get("/api/auto-mail")
    assert not sent and automail.list_alerts(session) == []
    assert session.query(MailLog).count() == 0


def test_smtp_down_then_alert_retried_until_delivered(session, smtp):
    sent, st = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test")
    st["down"] = True
    automail.tick(session, MON)                                     # retry
    assert automail.list_alerts(session) == []                      # 重試中不告警
    automail.tick(session, MON.replace(hour=12, minute=5))           # 截止 → error → 告警（也寄不出）
    (_k, ev), = automail.list_alerts(session)
    assert ev["status"] == "failed" and "寄不出" in ev["error"]
    texts = [x["text"] for x in automail.alert_banner(session, MON.replace(hour=13))]
    assert any("沒有完成" in t for t in texts) and any("告警信沒寄出" in t for t in texts)
    assert session.query(MailLog).filter_by(mode="alert", status="failed").count() == 1
    assert automail.health_status(session, MON.replace(hour=13))["overdue_unsent"] is True
    # SMTP 修好 → 下一次 tick（滿 10 分鐘）補寄告警，只寄一次
    st["down"] = False
    automail.tick(session, MON.replace(hour=12, minute=10))         # 未滿 10 分鐘 → 不試
    assert not sent
    automail.tick(session, MON.replace(hour=12, minute=20))
    automail.tick(session, MON.replace(hour=12, minute=40))
    alerts = [m for m in sent if "告警" in _subj(m)]
    assert len(alerts) == 1
    assert automail.list_alerts(session)[0][1]["status"] == "ok"
    assert not any("告警信沒寄出" in x["text"] for x in automail.alert_banner(session, MON.replace(hour=13)))


def test_no_alert_recipient_banner_then_delivered_after_setting(session, smtp):
    sent, st = smtp
    _load(session)
    _settings(session)
    st["fail_on"] = 1
    automail.tick(session, MON)
    texts = [x["text"] for x in automail.alert_banner(session, MON.replace(minute=5))]
    assert any("未設定告警收件人" in t for t in texts)
    assert session.query(MailLog).filter_by(mode="alert").count() == 0
    automail.tick(session, MON.replace(minute=30))                  # 還沒設 → 不寄
    assert not [m for m in sent if "告警" in _subj(m)]
    _settings(session, alert_recipients="ops@corp.test")
    automail.tick(session, MON.replace(minute=40))
    alerts = [m for m in sent if "告警" in _subj(m)]
    assert len(alerts) == 1 and alerts[0]["env"] == ["ops@corp.test"]


def test_alert_event_db_unique_across_sessions(engine, session, smtp):
    from sqlalchemy.orm import Session, sessionmaker
    sent, _ = smtp
    _load(session)
    cfg = _settings(session, alert_recipients="ops@corp.test")
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as a, factory() as b:
        r1 = automail.raise_alert(a, cfg, "2026-W44", "missed", "s", "b", MON)
        r2 = automail.raise_alert(b, cfg, "2026-W44", "missed", "s", "b", MON)
    assert r1 and r1["status"] == "ok" and r2 is None
    assert len(sent) == 1 and len(automail.list_alerts(session)) == 1


def test_banner_quiet_when_disabled_paused_or_enabled_late(session):
    _load(session)
    assert automail.alert_banner(session, MON.replace(hour=13)) == []            # 未啟用
    _settings(session, paused_week="2026-W44")
    assert automail.alert_banner(session, MON.replace(hour=13)) == []            # 暫停本週
    automail.save_settings(session, {"enabled": False, "paused_week": ""})
    automail.save_settings(session, {"enabled": True}, now=MON.replace(hour=13))  # 排程後才開
    assert automail.alert_banner(session, MON.replace(hour=14)) == []


def test_last_week_never_ran_detected_by_scheduler(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test")
    nxt = MON + dt.timedelta(days=7, hours=-1)     # 下週一 07:00，本週排程還沒到
    texts = [x["text"] for x in automail.alert_banner(session, nxt)]
    assert any("2026-W44" in t for t in texts) and not sent
    automail.tick(session, nxt)
    automail.tick(session, nxt.replace(minute=30))
    assert automail.claim_status(session, "2026-W44") == "missed"
    alerts = [m for m in sent if "告警" in _subj(m)]
    assert len(alerts) == 1 and "2026-W44" in _subj(alerts[0])
    assert automail.alert_banner(session, nxt.replace(minute=31)) == []   # 已記漏寄、告警已寄 → 上週不再出橫幅


def test_test_mode_run_does_not_count_as_formal(session, smtp):
    _load(session)
    _settings(session, test_mode=True, test_addresses="t1@corp.test")
    automail.tick(session, MON)
    assert automail.claim_status(session, "2026-W44") == "test"
    texts = [x["text"] for x in automail.alert_banner(session, MON.replace(hour=13))]
    assert any("測試模式" in t for t in texts)
    h = automail.health_status(session, MON.replace(hour=13))
    assert h["overdue_unsent"] is True and h["last_formal_sent_at"] is None and h["week_state"] == "test"
    # 完整測試寄送不認領本週 → 一樣算沒寄
    automail.save_settings(session, {"test_mode": False})
    automail.test_run(session, MON.replace(hour=9), "admin", ["t1@corp.test"])
    assert automail.health_status(session, MON.replace(hour=13))["overdue_unsent"] is True


def test_health_endpoint_no_personal_data(client, session):
    _load(session)
    automail.save_settings(session, {"enabled": True, "alert_recipients": "ops@corp.test", "test_mode": True,
                                     "test_addresses": "t1@corp.test", "departments": [
                                         {"department": DEPT, "enabled": True, "cc": "lead@corp.test"}]},
                           now=dt.datetime.now() - dt.timedelta(days=30))
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    am = j["auto_mail"]
    assert j["status"] == "ok" and isinstance(am["overdue_unsent"], bool) and am["enabled"] is True
    assert "@" not in r.text and "喬峰" not in r.text and DEPT not in r.text


def test_partial_refusal_counts_as_failure_and_alerts(session, smtp, monkeypatch):
    sent, st = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test")
    orig = automail.smtplib.SMTP

    class _Refuse(orig):
        def sendmail(self, frm, to, msg):
            super().sendmail(frm, to, msg)
            if "qiaofeng@corp.test" in to:
                return {"chief@corp.test": (550, b"no such user")}
            if "ops@corp.test" in to:
                return {"ops@corp.test": (550, b"full")}
            return {}

    monkeypatch.setattr(automail.smtplib, "SMTP", _Refuse)
    r = automail.tick(session, MON)
    q = [d for d in r["result"]["details"] if d["label"] == "喬峰"][0]
    assert q["status"] == "partial" and "chief@corp.test" in q["error"] and r["result"]["failed"] == 1
    assert session.query(MailLog).filter_by(status="partial").count() >= 1
    (_k, ev), = automail.list_alerts(session)
    assert ev["status"] == "partial"                 # 告警本身也被部分拒收 → 記下、不重寄
    automail.tick(session, MON.replace(minute=30))
    assert len([m for m in sent if "告警" in _subj(m)]) == 1
    assert any("告警信沒寄出" in x["text"] for x in automail.alert_banner(session, MON.replace(minute=31)))


# ── 單一分類／邊界 ──
def test_submitted_without_due_only_in_waiting(session):
    _load(session)
    cases.set_overlay(session, session.query(Finding).filter_by(host="h-nodue").one().id, {"progress": "例外送審中"})
    _settings(session)
    jobs = _jobs(session)
    assert not [i for j in jobs if j["kind"] == "dunning" for i in j["items"] if i["host"] == "h-nodue"]
    inv = [j for j in jobs if j["kind"] == "inventory"][0]
    head, wait = inv["body"].split("【等資安處理")
    assert "h-nodue" not in head and "h-nodue" in wait.split("二、盤點數字")[0]
    assert dict(inv["metrics"])["缺期限・待確認（請向資安確認到期日）"] == 0


def test_rescan_exception_window_boundaries(session):
    E = lambda h, n: FindingIn(sheet_key="s", close_status="未結案", department=DEPT, host=h, plugin_id="x" + h,  # noqa: E731
                               severity="Low", owner="慕容復", remediation_due=_d(-200), exception_due=_d(n),
                               remark="例外管理(iForm_1)")
    _load(session, extra=[E("e60", 60), E("e61", 61), E("eod", -3)])
    for h in ("e60", "e61", "eod"):
        cases.set_overlay(session, session.query(Finding).filter_by(host=h).one().id, {"progress": "等複掃"})
    _settings(session)
    items = {i["host"]: i for i in _dun(session)["慕容復"]["items"]}
    assert set(items) == {"e60", "eod"}                       # 第 60 天入、第 61 天不入、已逾期入
    assert items["e60"]["effective_due"] == _d(60)            # 窗口看的是例外期限
    assert all(automail.RESCAN_HINT in i["progress_text"] for i in items.values())


def test_rows_with_empty_id_not_collapsed(session, monkeypatch):
    _load(session)
    _settings(session)
    base = {"id": None, "owner": "喬峰", "severity": "High", "name": "n", "apply_bucket": None,
            "submitted": False, "target_date": None, "progress": ""}
    rows = [dict(base, host="x1", effective_due=_d(-1), overdue_days=1),
            dict(base, host="x2", effective_due=_d(-1), overdue_days=1),
            dict(base, host="x3", effective_due=None, overdue_days=None, apply_bucket="nodue")]
    monkeypatch.setattr(automail.query, "find", lambda *a, **k: [dict(r) for r in rows])
    q = _dun(session)["喬峰"]
    assert [i["host"] for i in q["items"]] == ["x1", "x2", "x3"]


def test_escalation_days_zero_and_robust_dates():
    T = TODAY
    assert automail.is_escalation({"overdue_days": 0}, T, 0) is False          # 0＝只有已逾期，今天到期不算
    assert automail.is_escalation({"overdue_days": 1}, T, 0) is True
    assert automail.is_escalation({"overdue_days": 0}, T, 14) is True
    assert automail.is_escalation({"overdue_days": -5, "target_date": T + dt.timedelta(days=3)}, T, 14) is False
    assert automail.is_escalation({"overdue_days": -5, "target_date": dt.datetime(2026, 1, 1)}, T, 14) is True
    assert automail.is_escalation({"overdue_days": -5, "target_date": "壞日期"}, T, 14) is False   # 記 log 略過
    assert automail.is_escalation({"overdue_days": "x"}, T, 14) is False


def test_bad_row_dates_do_not_crash_run(session, smtp, monkeypatch):
    _load(session)
    _settings(session)
    base = {"id": None, "owner": "喬峰", "severity": "High", "name": "n", "apply_bucket": None, "submitted": False,
            "progress": ""}
    rows = [dict(base, host="ok1", effective_due=_d(-1), overdue_days=1, target_date=None),
            dict(base, host="bad", effective_due=_d(-2), overdue_days=-2, target_date="2026/13/45")]
    monkeypatch.setattr(automail.query, "find", lambda *a, **k: [dict(r) for r in rows])
    r = automail.tick(session, MON)
    assert r["action"] == "send" and r["result"]["sent"] >= 1


def test_escalation_missing_manager_preview_and_alert(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, alert_recipients="ops@corp.test", departments=[
        {"department": DEPT, "enabled": True, "cc": "lead@corp.test", "send_inventory": True, "send_dunning": True}])
    pv = automail.preview(session, MON)
    q = [j for j in pv["jobs"] if j["label"] == "喬峰"][0]
    assert q["escalate_count"] == 2 and q["escalate_missing"] == "應升級但未設定主管"
    assert pv["totals"]["escalate_missing"] == 1
    automail.tick(session, MON)
    alert = [m for m in sent if "告警" in _subj(m)]
    assert len(alert) == 1 and "應升級但主管沒收到" in _body(alert[0]) and "喬峰" in _body(alert[0])


def test_escalation_missing_when_owner_mail_skipped(session):
    _load(session)
    session.query(User).filter(User.username.in_(["u1", "u3"])).update({"email": None}, synchronize_session=False)
    session.commit()
    _settings(session)
    q = _dun(session)["喬峰"]
    assert q["mode"] == "skip" and q["escalate_missing"].startswith("應升級但此信寄不出")


def test_wording_and_preview_test_recipients(session):
    _load(session)
    _settings(session, test_mode=True, test_addresses="t1@corp.test, t2@corp.test")
    pv = automail.preview(session, MON)
    q = [j for j in pv["jobs"] if j["label"] == "喬峰"][0]
    assert "（含已逾期）" in q["subject"]
    assert [i for i in q["items"] if i["section"] == "nodue"][0]["due_text"] == automail.NODUE_TEXT
    assert automail.NODUE_TEXT in q["body"]
    assert q["actual_to"] == ["t1@corp.test", "t2@corp.test"] and q["actual_cc"] == []
    assert q["to"] == ["qiaofeng@corp.test"]                  # 正式收件人保留給畫面對照


def test_int_settings_strict_and_legacy_normalised(session):
    _load(session)
    _settings(session)
    for k in ("dunning_days", "escalate_days", "weekday"):
        for bad in (True, False, 7.5, "abc", "3.0", None if k != "escalate_days" else "x"):
            with pytest.raises(ValueError):
                automail.save_settings(session, {k: bad})
    assert automail.save_settings(session, {"dunning_days": "45", "escalate_days": 7.0})["dunning_days"] == 45
    raw = appsettings.get_json(session, automail.KEY)
    raw.update(dunning_days="壞", escalate_days=True, weekday=9)
    appsettings.set_json(session, automail.KEY, raw)
    cfg = automail.get_settings(session)
    assert cfg["dunning_days"] == 60 and cfg["escalate_days"] == 14 and cfg["weekday"] == 0


def test_alerts_endpoint_and_view(client, session):
    _load(session)
    automail.save_settings(session, {"enabled": True, "departments": [{"department": DEPT, "enabled": True}]},
                           now=dt.datetime.now() - dt.timedelta(days=30))
    r = client.get("/api/auto-mail/alerts")
    assert r.status_code == 200 and isinstance(r.json()["items"], list)
    v = client.get("/api/auto-mail").json()
    assert "alerts" in v and v["settings"]["escalate_days"] == 14 and v["settings"]["alert_recipients"] == []


# ── 設定驗證 ──
def test_settings_validation_new_fields(session):
    _load(session)
    cfg = _settings(session, alert_recipients="ops@corp.test\nops2@corp.test", escalate_days="7")
    assert cfg["alert_recipients"] == ["ops@corp.test", "ops2@corp.test"] and cfg["escalate_days"] == 7
    assert cfg["departments"][0]["escalate_cc"] == ["chief@corp.test"]
    with pytest.raises(ValueError, match="告警收件人"):
        automail.save_settings(session, {"alert_recipients": "bad"})
    with pytest.raises(ValueError, match="升級副本"):
        automail.save_settings(session, {"departments": [{"department": DEPT, "enabled": True, "escalate_cc": "x"}]})
    with pytest.raises(ValueError, match="升級天數"):
        automail.save_settings(session, {"escalate_days": 400})
    with pytest.raises(ValueError, match="升級天數"):
        automail.save_settings(session, {"escalate_days": "abc"})


# ── 啟用主管副本總開關（預設關）──
def test_escalate_switch_default_off_and_not_via_save(session):
    _load(session)
    cfg = automail.save_settings(session, {"enabled": True, "escalate_enabled": True})
    assert cfg["escalate_enabled"] is False                 # 一般儲存改不到
    assert automail.DEFAULTS["escalate_enabled"] is False


def test_escalate_off_real_send_no_supervisor_cc(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, esc=False)
    pv = automail.preview(session, MON)
    q = [j for j in pv["jobs"] if j["label"] == "喬峰"][0]
    assert q["escalate_cc"] == [] and "chief@corp.test" not in q["cc"]
    assert q["escalate_would_cc"] == ["chief@corp.test"] and q["escalate_note"].startswith("主管副本尚未啟用（測試通過後再開）")
    assert q["escalate_missing"] == "" and pv["totals"]["escalate_pending"] == 1
    automail.tick(session, MON)
    assert sent and all("chief@corp.test" not in m["env"] and "chief@corp.test" not in (m["msg"]["Cc"] or "")
                        for m in sent)
    automail.run_now(session, MON.replace(hour=9), "admin", force=True)
    assert all("chief@corp.test" not in m["env"] for m in sent)


def test_escalate_on_real_send_has_cc(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, esc=True)
    automail.tick(session, MON)
    q = [m for m in sent if "qiaofeng@corp.test" in m["env"]][0]
    assert "chief@corp.test" in q["env"] and "chief@corp.test" in q["msg"]["Cc"]


def test_escalate_off_test_run_exercises_path_but_redirects(session, smtp):
    sent, _ = smtp
    _load(session)
    _settings(session, esc=False)
    automail.test_run(session, MON, "admin", ["t1@corp.test"])
    assert sent and all(m["env"] == ["t1@corp.test"] and not m["msg"].get("Cc") for m in sent)
    q = [m for m in sent if "喬峰" in _subj(m)][0]
    b = _body(q)
    assert "主管副本（主管副本尚未啟用（測試通過後再開））：chief@corp.test" in b
    assert "本信已副本主管：chief@corp.test" in b            # 測試信是「開了之後」的樣子


def test_escalate_switch_endpoint_audited(client, session):
    from webvuln.models import AuditLog
    _load(session)
    assert client.post("/api/auto-mail/escalate", json={"enabled": True}).status_code == 400
    r = client.post("/api/auto-mail/escalate", json={"enabled": True, "confirm": True})
    assert r.status_code == 200 and r.json()["settings"]["escalate_enabled"] is True
    a = session.query(AuditLog).filter_by(action="auto_mail_escalate").all()
    assert len(a) == 1 and "關 → 開" in a[0].detail

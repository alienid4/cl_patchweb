"""送審中（等核准）（2026-10-19）：承辦已把展延／例外申請送資安，但資安還沒更新 Excel。

只動承辦疊加層：到期日、處置階段、未結／逾期都照 Excel；只是離開「應申請未申請」、算在「送審中（等核准）」。
需申請＝應申請未申請＋送審中＋已核准（三類互斥）；資安核准反映到 Excel（或已結案）後，匯入時自動解除並留紀錄。
"""
import datetime as dt

from sqlalchemy import select

from webvuln import cases, changes
from webvuln.models import AuditLog, Case

T = dt.date.today()


def d(n):
    return (T + dt.timedelta(days=n)).isoformat()


def _f(i, **kw):
    base = dict(sheet_key="s1", plugin_id=str(200 + i), name=f"弱點{i}", host=f"10.40.0.{i}", severity="High",
                department="資訊架構部", owner="段譽", remediation_due=d(10), close_status="未結案",
                raw={"Plugin ID": str(200 + i), "Host": f"10.40.0.{i}"})
    base.update(kw)
    return base


def _batch1():
    return [
        _f(1, remediation_due=d(10)),                       # A：已過行動線 → 應申請未申請（之後標送審中）
        _f(2, remediation_due=d(-5)),                       # B：已逾期、應申請未申請（之後標展延送審中）
        _f(3, remediation_due=d(200)),                      # C：暫無需申請
        _f(4, remediation_due=d(-10), exception_due=d(90), remark="例外管理(iForm_9)"),   # D：已核准（例外）
        _f(5, remediation_due=d(5)),                        # E：應申請未申請（沒標）
    ]


def _imp(client, fs, name):
    r = client.post("/api/import", json={"source_file": name, "findings": fs})
    assert r.status_code == 200, r.text


def _ids(client):
    return {r["host"]: r["id"] for r in client.get("/api/findings", params={"status": "全部"}).json()}


def _hosts(client, **params):
    return {r["host"] for r in client.get("/api/findings", params=params).json()}


def _mark(client, fid, prog, **extra):
    body = {"set_progress": True, "progress": prog}
    body.update(extra)
    r = client.post(f"/api/findings/{fid}/overlay", json=body)
    assert r.status_code == 200, r.text


def _check_partition(client, horizon=None):
    """需申請三類互斥、相加＝需申請；需申請＋暫無需申請＝未結。"""
    hp = {} if horizon is None else {"horizon": horizon}
    sa = _hosts(client, should_apply="true", **hp)
    sub = _hosts(client, submitted="true", **hp)
    ap = _hosts(client, applied="true", **hp)
    uni = _hosts(client, apply_universe="true", **hp)
    nota = _hosts(client, not_apply="true", **hp)
    assert not (sa & sub) and not (sa & ap) and not (sub & ap)
    assert sa | sub | ap == uni
    assert not (uni & nota) and uni | nota == _hosts(client)
    return sa, sub, ap


def test_mark_submitted_moves_out_of_need_apply(client):
    _imp(client, _batch1(), "a.xlsx")
    ids = _ids(client)
    r0 = client.get("/api/report").json()
    assert r0["need_apply_count"] == 3 and r0["submitted_count"] == 0 and r0["applied_count"] == 1

    _mark(client, ids["10.40.0.1"], "要申請例外")
    _mark(client, ids["10.40.0.1"], "例外送審中")
    assert "10.40.0.1" not in _hosts(client, should_apply="true")
    assert _hosts(client, submitted="true") == {"10.40.0.1"}

    r = client.get("/api/report").json()
    assert r["need_apply_count"] == r0["need_apply_count"] - 1
    assert r["submitted_count"] == 1
    assert r["applied_count"] == r0["applied_count"]
    assert r["apply_universe"] == r["need_apply_count"] + r["submitted_count"] + r["applied_count"]
    assert r["apply_universe"] == r0["apply_universe"]          # 需申請總數不變，只是換一類
    assert r["unresolved"] == r0["unresolved"]
    assert [x["host"] for x in r["submitted_list"]] == ["10.40.0.1"]
    assert "10.40.0.1" not in {x["host"] for x in r["need_apply_list"]}

    row = next(x for x in client.get("/api/findings", params={"submitted": "true"}).json())
    assert row["progress"] == "例外送審中" and row["progress_state"] == "等核准"
    assert row["effective_due"] == d(10)                        # 到期日不動
    assert row["close_status"] == "未結案" and row["stage"] == "原始修補期限"
    assert row["submitted"] is True and row["should_apply"] is False
    _check_partition(client)

    # 已逾期的標送審中：仍算已逾期（不是核准）
    _mark(client, ids["10.40.0.2"], "展延送審中")
    assert "10.40.0.2" in _hosts(client, band="已逾期")
    assert _hosts(client, submitted="true") == {"10.40.0.1", "10.40.0.2"}
    r2 = client.get("/api/report").json()
    assert r2["overdue"] == r0["overdue"] and r2["submitted_count"] == 2 and r2["need_apply_count"] == 1
    # 首頁摘要／排行的「應提申請未提」也扣掉送審中
    assert client.get("/api/summary").json()["should_apply"] == 1


def test_horizon_excludes_submitted(client):
    _imp(client, _batch1(), "a.xlsx")
    ids = _ids(client)
    _mark(client, ids["10.40.0.1"], "例外送審中")
    for h in (0, 365):
        sa, sub, ap = _check_partition(client, h)
        assert "10.40.0.1" not in sa and "10.40.0.1" in sub
    # 365 天後 D 的例外已到期 → 移到應申請未申請（不是送審中，因為 D 沒標）
    sa, sub, ap = _check_partition(client, 365)
    assert "10.40.0.4" in sa and "10.40.0.4" not in ap


def test_reimport_auto_resolves_and_lands_in_applied(client, session):
    _imp(client, _batch1(), "a.xlsx")
    ids = _ids(client)
    _mark(client, ids["10.40.0.1"], "例外送審中")
    _mark(client, ids["10.40.0.2"], "展延送審中")
    b2 = _batch1()
    b2[0] = _f(1, remediation_due=d(10), exception_due=d(120), remark="例外管理(iForm_123)")   # A：資安核准例外
    b2[1] = _f(2, remediation_due=d(-5), close_status="已結案")                               # B：已結案
    _imp(client, b2, "b.xlsx")
    ids2 = _ids(client)

    a = next(x for x in client.get("/api/findings").json() if x["host"] == "10.40.0.1")
    assert a["stage"] == "例外管理中" and a["effective_due"] == d(120)
    assert a["progress"] == "" and a["progress_state"] is None
    assert "10.40.0.1" in _hosts(client, applied="true")
    assert _hosts(client, submitted="true") == set()

    ab = [c for c in session.execute(select(Case)).scalars().all() if c.host in ("10.40.0.1", "10.40.0.2")]
    assert len(ab) == 2 and all(c.status == "" for c in ab)
    logs = session.execute(select(AuditLog).where(AuditLog.action == "auto_resolve_submit")).scalars().all()
    assert len(logs) == 2 and all(lg.username == "系統" for lg in logs)
    assert {lg.target for lg in logs} == {f"vuln:{c.vuln_key}" for c in ab}

    h = client.get(f"/api/findings/{ids2['10.40.0.1']}/history").json()
    txt = " ".join(i["text"] for i in h["items"])
    assert "資安已核准，自動解除" in txt and "例外送審中（等核准）" in txt
    hb = client.get(f"/api/findings/{ids2['10.40.0.2']}/history").json()
    assert "已結案，自動解除" in " ".join(i["text"] for i in hb["items"])

    # 冪等：再跑一次沒有東西可解除
    from webvuln import query
    assert cases.auto_resolve_submitted(session, query.latest_batch(session)) == 0

    # 與上次比較：上次 2 筆送審中、本次 0；等式兜得起來；需申請三類相加＝需申請
    c = client.get("/api/compare").json()
    assert c["equation"]["ok"] is True, c["equation"]
    m = {x["key"]: x for x in c["metrics"]}
    assert (m["submitted"]["prev"], m["submitted"]["cur"]) == (2, 0)
    for side in ("prev", "cur"):
        assert m["apply_universe"][side] == m["need_apply"][side] + m["submitted"][side] + m["applied"][side]
        assert m["need_apply"][side] == sum(m[k][side] for k in ("todo", "wip", "subext", "subexc", "rescan"))
    assert m["applied"]["cur"] == 2                             # A 落到已核准（D 原本就是）
    assert {e["key"]: e["n"] for e in c["events"]}["approved"] == 1
    _check_partition(client)


def test_staleness_and_setting(client, session):
    _imp(client, _batch1(), "a.xlsx")
    ids = _ids(client)
    _mark(client, ids["10.40.0.1"], "例外送審中")
    c = next(c for c in session.execute(select(Case)).scalars().all() if c.status == "例外送審中")
    c.status_changed_at = dt.datetime.now() - dt.timedelta(days=20)
    session.commit()

    r = client.get("/api/report").json()
    assert r["submitted_stale_count"] == 1 and r["submit_stale_days"] == 14
    row = r["submitted_list"][0]
    assert row["progress_state"] == "送審逾時" and row["submit_days"] == 20
    assert "10.40.0.1" in {x["host"] for x in r["flagged_list"]}        # 送審逾時也列待追查
    assert "10.40.0.1" in _hosts(client, flagged="true")

    # 只改備註（編輯視窗存檔會帶同樣的進度）不會把送審時間重設
    _mark(client, ids["10.40.0.1"], "例外送審中", set_note=True, note="10/1 已送 iForm")
    assert client.get("/api/report").json()["submitted_stale_count"] == 1

    # 門檻調成 30 天 → 不算逾時
    s = client.post("/api/general-settings", json={"submit_stale_days": 30})
    assert s.status_code == 200, s.text
    assert s.json()["submit_stale_days"] == 30
    r2 = client.get("/api/report").json()
    assert r2["submitted_stale_count"] == 0 and r2["submitted_list"][0]["progress_state"] == "等核准"
    assert "10.40.0.1" not in _hosts(client, flagged="true")
    # 範圍 1～180
    assert client.post("/api/general-settings", json={"submit_stale_days": 999}).json()["submit_stale_days"] == 180


def test_reflected_stage_and_unknown_progress(client):
    _imp(client, _batch1(), "a.xlsx")
    ids = _ids(client)
    # 已在例外管理中又標例外送審中：已反映，算已核准，不算送審中
    _mark(client, ids["10.40.0.4"], "例外送審中")
    row = next(x for x in client.get("/api/findings").json() if x["host"] == "10.40.0.4")
    assert row["progress_state"] == "已反映" and row["submitted"] is False
    assert "10.40.0.4" in _hosts(client, applied="true") and "10.40.0.4" not in _hosts(client, submitted="true")
    # 不認得的進度值擋下
    r = client.post(f"/api/findings/{ids['10.40.0.1']}/overlay", json={"set_progress": True, "progress": "已核准"})
    assert r.status_code == 400


def test_audit_text_renders_submitted():
    t = changes._audit_text(str({"progress": "展延送審中"}))
    assert t == "處理進度 → 展延送審中（等核准）"
    t2 = changes._audit_text(str({"progress": "展延送審中→（資安已核准，自動解除）", "stage": "首次展延中", "due": "2027-01-19"}))
    assert t2.startswith("處理進度：展延送審中（等核准） → （資安已核准，自動解除）")
    assert "處置階段 → 首次展延中" in t2 and "到期日 → 2027-01-19" in t2


def test_expired_approval_not_applied_without_horizon(client):
    """2026-10-19 統一口徑：不給 horizon＝今天（同 horizon=0）。例外／展延今天已過期＝失去例外管理，
    不算已核准 → 應申請未申請；標了送審中（承辦已送續申請）→ 送審中；送審中的核准過期不會被自動解除。"""
    fs = _batch1() + [
        _f(6, remediation_due=d(-60), exception_due=d(-2), remark="例外管理(iForm_6)"),        # F：例外已過期
        _f(7, remediation_due=d(-60), first_extension_due=d(-1), remark="首次展延(iForm_7)"),  # G：展延已過期
    ]
    _imp(client, fs, "a.xlsx")
    ids = _ids(client)
    for h in (None, 0):
        sa, sub, ap = _check_partition(client, h)
        assert {"10.40.0.6", "10.40.0.7"} <= sa and not ({"10.40.0.6", "10.40.0.7"} & ap)
    r = client.get("/api/report").json()
    assert {"10.40.0.6", "10.40.0.7"} <= {x["host"] for x in r["need_apply_list"]}
    assert r["applied_count"] == 1                     # 只剩 D（例外到 d(90)）
    assert client.get("/api/summary").json()["should_apply"] == r["need_apply_count"]

    _mark(client, ids["10.40.0.6"], "例外送審中")
    _mark(client, ids["10.40.0.7"], "展延送審中")      # 展延已過期又標展延送審中：核准已失效，照樣等核准
    assert _hosts(client, submitted="true") == {"10.40.0.6", "10.40.0.7"}
    rows = {x["host"]: x for x in client.get("/api/findings").json()}
    assert rows["10.40.0.7"]["progress_state"] == "等核准" and rows["10.40.0.7"]["submitted"] is True
    assert rows["10.40.0.7"]["should_apply"] is False
    _check_partition(client)

    # 重匯同一份：過期的核准不是「資安已核准」，標記保留
    _imp(client, fs, "b.xlsx")
    assert _hosts(client, submitted="true") == {"10.40.0.6", "10.40.0.7"}
    c = client.get("/api/compare").json()
    m = {x["key"]: x for x in c["metrics"]}
    rep = client.get("/api/report").json()
    assert (m["need_apply"]["cur"], m["submitted"]["cur"], m["applied"]["cur"]) == \
        (rep["need_apply_count"], rep["submitted_count"], rep["applied_count"])

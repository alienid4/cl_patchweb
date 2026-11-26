"""資安已確認結案（待 Excel 更新）（2026-10-28）。

資安已確認修好／結案、來源 Excel 還沒更新：承辦標這個狀態＋證據（確認人／日期／依據）。
系統所有計數把它當已結案；Excel／finding 不改；重匯不洗掉；Excel 真的結案時自動解除並留紀錄。
"""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from webvuln import automail, casebackup, cases, importer, query, rowkey
from webvuln.db import _make_engine
from webvuln.models import AuditLog, Base, Case, Finding
from webvuln.schemas import FindingIn, ImportIn

T = dt.date.today()
DEPT = "資訊架構部"
CONF = "資安已確認結案"


def d(n):
    return (T + dt.timedelta(days=n)).isoformat()


def _f(i, **kw):
    base = dict(sheet_key="s1", plugin_id=str(300 + i), name=f"弱點{i}", host=f"10.50.0.{i}", severity="High",
                department=DEPT, owner="段譽", remediation_due=d(10), close_status="未結案",
                raw={"Plugin ID": str(300 + i), "Host": f"10.50.0.{i}", "結案狀態": "未結案", "負責單位": DEPT})
    base.update(kw)
    return base


def _batch():
    return [
        _f(1, remediation_due=d(-5)),        # A：已逾期、應申請未申請 → 標資安已確認
        _f(2, remediation_due=d(10)),        # B：應申請未申請
        _f(3, remediation_due=d(200)),       # C：暫無需申請
        _f(4, remediation_due=d(-3), owner="喬峰"),   # D：已逾期
        _f(5, remediation_due=d(-9), close_status="已結案", raw={"Host": "10.50.0.5", "結案狀態": "已結案"}),
    ]


EV = {"confirm_by": "資安王小明", "confirm_date": d(-1), "confirm_basis": "email：主旨 10/27 弱點複測結果"}


def _imp(client, fs, name):
    r = client.post("/api/import", json={"source_file": name, "findings": fs})
    assert r.status_code == 200, r.text


def _ids(client):
    return {r["host"]: r["id"] for r in client.get("/api/findings", params={"status": "全部"}).json()}


def _hosts(client, **params):
    return {r["host"] for r in client.get("/api/findings", params=params).json()}


def _confirm(client, fid, ev=EV, **extra):
    body = dict({"set_progress": True, "progress": CONF}, **(ev or {}))
    body.update(extra)
    return client.post(f"/api/findings/{fid}/overlay", json=body)


def _case_of(session, host):
    session.expire_all()
    return next(c for c in session.execute(select(Case)).scalars().all() if c.host == host)


def test_evidence_required(client, session):
    _imp(client, _batch(), "a.xlsx")
    ids = _ids(client)
    a = ids["10.50.0.1"]
    assert _confirm(client, a, ev=None).status_code == 400                                     # 沒證據
    assert _confirm(client, a, ev={k: v for k, v in EV.items() if k != "confirm_basis"}).status_code == 400
    assert _confirm(client, a, ev=dict(EV, confirm_date=d(1))).status_code == 400              # 未來日期
    assert _confirm(client, a, ev=dict(EV, confirm_date="2026/13/40")).status_code == 400
    assert _confirm(client, ids["10.50.0.5"]).status_code == 400                               # Excel 已結案
    # 失敗不留任何東西（含同時帶的備註）
    assert _confirm(client, a, ev=None, set_note=True, note="不該存").status_code == 400
    assert not next(r for r in client.get("/api/findings", params={"status": "全部"}).json() if r["id"] == a)["track_note"]
    r = _confirm(client, a)
    assert r.status_code == 200, r.text
    c = _case_of(session, "10.50.0.1")
    assert (c.status, c.confirm_by, c.confirm_date.isoformat(), c.confirm_basis) == (
        CONF, EV["confirm_by"], EV["confirm_date"], EV["confirm_basis"])
    # 同狀態重存（編輯視窗只改備註）不帶證據 → 沿用原證據
    assert client.post(f"/api/findings/{a}/overlay", json={"set_progress": True, "progress": CONF,
                                                           "set_note": True, "note": "x"}).status_code == 200
    assert _case_of(session, "10.50.0.1").confirm_by == EV["confirm_by"]
    # 清掉狀態 → 證據一起清
    assert client.post(f"/api/findings/{a}/overlay", json={"set_progress": True, "progress": ""}).status_code == 200
    c = _case_of(session, "10.50.0.1")
    assert (c.status, c.confirm_by, c.confirm_date, c.confirm_basis) == ("", None, None, None)
    # 清掉後再設、不帶證據 → 不能沿用舊的
    assert _confirm(client, a, ev=None).status_code == 400


def test_counts_drop_everywhere(client, session):
    _imp(client, _batch(), "a.xlsx")
    ids = _ids(client)
    r0 = client.get("/api/report").json()
    s0 = client.get("/api/summary").json()
    sh0 = {x["sheet_key"]: x["open"] for x in client.get("/api/sheets").json()}
    assert _confirm(client, ids["10.50.0.1"]).status_code == 200

    r = client.get("/api/report").json()
    assert r["unresolved"] == r0["unresolved"] - 1
    assert r["overdue"] == r0["overdue"] - 1
    assert r["need_apply_count"] == r0["need_apply_count"] - 1
    assert r["apply_universe"] == r["need_apply_count"] + r["submitted_count"] + r["applied_count"]
    assert r["sec_confirmed_count"] == 1 and r["excel_unresolved"] == r0["unresolved"]
    assert [x["host"] for x in r["sec_confirmed_list"]] == ["10.50.0.1"]
    row = r["sec_confirmed_list"][0]
    assert row["confirm_by"] == EV["confirm_by"] and row["progress_state"] == "已確認待更新"
    assert "10.50.0.1" not in {x["host"] for x in r["overdue_list"] + r["need_apply_list"]}

    # 下鑽：預設未結案不含；「資安已確認」只列它；全部含
    assert "10.50.0.1" not in _hosts(client)
    assert "10.50.0.1" not in _hosts(client, band="已逾期")
    assert _hosts(client, status="資安已確認") == {"10.50.0.1"}
    assert "10.50.0.1" in _hosts(client, status="全部")
    full = next(x for x in client.get("/api/findings", params={"status": "全部"}).json() if x["host"] == "10.50.0.1")
    assert full["close_status"] == "未結案"                          # Excel 原值不動
    assert full["eff_close_status"] == "資安已確認" and full["sec_confirmed"] is True
    for h in (None, 0, 365):   # 需申請三類＋暫無需申請＝系統未結
        hp = {} if h is None else {"horizon": h}
        parts = [_hosts(client, **{k: "true"}, **hp) for k in ("should_apply", "submitted", "applied", "not_apply")]
        assert sum(len(p) for p in parts) == len(_hosts(client)) and "10.50.0.1" not in set().union(*parts)

    s = client.get("/api/summary").json()
    assert s["unresolved"] == s0["unresolved"] - 1 and s["closed"] == s0["closed"] + 1
    assert s["overdue"] == s0["overdue"] - 1
    assert {x["sheet_key"]: x["open"] for x in client.get("/api/sheets").json()}["s1"] == sh0["s1"] - 1
    rk = {x["name"]: x for x in client.get("/api/ranking").json()}
    assert rk["段譽"]["unresolved"] == 2 and rk["段譽"]["closed"] == 2   # Excel 已結 E ＋ 資安已確認 A
    assert client.get("/api/trend").json()[-1]["open"] == r["unresolved"]
    assert sum(x["unresolved"] for x in client.get("/api/sla").json()) == r["unresolved"]
    assert client.get("/api/matrix").json()["total"] == r["unresolved"]

    rc = client.get("/api/reconcile").json()
    assert rc["all_ok"] is True, rc["checks"]
    line = next(c for c in rc["checks"] if c["name"].startswith("Excel 未結"))
    assert line["a"] == r0["unresolved"] and line["b"] == r["unresolved"] + 1 and line["ok"]


def test_snapshot_overview_matches(client):
    _imp(client, _batch(), "a.xlsx")
    ids = _ids(client)
    sig0 = client.get("/api/snapshot-meta").json()["ov_sig"]
    _confirm(client, ids["10.50.0.1"])
    assert client.get("/api/snapshot-meta").json()["ov_sig"] != sig0     # 快取要重抓
    snap = client.get("/api/snapshot", params={"full": 1}).json()
    rows = snap["sheets"][0]["rows"]
    st = {r["Host"]: r["結案狀態"] for r in rows}
    assert st["10.50.0.1"].startswith("已結案") and "資安已確認" in st["10.50.0.1"]
    assert st["10.50.0.2"] == "未結案"
    # 總覽（sheets.js classifyClose 的 Python 複刻）未結＝週報未結
    OPEN_WORDS = ['未結', '進行中', '處理中', '追蹤中', '未完成', '未修補', '未修復', '結案中', '修補中', '改善中']
    n_open = sum(1 for v in st.values() if any(w in v for w in OPEN_WORDS))
    assert n_open == client.get("/api/report").json()["unresolved"]


def test_compare_event_and_equation(client):
    _imp(client, _batch(), "a.xlsx")
    _imp(client, _batch(), "b.xlsx")
    ids = _ids(client)
    _confirm(client, ids["10.50.0.1"])
    c = client.get("/api/compare").json()
    assert c["equation"]["ok"] is True, c["equation"]
    ev = {e["key"]: e["n"] for e in c["events"]}
    assert ev["sec_confirmed"] == 1 and ev["closed"] == 0
    m = {x["key"]: x for x in c["metrics"]}
    assert m["unresolved"]["cur"] == m["unresolved"]["prev"] - 1
    assert m["unresolved"]["cur"] == client.get("/api/report").json()["unresolved"]
    rows = client.get("/api/compare/rows", params={"event": "sec_confirmed"}).json()
    assert [x["host"] for x in rows] == ["10.50.0.1"]
    # 再匯一次（上次＝已確認時拍下）：上次也當已結，不再算事件；等式照樣成立
    _imp(client, _batch(), "c.xlsx")
    c2 = client.get("/api/compare").json()
    assert c2["equation"]["ok"] is True and {e["key"]: e["n"] for e in c2["events"]}["sec_confirmed"] == 0
    m2 = {x["key"]: x for x in c2["metrics"]}
    assert m2["unresolved"]["prev"] == m2["unresolved"]["cur"]


def test_reimport_keeps_then_excel_close_clears(client, session):
    _imp(client, _batch(), "a.xlsx")
    ids = _ids(client)
    _confirm(client, ids["10.50.0.1"])
    _imp(client, _batch(), "b.xlsx")                       # 重匯（Excel 還沒更新）→ 保留
    assert _hosts(client, status="資安已確認") == {"10.50.0.1"}
    assert _case_of(session, "10.50.0.1").confirm_by == EV["confirm_by"]
    b3 = _batch()
    b3[0] = _f(1, remediation_due=d(-5), close_status="已結案")
    _imp(client, b3, "c.xlsx")                             # Excel 終於結案 → 自動解除
    c = _case_of(session, "10.50.0.1")
    assert (c.status, c.confirm_by, c.confirm_date, c.confirm_basis) == ("", None, None, None)
    logs = session.execute(select(AuditLog).where(AuditLog.action == "auto_resolve_confirm")).scalars().all()
    assert len(logs) == 1 and logs[0].username == "系統" and EV["confirm_basis"] in logs[0].detail
    fid = _ids(client)["10.50.0.1"]
    txt = " ".join(i["text"] for i in client.get(f"/api/findings/{fid}/history").json()["items"])
    assert "資安 Excel 已結案，自動解除" in txt
    assert "確認人 → 資安王小明" in txt and "依據 → " + EV["confirm_basis"] in txt     # 設定時的證據也在歷程
    _imp(client, b3, "d.xlsx")                             # 冪等
    assert len(session.execute(select(AuditLog).where(AuditLog.action == "auto_resolve_confirm")).scalars().all()) == 1
    # 來源消失 → 保留
    _confirm(client, _ids(client)["10.50.0.2"])
    _imp(client, [x for x in b3 if x["host"] != "10.50.0.2"], "e.xlsx")
    assert _case_of(session, "10.50.0.2").status == CONF


def test_stale_and_setting(client, session, super_login):
    _imp(client, _batch(), "a.xlsx")
    ids = _ids(client)
    assert _confirm(client, ids["10.50.0.1"], ev=dict(EV, confirm_date=d(-20))).status_code == 200
    r = client.get("/api/report").json()
    assert r["sec_confirmed_stale_count"] == 1 and r["confirm_stale_days"] == 14
    assert r["sec_confirmed_list"][0]["progress_state"] == "確認逾期未更新"
    assert r["sec_confirmed_list"][0]["confirm_days"] == 20
    assert "10.50.0.1" in {x["host"] for x in r["flagged_list"]} and r["progress"]["flagged"] >= 1
    assert "10.50.0.1" in _hosts(client, flagged="true")
    assert client.post("/api/general-settings", json={"confirm_stale_days": 30}).json()["confirm_stale_days"] == 30
    r2 = client.get("/api/report").json()
    assert r2["sec_confirmed_stale_count"] == 0 and "10.50.0.1" not in _hosts(client, flagged="true")
    assert client.post("/api/general-settings", json={"confirm_stale_days": 0}).json()["confirm_stale_days"] == 1


def test_auto_mail_dunning_and_inventory(client, session):
    _imp(client, _batch(), "a.xlsx")
    ids = _ids(client)
    _confirm(client, ids["10.50.0.1"], ev=dict(EV, confirm_date=d(-20)))
    cfg = automail.get_settings(session)
    jobs = automail.build_jobs(session, cfg, {"subject_prefix": ""}, T, departments=[DEPT], force=True)
    dun = [j for j in jobs if j["kind"] == automail.KIND_DUN]
    hosts = {it["host"] for j in dun for it in j["items"]}
    assert "10.50.0.1" not in hosts and "10.50.0.4" in hosts
    inv = next(j for j in jobs if j["kind"] == automail.KIND_INV)
    assert "資安已確認待 Excel 更新 1 筆（其中超過 14 天 1 筆）" in inv["body"]
    assert "· 未結：%d" % client.get("/api/report").json()["unresolved"] in inv["body"]


def test_bulk_with_evidence(client, session):
    _imp(client, _batch(), "a.xlsx")
    ids = _ids(client)
    two = [ids["10.50.0.2"], ids["10.50.0.3"]]
    r = client.post("/api/findings/bulk-overlay", json={"ids": two, "set_progress": True, "progress": CONF,
                                                         "set_note": True, "note": "不該存"})
    assert r.json()["applied"] == 0 and len(r.json()["failed"]) == 2
    assert all(not _case_of(session, h).track_note for h in ("10.50.0.2", "10.50.0.3"))
    r = client.post("/api/findings/bulk-overlay", json=dict({"ids": two, "set_progress": True, "progress": CONF}, **EV))
    assert r.json()["applied"] == 2
    assert _hosts(client, status="資安已確認") == {"10.50.0.2", "10.50.0.3"}
    assert all(_case_of(session, h).confirm_basis == EV["confirm_basis"] for h in ("10.50.0.2", "10.50.0.3"))


def _db(tmp_path, name):
    eng = _make_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False, class_=Session)()
    rowkey.migrate(s)
    importer.create_batch(s, ImportIn(source_file="t.xlsx", findings=[FindingIn(**_f(1)), FindingIn(**_f(2))]))
    return s


def test_casebackup_carries_evidence(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    fa = a.execute(select(Finding).where(Finding.host == "10.50.0.1")).scalars().first()
    cases.set_overlay(a, fa.id, dict({"progress": CONF}, **EV))
    data = casebackup.export(a)
    it = next(x for x in data["items"] if x["status"] == CONF)
    assert (it["confirm_by"], it["confirm_date"], it["confirm_basis"]) == (EV["confirm_by"], EV["confirm_date"], EV["confirm_basis"])
    # B 機同一筆已有不同證據 → 衝突，預設保留現有，勾覆蓋才換
    fb = b.execute(select(Finding).where(Finding.host == "10.50.0.1")).scalars().first()
    cases.set_overlay(b, fb.id, dict({"progress": CONF}, **dict(EV, confirm_by="別人")))
    r = casebackup.restore(b, data, apply=True)
    assert r["skipped_conflict"] == 1
    cb = b.execute(select(Case).where(Case.vuln_key == cases.key_str(fb))).scalars().first()
    assert cb.confirm_by == "別人"
    casebackup.restore(b, data, overwrite=True, apply=True)
    b.expire_all()
    cb = b.execute(select(Case).where(Case.vuln_key == cases.key_str(fb))).scalars().first()
    assert (cb.status, cb.confirm_by, cb.confirm_date.isoformat(), cb.confirm_basis) == (
        CONF, EV["confirm_by"], EV["confirm_date"], EV["confirm_basis"])
    assert casebackup.restore(b, data, apply=True)["same"] == 1
    assert query.find(b, status="資安已確認")[0]["host"] == "10.50.0.1"


def test_casebackup_skips_confirmed_when_target_closed(tmp_path):
    """還原到的那台這筆 Excel 已結案 → 不還原成資安已確認（備註照搬），預覽列警告。"""
    a = _db(tmp_path, "a.db")
    fa = a.execute(select(Finding).where(Finding.host == "10.50.0.1")).scalars().first()
    cases.set_overlay(a, fa.id, dict({"progress": CONF, "note": "照搬"}, **EV))
    data = casebackup.export(a)
    eng = _make_engine(f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    Base.metadata.create_all(eng)
    b = sessionmaker(bind=eng, expire_on_commit=False, class_=Session)()
    rowkey.migrate(b)
    importer.create_batch(b, ImportIn(source_file="t.xlsx", findings=[FindingIn(**_f(1, close_status="已結案")), FindingIn(**_f(2))]))
    pre = casebackup.restore(b, data)
    assert any("Excel 已不是未結案" in w for w in pre["warnings"])
    casebackup.restore(b, data, apply=True)
    c = b.execute(select(Case).where(Case.host == "10.50.0.1")).scalars().first()
    assert c.status != CONF and c.confirm_by is None and c.track_note == "照搬"


def test_db_migration_adds_columns(tmp_path):
    """舊 DB（case_overlay 沒有 confirm_* 欄）開機時自動補欄。"""
    import sqlite3
    from webvuln import db as dbm
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE case_overlay (id INTEGER PRIMARY KEY, vuln_key VARCHAR(400), status VARCHAR(30))")
    con.commit(); con.close()
    eng = _make_engine(f"sqlite:///{p.as_posix()}")
    dbm._ensure_columns(eng)
    con = sqlite3.connect(p)
    cols = {r[1] for r in con.execute("PRAGMA table_info(case_overlay)")}
    con.close()
    assert {"confirm_by", "confirm_date", "confirm_basis"} <= cols
    eng.dispose()

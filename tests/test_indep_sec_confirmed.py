"""獨立測試：資安已確認結案（待 Excel 更新）（2026-10-28）。

不是功能作者寫的；目的在找破口。已知問題標 xfail(strict=True)，修好後會變 XPASS → 失敗，提醒拿掉標記。
不碰真實 SMTP（automail 只建 jobs，不寄）。
"""
import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from webvuln import automail, casebackup, cases, config, importer, query, rowkey, security
from webvuln.db import _make_engine
from webvuln.models import AuditLog, Base, Case, Finding
from webvuln.schemas import FindingIn, ImportIn

T = dt.date.today()
CONF = "資安已確認結案"
A, B = "資訊架構部", "網路部"
S1, S2 = "1-主機弱點", "2-網站弱點"


def d(n):
    return (T + dt.timedelta(days=n)).isoformat()


def EV(days_ago=1, by="資安 陳大文", basis="email 10/27 複測結果"):
    return {"confirm_by": by, "confirm_date": d(-days_ago), "confirm_basis": basis}


def _f(host, sheet=S1, dept=A, owner="段譽", due=10, close="未結案", **kw):
    raw = {"Plugin ID": "p-" + host, "Host": host, "結案狀態": close, "負責單位": dept, "負責人": owner}
    base = dict(sheet_key=sheet, plugin_id="p-" + host, name="弱點" + host, host=host, severity="High",
                department=dept, owner=owner, remediation_due=(d(due) if due is not None else None),
                close_status=close, raw=raw)
    base.update(kw)
    return base


def dataset():
    return [
        _f("h01", due=-5),                                                   # 逾期 → 確認（新鮮）
        _f("h02", due=-3, owner="喬峰"),                                      # 逾期 open
        _f("h03", due=10),                                                   # 應申請 → 標展延送審中
        _f("h04", due=200),                                                  # 暫無需申請
        _f("h05", due=-9, close="已結案"),                                    # Excel 已結
        _f("h06", sheet=S2, due=None, exception_due=d(40), remark="例外申請#123"),   # 例外 → 確認（逾期 20 天）
        _f("h07", sheet=S2, dept=B, owner="虛竹", due=None, first_extension_due=d(5), remark="展延申請#9"),
        _f("h08", sheet=S2, dept=B, owner="虛竹", due=20),                     # → 確認（剛好 14 天）
        _f("h09", dept=B, owner="虛竹", due=3, close="其他"),                  # 其他
        _f("h10", dept=B, owner="", due=-30),                                # 逾期、無負責人
        _f("h11", sheet=S2, due=None),                                       # 無到期日
        _f("h12", sheet=S2, dept=B, owner="虛竹", due=-1),                     # 逾期 → 確認（15 天）
    ]


CONFIRMED = {"h01": 1, "h06": 20, "h08": 14, "h12": 15}
SYS_OPEN = {"h02", "h03", "h04", "h07", "h10", "h11"}


def _imp(client, fs, name="a.xlsx"):
    r = client.post("/api/import", json={"source_file": name, "findings": fs})
    assert r.status_code == 200, r.text


def _ids(client):
    return {r["host"]: r["id"] for r in client.get("/api/findings", params={"status": "全部"}).json()}


def _hosts(client, **p):
    return [r["host"] for r in client.get("/api/findings", params=p).json()]


def _confirm(client, fid, ev, **extra):
    body = dict({"set_progress": True, "progress": CONF}, **(ev or {}))
    body.update(extra)
    return client.post(f"/api/findings/{fid}/overlay", json=body)


def _setup(client):
    _imp(client, dataset(), "a.xlsx")
    ids = _ids(client)
    for h, n in CONFIRMED.items():
        r = _confirm(client, ids[h], EV(n))
        assert r.status_code == 200, r.text
    r = client.post(f"/api/findings/{ids['h03']}/overlay", json={"set_progress": True, "progress": "展延送審中"})
    assert r.status_code == 200
    return ids


# sheets.js classifyClose 的 Python 複刻（含 other）
OPEN_WORDS = ['未結', '進行中', '處理中', '追蹤中', '未完成', '未修補', '未修復', '結案中', '修補中', '改善中']
CLOSED_WORDS = ['已結案', '結案', '已修補', '已修復', '已完成', '完成', '已改善', '改善完成', '已解決', '無需修補']


def classify_close(s, close_date=None):
    s = (s or "").strip()
    if s:
        if any(w in s for w in OPEN_WORDS):
            return "open"
        if any(w in s for w in CLOSED_WORDS):
            return "closed"
        return "other"
    return "closed" if close_date else "open"


def _close_col(row):
    for k in row:
        if k.replace(" ", "") in ("結案狀態", "改善狀況"):
            return row[k]
    return ""


def _overview_open(client, full):
    snap = client.get("/api/snapshot", params={"full": 1} if full else {}).json()
    per = {}
    for sh in snap["sheets"]:
        per[sh.get("sheet_key") or sh.get("name")] = sum(
            1 for r in sh["rows"] if classify_close(_close_col(r)) == "open")
    return per


# ───────────────────────── 1. 全域一致性 ─────────────────────────
def test_global_consistency_all_endpoints(client):
    _setup(client)
    U = len(SYS_OPEN)
    rep = client.get("/api/report").json()
    summ = client.get("/api/summary").json()
    assert rep["unresolved"] == summ["unresolved"] == U
    assert sorted(_hosts(client)) == sorted(SYS_OPEN)
    assert rep["excel_unresolved"] == U + len(CONFIRMED)
    assert rep["sec_confirmed_count"] == 4
    assert rep["sec_confirmed_stale_count"] == 2          # h06(20)、h12(15)；h08 剛好 14 不算
    assert sum(x["open"] for x in client.get("/api/sheets").json()) == U
    assert client.get("/api/matrix").json()["total"] == U
    assert sum(x["unresolved"] for x in client.get("/api/sla").json()) == U
    assert client.get("/api/stage-stats").json()["total"] == U
    assert sum(x["total"] for x in client.get("/api/owner-summary").json()) == U
    rk_o = client.get("/api/ranking").json()
    rk_d = client.get("/api/ranking", params={"by": "department"}).json()
    assert sum(x["unresolved"] for x in rk_o) == sum(x["unresolved"] for x in rk_d) == U
    tr = client.get("/api/trend").json()[-1]
    assert tr["open"] == U
    # 逾期：h02、h10
    od = sorted(_hosts(client, band="已逾期"))
    assert od == ["h02", "h10"]
    assert rep["overdue"] == summ["overdue"] == tr["overdue"] == len(od) == sum(x["overdue"] for x in rk_o)
    assert {x["host"] for x in rep["overdue_list"]} == set(od)
    # 已結＝Excel 已結＋資安已確認；下鑽兩個狀態加起來要對得上
    assert summ["closed"] == 1 + 4
    assert len(_hosts(client, status="已結案")) + len(_hosts(client, status="資安已確認")) == summ["closed"]
    assert sum(x["closed"] for x in rk_o) == summ["closed"]
    # 需申請四類（含缺期限・待確認，V2.108）＋暫無需申請＝系統未結，各 horizon
    for hp in ({}, {"horizon": 0}, {"horizon": 30}, {"horizon": 365}):
        parts = [set(_hosts(client, **{k: "true"}, **hp))
                 for k in ("should_apply", "submitted", "applied", "no_due_bucket", "not_apply")]
        assert sum(map(len, parts)) == U, hp
        assert len(set().union(*parts)) == U, hp
        assert not (set().union(*parts) & set(CONFIRMED)), hp
    assert rep["apply_universe"] == (rep["need_apply_count"] + rep["submitted_count"] + rep["applied_count"]
                                     + rep["no_due_count"])
    assert rep["need_apply_count"] == summ["should_apply"] == len(_hosts(client, should_apply="true"))
    assert rep["submitted_count"] == len(_hosts(client, submitted="true")) == 1
    # 任何未結清單都不含確認列
    lists = rep["overdue_list"] + rep["need_apply_list"] + rep["submitted_list"] + rep.get("apply_intent_list", [])
    assert not ({x["host"] for x in lists} & set(CONFIRMED))
    assert {x["host"] for x in rep["sec_confirmed_list"]} == set(CONFIRMED)
    # 對帳
    rc = client.get("/api/reconcile").json()
    assert rc["all_ok"], rc["checks"]
    assert rc["excel_open"] == 10 and rc["sec_confirmed"] == 4
    # 總覽（快照，瀏覽器算）未結＝週報未結；各表也對得上 /api/sheets
    sh = {x["sheet_key"]: x["open"] for x in client.get("/api/sheets").json()}
    for full in (False, True):
        ov = _overview_open(client, full)
        assert sum(ov.values()) == U, (full, ov)
        assert ov == sh, (full, ov, sh)


@pytest.mark.parametrize("dept,expect", [(A, {"h02", "h03", "h04", "h11"}), (B, {"h07", "h10"})])
def test_per_department_consistency(client, dept, expect):
    _setup(client)
    rep = client.get("/api/report", params={"department": dept}).json()
    summ = client.get("/api/summary", params={"department": dept}).json()
    assert set(_hosts(client, department=dept)) == expect
    assert rep["unresolved"] == summ["unresolved"] == len(expect)
    assert client.get("/api/matrix", params={"department": dept}).json()["total"] == len(expect)
    assert sum(x["total"] for x in client.get("/api/owner-summary", params={"department": dept}).json()) == len(expect)
    assert client.get("/api/trend", params={"department": dept}).json()[-1]["open"] == len(expect)
    rc = client.get("/api/reconcile", params={"department": dept}).json()
    assert rc["all_ok"], rc["checks"]
    nconf = sum(1 for h in CONFIRMED if next(x for x in dataset() if x["host"] == h)["department"] == dept)
    assert rep["sec_confirmed_count"] == nconf and rep["excel_unresolved"] == len(expect) + nconf


def test_compare_equation_mixed(client):
    _imp(client, dataset(), "a.xlsx")
    _imp(client, dataset(), "b.xlsx")
    ids = _ids(client)
    for h, n in CONFIRMED.items():
        assert _confirm(client, ids[h], EV(n)).status_code == 200
    c = client.get("/api/compare").json()
    assert c["equation"]["ok"], c["equation"]
    ev = {e["key"]: e["n"] for e in c["events"]}
    assert ev["sec_confirmed"] == 4 and ev["closed"] == 0
    m = {x["key"]: x for x in c["metrics"]}
    rep = client.get("/api/report").json()
    assert m["unresolved"]["cur"] == rep["unresolved"]
    assert m["overdue"]["cur"] == rep["overdue"]
    assert m["need_apply"]["cur"] == rep["need_apply_count"]
    for dept in (A, B):
        cd = client.get("/api/compare", params={"department": dept}).json()
        assert cd["equation"]["ok"], (dept, cd["equation"])


# ───────────────────────── 2. 重匯 ─────────────────────────
def _audit(session, action="auto_resolve_confirm"):
    session.expire_all()
    return session.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _case(session, host):
    session.expire_all()
    return next(c for c in session.execute(select(Case)).scalars().all() if c.host == host)


def test_reimport_lifecycle(client, session):
    _setup(client)
    r0 = client.get("/api/report").json()
    s0 = client.get("/api/summary").json()
    _imp(client, dataset(), "b.xlsx")                        # 同一份 → 狀態保留、數字不變
    r1 = client.get("/api/report").json()
    assert (r1["unresolved"], r1["overdue"], r1["sec_confirmed_count"]) == (r0["unresolved"], r0["overdue"], 4)
    assert client.get("/api/summary").json()["closed"] == s0["closed"]
    # Excel 把 h01 結案、h06 改成「其他」→ 兩筆自動解除；系統未結不變
    b2 = [(_f("h01", due=-5, close="已結案") if x["host"] == "h01" else
           _f("h06", sheet=S2, due=None, exception_due=d(40), remark="例外申請#123", close="其他") if x["host"] == "h06"
           else x) for x in dataset()]
    _imp(client, b2, "c.xlsx")
    r2 = client.get("/api/report").json()
    assert r2["unresolved"] == r0["unresolved"] and r2["sec_confirmed_count"] == 2
    for h in ("h01", "h06"):
        c = _case(session, h)
        assert (c.status, c.confirm_by, c.confirm_date, c.confirm_basis) == ("", None, None, None)
    logs = _audit(session)
    assert len(logs) == 2
    assert all("資安 陳大文" in l.detail and "email 10/27 複測結果" in l.detail for l in logs)
    assert any(d(-1) in l.detail for l in logs)
    assert client.get("/api/reconcile").json()["all_ok"]
    # h08 從 Excel 消失 → 保留；回來仍未結 → 仍是資安已確認、不算未結
    b3 = [x for x in b2 if x["host"] != "h08"]
    _imp(client, b3, "d.xlsx")
    assert _case(session, "h08").status == CONF
    _imp(client, b2, "e.xlsx")
    assert "h08" in _hosts(client, status="資安已確認") and "h08" not in _hosts(client)
    # h01 Excel 再改回未結（解除後）→ 一般未結
    b4 = [(_f("h01", due=-5) if x["host"] == "h01" else x) for x in b2]
    _imp(client, b4, "f.xlsx")
    assert "h01" in _hosts(client)
    row = next(x for x in client.get("/api/findings").json() if x["host"] == "h01")
    assert row["progress"] == "" and row["sec_confirmed"] is False and row["confirm_by"] is None
    assert client.get("/api/report").json()["unresolved"] == r0["unresolved"] + 1
    assert client.get("/api/reconcile").json()["all_ok"]
    c = client.get("/api/compare").json()
    assert c["equation"]["ok"], c["equation"]


def test_trend_prev_point_matches_compare_prev(client):
    """趨勢線上一點 vs 與上次比較的「上次未結」。"""
    _imp(client, dataset(), "a.xlsx")
    ids = _ids(client)
    assert _confirm(client, ids["h01"], EV(1)).status_code == 200
    _imp(client, dataset(), "b.xlsx")
    tr = client.get("/api/trend").json()
    c = client.get("/api/compare").json()
    m = {x["key"]: x for x in c["metrics"]}
    # 2026-10-28 修正：舊批也用拍下的進度，趨勢上一點＝比較的「上次未結」
    assert tr[-1]["open"] == m["unresolved"]["cur"]
    assert tr[-2]["open"] == m["unresolved"]["prev"]


def test_close_stats_vs_compare_when_excel_finally_closes(client):
    """資安已確認後 Excel 才結案：/api/close-stats 算「本期新結案」、/api/compare 不算結案（上次就已當已結）。"""
    _imp(client, dataset(), "a.xlsx")
    ids = _ids(client)
    assert _confirm(client, ids["h01"], EV(1)).status_code == 200
    _imp(client, dataset(), "b.xlsx")
    b = [(_f("h01", due=-5, close="已結案") if x["host"] == "h01" else x) for x in dataset()]
    _imp(client, b, "c.xlsx")
    cs = client.get("/api/close-stats").json()
    ev = {e["key"]: e["n"] for e in client.get("/api/compare").json()["events"]}
    # 2026-10-28 修正：兩邊同口徑——上次就已當已結，本期都不算新結案
    assert (cs["new_closed"], ev["closed"]) == (0, 0)


# ───────────────────────── 3. 驗證 ─────────────────────────
def test_validation_edge_cases(client, session):
    _imp(client, dataset(), "a.xlsx")
    ids = _ids(client)
    a = ids["h02"]
    for bad in ({"confirm_by": "  ", "confirm_date": d(-1), "confirm_basis": "x"},
                {"confirm_by": "x", "confirm_date": d(-1), "confirm_basis": "\t "},
                {"confirm_by": "x", "confirm_date": "", "confirm_basis": "x"},
                {"confirm_by": "x", "confirm_date": "abc", "confirm_basis": "x"},
                {"confirm_by": "x", "confirm_date": d(1), "confirm_basis": "x"}):
        r = _confirm(client, a, bad, set_note=True, note="不該存", set_target=True, target_date=d(3))
        assert r.status_code == 400, (bad, r.text)
    c = _case(session, "h02")
    assert c.status == "" and not c.track_note and c.target_date is None and c.confirm_by is None
    # 「其他」與已結案都拒絕
    assert _confirm(client, ids["h09"], EV()).status_code == 400
    assert _confirm(client, ids["h05"], EV()).status_code == 400
    # 今天 OK；日期帶時間的格式不能 500
    assert _confirm(client, a, EV(0)).status_code == 200
    r = _confirm(client, a, dict(EV(), confirm_date=d(-2) + "T09:00:00"))
    assert r.status_code in (200, 400), r.text
    # 超長字串截斷不 500
    r = _confirm(client, a, EV(by="王" * 300, basis="依" * 900))
    assert r.status_code == 200
    c = _case(session, "h02")
    assert len(c.confirm_by) == 100 and len(c.confirm_basis) == 500
    # 部分更新：只改依據，其他沿用
    r = client.post(f"/api/findings/{a}/overlay", json={"set_progress": True, "progress": CONF, "confirm_basis": "iForm#77"})
    assert r.status_code == 200
    c = _case(session, "h02")
    assert c.confirm_basis == "iForm#77" and len(c.confirm_by) == 100
    # 改成別的進度 → 證據清掉，回到未結
    assert client.post(f"/api/findings/{a}/overlay", json={"set_progress": True, "progress": "處理中"}).status_code == 200
    c = _case(session, "h02")
    assert (c.status, c.confirm_by, c.confirm_date, c.confirm_basis) == ("處理中", None, None, None)
    assert "h02" in _hosts(client)
    # 只帶證據、沒 set_progress → 不動
    r = client.post(f"/api/findings/{a}/overlay", json=dict({"set_note": True, "note": "n"}, **EV()))
    assert r.status_code == 200 and _case(session, "h02").status == "處理中" and _case(session, "h02").confirm_by is None


def test_bulk_partial(client, session):
    _imp(client, dataset(), "a.xlsx")
    ids = _ids(client)
    body = dict({"ids": [ids["h02"], ids["h05"], ids["h09"], 999999, ids["h04"]], "set_progress": True, "progress": CONF,
                 "set_note": True, "note": "批次備註"}, **EV())
    r = client.post("/api/findings/bulk-overlay", json=body).json()
    assert r["applied"] == 2 and len(r["failed"]) == 3
    for h in ("h05", "h09"):
        c = next((c for c in session.execute(select(Case)).scalars().all() if c.host == h), None)
        session.expire_all()
        assert c is None or (not c.track_note and c.status == "" and c.confirm_by is None), h
    assert set(_hosts(client, status="資安已確認")) == {"h02", "h04"}
    # 批次缺證據 → 全部失敗、什麼都不寫
    r = client.post("/api/findings/bulk-overlay", json={"ids": [ids["h10"], ids["h11"]], "set_progress": True,
                                                         "progress": CONF, "confirm_by": "x",
                                                         "set_note": True, "note": "不該存"}).json()
    assert r["applied"] == 0 and len(r["failed"]) == 2
    assert all(not _case(session, h).track_note for h in ("h10", "h11"))


def test_permissions(client, engine, monkeypatch):
    _imp(client, dataset(), "a.xlsx")
    ids = _ids(client)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        security.create_user(s, "dB", "pw12345", role="dept_admin", display_name="窗口B", department=B)
        security.create_user(s, "u1", "pw12345", role="user", display_name="喬峰", department=A)
    monkeypatch.setattr(config, "NO_AUTH", False)
    assert _confirm(client, ids["h02"], EV()).status_code == 401
    client.post("/api/login", json={"username": "dB", "password": "pw12345"})
    assert _confirm(client, ids["h02"], EV()).status_code == 403          # A 部門
    assert _confirm(client, ids["h10"], EV()).status_code == 200          # B 部門
    r = client.post("/api/findings/bulk-overlay", json=dict({"ids": [ids["h02"], ids["h12"]], "set_progress": True,
                                                              "progress": CONF}, **EV())).json()
    assert r["applied"] == 1 and len(r["skipped"]) == 1
    client.post("/api/logout")
    client.post("/api/login", json={"username": "u1", "password": "pw12345"})
    assert _confirm(client, ids["h01"], EV()).status_code == 403          # 段譽的
    assert _confirm(client, ids["h02"], EV()).status_code == 200          # 自己的


# ───────────────────────── 4. 逾期 ─────────────────────────
def test_stale_boundary_and_setting(client):
    _setup(client)
    rep = client.get("/api/report").json()
    st = {x["host"]: x["progress_state"] for x in rep["sec_confirmed_list"]}
    assert st == {"h01": "已確認待更新", "h08": "已確認待更新", "h06": "確認逾期未更新", "h12": "確認逾期未更新"}
    fl = set(_hosts(client, flagged="true"))
    assert {"h06", "h12"} <= fl and not ({"h01", "h08"} & fl)
    assert rep["progress"]["flagged"] == len(rep["flagged_list"]) == len(fl)
    assert rep["progress"]["sec_confirmed_stale"] == 2
    assert client.post("/api/general-settings", json={"confirm_stale_days": 13}).status_code == 200
    rep = client.get("/api/report").json()
    assert rep["sec_confirmed_stale_count"] == 3 and "h08" in set(_hosts(client, flagged="true"))
    assert rep["progress"]["flagged"] == len(rep["flagged_list"]) == len(_hosts(client, flagged="true"))
    assert client.post("/api/general-settings", json={"confirm_stale_days": 999}).json()["confirm_stale_days"] == 180
    # 依部門的待追查也一致
    for dept in (A, B):
        r = client.get("/api/report", params={"department": dept}).json()
        assert r["progress"]["flagged"] == len(_hosts(client, flagged="true", department=dept)), dept


# ───────────────────────── 5. 快照／總覽 ─────────────────────────
def test_snapshot_cache_invalidation(client):
    _imp(client, dataset(), "a.xlsx")
    ids = _ids(client)
    before_light = _overview_open(client, False)
    sig0 = client.get("/api/snapshot-meta").json()["ov_sig"]
    assert _confirm(client, ids["h02"], EV()).status_code == 200
    sig1 = client.get("/api/snapshot-meta").json()["ov_sig"]
    assert sig1 != sig0
    after_light = _overview_open(client, False)                         # 走伺服器快取也要變
    assert after_light[S1] == before_light[S1] - 1
    # 改證據（同狀態）不需要變 sig；清掉 → sig 回到原值、總覽回原數
    client.post(f"/api/findings/{ids['h02']}/overlay", json={"set_progress": True, "progress": ""})
    assert client.get("/api/snapshot-meta").json()["ov_sig"] == sig0
    assert _overview_open(client, False) == before_light
    assert _overview_open(client, True) == before_light


def test_snapshot_sheet_without_close_column(client):
    fs = dataset()
    x = _f("h20", sheet="3-無狀態欄", due=10)
    x["raw"] = {k: v for k, v in x["raw"].items() if k != "結案狀態"}
    _imp(client, fs + [x], "a.xlsx")
    ids = _ids(client)
    assert _confirm(client, ids["h20"], EV()).status_code == 200
    sh = {s["sheet_key"]: s["open"] for s in client.get("/api/sheets").json()}
    assert _overview_open(client, True)["3-無狀態欄"] == sh["3-無狀態欄"]


def test_snapshot_close_header_prefix_variant(client):
    x = _f("h21", due=10)
    x["raw"] = {("結案狀態(資安填)" if k == "結案狀態" else k): v for k, v in x["raw"].items()}
    _imp(client, [x], "a.xlsx")
    ids = _ids(client)
    assert _confirm(client, ids["h21"], EV()).status_code == 200
    rows = client.get("/api/snapshot", params={"full": 1}).json()["sheets"][0]["rows"]
    assert classify_close(rows[0]["結案狀態(資安填)"]) == "closed"


# ───────────────────────── 6. 備份還原 ─────────────────────────
def _db(tmp_path, name, fs):
    eng = _make_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False, class_=Session)()
    rowkey.migrate(s)
    importer.create_batch(s, ImportIn(source_file="t.xlsx", findings=[FindingIn(**x) for x in fs]))
    return s


def test_casebackup_fresh_and_old_format(tmp_path):
    a = _db(tmp_path, "a.db", dataset())
    fa = {f.host: f for f in a.execute(select(Finding)).scalars().all()}
    for h, n in CONFIRMED.items():
        cases.set_overlay(a, fa[h].id, dict({"progress": CONF}, **EV(n)))
    data = casebackup.export(a)
    b = _db(tmp_path, "b.db", dataset())
    pre = casebackup.restore(b, data)
    assert pre["new"] == 4 and not pre["applied"]
    r = casebackup.restore(b, data, apply=True)
    assert r["applied"] and r["new"] == 4
    rb = query.weekly_report(b)
    ra = query.weekly_report(a)
    assert (rb["unresolved"], rb["sec_confirmed_count"], rb["sec_confirmed_stale_count"]) == (
        ra["unresolved"], ra["sec_confirmed_count"], ra["sec_confirmed_stale_count"])
    assert casebackup.restore(b, data, apply=True)["same"] == 4
    # 舊格式（沒有 confirm_* 欄、狀態是舊值）
    old = dict(data, items=[{k: v for k, v in it.items() if not k.startswith("confirm_")} for it in data["items"]])
    for it in old["items"]:
        it["status"] = "處理中"
    c = _db(tmp_path, "c.db", dataset())
    r = casebackup.restore(c, old, apply=True)
    assert r["new"] == 4
    assert all(x.confirm_by is None for x in c.execute(select(Case)).scalars().all())
    assert query.weekly_report(c)["sec_confirmed_count"] == 0


def test_casebackup_restore_confirmed_without_evidence(tmp_path):
    """手改／壞掉的備份檔：狀態是資安已確認結案但沒有證據 → 處理進度不還原、預覽列警告（2026-10-28 修正）。"""
    a = _db(tmp_path, "a.db", dataset())
    fa = {f.host: f for f in a.execute(select(Finding)).scalars().all()}
    cases.set_overlay(a, fa["h02"].id, dict({"progress": CONF}, **EV()))
    data = casebackup.export(a)
    for it in data["items"]:
        it["confirm_by"] = it["confirm_date"] = it["confirm_basis"] = None
    b = _db(tmp_path, "b.db", dataset())
    pre = casebackup.restore(b, data)
    assert any("缺證據" in w for w in pre["warnings"])
    casebackup.restore(b, data, apply=True)
    cb = next(c for c in b.execute(select(Case)).scalars().all() if c.host == "h02")
    assert cb.status != CONF and cb.confirm_by is None
    assert query.weekly_report(b)["sec_confirmed_count"] == 0


# ───────────────────────── 7. 自動寄信 ─────────────────────────
def test_automail_jobs(client, session, monkeypatch):
    import smtplib

    def _boom(*a, **k):
        raise AssertionError("不應連 SMTP")
    monkeypatch.setattr(smtplib, "SMTP", _boom)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _boom)
    _setup(client)
    cfg = automail.get_settings(session)
    jobs = automail.build_jobs(session, cfg, {"subject_prefix": ""}, T, departments=[A, B], force=True)
    dun_hosts = {it["host"] for j in jobs if j["kind"] == automail.KIND_DUN for it in j["items"]}
    assert not (dun_hosts & set(CONFIRMED))
    assert {"h02", "h10"} <= dun_hosts
    for dept, nconf, nstale in ((A, 2, 1), (B, 2, 1)):
        inv = next(j for j in jobs if j["kind"] == automail.KIND_INV and j["department"] == dept)
        assert "資安已確認待 Excel 更新 %d 筆（其中超過 14 天 %d 筆）" % (nconf, nstale) in inv["body"], inv["body"]
        rep = client.get("/api/report", params={"department": dept}).json()
        assert "· 未結：%d" % rep["unresolved"] in inv["body"]

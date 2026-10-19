"""獨立測試（非實作者撰寫）：送審中（等核准）功能的對帳、前後端口徑、自動解除、逾時時鐘、驗證。"""
import datetime as dt
import itertools

import pytest
from sqlalchemy import select

from webvuln import cases, query
from webvuln.models import AuditLog, Case

T = dt.date.today()
PROGS = ["", "處理中", "要申請展延", "要申請例外", "等複掃", "展延送審中", "例外送審中"]


def d(n):
    return (T + dt.timedelta(days=n)).isoformat()


# 各種「桶」：名稱 → finding 欄位
BUCKETS = {
    "ovd": dict(remediation_due=d(-5)),                       # 原始、已逾期
    "near": dict(remediation_due=d(10)),                      # 原始、已過行動線
    "mid": dict(remediation_due=d(60)),                       # 原始、30~90 天後才過行動線
    "far": dict(remediation_due=d(200)),                      # 原始、遠
    "vfar": dict(remediation_due=d(500)),                     # 原始、365 天也還沒到
    "nodue": dict(remediation_due=None),                      # 無到期日
    "extv": dict(remediation_due=d(-30), first_extension_due=d(40), remark="首次展延(iForm_1)"),
    "extx": dict(remediation_due=d(-60), first_extension_due=d(-3), remark="首次展延(iForm_2)"),
    "excv": dict(remediation_due=d(-90), exception_due=d(100), remark="例外管理(iForm_3)"),
    "excx": dict(remediation_due=d(-90), exception_due=d(-2), remark="例外管理(iForm_4)"),
    "excf": dict(remediation_due=d(-90), exception_due=d(400), remark="例外管理(iForm_5)"),
    "closed": dict(remediation_due=d(-5), close_status="已結案"),
}


def _rows():
    out = []
    for i, ((bk, kw), p) in enumerate(itertools.product(BUCKETS.items(), PROGS)):
        host = f"10.9.{i // 200}.{i % 200}"
        base = dict(sheet_key="s1", plugin_id=str(9000 + i), name=f"v{i}", host=host,
                    severity=["Critical", "High", "Medium", "Low"][i % 4], department="D", owner="o",
                    close_status="未結案", raw={"Host": host})
        base.update(kw)
        out.append((base, bk, p))
    return out


def _imp(client, fs, name="a.xlsx"):
    r = client.post("/api/import", json={"source_file": name, "findings": fs})
    assert r.status_code == 200, r.text


def _mark(client, fid, prog, **extra):
    body = {"set_progress": True, "progress": prog}
    body.update(extra)
    r = client.post(f"/api/findings/{fid}/overlay", json=body)
    assert r.status_code == 200, r.text


def _ids_of(client, **params):
    return {r["id"] for r in client.get("/api/findings", params=params).json()}


def _setup_mixed(client):
    rows = _rows()
    _imp(client, [r[0] for r in rows])
    by_host = {r["host"]: r for r in client.get("/api/findings", params={"status": "全部"}).json()}
    meta = {}
    for base, bk, p in rows:
        r = by_host[base["host"]]
        meta[r["id"]] = (bk, p)
        if p:
            _mark(client, r["id"], p)
    return meta


def _js_calc(rows, H):
    """webext.js applyTree calc() 的 Python 複刻（含 submitPendingRow）。"""
    LEAD = 30
    sets = {"sa": set(), "app": set(), "sub": set(), "not": set()}
    for r in rows:
        ap = r["stage"] in ("例外管理中", "首次展延中")
        dd = (dt.date.fromisoformat(r["effective_due"]) - T).days if r["effective_due"] else None
        sa = apv = False
        if ap:
            if dd is not None and dd < H:
                sa = True
            else:
                apv = True
        elif r["stage"] == "原始修補期限" and dd is not None and dd - LEAD <= H:
            sa = True
        # 2026-10-19 統一口徑（query.apply_bucket／webext.js applyTree）：標了送審中、且不是有效的已核准 → 送審中
        #（含核准已過期、承辦已送續申請的列）；有效的已核准一律算已核准
        pend = r["progress"] in ("展延送審中", "例外送審中")
        sub = (not apv) and pend
        if apv:
            sets["app"].add(r["id"])
        if sub:
            sets["sub"].add(r["id"])
        elif sa:
            sets["sa"].add(r["id"])
        if not (apv or sa or sub):
            sets["not"].add(r["id"])
    return sets


@pytest.mark.parametrize("horizon", [None, 0, 30, 90, 365])
def test_indep_partition_all_horizons(client, horizon):
    meta = _setup_mixed(client)
    hp = {} if horizon is None else {"horizon": horizon}
    sa = _ids_of(client, should_apply="true", **hp)
    sub = _ids_of(client, submitted="true", **hp)
    ap = _ids_of(client, applied="true", **hp)
    na = _ids_of(client, not_apply="true", **hp)
    uni = _ids_of(client, apply_universe="true", **hp)
    allopen = _ids_of(client)
    assert not (sa & sub) and not (sa & ap) and not (sub & ap) and not (uni & na)
    assert sa | sub | ap == uni
    assert len(sa) + len(sub) + len(ap) + len(na) == len(allopen)
    # 不變式：沒標送審中的列絕不出現在送審中；已關閉的列不在任何集合
    for i in sub:
        assert meta[i][1] in ("展延送審中", "例外送審中")
    closed = {i for i, (bk, _) in meta.items() if bk == "closed"}
    assert not (closed & allopen)
    if horizon is None:
        rep = client.get("/api/report").json()
        assert rep["need_apply_count"] == len(sa), (rep["need_apply_count"], len(sa))
        assert rep["submitted_count"] == len(sub)
        assert rep["applied_count"] == len(ap)
        assert rep["apply_universe"] == len(uni)
        assert rep["unresolved"] == len(allopen)
        assert {x["id"] for x in rep["submitted_list"]} == sub
        assert {x["id"] for x in rep["need_apply_list"]} == sa
        # summary / 排行
        assert client.get("/api/summary").json()["should_apply"] == len(sa)
    else:
        # 前端 applyTree 複刻（H 對應後端 horizon）
        rows = client.get("/api/findings", params={"status": "未結案"}).json()
        js = _js_calc(rows, horizon)
        assert js["sa"] == sa, ("sa", sorted(meta[i] for i in js["sa"] ^ sa))
        assert js["sub"] == sub, ("sub", sorted(meta[i] for i in js["sub"] ^ sub))
        assert js["app"] == ap, ("app", sorted(meta[i] for i in js["app"] ^ ap))
        assert js["not"] == na


def test_indep_tree_leaves_sum_to_sa(client):
    """applyTree 的 KS 葉子（排除 pend）應相加＝應申請未申請；若 saRows 有送審中值會漏算。"""
    meta = _setup_mixed(client)
    bad = {}
    for H in (0, 30, 90, 365):
        rows = client.get("/api/findings", params={"status": "未結案"}).json()
        js = _js_calc(rows, H)
        byid = {r["id"]: r for r in rows}
        pend_in_sa = [meta[i] for i in js["sa"] if byid[i]["progress"] in ("展延送審中", "例外送審中")]
        if pend_in_sa:
            bad[H] = pend_in_sa
    # 記錄結果供報告（預期：已反映核准但又到期的送審中列，落在 sa 但葉子不計）
    assert not bad, bad


def test_indep_compare_two_imports(client):
    meta = _setup_mixed(client)
    rows = _rows()
    fs2 = []
    for base, bk, p in rows:
        b = dict(base)
        # 第二批：原始逾期列中的送審中 → 資安核准展延；near 的例外送審中 → 只有展延（不該解除）
        if bk == "ovd" and p == "展延送審中":
            b.update(first_extension_due=d(60), remark="首次展延(iForm_x)")
        if bk == "near" and p == "例外送審中":
            b.update(first_extension_due=d(60), remark="首次展延(iForm_y)")
        if bk == "far" and p in ("展延送審中", "例外送審中"):
            b.update(close_status="已結案")
        fs2.append(b)
    _imp(client, fs2, "b.xlsx")
    c = client.get("/api/compare").json()
    assert c["equation"]["ok"] is True, c["equation"]
    m = {x["key"]: x for x in c["metrics"]}
    rep = client.get("/api/report").json()
    for side in ("prev", "cur"):
        assert m["apply_universe"][side] == m["need_apply"][side] + m["submitted"][side] + m["applied"][side]
        assert m["unresolved"][side] == m["apply_universe"][side] + m["not_apply"][side]
        assert m["need_apply"][side] == sum(m[k][side] for k in ("todo", "wip", "subext", "subexc", "rescan"))
    assert m["need_apply"]["cur"] == rep["need_apply_count"]
    assert m["submitted"]["cur"] == rep["submitted_count"]
    assert m["applied"]["cur"] == rep["applied_count"]
    assert m["unresolved"]["cur"] == rep["unresolved"]
    near_exc = next(r for r in client.get("/api/findings").json()
                    if r["stage"] == "首次展延中" and r["progress"] == "例外送審中" and r["effective_due"] == d(60))
    assert near_exc["id"] not in _ids_of(client, submitted="true")
    assert near_exc["id"] in _ids_of(client, applied="true")


def test_indep_row_flag_submitted_matches_list(client):
    _setup_mixed(client)
    rows = client.get("/api/findings").json()
    sub = _ids_of(client, submitted="true")
    assert {r["id"] for r in rows if r["submitted"]} == sub


def test_indep_auto_resolve_edges(client, session):
    fs = [
        dict(sheet_key="s1", plugin_id="1", name="a", host="h1", severity="High", remediation_due=d(5), close_status="未結案"),
        dict(sheet_key="s1", plugin_id="2", name="b", host="h2", severity="High", remediation_due=d(5), close_status="未結案"),
        dict(sheet_key="s1", plugin_id="3", name="c", host="h3", severity="High", remediation_due=d(5), close_status="未結案"),
        dict(sheet_key="s1", plugin_id="4", name="d", host="h4", severity="High", remediation_due=d(5), close_status="未結案"),
    ]
    _imp(client, fs)
    ids = {r["host"]: r["id"] for r in client.get("/api/findings").json()}
    _mark(client, ids["h1"], "例外送審中")
    _mark(client, ids["h2"], "展延送審中")
    _mark(client, ids["h3"], "展延送審中")
    _mark(client, ids["h4"], "展延送審中")
    fs2 = [dict(x) for x in fs]
    fs2[0].update(first_extension_due=d(60), remark="首次展延(iForm_1)")   # h1 例外送審中 + 只有展延 → 不解除
    fs2[1].update(first_extension_due=d(60), remark="首次展延(iForm_2)")   # h2 解除
    fs2[2].update(close_status="其他")                                      # h3 結案狀態「其他」
    del fs2[3]                                                             # h4 從來源消失
    _imp(client, fs2, "b.xlsx")
    st = {c.host: c for c in session.execute(select(Case)).scalars().all()}
    for c in st.values():
        session.refresh(c)
    assert st["h1"].status == "例外送審中"
    assert st["h2"].status == ""
    assert st["h4"].status == "展延送審中" and st["h4"].is_orphan
    h3_status = st["h3"].status
    n_logs = len(session.execute(select(AuditLog).where(AuditLog.action == "auto_resolve_submit")).scalars().all())
    assert n_logs == 1
    # 再匯兩次同一份：冪等、不重複寫紀錄
    _imp(client, fs2, "c.xlsx")
    _imp(client, fs2, "d.xlsx")
    assert len(session.execute(select(AuditLog).where(AuditLog.action == "auto_resolve_submit")).scalars().all()) == 1
    # h4 回來且已核准 → 這次才解除
    fs3 = [dict(x) for x in fs2] + [dict(fs[3], first_extension_due=d(60), remark="首次展延(iForm_4)")]
    _imp(client, fs3, "e.xlsx")
    session.expire_all()
    st = {c.host: c for c in session.execute(select(Case)).scalars().all()}
    assert st["h4"].status == ""
    assert len(session.execute(select(AuditLog).where(AuditLog.action == "auto_resolve_submit")).scalars().all()) == 2
    # Excel 欄位沒被改：h1 的 finding 仍是原值
    r1 = next(r for r in client.get("/api/findings", params={"status": "全部"}).json() if r["host"] == "h1")
    assert r1["stage"] == "首次展延中" and r1["effective_due"] == d(60)
    # 報告 h3（其他）：不是未結 → 不計任何未結類；回報 Case 狀態供參考
    print("h3 (close_status=其他) case status after import:", repr(h3_status))


def test_indep_duplicate_key_rows(client, session):
    """同鍵兩列（第一列原始、第二列展延）：自動解除只看第一列。"""
    base = dict(sheet_key="s1", plugin_id="7", name="dup", host="hd", severity="High",
                remediation_due=d(5), close_status="未結案")
    _imp(client, [base])
    fid = client.get("/api/findings").json()[0]["id"]
    _mark(client, fid, "展延送審中")
    _imp(client, [dict(base), dict(base, first_extension_due=d(60), remark="首次展延(iForm_9)")], "b.xlsx")
    session.expire_all()
    c = session.execute(select(Case)).scalars().first()
    rows = client.get("/api/findings").json()
    print("dup-key: case status", repr(c.status), [(r["stage"], r["submitted"]) for r in rows])
    # 兩列合計：一列送審中、一列已核准（不重複計）
    rep = client.get("/api/report").json()
    assert rep["apply_universe"] == rep["need_apply_count"] + rep["submitted_count"] + rep["applied_count"]


def test_indep_stale_clock(client, session):
    fs = [dict(sheet_key="s1", plugin_id=str(i), name=f"n{i}", host=f"s{i}", severity="High",
               remediation_due=d(5), close_status="未結案") for i in range(3)]
    _imp(client, fs)
    ids = {r["host"]: r["id"] for r in client.get("/api/findings").json()}
    for h in ids:
        _mark(client, ids[h], "展延送審中")

    def age(h):
        session.expire_all()
        c = next(c for c in session.execute(select(Case)).scalars().all() if c.host == h)
        return c

    def backdate(h, days):
        c = age(h)
        c.status_changed_at = dt.datetime.now() - dt.timedelta(days=days)
        session.commit()

    for h in ids:
        backdate(h, 20)
    # 只改 note / target_date（不帶 progress）
    r = client.post(f"/api/findings/{ids['s0']}/overlay", json={"set_note": True, "note": "x"})
    assert r.status_code == 200
    r = client.post(f"/api/findings/{ids['s0']}/overlay", json={"set_target": True, "target_date": d(30)})
    assert r.status_code == 200
    assert (dt.datetime.now() - age("s0").status_changed_at).days >= 19
    # 編輯視窗：全部欄位＋同一個進度 → 不重設
    _mark(client, ids["s0"], "展延送審中", set_note=True, note="y", set_owner=True, owner="o",
          set_department=True, department="", set_target=True, target_date="")
    assert (dt.datetime.now() - age("s0").status_changed_at).days >= 19
    # 進度改變 → 重設
    _mark(client, ids["s1"], "例外送審中")
    assert (dt.datetime.now() - age("s1").status_changed_at).days == 0
    # 批次：同值不重設、不同值重設
    r = client.post("/api/findings/bulk-overlay", json={"ids": [ids["s0"], ids["s2"]], "set_progress": True,
                                                         "progress": "展延送審中", "set_note": True, "note": "bulk"})
    assert r.status_code == 200 and r.json()["applied"] == 2
    assert (dt.datetime.now() - age("s0").status_changed_at).days >= 19
    assert (dt.datetime.now() - age("s2").status_changed_at).days >= 19
    rep = client.get("/api/report").json()
    assert rep["submitted_stale_count"] == 2   # s0, s2；s1 剛改
    assert {x["host"] for x in rep["flagged_list"]} >= {"s0", "s2"}
    r = client.post("/api/findings/bulk-overlay", json={"ids": [ids["s2"]], "set_progress": True, "progress": "例外送審中"})
    assert r.json()["applied"] == 1
    assert (dt.datetime.now() - age("s2").status_changed_at).days == 0
    # 清除再設 → 重設（合理）
    backdate("s0", 20)
    _mark(client, ids["s0"], "")
    _mark(client, ids["s0"], "展延送審中")
    assert (dt.datetime.now() - age("s0").status_changed_at).days == 0


def test_indep_stale_boundary_and_settings(client, session):
    fs = [dict(sheet_key="s1", plugin_id="1", name="n", host="b1", severity="High",
               remediation_due=d(5), close_status="未結案")]
    _imp(client, fs)
    fid = client.get("/api/findings").json()[0]["id"]
    _mark(client, fid, "展延送審中")
    c = session.execute(select(Case)).scalars().first()
    c.status_changed_at = dt.datetime.now() - dt.timedelta(days=14, hours=1)
    session.commit()
    row = client.get("/api/findings").json()[0]
    assert row["progress_state"] == "送審逾時" and row["submit_days"] == 14
    # 設定：範圍與非法值
    assert client.post("/api/general-settings", json={"submit_stale_days": 0}).json()["submit_stale_days"] == 1
    assert client.post("/api/general-settings", json={"submit_stale_days": "abc"}).json()["submit_stale_days"] == 14
    g = client.post("/api/general-settings", json={"submit_stale_days": 20}).json()
    assert g["submit_stale_days"] == 20 and g["session_ttl_hours"] == 12
    assert client.get("/api/findings").json()[0]["progress_state"] == "等核准"


@pytest.mark.parametrize("val,ok", [("", True), ("處理中", True), ("要申請展延", True), ("要申請例外", True),
                                     ("等複掃", True), ("展延送審中", True), ("例外送審中", True),
                                     ("  展延送審中 ", True), (None, True),
                                     ("__clear__", False), ("已核准", False), ("結案申請中", False)])
def test_indep_progress_validation(client, val, ok):
    _imp(client, [dict(sheet_key="s1", plugin_id="1", name="n", host="v1", severity="High",
                       remediation_due=d(5), close_status="未結案")])
    fid = client.get("/api/findings").json()[0]["id"]
    r = client.post(f"/api/findings/{fid}/overlay", json={"set_progress": True, "progress": val})
    assert (r.status_code == 200) == ok, (val, r.status_code, r.text)


def test_indep_bulk_invalid_progress_no_partial_write(client, session):
    """批次帶不認得的進度＋備註：整筆應失敗，備註不應被偷偷寫進去。"""
    _imp(client, [dict(sheet_key="s1", plugin_id=str(i), name="n", host=f"p{i}", severity="High",
                       remediation_due=d(5), close_status="未結案") for i in range(2)])
    ids = [r["id"] for r in client.get("/api/findings").json()]
    r = client.post("/api/findings/bulk-overlay", json={"ids": ids, "set_progress": True, "progress": "亂填",
                                                         "set_note": True, "note": "不該存"})
    assert r.status_code == 200
    j = r.json()
    assert j["applied"] == 0 and len(j["failed"]) == 2
    rows = client.get("/api/findings").json()
    assert all(not r["track_note"] for r in rows), [r["track_note"] for r in rows]


def test_indep_single_invalid_progress_no_partial_write(client):
    _imp(client, [dict(sheet_key="s1", plugin_id="1", name="n", host="q1", severity="High",
                       remediation_due=d(5), close_status="未結案")])
    fid = client.get("/api/findings").json()[0]["id"]
    r = client.post(f"/api/findings/{fid}/overlay", json={"set_progress": True, "progress": "亂填",
                                                          "set_note": True, "note": "不該存"})
    assert r.status_code == 400
    assert not client.get("/api/findings").json()[0]["track_note"]


def test_indep_closed_row_marked_not_counted(client):
    _imp(client, [dict(sheet_key="s1", plugin_id="1", name="n", host="c1", severity="High",
                       remediation_due=d(-5), close_status="已結案")])
    fid = client.get("/api/findings", params={"status": "全部"}).json()[0]["id"]
    _mark(client, fid, "展延送審中")
    rep = client.get("/api/report").json()
    assert rep["submitted_count"] == 0 and rep["progress"]["flagged"] == 0
    assert _ids_of(client, submitted="true", status="全部") == set()

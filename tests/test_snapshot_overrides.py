"""總覽（快照）要跟週報同一套數字：系統上改過的負責人／部門要寫回快照列（2026-10-06：同部門差 6 筆）。"""
from webvuln import query


def _import(client):
    fs = []
    for i in range(3):
        fs.append(dict(sheet_key="s1", plugin_id=str(100 + i), name=f"弱點{i}", host=f"10.30.5.{i}",
                       severity="High", department="資訊架構部", owner="段譽", remediation_due="2026-07-19",
                       close_status="未結案",
                       raw={"Plugin ID": str(100 + i), "Host": f"10.30.5.{i}", "負責單位": "資訊架構部", "負責人": "段譽"}))
    fs.append(dict(sheet_key="s8", plugin_id="900", name="規則", host="h8", severity="High",
                   department="資訊架構部", owner="林甲", close_status="未結案",
                   raw={"Plugin ID": "900", "負責單位": "資訊架構部-林甲"}))
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": fs}).status_code == 200
    return {r["host"]: r["id"] for r in client.get("/api/findings", params={"status": "全部"}).json()}


def _rows(client, sheet):
    snap = client.get("/api/snapshot", params={"full": 1}).json()
    return next(s for s in snap["sheets"] if s["name"] == sheet)["rows"]


def test_override_reflected_in_snapshot(client):
    ids = _import(client)
    assert [r["負責單位"] for r in _rows(client, "s1")] == ["資訊架構部"] * 3
    r = client.post(f"/api/findings/{ids['10.30.5.1']}/overlay",
                    json={"set_owner": True, "owner": "陳威廷", "set_department": True, "department": "網路部"})
    assert r.status_code == 200, r.text
    rows = _rows(client, "s1")   # 快取要跟著失效
    assert [(x["負責單位"], x["負責人"]) for x in rows] == [
        ("資訊架構部", "段譽"), ("網路部", "陳威廷"), ("資訊架構部", "段譽")]


def test_override_on_combined_unit_owner_cell(client):
    ids = _import(client)
    client.post(f"/api/findings/{ids['h8']}/overlay", json={"set_owner": True, "owner": "何佳璇"})
    assert _rows(client, "s8")[0]["負責單位"] == "資訊架構部-何佳璇"


def test_report_buckets_exclusive(client):
    import datetime as dt
    t = dt.date.today()
    d = lambda n: (t + dt.timedelta(days=n)).isoformat()
    fs = [dict(sheet_key="s1", plugin_id=str(i), name=f"v{i}", host=f"h{i}", severity=sev, department="A",
               owner="x", remediation_due=due, close_status="未結案")
          for i, (sev, due) in enumerate([("Critical", d(-5)), ("High", d(10)), ("High", d(200)),
                                          ("Critical", None), ("Medium", d(-1)), ("Medium", d(5))])]
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": fs}).status_code == 200
    rep = client.get("/api/report").json()
    assert (rep["overdue"], rep["soon"], rep["high_risk_only"], rep["high_risk"]) == (2, 2, 2, 4)
    assert sorted(r["host"] for r in client.get("/api/findings", params={"risk": "high_only"}).json()) == ["h2", "h3"]

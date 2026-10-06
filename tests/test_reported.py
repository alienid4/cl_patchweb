"""「已回報」：處理進度／預計完成日／追蹤備註任一有填，就要查得到（2026-10-05 使用者找不到自己標過「處理中」的那筆）。"""


def _import(client):
    fs = [dict(sheet_key="s1", plugin_id=str(100 + i), name=f"弱點{i}", host=f"10.30.5.{i}",
               severity="High", department="資訊架構部", owner="段譽" if i < 3 else "喬峰",
               remediation_due="2026-07-19", close_status="未結案") for i in range(5)]
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": fs}).status_code == 200
    return {r["host"]: r["id"] for r in client.get("/api/findings").json()}


def test_reported_filter_and_count(client):
    ids = _import(client)
    ov = lambda h, body: client.post(f"/api/findings/{ids[h]}/overlay", json=body)
    assert ov("10.30.5.0", {"set_progress": True, "progress": "處理中"}).status_code == 200
    assert ov("10.30.5.1", {"set_target": True, "target_date": "2026-10-08"}).status_code == 200
    assert ov("10.30.5.3", {"set_note": True, "note": "預計10/5-10/8進行修補"}).status_code == 200
    assert ov("10.30.5.4", {"set_note": True, "note": "   "}).status_code == 200   # 只有空白不算回報

    rows = client.get("/api/findings", params={"reported": "true"}).json()
    assert sorted(r["host"] for r in rows) == ["10.30.5.0", "10.30.5.1", "10.30.5.3"]

    # 依負責人：段譽回報 2 筆(.0 .1)、喬峰 1 筆(.3)
    by = {}
    for r in rows:
        by[r["owner"]] = by.get(r["owner"], 0) + 1
    assert by == {"段譽": 2, "喬峰": 1}


def test_note_without_progress_flag(client):
    """⚠ 待補進度：追蹤備註有寫、處理進度沒設（2026-10-06 防呆）。"""
    ids = _import(client)
    ov = lambda h, body: client.post(f"/api/findings/{ids[h]}/overlay", json=body)
    assert ov("10.30.5.0", {"set_note": True, "note": "10/5 修補完畢"}).status_code == 200            # 有備註、沒進度 → 要抓
    assert ov("10.30.5.1", {"set_note": True, "note": "已修補", "set_progress": True, "progress": "等複掃"}).status_code == 200  # 有設 → 不抓
    assert ov("10.30.5.2", {"set_note": True, "note": "   "}).status_code == 200                       # 只有空白 → 不抓
    rows = client.get("/api/findings", params={"note_no_progress": "true"}).json()
    assert [r["host"] for r in rows] == ["10.30.5.0"]
    assert client.get("/api/report").json()["progress"]["note_no_progress"] == 1

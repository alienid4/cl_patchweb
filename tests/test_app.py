from webvuln.schemas import FindingIn, ImportIn


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "弱點彙總" in r.text


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_summary_after_import_via_api(client):
    payload = ImportIn(source_file="x.xlsx", findings=[
        FindingIn(host="h1", severity="Critical", department="資訊架構部",
                  owner="喬峰", remediation_due="2026-01-01", close_status="未結案"),
    ]).model_dump()
    assert client.post("/api/import", json=payload).status_code == 200
    s = client.get("/api/summary").json()
    assert s["unresolved"] == 1
    assert s["overdue"] == 1   # 2026-01-01 早已過
    rows = client.get("/api/findings?band=已逾期").json()
    assert len(rows) == 1 and rows[0]["host"] == "h1"


def test_snapshot_meta(client):
    # 無資料
    m0 = client.get("/api/snapshot-meta").json()
    assert m0["batch_id"] is None and m0["row_count"] == 0
    # 匯入後有批次識別
    client.post("/api/import", json={"findings": [
        {"host": "h", "plugin_id": "p", "sheet_key": "s", "close_status": "未結案"}]})
    m1 = client.get("/api/snapshot-meta").json()
    assert m1["batch_id"] is not None and m1["row_count"] == 1 and m1["imported_at"]

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

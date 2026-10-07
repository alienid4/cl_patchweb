"""與上次匯入比較（2026-10-07）：兩次匯入之間每一筆變了什麼、主管看的數字、點數字的清單、歷程、刪錯批。"""
import datetime as dt

from webvuln import changes, rowkey
from webvuln.models import ChangeRun, FindingChange

T = dt.date.today()


def d(n):
    return (T + dt.timedelta(days=n)).isoformat()


def _f(i, **kw):
    base = dict(sheet_key="s1", plugin_id=str(100 + i), name=f"弱點{i}", host=f"10.30.5.{i}", severity="High",
                department="資訊架構部", owner="段譽", remediation_due=d(10), close_status="未結案",
                raw={"Plugin ID": str(100 + i), "Host": f"10.30.5.{i}", "複測狀態": ""})
    base.update(kw)
    return base


def _imp(client, fs, name):
    r = client.post("/api/import", json={"source_file": name, "findings": fs})
    assert r.status_code == 200, r.text


def _setup(client, session):
    rowkey.mark_migrated(session) if hasattr(rowkey, "mark_migrated") else None
    first = [_f(0), _f(1), _f(2), _f(3), _f(4, remediation_due=d(200)), _f(5, department="網路部")]
    _imp(client, first, "a.xlsx")
    ids = {r["host"]: r["id"] for r in client.get("/api/findings").json()}
    # 承辦在第一批標了展延送審（.1）
    assert client.post(f"/api/findings/{ids['10.30.5.1']}/overlay",
                       json={"set_progress": True, "progress": "要申請展延"}).status_code == 200
    second = [
        _f(0, close_status="已結案"),                                             # 結案
        _f(1, exception_due=d(90), remark="例外管理(iForm_1)"),                   # 送審→核准（例外）
        _f(2, raw={"Plugin ID": "102", "Host": "10.30.5.2", "複測狀態": "(已通過)複測無發現弱點"}),  # 複測已修復
        _f(3, remediation_due=d(-3)),                                             # 到期日變了＋新增逾期
        # .4 從來源消失
        _f(5, department="網路部"),
        _f(6),                                                                    # 新出現
    ]
    _imp(client, second, "b.xlsx")


def test_compare_numbers_events_and_equation(client, session):
    _setup(client, session)
    c = client.get("/api/compare", params={"department": "資訊架構部"}).json()
    assert c["has_prev"] is True and c["progress_recorded"] is True
    m = {x["key"]: x for x in c["metrics"]}
    assert (m["unresolved"]["prev"], m["unresolved"]["cur"]) == (5, 4)     # 上次 0-4；本次 1,2,3,6
    assert m["subext"]["prev"] == 1                                       # 上次拍到的展延送審
    e = {x["key"]: x["n"] for x in c["events"]}
    assert e["closed"] == 1 and e["approved"] == 1 and e["retest_fixed"] == 1
    assert e["new"] == 1 and e["gone"] == 1 and e["new_overdue"] == 1 and e["due_changed"] == 2
    assert c["equation"]["ok"] is True, c["equation"]
    assert "結案 1 筆" in c["summary"]


def test_compare_rows_and_notes(client, session):
    _setup(client, session)
    rows = client.get("/api/compare/rows", params={"event": "approved", "department": "資訊架構部"}).json()
    assert [r["host"] for r in rows] == ["10.30.5.1"]
    assert "處置階段 原始修補期限 → 例外管理中" in rows[0]["change_note"]
    gone = client.get("/api/compare/rows", params={"event": "gone", "department": "資訊架構部"}).json()
    assert gone[0]["host"] == "10.30.5.4" and gone[0]["readonly"] is True
    minus = client.get("/api/compare/rows", params={"metric": "unresolved", "side": "minus",
                                                    "department": "資訊架構部"}).json()
    assert sorted(r["host"] for r in minus) == ["10.30.5.0", "10.30.5.4"]


def test_history_and_change_records(client, session):
    _setup(client, session)
    fid = next(r["id"] for r in client.get("/api/findings").json() if r["host"] == "10.30.5.1")
    h = client.get(f"/api/findings/{fid}/history").json()
    texts = [i["text"] for i in h["items"]]
    assert texts[0].startswith("首次出現")
    assert any("處理進度 → 要申請展延" in t for t in texts)          # 系統操作
    assert any(t.startswith("處置階段：原始修補期限 → 例外管理中") for t in texts)   # 匯入變化
    assert session.query(ChangeRun).count() == 2


def test_delete_wrong_batch_restores_previous(client, session):
    _setup(client, session)
    bs = client.get("/api/batches").json()
    latest = bs[0]["id"]
    r = client.delete(f"/api/batches/{latest}")
    assert r.status_code == 200, r.text
    assert len(client.get("/api/findings").json()) == 6                     # 回到第一批（6 筆未結）
    assert session.query(FindingChange).filter(FindingChange.batch_id == latest).count() == 0
    assert client.get("/api/compare").json()["has_prev"] is False
    assert client.delete(f"/api/batches/{bs[1]['id']}").status_code == 400   # 只剩一批不能刪


def test_backfill_idempotent(client, session):
    _setup(client, session)
    session.query(FindingChange).delete(); session.query(ChangeRun).delete(); session.commit()
    assert changes.backfill(session) == 2
    assert changes.backfill(session) == 0

"""開頁快照：計數字串展開的重複列要收回、沒名字的空白欄要拿掉（2026-10-05 第 9 表 28 列被畫面放大成 1042 筆）。"""
from webvuln import importer, query
from webvuln.schemas import FindingIn, ImportIn


def _sec_header_rows():
    # 模擬單機版解析器的輸出：Excel 一列「中*2 低*1」被展開成 3 筆，三筆帶同一份原始列
    r1 = {"部門": "資訊架構部", "IP": "10.30.7.1", "風險": "中*2 低*1"}
    r2 = {"部門": "資訊架構部", "IP": "10.30.7.2", "風險": "高*1"}
    r3 = {"部門": "資訊架構部", "IP": "10.30.7.3", "風險": "中*2"}   # Excel 本來就有兩列一模一樣
    raws = [r1, r1, r1, r2, r3, r3, r3, r3]
    return [FindingIn(sheet_key="9-Security Header", host=r["IP"], severity="Medium",
                      close_status="未結案", raw=r) for r in raws]


def test_collapse_counted_rows(session):
    importer.create_batch(session, ImportIn(source_file="t.xlsx", findings=_sec_header_rows(),
                                            sheet_columns={"9-Security Header": ["部門", "IP", "風險"]}))
    snap = query.snapshot(session)
    rows = snap["sheets"][0]["rows"]
    assert [r["IP"] for r in rows] == ["10.30.7.1", "10.30.7.2", "10.30.7.3", "10.30.7.3"]
    # DB 本身不動：明細仍是展開後的 8 筆
    assert len(query.find(session, status="全部")) == 8


def test_drop_blank_unnamed_columns(session):
    raws = [{"Host": "10.30.8.1", "Name": "A", "": None},
            {"Host": "10.30.8.2", "Name": "", "": ""}]
    importer.create_batch(session, ImportIn(source_file="t.xlsx", sheet_columns={"s": ["Host", "Name", "", ""]},
                                            findings=[FindingIn(sheet_key="s", host=r["Host"], close_status="未結案", raw=r) for r in raws]))
    sh = query.snapshot(session)["sheets"][0]
    assert sh["columns"] == ["Host", "Name"]          # 有欄名的保留(Name 第二列空白也留)，沒名字又全空的拿掉
    assert all("" not in r for r in sh["rows"])


def test_gzip(client):
    importer_rows = [{"sheet_key": "s", "host": f"10.30.9.{i}", "close_status": "未結案",
                      "raw": {"Host": f"10.30.9.{i}", "Description": "x" * 500}} for i in range(20)]
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": importer_rows}).status_code == 200
    r = client.get("/api/snapshot", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"


def test_display_host_falls_back_to_asset_name(session):
    """沒主機的列顯示資產名稱，但 DB 的 host 與承辦鍵不變（重匯後承辦進度才對得上）。"""
    importer.create_batch(session, ImportIn(source_file="t.xlsx", findings=[
        FindingIn(sheet_key="10-外部威脅情資", name="情資A", close_status="未結案", raw={"外部情資": "情資A", "資產名稱": "官網主機"}),
        FindingIn(sheet_key="1-系統弱點", host="10.30.6.1", name="B", close_status="未結案", raw={"Host": "10.30.6.1", "資產名稱": "別的名字"}),
    ]))
    rows = {r["name"]: r for r in query.find(session)}
    assert rows["情資A"]["host"] == "官網主機"
    assert rows["B"]["host"] == "10.30.6.1"            # 有主機就用主機
    from webvuln.models import Finding
    f = session.query(Finding).filter(Finding.name == "情資A").one()
    assert f.host is None and query.vuln_key(f)[2] == ""   # 存的跟鍵都沒變

"""每一列的穩定識別碼與一次性轉換（2026-10-05：同鍵多列共用一筆承辦記錄，標一列全部一起變）。"""
from sqlalchemy import select

from webvuln import cases, importer, query, rowkey
from webvuln.models import AppSetting, Attachment, Case, Finding
from webvuln.schemas import FindingIn, ImportIn


def _f(sheet, raw, host=None, plugin=None, status="未結案"):
    return FindingIn(sheet_key=sheet, host=host, plugin_id=plugin, name=raw.get("Name") or raw.get("Audit Name"),
                     close_status=status, raw=raw)


def _rows():
    s1 = "1-系統弱點掃描弱點"
    return [
        # 同主機同 Plugin、不同埠：舊鍵撞在一起，新鍵要分開
        _f(s1, {"Plugin ID": "1001", "Host": "10.30.1.1", "Port": "443", "Protocol": "tcp", "年度": 2026}, "10.30.1.1", "1001"),
        _f(s1, {"Plugin ID": "1001", "Host": "10.30.1.1", "Port": "8443", "Protocol": "tcp", "年度": 2026}, "10.30.1.1", "1001"),
        # 8-BAS：沒有 Plugin、沒有主機，全表舊鍵相同
        _f("8-BAS演練", {"Audit Name": "BAS-A", "年度": 2026}),
        _f("8-BAS演練", {"Audit Name": "BAS-B", "年度": 2026}),
        _f("8-BAS演練", {"Audit Name": "BAS-C", "年度": 2026}),
        # 9：計數列展開成 3 筆（同一 Excel 列）→ 共用一個鍵
        *[_f("9-Security Header檢視追蹤", {"網址": "https://a.example", "IP": "10.30.9.1", "風險": "中*2 低*1"}, "10.30.9.1")] * 3,
        # 10：組合後仍相同的兩列 → 依先後加 #2
        _f("10-外部威脅情資", {"外部情資": "情資X", "資產名稱": "主機甲", "負責人": "甲"}),
        _f("10-外部威脅情資", {"外部情資": "情資X", "資產名稱": "主機甲", "負責人": "乙"}),
    ]


def _load(session):
    return importer.create_batch(session, ImportIn(source_file="t.xlsx", findings=_rows()))


def test_assign_keys():
    class F:  # 輕量假物件
        def __init__(self, fi):
            self.sheet_key, self.plugin_id, self.host, self.raw, self.row_key = fi.sheet_key, fi.plugin_id, fi.host, fi.raw, None
    fs = [F(x) for x in _rows()]
    rowkey.assign(fs)
    ks = [f.row_key for f in fs]
    assert ks[0] != ks[1]                                  # 不同埠分開
    assert len({ks[2], ks[3], ks[4]}) == 3                 # BAS 每列一個
    assert ks[5] == ks[6] == ks[7]                         # 計數列展開出的同一列共用
    assert ks[9] == ks[8] + "#2"                           # 仍相同的依先後加序號
    assert all(k.startswith(rowkey.PREFIX) for k in ks)


def test_migrate_copies_shared_and_is_idempotent(session):
    b = _load(session)                                     # 測試庫沒轉換過 → 走舊鍵
    assert not rowkey.migrated(session)
    fs = session.execute(select(Finding).where(Finding.batch_id == b.id).order_by(Finding.id)).scalars().all()
    bas = [f for f in fs if f.sheet_key.startswith("8-")]
    port443 = fs[0]
    # 舊制下：在 BAS 標一筆「處理中」＋備註；在 443 那列標預計完成日；BAS 掛一個附件
    c_bas = session.execute(select(Case).where(Case.vuln_key == cases.key_str(bas[0]))).scalar_one()
    c_bas.status, c_bas.track_note = "處理中", "先處理"
    c_443 = session.execute(select(Case).where(Case.vuln_key == cases.key_str(port443))).scalar_one()
    session.add(Attachment(vuln_key=c_bas.vuln_key, sheet_key=bas[0].sheet_key, orig_name="wbs.pdf",
                           stored_name="x.pdf", sha256="ab", size=1))
    session.commit()
    n_case_before = session.query(Case).count()

    rep = rowkey.migrate(session)
    assert rep is not None and rowkey.migrated(session)
    # BAS：一筆複製到 3 列，三列都是「處理中＋先處理」（使用者選 A）
    bas_cases = [session.execute(select(Case).where(Case.vuln_key == cases.key_str(f))).scalar_one() for f in bas]
    assert len({c.vuln_key for c in bas_cases}) == 3
    assert all(c.status == "處理中" and c.track_note == "先處理" for c in bas_cases)
    # 附件跟著複製：三列各看得到一個，共用同一實體檔
    att = session.execute(select(Attachment)).scalars().all()
    assert sorted(a.vuln_key for a in att) == sorted(c.vuln_key for c in bas_cases)
    assert {a.stored_name for a in att} == {"x.pdf"}
    # 不同埠的兩列：舊的一筆複製成兩筆（原本就共用）
    assert any(r["rows"] == 2 for r in rep["copied"])
    assert any(r["rows"] == 3 and r["status"] == "處理中" and r["has_note"] for r in rep["copied"])
    assert session.query(Case).count() > n_case_before
    # 冪等：再跑一次什麼都不做
    n_after = session.query(Case).count()
    assert rowkey.migrate(session) is None and session.query(Case).count() == n_after


def test_after_migration_rows_are_independent(client, engine):
    from sqlalchemy.orm import Session as S
    r = client.post("/api/import", json={"source_file": "t.xlsx", "findings": [x.model_dump() for x in _rows()]})
    assert r.status_code == 200
    with S(engine) as s:
        rowkey.migrate(s)
    # 轉換後重匯一次（正式上線後的常態），再只標 BAS-A
    assert client.post("/api/import", json={"source_file": "t2.xlsx", "findings": [x.model_dump() for x in _rows()]}).status_code == 200
    rows = client.get("/api/findings").json()
    bas = {x["name"]: x["id"] for x in rows if (x["sheet_key"] or "").startswith("8-")}
    assert client.post(f"/api/findings/{bas['BAS-A']}/overlay", json={"set_progress": True, "progress": "處理中"}).status_code == 200
    after = {x["name"]: x["progress"] for x in client.get("/api/findings").json() if (x["sheet_key"] or "").startswith("8-")}
    assert after == {"BAS-A": "處理中", "BAS-B": "", "BAS-C": ""}     # 只有那一列變

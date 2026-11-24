"""V2.110（2026-11-23 三件事故）：
1. 匯入檢核報告：日期讀不到、缺期限、已申請沒日期、工作表／欄位不見、上一批未結這批整筆消失、內容相同。
2. 舊檔防呆：檔名資料日期（VYYYYMMDD）比目前最新一批舊 → 409，confirm_older 才覆蓋。
3. 規則版本：最新一批是舊規則算的 → 前端用同一套解析重算、後端就地更新（不新增批次、冪等）。
4. 一致性：伺服器 /api/report 的未結／逾期／30 天內 ＝ 前端解析 /api/snapshot 的同一組數字。
前端部分用 node 實際跑原檔（沒裝 node 就略過那幾題）。
"""
import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from webvuln import importcheck, logic
from webvuln.models import AuditLog, Finding, ImportBatch

FRONT = Path(__file__).resolve().parents[1] / "frontend"
T = dt.date.today()


def d(n):
    return (T + dt.timedelta(days=n)).isoformat()


def _f(sheet, pid, host, due=None, close="未結案", **kw):
    raw = {"Plugin ID": pid, "Host": host, "Name": "v" + str(pid), "負責人": "x", "修補期限": due or "", "結案狀態": close}
    raw.update(kw.pop("raw_extra", {}))
    return dict(sheet_key=sheet, plugin_id=pid, name="v" + str(pid), host=host, severity="High", department="A",
                owner="x", remediation_due=due, close_status=close, raw=raw, **kw)


COLS1 = ["Plugin ID", "Host", "修補期限", "結案狀態", "備註"]


def _imp(client, name, findings, cols=None, **extra):
    cols = cols or {f["sheet_key"]: COLS1 for f in findings}
    body = {"source_file": name, "findings": findings, "sheet_columns": cols}
    body.update(extra)
    return client.post("/api/import", json=body)


def _imp_confirmed(client, name, findings, cols=None, **extra):
    """會被 409 擋（工作表不見／舊檔）的匯入：照前端流程帶確認＋409 回的最新批 id 再送一次。"""
    r = _imp(client, name, findings, cols=cols, **extra)
    if r.status_code != 409:
        return r
    det = r.json()["detail"]
    return _imp(client, name, findings, cols=cols, confirm_older=bool(det.get("older")),
                confirm_missing_sheets=bool(det.get("sheets_missing")),
                expected_latest_batch_id=det["latest_batch_id"], **extra)


# ── 1. 匯入檢核報告 ──
def test_report_contents(client):
    b1 = [_f("1-系統", "1", "h1", d(10)), _f("1-系統", "2", "h2", d(-5)), _f("1-系統", "3", "h3", d(5), close="已結案"),
          _f("2-IoT", "9", "i1", d(20))]
    r = _imp(client, "彙總V20261111.xlsx", b1)
    assert r.status_code == 200, r.text
    rep1 = r.json()["report"]
    assert rep1["prev_batch_id"] is None and rep1["counts"]["disappeared"] == 0

    # 第二批：h2（上批未結）整筆不見、h3（上批已結）不見不算、2-IoT 整張不見、1-系統 少了「備註」欄、h4 缺期限
    b2 = [_f("1-系統", "1", "h1", d(10), row_no=2), _f("1-系統", "4", "h4", None, row_no=3)]
    checks = {"bad_dates": [{"sheet": "1-系統", "row": 3, "field": "修補期限", "column": "修補期限", "raw": "待確認"}],
              "apply_no_date": [{"sheet": "1-系統", "row": 2, "kind": "例外", "basis": "例外單號欄", "remark": ""}],
              "parse_warnings": []}
    r = _imp_confirmed(client, "彙總V20261116.xlsx", b2, cols={"1-系統": ["Plugin ID", "Host", "修補期限", "結案狀態", "新欄"]},
             client_checks=checks, rules_version=logic.RULES_VERSION)
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    c = rep["counts"]
    assert c["bad_dates"] == 1 and c["apply_no_date"] == 1
    assert c["no_due"] == 1 and rep["no_due"][0]["row"] == 3 and "待確認" in rep["no_due"][0]["raw"]
    assert c["disappeared"] == 2 and {x["host"] for x in rep["disappeared"]} == {"h2", "i1"}   # h3 上批已結，不算
    assert rep["disappeared_by_sheet"] == {"1-系統": 1, "2-IoT": 1}
    assert [s["sheet"] for s in rep["sheets_missing"]] == ["2-IoT"]
    assert rep["sheets_missing"][0]["status"].startswith("整張不見")
    assert rep["columns_gone"] == [{"sheet": "1-系統", "columns": ["備註"]}]
    assert rep["columns_new"] == [{"sheet": "1-系統", "columns": ["新欄"]}]
    assert rep["data_date"] == "2026-11-16" and rep["prev_data_date"] == "2026-11-11"
    assert rep["client_checked"] is True and rep["same_content_as_prev"] is False
    assert rep["issues"] >= 6
    # 存在批次上、可重開；批次清單帶問題數
    g = client.get("/api/import-report").json()
    assert g["batch_id"] == r.json()["batch_id"] and g["report"]["counts"] == c
    bs = client.get("/api/batches").json()
    assert bs[0]["has_report"] is True and bs[0]["report_issues"] == rep["issues"]
    assert client.get("/api/import-report", params={"batch_id": 99999}).status_code == 404


def test_disappeared_counts_whole_sheet_items(client):
    """整張表不見時，那張表上批未結的也列在消失的項目（計數照實）。"""
    _imp(client, "a.xlsx", [_f("1-系統", "1", "h1", d(3)), _f("2-IoT", "9", "i1", d(3)), _f("2-IoT", "8", "i2", d(3))])
    rep = _imp_confirmed(client, "b.xlsx", [_f("1-系統", "1", "h1", d(3))]).json()["report"]
    assert rep["counts"]["disappeared"] == 2 and rep["disappeared_by_sheet"] == {"2-IoT": 2}
    assert rep["sheets_missing"][0]["prev_rows"] == 2


def test_zero_rows_sheet_and_same_content(client):
    fs = [_f("1-系統", "1", "h1", d(3)), _f("2-IoT", "9", "i1", d(3))]
    _imp(client, "a.xlsx", fs)
    rep = _imp(client, "a.xlsx", fs).json()["report"]
    assert rep["same_content_as_prev"] is True and rep["counts"]["disappeared"] == 0
    # 2-IoT 欄位還在、但 0 筆
    rep = _imp_confirmed(client, "c.xlsx", fs[:1], cols={"1-系統": COLS1, "2-IoT": COLS1}).json()["report"]
    assert rep["sheets_missing"] == [{"sheet": "2-IoT", "prev_rows": 1, "now_rows": 0, "status": "這次 0 筆"}]


def test_old_frontend_without_checks_still_imports(client):
    r = _imp(client, "a.xlsx", [_f("1-系統", "1", "h1", None)])
    assert r.status_code == 200
    rep = r.json()["report"]
    assert rep["client_checked"] is False and rep["counts"]["no_due"] == 1 and rep["rules_version"] is None


# ── 2. 舊檔防呆 ──
def test_data_date_from_name():
    assert importcheck.data_date("弱點彙總表V20261116.xlsx") == dt.date(2026, 11, 16)
    assert importcheck.data_date("v20261111_x.xlsx") == dt.date(2026, 11, 11)
    assert importcheck.data_date("report.xlsx") is None
    assert importcheck.data_date("V20261340.xlsx") is None          # 不合法日期不猜
    assert importcheck.data_date("V202611161.xlsx") is None         # 9 位數不是日期


def test_older_file_guard(client, engine):
    assert _imp(client, "彙總V20261116.xlsx", [_f("1-系統", "1", "h1", d(3))]).status_code == 200
    r = _imp(client, "彙總V20261111.xlsx", [_f("1-系統", "1", "h1", d(9))])
    assert r.status_code == 409
    det = r.json()["detail"]
    assert det["code"] == "older_file" and det["new_date"] == "2026-11-11" and det["latest_date"] == "2026-11-16"
    assert det["latest_file"] == "彙總V20261116.xlsx"
    assert len(client.get("/api/batches").json()) == 1                 # 沒有被偷偷換掉
    # 同一天或較新、或檔名讀不出日期 → 不擋
    assert _imp(client, "彙總V20261116_重出.xlsx", [_f("1-系統", "1", "h1", d(3))]).status_code == 200
    # 明確確認 → 覆蓋，報告與稽核都記下
    # 確認沒帶「當時看到的最新批」→ 不算數（確認要綁狀態）
    assert _imp(client, "彙總V20261111.xlsx", [_f("1-系統", "1", "h1", d(9))], confirm_older=True).status_code == 409
    latest = client.get("/api/snapshot-meta").json()["batch_id"]
    r = _imp(client, "彙總V20261111.xlsx", [_f("1-系統", "1", "h1", d(9))], confirm_older=True,
             expected_latest_batch_id=latest)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["older_file"]["latest_date"] == "2026-11-16"
    assert client.get("/api/snapshot-meta").json()["data_date"] == "2026-11-11"
    with sessionmaker(bind=engine)() as s:
        a = s.execute(select(AuditLog).where(AuditLog.action == "import").order_by(AuditLog.id.desc())).scalars().first()
        assert "舊檔覆蓋已確認" in a.detail


def test_no_guard_without_dates(client):
    assert _imp(client, "V20261116.xlsx", [_f("1-系統", "1", "h1", d(3))]).status_code == 200
    assert _imp(client, "report.xlsx", [_f("1-系統", "1", "h1", d(3))]).status_code == 200


# ── 3. 規則版本就地重算 ──
def test_rules_version_front_equals_back():
    js = (FRONT / "js" / "sheets.js").read_text(encoding="utf-8")
    m = re.search(r"var RULES_VERSION = '([^']+)'", js)
    assert m and m.group(1) == logic.RULES_VERSION, "前端 sheets.js RULES_VERSION 要跟後端 logic.RULES_VERSION 同值"


def _old_rules_batch(client):
    """模擬舊規則匯入：期限是文字「DMZ 2026/9/27…」舊版讀不到（None），例外單號有填但舊版不認。"""
    fs = [_f("10-外部威脅情資", None, None, None, raw_extra={"修補期限": "DMZ 2026/9/27\nintranet 2027/2/27"}),
          _f("10-外部威脅情資", None, None, "2025-10-01", raw_extra={"例外單號": "E1", "例外核准期限": "2030/12/31"})]
    fs[0]["host"], fs[1]["host"] = "a1", "a2"
    r = _imp(client, "V20261116.xlsx", fs)   # 舊版前端：沒帶 rules_version
    assert r.status_code == 200
    return r.json()["batch_id"]


def test_recompute_updates_in_place(client, engine):
    bid = _old_rules_batch(client)
    meta = client.get("/api/snapshot-meta").json()
    assert meta["rules_version"] == logic.RULES_VERSION and meta["batch_rules_version"] is None
    src = client.get("/api/recompute/source").json()
    assert src["batch_id"] == bid
    rows = src["sheets"][0]["rows"]
    assert src["sheets"][0]["columns"][-1] == "__fid" and all("__fid" in r for r in rows)
    ids = [r["__fid"] for r in rows]
    items = [{"id": ids[0], "remediation_due": "2026-09-27", "close_status": "未結案"},
             {"id": ids[1], "remediation_due": "2025-10-01", "exception_due": "2030-12-31", "close_status": "未結案"}]
    # 前端規則版本不同 → 拒絕（舊快取的 JS 不能用舊規則寫回去）
    r = client.post("/api/recompute", json={"batch_id": bid, "rules_version": "OLD", "items": items})
    assert r.status_code == 400
    # 批次不是最新 → 409
    r = client.post("/api/recompute", json={"batch_id": bid + 100, "rules_version": logic.RULES_VERSION, "items": items})
    assert r.status_code == 409
    r = client.post("/api/recompute", json={"batch_id": bid, "rules_version": logic.RULES_VERSION, "items": items})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["already"] is False and j["updated"] == 2 and j["missing"] == 0
    with sessionmaker(bind=engine)() as s:
        assert s.execute(select(func.count(ImportBatch.id))).scalar_one() == 1      # 沒有新增批次
        b = s.get(ImportBatch, bid)
        assert b.rules_version == logic.RULES_VERSION
        assert b.import_report["recompute"][0]["updated"] == 2
        f0, f1 = s.get(Finding, ids[0]), s.get(Finding, ids[1])
        assert f0.remediation_due == dt.date(2026, 9, 27) and f0.effective_due == dt.date(2026, 9, 27)
        assert f1.exception_due == dt.date(2030, 12, 31) and f1.stage == logic.STAGE_EXCEPTION   # 例外單號欄＝已申請
        assert f1.effective_due == dt.date(2030, 12, 31)
        assert s.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "recompute_rules")).scalar_one() == 1
    assert client.get("/api/snapshot-meta").json()["batch_rules_version"] == logic.RULES_VERSION
    # 冪等：第二次（另一個管理者同時開頁）什麼都不做、不再留稽核
    r = client.post("/api/recompute", json={"batch_id": bid, "rules_version": logic.RULES_VERSION, "items": items})
    assert r.json()["already"] is True and r.json()["updated"] == 0
    with sessionmaker(bind=engine)() as s:
        assert s.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "recompute_rules")).scalar_one() == 1


def test_recompute_needs_super(client, monkeypatch):
    from webvuln import config
    bid = _old_rules_batch(client)
    monkeypatch.setattr(config, "NO_AUTH", False)
    assert client.get("/api/recompute/source").status_code == 401
    assert client.post("/api/recompute", json={"batch_id": bid, "rules_version": logic.RULES_VERSION, "items": []}).status_code == 401
    assert client.get("/api/import-report").status_code == 401


def test_new_frontend_import_marks_current_rules(client):
    r = _imp(client, "a.xlsx", [_f("1-系統", "1", "h1", d(3))], rules_version=logic.RULES_VERSION)
    assert client.get("/api/snapshot-meta").json()["batch_rules_version"] == logic.RULES_VERSION
    j = client.post("/api/recompute", json={"batch_id": r.json()["batch_id"], "rules_version": logic.RULES_VERSION,
                                            "items": []}).json()
    assert j["already"] is True


# ── 前端（node 跑原檔）──
_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const dir = process.argv[2];
const ctx = { console };
ctx.window = ctx; ctx.document = {};
ctx.XLSX = { utils: { sheet_to_json: (ws) => ws } };   // ws 直接給二維陣列
vm.createContext(ctx);
for (const f of ['config/config.js', 'config/profiles.js', 'js/utils.js', 'js/sheets.js', 'js/analysis.js'])
  vm.runInContext(fs.readFileSync(dir + '/' + f, 'utf8'), ctx, { filename: f });
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const iso = (d) => d ? d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') : null;
// sheets: [{name, columns, rows:[{col:val}]}]（跟 /api/snapshot、/api/recompute/source 同形）
const wb = { SheetNames: [], Sheets: {} };
input.sheets.forEach((s) => {
  const cols = s.columns.slice();   // 同 json_to_sheet：列裡有、欄序沒列到的鍵補在後面
  s.rows.forEach((r) => Object.keys(r).forEach((k) => { if (cols.indexOf(k) < 0) cols.push(k); }));
  const m = [cols].concat(s.rows.map((r) => cols.map((h) => (r[h] === undefined || r[h] === null) ? '' : r[h])));
  wb.SheetNames.push(String(s.name).slice(0, 31)); wb.Sheets[String(s.name).slice(0, 31)] = m;
});
const parsed = ctx.MultiSheet.parseWorkbookWb(wb);
const openRecs = [];
const out = { rules: ctx.MultiSheet.RULES_VERSION, checks: ctx.MultiSheet.checkImport(parsed), recs: [],
              counts: { unresolved: 0, overdue: 0, soon: 0, due_days_sum: 0 } };
parsed.forEach((res) => res.records.forEach((r) => {
  ctx.Analysis.finalizeRecord(r);
  out.recs.push({ sheet: r.sheet, row: r.rowNo, fid: r.raw.__fid, close: r.closeBucket, remark: r.remark || null,
                  fix: iso(r.fixDeadline || r.otherDue), ext: iso(r.firstExtension), exc: iso(r.exceptionApproval), realDue: iso(r.realDue) });
  if (r.closeBucket === 'open') {
    out.counts.unresolved++;
    openRecs.push(r);
    if (r.daysLeft !== null) out.counts.due_days_sum += r.daysLeft;
    if (r.daysLeft !== null && r.daysLeft < 0) out.counts.overdue++;
    else if (r.daysLeft !== null && r.daysLeft <= 30) out.counts.soon++;
  }
}));
out.counts.row_sig = ctx.MultiSheet.rowSig(openRecs);
process.stdout.write(JSON.stringify(out));
"""


def _run_front(tmp_path, sheets):
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node，略過前端原檔測試")
    h = tmp_path / "harness.js"
    h.write_text(_HARNESS, encoding="utf-8")
    p = subprocess.run([node, str(h), str(FRONT)], input=json.dumps({"sheets": sheets}, ensure_ascii=False).encode("utf-8"),
                       capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    return json.loads(p.stdout.decode("utf-8"))


def test_front_check_import(tmp_path):
    cols = ["外部情資", "資產名稱", "負責人", "修補期限", "首次展延期限", "例外單號", "例外核准期限", "備註", "結案狀態"]
    rows = [
        {"外部情資": "壞日期", "資產名稱": "a1", "負責人": "x", "修補期限": "待確認", "結案狀態": "未結案"},
        {"外部情資": "佔位字", "資產名稱": "a2", "負責人": "x", "修補期限": "2026/1/5", "首次展延期限": "-", "結案狀態": "未結案"},
        {"外部情資": "單號無日期", "資產名稱": "a3", "負責人": "x", "修補期限": "2026/1/5", "例外單號": "E9", "結案狀態": "未結案"},
        {"外部情資": "已結不檢查", "資產名稱": "a4", "負責人": "x", "修補期限": "看不懂", "結案狀態": "已結案"},
        {"外部情資": "文字多日期", "資產名稱": "a5", "負責人": "x", "修補期限": "DMZ 2026/9/27\nintranet 2027/2/27", "結案狀態": "未結案"},
        {"外部情資": "不合法", "資產名稱": "a6", "負責人": "x", "修補期限": "2025/2/31", "結案狀態": "未結案"},
        {"外部情資": "展延備註無日期", "資產名稱": "a7", "負責人": "x", "修補期限": "2026/1/5", "備註": "首次展延(iForm_1)", "結案狀態": "未結案"},
    ]
    out = _run_front(tmp_path, [{"name": "10-外部威脅情資", "columns": cols, "rows": rows}])
    bad = {(b["name"], b["row"], b["column"]) for b in out["checks"]["bad_dates"]}
    assert bad == {("壞日期", 2, "修補期限"), ("不合法", 7, "修補期限")}
    nd = {(x["name"], x["kind"], x["basis"]) for x in out["checks"]["apply_no_date"]}
    assert nd == {("單號無日期", "例外", "例外單號欄"), ("展延備註無日期", "展延", "備註")}
    assert out["rules"] and out["checks"]["parse_warnings"] == []


def test_front_recompute_roundtrip_and_consistency(client, tmp_path, engine):
    """舊規則存的批次 → 前端解析 /api/recompute/source → POST /api/recompute → 後端數字 ＝ 前端解析 /api/snapshot。"""
    fs = [_f("10-外部威脅情資", None, None, None, raw_extra={"資產名稱": "a1", "外部情資": "w",
                                                       "修補期限": "DMZ " + d(-3).replace("-", "/") + "\nintranet 2030/2/27"}),
          _f("10-外部威脅情資", None, None, d(-40), raw_extra={"資產名稱": "a2", "外部情資": "vm", "修補期限": d(-40).replace("-", "/"),
                                                         "例外單號": "E1", "例外核准期限": d(20).replace("-", "/")}),
          _f("10-外部威脅情資", None, None, d(5), raw_extra={"資產名稱": "a3", "外部情資": "ok", "修補期限": d(5).replace("-", "/")})]
    for i, f in enumerate(fs):
        f["host"] = None
        f["raw"].pop("Plugin ID"); f["raw"].pop("Host"); f["raw"].pop("Name")
        f["name"] = f["raw"]["外部情資"]
    cols = {"10-外部威脅情資": ["外部情資", "資產名稱", "修補期限", "例外單號", "例外核准期限", "結案狀態"]}
    r = _imp(client, "V20261116.xlsx", fs, cols=cols)        # 舊版前端：第 1 筆讀不到期限、第 2 筆沒認單號
    bid = r.json()["batch_id"]
    rep0 = client.get("/api/report").json()
    snap = json.loads(client.get("/api/snapshot").content)
    live = _run_front(tmp_path, [{"name": s["name"], "columns": s["columns"], "rows": s["rows"]} for s in snap["sheets"]])["counts"]
    assert (rep0["overdue"], live["overdue"]) == (1, 1)       # 舊：只有 a2(-40)；新：a1(-3) 逾期、a2 例外到 +20
    assert rep0["soon"] != live["soon"] and rep0["due_days_sum"] != live["due_days_sum"]                         # ← 事故 b：伺服器跟畫面對不上（自檢要抓到）

    src = client.get("/api/recompute/source").json()
    out = _run_front(tmp_path, src["sheets"])
    items, seen = [], set()
    for x in out["recs"]:
        if x["fid"] in seen:
            continue
        seen.add(x["fid"])
        items.append({"id": x["fid"], "remediation_due": x["fix"], "first_extension_due": x["ext"],
                      "exception_due": x["exc"], "close_status": "未結案" if x["close"] == "open" else "已結案",
                      "remark": x["remark"]})
    j = client.post("/api/recompute", json={"batch_id": bid, "rules_version": out["rules"], "items": items}).json()
    assert j["updated"] == 2 and j["missing"] == 0
    rep1 = client.get("/api/report").json()
    assert {k: rep1[k] for k in ("unresolved", "overdue", "soon", "due_days_sum", "row_sig")} == live    # 重算後兩邊一致
    with sessionmaker(bind=engine)() as s:
        for x in out["recs"]:   # 後端存的到期日＝前端即時算的到期日（逐筆）
            f = s.get(Finding, x["fid"])
            assert (f.effective_due.isoformat() if f.effective_due else None) == x["realDue"], x


# ── GPT 檢核／裁判追加（V2.110）──
def test_missing_sheet_guard_409_and_confirm(client, engine):
    _imp(client, "a.xlsx", [_f("1-系統", "1", "h1", d(3)), _f("2-IoT", "9", "i1", d(3))])
    r = _imp(client, "b.xlsx", [_f("1-系統", "1", "h1", d(3))])
    assert r.status_code == 409
    det = r.json()["detail"]
    assert det["code"] == "sheets_missing" and det["older"] is None
    assert det["sheets_missing"] == [{"sheet": "2-IoT", "prev_rows": 1, "now_rows": 0, "status": "整張不見（可能改名）"}]
    assert len(client.get("/api/batches").json()) == 1
    # 0 筆也擋
    r0 = _imp(client, "b.xlsx", [_f("1-系統", "1", "h1", d(3))], cols={"1-系統": COLS1, "2-IoT": COLS1})
    assert r0.status_code == 409 and r0.json()["detail"]["sheets_missing"][0]["status"] == "這次 0 筆"
    # 只確認舊檔、沒確認工作表 → 照樣擋
    r1 = _imp(client, "b.xlsx", [_f("1-系統", "1", "h1", d(3))], confirm_older=True,
              expected_latest_batch_id=det["latest_batch_id"])
    assert r1.status_code == 409 and r1.json()["detail"]["code"] == "sheets_missing"
    r2 = _imp(client, "b.xlsx", [_f("1-系統", "1", "h1", d(3))], confirm_missing_sheets=True,
              expected_latest_batch_id=det["latest_batch_id"])
    assert r2.status_code == 200, r2.text
    assert r2.json()["report"]["sheets_missing_confirmed"] is True
    with sessionmaker(bind=engine)() as s:
        a = s.execute(select(AuditLog).where(AuditLog.action == "import").order_by(AuditLog.id.desc())).scalars().first()
        assert "工作表不見已確認" in a.detail


def test_confirm_bound_to_latest_batch(client):
    """使用者確認的是 #1，期間有人匯入了 #2 → 確認作廢，409 latest_changed，什麼都沒寫。"""
    _imp(client, "V20261116.xlsx", [_f("1-系統", "1", "h1", d(3))])
    det = _imp(client, "V20261111.xlsx", [_f("1-系統", "1", "h1", d(3))]).json()["detail"]
    assert _imp(client, "V20261117.xlsx", [_f("1-系統", "1", "h1", d(3))]).status_code == 200   # 別人插隊
    r = _imp(client, "V20261111.xlsx", [_f("1-系統", "1", "h1", d(3))], confirm_older=True,
             expected_latest_batch_id=det["latest_batch_id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] in ("latest_changed", "older_file")
    assert len(client.get("/api/batches").json()) == 2


def test_create_batch_atomic_latest_check(session):
    """匯入發佈那一步再驗一次：expected 不是目前最新 → LatestChanged，不寫入。"""
    from webvuln import importer
    from webvuln.schemas import FindingIn, ImportIn
    data = ImportIn(source_file="a.xlsx", findings=[FindingIn(sheet_key="1-系統", plugin_id="1", host="h")])
    b1 = importer.create_batch(session, data)
    b2 = importer.create_batch(session, data)
    with pytest.raises(importcheck.LatestChanged):
        importer.create_batch(session, data, expected_latest=b1.id)
    assert session.execute(select(func.count(ImportBatch.id))).scalar_one() == 2
    assert session.get(ImportBatch, b2.id).is_latest is True
    b3 = importer.create_batch(session, data, expected_latest=b2.id)
    assert b3.is_latest and session.get(ImportBatch, b2.id).is_latest is False


def test_recompute_strict_items(client, engine):
    bid = _old_rules_batch(client)
    ids = [r["__fid"] for r in client.get("/api/recompute/source").json()["sheets"][0]["rows"]]
    one = lambda i: {"id": i, "remediation_due": "2026-01-01", "close_status": "未結案"}
    post = lambda items: client.post("/api/recompute", json={"batch_id": bid, "rules_version": logic.RULES_VERSION, "items": items})
    for items, key in (([], None), ([one(ids[0])], "missing"), ([one(i) for i in ids] + [one(99999)], "extra"),
                       ([one(ids[0]), one(ids[0]), one(ids[1])], "duplicate")):
        r = post(items)
        assert r.status_code == 400, (items, r.text)
        det = r.json()["detail"]
        assert det["expected"] == 2
        if key:
            assert det[key] >= 1
    with sessionmaker(bind=engine)() as s:   # 一筆都沒寫、版本沒蓋
        assert s.get(ImportBatch, bid).rules_version is None
        assert s.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "recompute_rules")).scalar_one() == 0
    # 換批之後才送：409（不是 already）
    _imp(client, "V20261117.xlsx", [_f("10-外部威脅情資", "1", "z", d(3))], cols={"10-外部威脅情資": COLS1})
    assert post([one(i) for i in ids]).status_code == 409


def test_recompute_guard_requires_latest(session):
    """條件式 UPDATE 也要求 is_latest：批次被別人換掉（不是最新）→ LookupError，不是 already。"""
    from webvuln import importer
    from webvuln.schemas import FindingIn, ImportIn, RecomputeItem
    b = importer.create_batch(session, ImportIn(source_file="a.xlsx", findings=[FindingIn(sheet_key="1-系統", plugin_id="1", host="h")]))
    fid = b.findings[0].id
    importer.create_batch(session, ImportIn(source_file="b.xlsx", findings=[FindingIn(sheet_key="1-系統", plugin_id="1", host="h")]))
    with pytest.raises(LookupError):
        importcheck.recompute(session, b.id, logic.RULES_VERSION, [RecomputeItem(id=fid)])


def test_compare_rules_note(client, engine):
    _imp(client, "a.xlsx", [_f("1-系統", "1", "h1", d(3))])                                    # 舊版前端：無版本
    _imp(client, "b.xlsx", [_f("1-系統", "1", "h1", d(3))], rules_version=logic.RULES_VERSION)
    cp = client.get("/api/compare").json()
    assert cp["rules_note"] and logic.RULES_VERSION in cp["rules_note"] and "規則變更" in cp["rules_note"]
    _imp(client, "c.xlsx", [_f("1-系統", "1", "h1", d(3))], rules_version=logic.RULES_VERSION)
    assert client.get("/api/compare").json()["rules_note"] is None                            # 同版本不提示


def test_batch_list_report_error_not_no_issue(client, engine):
    _imp(client, "a.xlsx", [_f("1-系統", "1", "h1", d(3))])
    with sessionmaker(bind=engine)() as s:
        b = s.execute(select(ImportBatch)).scalars().first()
        b.import_report = {"version": 1, "error": "boom"}
        s.commit()
    row = client.get("/api/batches").json()[0]
    assert row["has_report"] is True and row["report_error"] is True and row["report_issues"] is None


def test_report_and_snapshot_carry_batch_id(client):
    bid = _imp(client, "a.xlsx", [_f("1-系統", "1", "h1", d(3))]).json()["batch_id"]
    assert client.get("/api/report").json()["batch_id"] == bid
    assert json.loads(client.get("/api/snapshot").content)["batch_id"] == bid


def test_row_sig_detects_swapped_due_dates(client, tmp_path, engine):
    """裁判追加：兩列到期日對調（筆數、逾期、加總都不變）→ 逐筆指紋要對不上；修好後要一致。"""
    cols = {"1-系統": COLS1}
    fs = [_f("1-系統", "1", "h1", d(10)), _f("1-系統", "2", "h2", d(40))]
    _imp(client, "a.xlsx", fs, cols=cols, rules_version=logic.RULES_VERSION)
    def front():
        snap = json.loads(client.get("/api/snapshot").content)
        return _run_front(tmp_path, [{"name": s["name"], "columns": s["columns"], "rows": s["rows"]} for s in snap["sheets"]])["counts"]
    rep, live = client.get("/api/report").json(), front()
    assert rep["row_sig"] == live["row_sig"]                       # 正常時一致（前後端同算法）
    # 伺服器存的到期日兩列對調（模擬判錯）：Excel 原值不變
    with sessionmaker(bind=engine)() as s:
        a, b = s.execute(select(Finding).order_by(Finding.id)).scalars().all()
        a.effective_due, b.effective_due = b.effective_due, a.effective_due
        s.commit()
    rep2 = client.get("/api/report").json()
    assert {k: rep2[k] for k in ("unresolved", "overdue", "soon", "due_days_sum")} == \
           {k: live[k] for k in ("unresolved", "overdue", "soon", "due_days_sum")}   # 加總抓不到
    assert rep2["row_sig"] != live["row_sig"]                      # 逐筆指紋抓得到


def test_front_check_both_applied_and_placeholder(tmp_path):
    cols = ["外部情資", "資產名稱", "負責人", "修補期限", "首次展延期限", "例外核准期限", "例外單號", "弱點修補展延單號", "結案狀態"]
    rows = [{"外部情資": "兩個都沒日期", "資產名稱": "a1", "負責人": "x", "修補期限": "2026/1/5", "例外單號": "E1",
             "弱點修補展延單號": "X1", "結案狀態": "未結案"},
            {"外部情資": "原型屬性字", "資產名稱": "a2", "負責人": "x", "修補期限": "2026/1/5", "首次展延期限": "constructor",
             "結案狀態": "未結案"},
            {"外部情資": "單號是constructor", "資產名稱": "a3", "負責人": "x", "修補期限": "2026/1/5",
             "例外單號": "constructor", "例外核准期限": "2030/1/1", "結案狀態": "未結案"}]
    out = _run_front(tmp_path, [{"name": "10-外部威脅情資", "columns": cols, "rows": rows}])
    kinds = sorted(x["kind"] for x in out["checks"]["apply_no_date"] if x["name"] == "兩個都沒日期")
    assert kinds == ["例外", "展延"]
    assert [b["name"] for b in out["checks"]["bad_dates"]] == ["原型屬性字"]          # 'constructor' 不是佔位字
    r3 = next(x for x in out["recs"] if x["remark"] is None and x["exc"] == "2030-01-01")
    assert r3["realDue"] == "2030-01-01"                                              # 單號 'constructor' 算有填

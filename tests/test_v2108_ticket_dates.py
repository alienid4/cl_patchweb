"""V2.108（2026-11-23 真實 Excel 校準）：
1. 單號欄＝已申請：「10-外部威脅情資」用「例外單號」「弱點修補展延單號」欄，不在備註寫 iForm。
2. 期限格是文字／多個日期（「DMZ 2026/9/27\\nintranet 2027/2/27」）→ 取最早那天。
3. 未結但沒有可用到期日 → 「缺期限・待確認」（算需申請），不再掉進「暫無需申請（期限內）」。
前端（utils.js／sheets.js／analysis.js）用 node 實際跑原檔驗證（沒裝 node 就略過那幾題），後端走 API。
"""
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from webvuln import logic

FRONT = Path(__file__).resolve().parents[1] / "frontend"
D = dt.date

# 真實列（去識別化）：VMware 例外單號有填、備註空
VMWARE_RAW = {"資產名稱": "vc01", "外部情資": "VMware vCenter", "風險等級": "High", "負責單位": "A", "負責人": "x",
              "修補期限": "2025/10/01", "首次展延期限": "2025/11/01",
              "例外單號": "25092423289\n202605008(紙本)", "例外核准期限": "2026/12/31",
              "弱點修補展延單號": "", "備註": "", "結案狀態": "未結案"}
WINDOWS_RAW = {"資產名稱": "win01", "外部情資": "Microsoft Windows", "風險等級": "High", "負責單位": "A", "負責人": "x",
               "修補期限": "DMZ 2026/9/27\nintranet 2027/2/27", "首次展延期限": "DMZ 2026/10/27",
               "例外單號": "", "例外核准期限": "",
               "弱點修補展延單號": "首次展延_\niForm_26090192374", "備註": "", "結案狀態": "未結案"}


# ── 1. 後端：單號欄 ──
def test_ticket_column_counts_as_applied_exception():
    exc, ext, rem = D(2026, 12, 31), D(2025, 11, 1), D(2025, 10, 1)
    assert logic.compute_effective_due(exc, ext, rem, "", VMWARE_RAW) == exc
    assert logic.compute_stage(exc, ext, rem, "", VMWARE_RAW) == logic.STAGE_EXCEPTION
    # 沒有 raw（舊呼叫法）＝原本行為：備註空就用修補期限
    assert logic.compute_effective_due(exc, ext, rem, "") == rem
    assert logic.compute_stage(exc, ext, rem, "") == logic.STAGE_ORIGINAL


def test_ticket_column_counts_as_applied_extension():
    ext, rem = D(2026, 10, 27), D(2026, 9, 27)
    assert logic.compute_effective_due(None, ext, rem, "", WINDOWS_RAW) == ext
    assert logic.compute_stage(None, ext, rem, "", WINDOWS_RAW) == logic.STAGE_EXTENSION


def test_ticket_rules_priority_and_blanks():
    exc, ext, rem = D(2027, 1, 1), D(2026, 12, 1), D(2026, 1, 1)
    both = {"例外單號": "E1", "弱點修補展延單號": "X1"}
    assert logic.compute_stage(exc, ext, rem, None, both) == logic.STAGE_EXCEPTION   # 例外優先
    assert logic.compute_stage(None, ext, rem, None, both) == logic.STAGE_EXTENSION  # 沒例外日期 → 展延
    assert logic.compute_stage(None, None, rem, None, both) == logic.STAGE_ORIGINAL  # 單號有、日期沒有 → 原始
    for blank in ("", "  ", "-", "無", "N/A", None):
        r = {"例外單號": blank, "弱點修補展延單號": blank}
        assert logic.compute_stage(exc, ext, rem, None, r) == logic.STAGE_ORIGINAL, blank
    # 欄名要同時含「例外」與「單號」；只有「單號」或只有「例外」不算
    assert logic.compute_stage(exc, None, rem, None, {"單號": "A1", "例外說明": "有"}) == logic.STAGE_ORIGINAL
    # 單號是數字也算有填
    assert logic.compute_stage(exc, None, rem, None, {"例外單號": 25092423289}) == logic.STAGE_EXCEPTION


def test_import_uses_ticket_columns(client):
    f = dict(sheet_key="10-外部威脅情資", plugin_id=None, name="VMware vCenter", host=None, severity="High",
             department="A", owner="x", remediation_due="2025-10-01", first_extension_due="2025-11-01",
             exception_due="2026-12-31", close_status="未結案", remark=None, raw=VMWARE_RAW)
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": [f]}).status_code == 200
    r = client.get("/api/findings", params={"status": "全部"}).json()[0]
    assert r["stage"] == logic.STAGE_EXCEPTION
    assert r["effective_due"] == "2026-12-31"
    assert r["exc_applied"] is True          # 例外核准期限那格不能標灰
    assert r["host"] == "vc01"


# ── 3. 缺期限・待確認 ──
def test_no_due_bucket(client):
    t = dt.date.today()
    d = lambda n: (t + dt.timedelta(days=n)).isoformat()
    base = dict(sheet_key="s1", name="v", severity="High", department="A", owner="x", close_status="未結案")
    fs = [dict(base, plugin_id="1", host="h_nodue", remediation_due=None),                 # 缺期限
          dict(base, plugin_id="2", host="h_far", remediation_due=d(300)),                 # 暫無需申請
          dict(base, plugin_id="3", host="h_ovd", remediation_due=d(-3)),                  # 應申請未申請
          dict(base, plugin_id="4", host="h_exc", remediation_due=d(-90), exception_due=d(120),
               remark="例外管理(iForm_1)"),                                                # 已核准
          dict(base, plugin_id="5", host="h_nodue2", remediation_due=None, close_status="已結案")]  # 已結不算
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": fs}).status_code == 200
    hosts = lambda **p: sorted(r["host"] for r in client.get("/api/findings", params=p).json())
    for hp in ({}, {"horizon": 0}, {"horizon": 90}):
        assert hosts(no_due_bucket="true", **hp) == ["h_nodue"], hp
        assert "h_nodue" not in hosts(not_apply="true", **hp), hp
        assert "h_nodue" in hosts(apply_universe="true", **hp), hp
        parts = [hosts(**{k: "true"}, **hp) for k in ("should_apply", "submitted", "applied", "no_due_bucket", "not_apply")]
        assert sorted(sum(parts, [])) == hosts(**hp)              # 五類互斥、相加＝未結
    assert hosts(not_apply="true") == ["h_far"]
    rep = client.get("/api/report").json()
    assert rep["no_due_count"] == 1
    assert rep["apply_universe"] == (rep["need_apply_count"] + rep["submitted_count"] + rep["applied_count"]
                                     + rep["no_due_count"]) == 3
    assert rep["unresolved"] - rep["apply_universe"] == 1            # 暫無需申請只剩 h_far
    # 與上次比較：缺期限也算需申請
    assert client.post("/api/import", json={"source_file": "t2.xlsx", "findings": fs}).status_code == 200
    m = {x["key"]: x for x in client.get("/api/compare").json()["metrics"]}
    assert m["nodue"]["cur"] == 1
    assert m["unresolved"]["cur"] == m["apply_universe"]["cur"] + m["not_apply"]["cur"]


def test_no_due_submitted_goes_to_submitted(client):
    """沒到期日但承辦已標送審中 → 算送審中，不重複算缺期限。"""
    f = dict(sheet_key="s1", plugin_id="1", name="v", host="h1", severity="High", department="A", owner="x",
             close_status="未結案", remediation_due=None)
    assert client.post("/api/import", json={"source_file": "t.xlsx", "findings": [f]}).status_code == 200
    fid = client.get("/api/findings").json()[0]["id"]
    assert client.post(f"/api/findings/{fid}/overlay", json={"set_progress": True, "progress": "展延送審中"}).status_code == 200
    ids = lambda **p: {r["id"] for r in client.get("/api/findings", params=p).json()}
    assert ids(submitted="true") == {fid}
    assert ids(no_due_bucket="true") == set()


# ── 前端原檔（node）──
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
const out = {};
out.parse = input.dates.map((v) => iso(ctx.Utils.parseDateEarliest(v)));
const wb = { SheetNames: [], Sheets: {} };
input.sheets.forEach((s) => {
  const rows = s.rows.map((r) => s.headers.map((h) => {
    const v = r[h];
    return (v && typeof v === 'object' && v.date) ? new Date(v.date[0], v.date[1] - 1, v.date[2]) : (v === undefined ? '' : v);
  }));
  wb.SheetNames.push(s.name); wb.Sheets[s.name] = [s.headers].concat(rows);
});
out.recs = [];
ctx.MultiSheet.parseWorkbookWb(wb).forEach((res) => res.records.forEach((r) => {
  ctx.Analysis.finalizeRecord(r);
  out.recs.push({ sheet: r.sheet, name: r.name, realDue: iso(r.realDue), stage: r.stage,
                  fix: iso(r.fixDeadline), ext: iso(r.firstExtension), exc: iso(r.exceptionApproval) });
}));
process.stdout.write(JSON.stringify(out));
"""


def _run_front(tmp_path, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node，略過前端原檔測試")
    h = tmp_path / "harness.js"
    h.write_text(_HARNESS, encoding="utf-8")
    p = subprocess.run([node, str(h), str(FRONT)], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    return json.loads(p.stdout.decode("utf-8"))


def test_front_parse_date_earliest(tmp_path):
    dates = ["DMZ 2026/9/27\nintranet 2027/2/27", "DMZ 2026/10/27", "2025/4/12", "2025-04-12", "2025.4.12",
             "2025年4月12日", "114/9/27", "intranet 2027/2/27 / DMZ 2026-09-27", "待確認", "", "2025/2/31",
             46022]   # Excel 序號 46022 = 2025-12-31
    out = _run_front(tmp_path, {"dates": dates, "sheets": []})
    assert out["parse"] == ["2026-09-27", "2026-10-27", "2025-04-12", "2025-04-12", "2025-04-12",
                            "2025-04-12", "2025-09-27", "2026-09-27", None, None, None, "2025-12-31"]


def test_front_records_match_backend(tmp_path):
    h10 = list(VMWARE_RAW.keys())
    sheets = [
        {"name": "10-外部威脅情資", "headers": h10, "rows": [VMWARE_RAW, WINDOWS_RAW,
            dict(VMWARE_RAW, 外部情資="無單號", 例外單號="")]},
        # 表 1（系統弱點掃描）照舊：日期是 Date、備註決定是否已申請
        {"name": "1-系統弱點掃描", "headers": ["Plugin ID", "Host", "Name", "風險等級", "負責單位", "負責人", "修補期限",
                                          "首次展延上限", "例外核准期限", "備註", "結案狀態"],
         "rows": [
             {"Plugin ID": "1", "Host": "10.0.0.1", "Name": "s1-原始", "風險等級": "High", "負責單位": "A", "負責人": "x",
              "修補期限": {"date": [2026, 1, 5]}, "首次展延上限": {"date": [2026, 3, 5]}, "備註": "", "結案狀態": "未結案"},
             {"Plugin ID": "2", "Host": "10.0.0.2", "Name": "s1-展延", "風險等級": "High", "負責單位": "A", "負責人": "x",
              "修補期限": {"date": [2026, 1, 5]}, "首次展延上限": {"date": [2026, 3, 5]}, "備註": "首次展延(iForm_9)",
              "結案狀態": "未結案"},
             {"Plugin ID": "3", "Host": "10.0.0.3", "Name": "s1-缺期限", "風險等級": "High", "負責單位": "A",
              "負責人": "x", "修補期限": "", "備註": "", "結案狀態": "未結案"},
         ]},
    ]
    out = _run_front(tmp_path, {"dates": [], "sheets": sheets})
    by = {r["name"]: r for r in out["recs"]}
    assert (by["VMware vCenter"]["realDue"], by["VMware vCenter"]["stage"]) == ("2026-12-31", "exception")
    w = by["Microsoft Windows"]
    assert (w["fix"], w["ext"], w["realDue"], w["stage"]) == ("2026-09-27", "2026-10-27", "2026-10-27", "extension")
    assert (by["無單號"]["realDue"], by["無單號"]["stage"]) == ("2025-10-01", "original")
    assert (by["s1-原始"]["realDue"], by["s1-原始"]["stage"]) == ("2026-01-05", "original")
    assert (by["s1-展延"]["realDue"], by["s1-展延"]["stage"]) == ("2026-03-05", "extension")
    assert by["s1-缺期限"]["realDue"] is None
    # 前後端同一套：把前端算好的日期丟給後端規則，結果要一樣
    for raw, name in ((VMWARE_RAW, "VMware vCenter"), (WINDOWS_RAW, "Microsoft Windows")):
        r = by[name]
        iso = lambda s: D.fromisoformat(s) if s else None
        be = logic.compute_effective_due(iso(r["exc"]), iso(r["ext"]), iso(r["fix"]), "", raw)
        assert be.isoformat() == r["realDue"], name


def test_front_remark_exception_date_when_no_exc_column(tmp_path):
    """表 9 沒有例外核准期限欄：備註「申請例外於2026/12/31」→ 例外到 2026-12-31，不可用修補期限文字裡的 2025/11/8 判逾期。"""
    hs = ["網址", "發現", "風險", "部門", "負責人員", "修補期限", "首次展延期限", "預計完成日", "結案狀態", "備註"]
    row = {"網址": "a.example", "發現": "CSP", "風險": "中*4", "部門": "專案開發部", "負責人員": "x",
           "修補期限": "中：2025/11/8\n低：若不修補，需留下評估紀錄", "首次展延期限": {"date": [2025, 12, 8]},
           "預計完成日": {"date": [2026, 12, 31]}, "結案狀態": "未結案\n(申請例外)",
           "備註": "#25102249243例外申請於2026/6/30修補\n#26062617179 申請例外於2026/12/31修"}
    no_exc = dict(row, 發現="無例外", 備註="")
    out = _run_front(tmp_path, {"dates": [], "sheets": [{"name": "9-Security Header", "headers": hs, "rows": [row, no_exc]}]})
    r = next(x for x in out["recs"] if x["name"] == "CSP")
    assert (r["exc"], r["realDue"], r["stage"]) == ("2026-12-31", "2026-12-31", "exception")
    n = next(x for x in out["recs"] if x["name"] == "無例外")
    assert n["exc"] is None and n["realDue"] == "2025-11-08"

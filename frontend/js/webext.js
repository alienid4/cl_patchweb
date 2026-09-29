/* ============================================================
 * js/webext.js  — 網頁版黏合層（不改單機版任何原檔）
 * 1) 開頁：抓伺服器最新快照 → 重建 workbook → 餵回原本 #file-input →
 *    走單機版原本的解析/render，畫面一模一樣（差別只在資料來自伺服器）。
 * 2) 使用者上傳 Excel：照原本渲染之外，另把 raw 存回伺服器(/api/import) 供持久化與新功能。
 * 原本 js 一律不動；本檔只做「加法」，載入順序在 main.js 之後。
 * ============================================================ */
(function (global) {
  'use strict';
  var suppressPersist = false;  // 由伺服器回填的合成上傳不要再存回伺服器（避免回圈）

  function isoLocal(d) {
    if (!d) return null;
    if (typeof d === 'string') return d.slice(0, 10) || null;
    if (d instanceof Date && !isNaN(d)) {
      var m = String(d.getMonth() + 1).padStart(2, '0'), day = String(d.getDate()).padStart(2, '0');
      return d.getFullYear() + '-' + m + '-' + day;
    }
    return null;
  }
  function cleanRaw(raw) {
    if (!raw || typeof raw !== 'object') return raw;
    var out = {};
    Object.keys(raw).forEach(function (k) {
      var v = raw[k];
      out[k] = (v instanceof Date && !isNaN(v))
        ? v.getFullYear() + '/' + String(v.getMonth() + 1).padStart(2, '0') + '/' + String(v.getDate()).padStart(2, '0')
        : v;
    });
    return out;
  }
  function recToFinding(r) {
    return {
      sheet_key: r.sheet, plugin_id: r.pluginId || null, name: r.name || null, host: r.host || null,
      severity: (r.severity && r.severity !== 'Unknown') ? r.severity : null,
      severity_raw: r.severityRaw || null,
      department: r.unit || null,
      owner: (r.owner && r.owner !== '(未指定)') ? r.owner : null,
      remediation_due: isoLocal(r.fixDeadline || r.otherDue),
      first_extension_due: isoLocal(r.firstExtension),
      exception_due: isoLocal(r.exceptionApproval),
      close_status: r.closeBucket === 'closed' ? '已結案' : r.closeBucket === 'other' ? '其他' : '未結案',
      close_date: isoLocal(r.closeDate),
      remark: r.remark || null,
      raw: cleanRaw(r.raw) || null,
    };
  }

  /* 把伺服器快照重建成 workbook（沿用原欄序、原值），再包成 File */
  function snapshotToFile(snap) {
    var wb = XLSX.utils.book_new();
    snap.sheets.forEach(function (s) {
      var ws = XLSX.utils.json_to_sheet(s.rows, { header: s.columns });
      var name = String(s.name || 'sheet').slice(0, 31);
      XLSX.utils.book_append_sheet(wb, ws, name);
    });
    var out = XLSX.write(wb, { type: 'array', bookType: 'xlsx' });
    return new File([out], snap.source_file || 'server.xlsx',
      { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
  }

  function feedToApp(file, fromServer) {
    var input = document.getElementById('file-input');
    if (!input) return;
    suppressPersist = !!fromServer;
    var dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    // change 是同步派發，原本 handleFile 會接手；旗標於下一輪還原
    setTimeout(function () { suppressPersist = false; }, 0);
  }

  async function loadFromServer() {
    try {
      var r = await fetch('/api/snapshot');
      if (!r.ok) return false;
      var snap = await r.json();
      if (!snap || !snap.sheets || !snap.sheets.length) return false;
      feedToApp(snapshotToFile(snap), true);
      return true;
    } catch (e) { return false; }
  }

  /* 使用者上傳 → 解析成 findings → 存回伺服器（與原本渲染並行，不干擾） */
  async function persistToServer(file) {
    if (suppressPersist) return;
    if (!global.MultiSheet) return;
    try {
      var buf = await file.arrayBuffer();
      var sheets = global.MultiSheet.parseWorkbook(buf);
      if (!sheets.length) return;
      var findings = [], sheet_columns = {};
      sheets.forEach(function (s) {
        sheet_columns[s.name] = s.headers;
        s.records.forEach(function (rec) { findings.push(recToFinding(rec)); });
      });
      var rp = await fetch('/api/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_file: file.name, findings: findings, sheet_columns: sheet_columns }),
      });
      if (rp.ok && global.UI && global.UI.toast) {
        var b = await rp.json();
        global.UI.toast('已存到伺服器（' + b.row_count + ' 筆）', 'success');
      }
    } catch (e) { /* 存伺服器失敗不影響本機渲染 */ }
  }

  function wirePersist() {
    var input = document.getElementById('file-input');
    if (input) input.addEventListener('change', function (e) {
      if (e.target.files && e.target.files[0]) persistToServer(e.target.files[0]);
    });
    var dz = document.getElementById('drop-zone');
    if (dz) dz.addEventListener('drop', function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) persistToServer(f);
    });
  }

  function start() {
    wirePersist();
    loadFromServer();  // 有伺服器資料就自動載入；沒有則維持原本上傳畫面
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})(window);

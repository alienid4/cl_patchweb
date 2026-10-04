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
      } else if ((rp.status === 401 || rp.status === 403) && global.UI && global.UI.toast) {
        // 關掉免登入後，要先登入(承辦/管理員)才能把資料存進伺服器
        global.UI.toast('本機已顯示，但未存到伺服器：請先登入（承辦／管理員）再匯入', 'error');
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

  /* ===================== 新功能（加法，風格沿用原本）===================== */
  var U = global.Utils, UI = global.UI;
  var me = { authenticated: false };
  var PIPE = ['未申請', '待主管', '待資安', '核准', '完成', '退回補件'];
  var NEXT = { '未申請': ['待主管'], '待主管': ['待資安', '退回補件'], '待資安': ['核准', '退回補件'],
    '核准': ['完成'], '退回補件': ['待主管'], '完成': [] };
  function canWrite() { return me.open_write || (me.authenticated && (me.role === 'admin' || me.role === '承辦')); }
  async function jget(u) { var r = await fetch(u); if (!r.ok) throw new Error(u + ' ' + r.status); return r.json(); }
  // 讀「左側目前選的部門」（沿用原本 #my-dept-select），承辦管線各面板都要吃它
  function curDept() { var s = document.getElementById('my-dept-select'); var v = s && s.value; return (v && v !== '__all__') ? v : null; }
  function qd(params) { var d = curDept(); if (d) (params = params || {}).department = d; return new URLSearchParams(params || {}).toString(); }

  function card(value, label, danger, onDrill) {
    var c = U.el('div', { class: 'metric-card' }, [
      U.el('div', { class: 'metric-value', text: String(value) }),
      U.el('div', { class: 'metric-label', text: label }),
    ]);
    if (danger && value && value !== '0') c.querySelector('.metric-value').style.color = '#c0392b';
    if (onDrill && value && value !== '0' && value !== '—') {
      c.style.cursor = 'pointer'; c.title = '點我看明細';
      c.addEventListener('click', onDrill);
    }
    return c;
  }
  function group(title, cards) {
    var wrap = U.el('div', { class: 'metric-group' }, [U.el('div', { class: 'metric-group-title', text: title })]);
    var cs = U.el('div', { class: 'metric-cards' });
    cards.forEach(function (c) { cs.appendChild(c); });
    wrap.appendChild(cs);
    return wrap;
  }

  // 讓表格可點欄位排序（數字欄按數值、其餘按字串；再點反向）。點了會在欄名顯示 ▲/▼,讓排序看得見。
  function makeSortable(table) {
    var ths = table.tHead ? table.tHead.rows[0].cells : [];
    function clearInd() {
      for (var k = 0; k < ths.length; k++) {
        var s = ths[k].querySelector('.webext-sortind'); if (s) s.textContent = '';
      }
    }
    for (var i = 0; i < ths.length; i++) (function (ci, th) {
      th.style.cursor = 'pointer'; th.title = '點我排序';
      if (!th.querySelector('.webext-sortind')) {
        th.appendChild(U.el('span', { class: 'webext-sortind', style: 'color:#1a7f4b;font-weight:700' }));
      }
      var asc = true;
      th.addEventListener('click', function () {
        var tb = table.tBodies[0]; if (!tb) return;
        var rows = Array.prototype.slice.call(tb.rows).filter(function (r) { return r.cells.length === ths.length; });
        rows.sort(function (a, b) {
          var x = a.cells[ci].textContent.trim(), y = b.cells[ci].textContent.trim();
          var nx = parseFloat(x.replace(/[^\d.-]/g, '')), ny = parseFloat(y.replace(/[^\d.-]/g, ''));
          var both = !isNaN(nx) && !isNaN(ny) && x !== '' && y !== '';
          var r = both ? (nx - ny) : (x > y ? 1 : x < y ? -1 : 0);
          return asc ? r : -r;
        });
        clearInd();
        var ind = th.querySelector('.webext-sortind'); if (ind) ind.textContent = asc ? ' ▲' : ' ▼';
        asc = !asc;
        rows.forEach(function (r) { tb.appendChild(r); });
      });
    })(i, ths[i]);
  }

  // ===== 小項內的頁籤：沿用原生膠囊樣式(.subtabs/.subtab-btn)，全系統一致 =====
  var _tabState = {};   // stateKey -> 目前開著第幾個頁籤(重繪時還原，不會跳回第一頁)
  // tabs = [{label, render(container)}]；stateKey 讓重繪(如編輯後)停在同一個頁籤
  function renderTabs(host, tabs, stateKey) {
    var bar = U.el('nav', { class: 'subtabs' });   // 與總覽「項目狀態/圖表/趨勢」同款膠囊
    var body = U.el('div');
    var btns = [];
    var initial = (stateKey && _tabState[stateKey] != null) ? _tabState[stateKey] : 0;
    if (initial >= tabs.length) initial = 0;
    function select(i) {
      btns.forEach(function (b, j) { b.classList.toggle('active', j === i); });
      if (stateKey) _tabState[stateKey] = i;
      body.innerHTML = '';
      tabs[i].render(body);
    }
    tabs.forEach(function (t, i) {
      var b = U.el('button', { class: 'subtab-btn', text: t.label });
      b.addEventListener('click', function () { select(i); });
      btns.push(b); bar.appendChild(b);
    });
    host.appendChild(bar); host.appendChild(body);
    select(initial);
  }

  // 下鑽：開視窗顯示 /api/findings 篩出的明細（可排序、可匯出此清單）
  async function openFindings(title, params) {
    var box = U.el('div');
    var cols = [['host', '主機'], ['owner', '負責人'], ['severity', '嚴重度'], ['name', '弱點'],
      ['plugin_id', 'Plugin'], ['effective_due', '到期日'], ['remediation_due', '原始期限'], ['overdue_days', '逾期天數'],
      ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
      ['department', '部門'], ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
    var curRows = [];   // 載入後填入,供「匯出」用(匯的是眼前這份子集)
    var footer = U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
      listExportButtons(function () { return curRows; }, title));   // 完整/簡易 兩顆
    UI.openModal(title, box, { footer: footer, wide: true });   // 寬版(近滿版)：欄多，免左右拉
    var rows;
    try { rows = await jget('/api/findings?' + qd(Object.assign({ status: '未結案' }, params || {}))); }
    catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
    if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無資料' })); return; }
    curRows = rows;
    var self = function () { openFindings(title, params); };
    var nPlugin = (function () { var s = {}; rows.forEach(function (r) { s[(r.plugin_id || '') + '|' + (r.name || '')] = 1; }); return Object.keys(s).length; })();
    box.appendChild(U.el('p', { class: 'empty-hint', text: '共 ' + rows.length + ' 筆、' + nPlugin + ' 種弱點'
      + (canWrite() ? '；點 ✏️ 編輯（存系統、重匯不洗）' : '') + '。修補以「弱點」為單位（同弱點常一次補完多台）。' }));
    // 切換：依負責人(預設，追人) / 依弱點彙總 / 依主機明細
    var tgl = U.el('nav', { class: 'subtabs', style: 'margin:4px 0 10px' });
    var bO = U.el('button', { class: 'subtab-btn active', text: '依負責人' });
    var bP = U.el('button', { class: 'subtab-btn', text: '依弱點彙總' });
    var bH = U.el('button', { class: 'subtab-btn', text: '依主機（明細）' });
    tgl.appendChild(bO); tgl.appendChild(bP); tgl.appendChild(bH); box.appendChild(tgl);
    var listBox = U.el('div'); box.appendChild(listBox);
    var mode = 'byowner';
    function setMode(m, btn) { mode = m; [bO, bP, bH].forEach(function (b) { b.classList.remove('active'); }); btn.classList.add('active'); draw(); }
    bO.addEventListener('click', function () { setMode('byowner', bO); });
    bP.addEventListener('click', function () { setMode('byplugin', bP); });
    bH.addEventListener('click', function () { setMode('byhost', bH); });

    // 依負責人：誰還有幾支＋其中逾期(主角度，追人不追 IP)；點名字展開他的明細
    function drawOwner() {
      var groups = {};
      rows.forEach(function (r) { var k = ((r.owner || '').trim()) || '— 未指派'; (groups[k] = groups[k] || []).push(r); });
      var keys = Object.keys(groups).sort(function (a, b) { return groups[b].length - groups[a].length; });
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, ['負責人', '部門', '筆數', '其中逾期', '附件'].map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      var icols = [['host', '主機'], ['severity', '嚴重度'], ['name', '弱點'], ['plugin_id', 'Plugin'],
        ['effective_due', '到期日'], ['remediation_due', '原始期限'], ['overdue_days', '逾期天數'],
        ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
        ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
      keys.forEach(function (k) {
        var grp = groups[k], r0 = grp[0];
        var od = grp.filter(function (x) { return x.overdue_days != null && x.overdue_days > 0; }).length;
        var att = grp.reduce(function (s, x) { return s + (x.att_count || 0); }, 0);
        var nameTd = U.el('td', { text: '▸ ' + k, style: 'font-weight:600;color:#1a7f4b;cursor:pointer;text-align:left;min-width:120px' });
        var head = U.el('tr', { style: 'cursor:pointer' }, [nameTd,
          U.el('td', { text: r0.department || '' }),
          U.el('td', { text: String(grp.length), style: 'font-weight:700' }),
          U.el('td', { text: od ? String(od) : '—', style: od ? 'color:#c0392b;font-weight:600' : '' }),
          U.el('td', { text: att ? ('📎' + att) : '' })]);
        var inner = U.el('table', { class: 'tracking-table', style: 'margin:0' });
        var ih = icols.map(function (c) { return c[1]; }); ih.push('操作');
        inner.appendChild(U.el('thead', {}, [U.el('tr', {}, ih.map(function (h) { return U.el('th', { text: h }); }))]));
        var itb = U.el('tbody');
        grp.forEach(function (r) {
          var tds = icols.map(function (c) { var td = U.el('td', { text: r[c[0]] == null ? '' : String(r[c[0]]) }); if (c[0] === 'name') { td.style.whiteSpace = 'normal'; td.style.textAlign = 'left'; } return td; });
          tds.push(opsCell(r, self));
          itb.appendChild(U.el('tr', {}, tds));
        });
        inner.appendChild(itb); makeSortable(inner);
        var detail = U.el('tr', { class: 'hidden' }, [U.el('td', { colspan: '5', style: 'background:#f6f8f7;padding:6px' }, [inner])]);
        head.addEventListener('click', function () { var hid = detail.classList.toggle('hidden'); nameTd.textContent = (hid ? '▸ ' : '▾ ') + k; });
        tb.appendChild(head); tb.appendChild(detail);
      });
      table.appendChild(tb); listBox.appendChild(table);
    }

    function drawHost() {
      var heads = cols.map(function (c) { return c[1]; }); heads.push('操作');
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, heads.map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      rows.forEach(function (r) {
        var tds = cols.map(function (c) {
          var td = U.el('td', { text: r[c[0]] == null ? '' : String(r[c[0]]) });
          if (c[0] === 'name') { td.style.whiteSpace = 'normal'; td.style.textAlign = 'left'; }
          return td;
        });
        tds.push(opsCell(r, self));
        tb.appendChild(U.el('tr', {}, tds));
      });
      table.appendChild(tb); listBox.appendChild(table); makeSortable(table);
    }
    // 依弱點彙總的排序狀態（點表頭切換；預設台數多→少）
    var SEV_RANK = { Critical: 4, High: 3, Medium: 2, Low: 1 };
    var _pSort = { key: 'count', dir: -1 };
    function drawPlugin() {
      var groups = {};
      rows.forEach(function (r) { var k = (r.plugin_id || '') + '|' + (r.name || ''); (groups[k] = groups[k] || []).push(r); });
      // 每個弱點彙總成一列的聚合值（供排序與顯示）
      var aggs = Object.keys(groups).map(function (k) {
        var grp = groups[k], r0 = grp[0];
        var od = grp.filter(function (x) { return x.overdue_days != null && x.overdue_days > 0; }).length;
        function minDate(field) { var ds = grp.map(function (x) { return x[field]; }).filter(Boolean).sort(); return ds.length ? ds[0] : ''; }
        function dueTxt(field) { var ds = {}; grp.forEach(function (x) { if (x[field]) ds[x[field]] = 1; }); var ks = Object.keys(ds).sort(); return ks.length <= 1 ? (ks[0] || '—') : (ks[0] + ' 等'); }
        return { k: k, grp: grp, r0: r0, count: grp.length, od: od,
          name: r0.name || r0.plugin_id || '', sev: SEV_RANK[r0.severity] || 0,
          effMin: minDate('effective_due'), origMin: minDate('remediation_due'),
          effTxt: dueTxt('effective_due'), origTxt: dueTxt('remediation_due'),
          stage: r0.stage || '', plugin: r0.plugin_id || '' };
      });
      aggs.sort(function (a, b) {
        var kf = _pSort.key, va = a[kf], vb = b[kf];
        if (va < vb) return -1 * _pSort.dir; if (va > vb) return 1 * _pSort.dir;
        return b.count - a.count;   // 同值再以台數多者在前
      });
      var table = U.el('table', { class: 'tracking-table' });
      // 可排序表頭：[顯示字, 排序鍵]；點擊切換升降序
      var HEADS = [['弱點', 'name'], ['Plugin', 'plugin'], ['嚴重度', 'sev'], ['台數', 'count'],
        ['到期日', 'effMin'], ['原始期限', 'origMin'], ['處置階段', 'stage'], ['其中逾期', 'od']];
      var htr = U.el('tr', {});
      HEADS.forEach(function (h) {
        var arrow = _pSort.key === h[1] ? (_pSort.dir === 1 ? ' ▲' : ' ▼') : '';
        var th = U.el('th', { text: h[0] + arrow, style: 'cursor:pointer;user-select:none' });
        th.title = '點擊依此欄排序';
        th.addEventListener('click', function () {
          if (_pSort.key === h[1]) _pSort.dir *= -1;
          else { _pSort.key = h[1]; _pSort.dir = (h[1] === 'count' || h[1] === 'sev' || h[1] === 'od') ? -1 : 1; }
          draw();
        });
        htr.appendChild(th);
      });
      table.appendChild(U.el('thead', {}, [htr]));
      var tb = U.el('tbody');
      var icols = [['host', '主機'], ['effective_due', '到期日'], ['remediation_due', '原始期限'], ['overdue_days', '逾期天數'],
        ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
        ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
      aggs.forEach(function (a) {
        var grp = a.grp, r0 = a.r0;
        var nameTd = U.el('td', { text: '▸ ' + a.name, style: 'white-space:normal;font-weight:600;color:#1a7f4b;cursor:pointer;min-width:240px;text-align:left' });
        var head = U.el('tr', { style: 'cursor:pointer' }, [nameTd,
          U.el('td', { text: a.plugin }), U.el('td', { text: r0.severity || '' }),
          U.el('td', { text: String(a.count), style: 'font-weight:700' }),
          U.el('td', { text: a.effTxt }), U.el('td', { text: a.origTxt }), U.el('td', { text: a.stage }),
          U.el('td', { text: a.od ? String(a.od) : '—', style: a.od ? 'color:#c0392b;font-weight:600' : '' })]);
        // 展開：這個弱點影響的主機
        var inner = U.el('table', { class: 'tracking-table', style: 'margin:0' });
        var ih = icols.map(function (c) { return c[1]; }); ih.push('操作');
        inner.appendChild(U.el('thead', {}, [U.el('tr', {}, ih.map(function (h) { return U.el('th', { text: h }); }))]));
        var itb = U.el('tbody');
        grp.forEach(function (r) {
          var tds = icols.map(function (c) { return U.el('td', { text: r[c[0]] == null ? '' : String(r[c[0]]) }); });
          tds.push(opsCell(r, self));
          itb.appendChild(U.el('tr', {}, tds));
        });
        inner.appendChild(itb); makeSortable(inner);
        var detail = U.el('tr', { class: 'hidden' }, [U.el('td', { colspan: String(HEADS.length), style: 'background:#f6f8f7;padding:6px' }, [inner])]);
        head.addEventListener('click', function () { var hid = detail.classList.toggle('hidden'); nameTd.textContent = (hid ? '▸ ' : '▾ ') + a.name; });
        tb.appendChild(head); tb.appendChild(detail);
      });
      table.appendChild(tb); listBox.appendChild(table);
    }
    function draw() { listBox.innerHTML = ''; (mode === 'byowner' ? drawOwner : mode === 'byplugin' ? drawPlugin : drawHost)(); }
    draw();
  }

  // 既有部門/負責人清單(可搜尋下拉用)；開一次抓、之後快取
  var _deptList = null, _ownerList = null;
  async function ensureLists() {
    if (_deptList && _ownerList) return;
    try { _deptList = await jget('/api/departments'); } catch (e) { _deptList = _deptList || []; }
    try { _ownerList = await jget('/api/owners'); } catch (e) { _ownerList = _ownerList || []; }
  }
  function _datalist(id, items) {
    return U.el('datalist', { id: id }, (items || []).map(function (v) { return U.el('option', { value: v }); }));
  }

  // 申請佐證文件面板（展延／例外的 WBS、理由說明…）：列出／下載／上傳／刪除。掛此弱點、重匯不洗。
  var ATTACH_KINDS = ['展延申請書', '例外申請書', 'WBS', '佐證', '其他'];
  function fmtSize(n) { n = n || 0; if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB'; if (n >= 1024) return Math.round(n / 1024) + ' KB'; return n + ' B'; }
  function attachPanel(row) {
    var wrap = U.el('div', { style: 'margin:2px 0 12px' });
    var listBox = U.el('div'); wrap.appendChild(listBox);
    var writable = canWrite();
    async function draw() {
      listBox.innerHTML = '';
      var atts;
      try { atts = await jget('/api/findings/' + row.id + '/attachments'); }
      catch (e) { listBox.appendChild(U.el('p', { class: 'empty-hint', text: '讀取附件失敗（可能需登入）。' })); return; }
      if (!atts.length) { listBox.appendChild(U.el('p', { class: 'empty-hint', text: '尚無佐證文件。' })); return; }
      var t = U.el('table', { class: 'tracking-table', style: 'margin:0' });
      t.appendChild(U.el('thead', {}, [U.el('tr', {}, ['類型', '檔名', '大小', '上傳者', '時間', '操作'].map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      atts.forEach(function (a) {
        var dl = U.el('a', { href: '/api/attachments/' + a.id + '/download', text: '下載', style: 'color:#1a7f4b;font-weight:600' });
        var ops = U.el('div', { style: 'display:flex;gap:10px;justify-content:center;align-items:center' }, [dl]);
        if (writable) {
          var del = U.el('button', { class: 'btn btn-sm', text: '刪', title: '刪除此附件' });
          del.addEventListener('click', async function () {
            if (!window.confirm('刪除附件「' + a.orig_name + '」？')) return;
            var r = await fetch('/api/attachments/' + a.id, { method: 'DELETE' });
            if (!r.ok) { UI.toast('刪除失敗', 'error'); return; }
            UI.toast('已刪除', 'success'); draw();
          });
          ops.appendChild(del);
        }
        tb.appendChild(U.el('tr', {}, [
          U.el('td', { text: a.kind }), U.el('td', { text: a.orig_name, style: 'text-align:left' }),
          U.el('td', { text: fmtSize(a.size) }), U.el('td', { text: a.uploaded_by || '' }),
          U.el('td', { text: (a.uploaded_at || '').replace('T', ' ').slice(0, 16) }), ops,
        ]));
      });
      t.appendChild(tb); listBox.appendChild(t);
    }
    if (writable) {
      var kindSel = U.el('select', {}, ATTACH_KINDS.map(function (k) { return U.el('option', { value: k, text: k }); }));
      kindSel.value = '展延申請書';
      var fileInput = U.el('input', { type: 'file', multiple: 'multiple' });
      var up = U.el('button', { class: 'btn btn-secondary btn-sm', text: '上傳' });
      up.addEventListener('click', async function () {
        var files = fileInput.files; if (!files || !files.length) { UI.toast('請先選檔案', 'error'); return; }
        up.disabled = true; up.textContent = '上傳中…';
        for (var i = 0; i < files.length; i++) {
          var f = files[i];
          try {
            var r = await fetch('/api/findings/' + row.id + '/attachments?name=' + encodeURIComponent(f.name) + '&kind=' + encodeURIComponent(kindSel.value), { method: 'POST', body: f });
            if (!r.ok) { var e = await r.json().catch(function () { return {}; }); UI.toast(f.name + '：' + (e.detail || r.status), 'error'); }
          } catch (err) { UI.toast(f.name + ' 上傳失敗', 'error'); }
        }
        up.disabled = false; up.textContent = '上傳'; fileInput.value = ''; draw();
        if (row) row.att_count = (row.att_count || 0) + 0;   // 標記已變動（清單重繪時會更新）
      });
      wrap.appendChild(U.el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px' }, [kindSel, fileInput, up]));
    }
    draw();
    return wrap;
  }

  // 管理員編輯一筆弱點的可寫欄位：開視窗、存系統疊加層、重匯不洗掉、不動 Excel
  async function editOverlay(row, done) {
    await ensureLists();
    var title = (row.host || '') + ' / ' + (row.name || row.plugin_id || '');
    // 負責人：可搜尋下拉＋可新增(找不到就打新名字,有人離職/新進)
    var owner = U.el('input', { type: 'text', value: row.owner || '', list: 'webext-owners', autocomplete: 'off', placeholder: '可輸入搜尋；找不到可直接打新名字' });
    // 部門：可搜尋下拉，但只能選現有(存檔時驗證)，避免打錯多出部門
    var dept = U.el('input', { type: 'text', value: row.department || '', list: 'webext-depts', autocomplete: 'off', placeholder: '從清單選（避免打錯新增部門）' });
    var target = U.el('input', { type: 'date', value: row.target_date || '' });
    var progress = U.el('select', {}, [
      U.el('option', { value: '', text: '未標記' }),
      U.el('option', { value: '處理中', text: '處理中（修補中）' }),
      U.el('option', { value: '要申請展延', text: '要申請展延（第一次做不完）' }),
      U.el('option', { value: '要申請例外', text: '要申請例外（展延後還做不完）' }),
      U.el('option', { value: '等複掃', text: '等複掃（承辦回報做完、等資安複掃）' }),
    ]);
    progress.value = row.progress || '';
    var note = U.el('textarea', { rows: '4' });
    note.value = row.track_note || '';
    [owner, dept, target, progress, note].forEach(function (i) { i.style.cssText = 'display:block;width:100%;margin:4px 0 12px;padding:8px;border:1px solid #e3e6ea;border-radius:6px;font-size:14px;font-family:inherit'; });
    var body = U.el('div', {}, [
      U.el('p', { class: 'empty-hint', text: title }),
      U.el('label', { text: '負責人（留空＝取消覆蓋、回到 Excel 值）' }), owner,
      U.el('label', { text: '部門（負責人可能是別單位的人；留空＝回到 Excel 值）' }), dept,
      U.el('label', { text: '處理進度（系統內自己標，不碰 Excel；結論仍以資安 Excel 為主）' }), progress,
      U.el('label', { text: '預計完成日（承辦回報預計哪天做完；留空＝清除。供主管週報彙總）' }), target,
      U.el('label', { text: '追蹤備註（承辦回報：何時做什麼動作。只存系統，不會動到 Excel 原備註）' }), note,
      _datalist('webext-owners', _ownerList),
      _datalist('webext-depts', _deptList),
    ]);
    // 申請佐證文件（展延／例外的 WBS、理由說明…）
    var attHint = U.el('p', { class: 'empty-hint', style: 'color:#c0392b;margin:0 0 4px' });
    function updAttHint() { attHint.textContent = (progress.value === '要申請展延' || progress.value === '要申請例外') ? '⚠ 要申請展延／例外：請附上 WBS 或展延理由說明等佐證文件。' : ''; }
    progress.addEventListener('change', updAttHint); updAttHint();
    body.appendChild(U.el('label', { text: '申請佐證文件（展延／例外的 WBS、理由說明等；掛在此弱點、重匯不洗；下載需登入）' }));
    body.appendChild(attHint);
    body.appendChild(attachPanel(row));
    var save = U.el('button', { class: 'btn btn-primary', text: '存檔' });
    save.addEventListener('click', async function () {
      // 部門只能選現有(避免打錯多出部門)；留空＝清除回 Excel 值
      var dv = (dept.value || '').trim();
      if (dv && (_deptList || []).indexOf(dv) < 0) {
        UI.toast('部門「' + dv + '」不在清單中。請從既有部門選擇（避免打錯新增部門）；留空＝回到 Excel 值。', 'error');
        dept.focus(); return;
      }
      var r = await fetch('/api/findings/' + row.id + '/overlay', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ set_owner: true, owner: owner.value, set_department: true, department: dept.value, set_target: true, target_date: target.value, set_progress: true, progress: progress.value, set_note: true, note: note.value })
      });
      if (!r.ok) { var e = await r.json().catch(function () { return {}; }); UI.toast('存失敗：' + (e.detail || r.status), 'error'); return; }
      UI.closeModal(); UI.toast('已更新', 'success'); if (done) done();
    });
    UI.openModal('編輯弱點（承辦管理）', body, { footer: save });
    setTimeout(function () { owner.focus(); }, 0);
  }
  var editOwner = editOverlay;  // 相容舊呼叫名

  // 看這一筆的「原始資料」(原始 Excel 整列，26~28 欄；資安維護，唯讀)
  function showRawModal(row) {
    var raw = (row && row.raw) ? row.raw : {};
    var keys = Object.keys(raw);
    var box = U.el('div');
    if (!keys.length) {
      box.appendChild(U.el('p', { class: 'empty-hint', text: '這筆沒有原始資料（可能是舊批次匯入前的資料）。' }));
    } else {
      box.appendChild(U.el('p', { class: 'empty-hint', text: '原始 Excel 整列，共 ' + keys.length + ' 欄（資安維護，唯讀）。' }));
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, [U.el('th', { text: '欄位' }), U.el('th', { text: '值' })])]));
      var tb = U.el('tbody');
      keys.forEach(function (k) {
        tb.appendChild(U.el('tr', {}, [
          U.el('td', { text: k, style: 'white-space:nowrap;font-weight:600;color:#355' }),
          U.el('td', { text: raw[k] == null ? '' : String(raw[k]) }),
        ]));
      });
      table.appendChild(tb); box.appendChild(table);
    }
    UI.openModal('原始資料 — ' + (row.host || '') + ' / ' + (row.name || row.plugin_id || ''), box);
  }
  // 一列「操作」欄：固定有 🔍(看原始)，可寫入時再加 ✏️(編輯)
  function opsCell(row, onEditDone) {
    var ops = U.el('td');
    if (row && row.att_count) {   // 有申請佐證文件：顯示 📎N（點 ✏️ 進編輯看/下載）
      ops.appendChild(U.el('span', { text: '📎' + row.att_count, title: row.att_count + ' 份申請佐證文件（點 ✏️ 查看／下載）', style: 'margin-right:4px;font-size:13px;color:#1a7f4b;font-weight:700' }));
    }
    var rawBtn = U.el('button', { class: 'btn btn-sm', text: '🔍', title: '看原始資料', style: 'margin-right:4px' });
    rawBtn.addEventListener('click', function () { showRawModal(row); });
    ops.appendChild(rawBtn);
    if (canWrite()) {
      var eb = U.el('button', { class: 'btn btn-sm', text: '✏️', title: '編輯' });
      eb.addEventListener('click', function () { editOverlay(row, onEditDone); });
      ops.appendChild(eb);
    }
    return ops;
  }

  // ---- 登入 ----
  async function refreshMe() {
    try { me = await jget('/api/me'); } catch (e) { me = { authenticated: false }; }
    var span = document.getElementById('webext-user');
    var btn = document.getElementById('webext-login-btn');
    if (!span || !btn) return;
    if (me.open_write && !me.authenticated) {
      // 免登入模式：不需登入即可寫入，隱藏登入鈕避免困惑
      span.classList.add('hidden'); btn.classList.add('hidden');
      return;
    }
    btn.classList.remove('hidden');
    if (me.authenticated) {
      span.textContent = (me.display_name || me.username) + '（' + me.role + '）';
      span.classList.remove('hidden'); btn.textContent = '登出';
    } else {
      span.classList.add('hidden'); btn.textContent = '登入';
    }
  }
  function openLogin() {
    var user = U.el('input', { type: 'text', id: 'wx-user', placeholder: '帳號', autocomplete: 'username' });
    var pass = U.el('input', { type: 'password', id: 'wx-pass', placeholder: '密碼', autocomplete: 'current-password' });
    var err = U.el('p', { class: 'empty-hint', style: 'color:#c0392b;display:none' });
    [user, pass].forEach(function (i) { i.style.cssText = 'display:block;width:100%;margin:6px 0;padding:8px;border:1px solid #e3e6ea;border-radius:6px'; });
    var body = U.el('div', {}, [U.el('label', { text: '帳號' }), user, U.el('label', { text: '密碼' }), pass, err]);
    var submit = U.el('button', { class: 'btn btn-primary', text: '登入' });
    async function doLogin() {
      var r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user.value.trim(), password: pass.value }) });
      if (!r.ok) { var e = await r.json().catch(function () { return {}; }); err.textContent = e.detail || '登入失敗'; err.style.display = 'block'; return; }
      UI.closeModal(); await refreshMe(); refreshGov(); UI.toast('已登入', 'success');
    }
    submit.addEventListener('click', doLogin);
    pass.addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
    UI.openModal('登入', body, { footer: submit });
    setTimeout(function () { user.focus(); }, 0);
  }
  async function doLogout() { await fetch('/api/logout', { method: 'POST' }); await refreshMe(); refreshGov(); UI.toast('已登出', 'info'); }

  // ---- 承辦管線 ----
  async function transition(id, to, rerender) {
    var r = await fetch('/api/cases/' + id + '/transition', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to: to }) });
    if (!r.ok) { var e = await r.json().catch(function () { return {}; }); UI.toast('失敗：' + (e.detail || r.status), 'error'); return; }
    UI.toast('已更新狀態', 'success'); if (rerender) rerender();
  }
  // 清除「已消失」案件（來源已無的 orphan case）；需寫入權限，後端 require_write_role
  // 用於 D 查核 › 對帳健檢 的維護區（原「送審進度」頁已併入主管週報/查核）
  async function purgeOrphans(done) {
    if (!window.confirm('確定清除所有「已消失」案件？此動作會刪掉來源已不存在的舊案件紀錄（不影響現有弱點）。')) return;
    try {
      var r = await fetch('/api/cases/purge-orphans', { method: 'POST' });
      if (!r.ok) { var e = await r.json().catch(function () { return {}; }); UI.toast('清除失敗：' + (e.detail || r.status), 'error'); return; }
      var j = await r.json();
      UI.toast('已清除 ' + (j.deleted || 0) + ' 筆已消失案件', 'success');
      if (done) done();
    } catch (e) { UI.toast('清除失敗', 'error'); }
  }

  // 下鑽：開視窗顯示 /api/cases 某關卡的案件（可排序）
  async function openCasesModal(title, params) {
    var box = U.el('div');
    var cols = [['host', '主機'], ['plugin_id', 'Plugin'], ['owner', '負責人'], ['department', '部門'], ['status', '狀態'], ['source_closed', '來源']];
    var curRows = [];
    var exportBtn = U.el('button', { class: 'btn btn-secondary', text: '匯出此清單 (CSV)' });
    exportBtn.addEventListener('click', function () {
      if (!curRows.length) { UI.toast('沒有可匯出的資料', 'error'); return; }
      exportRowsCSV(cols.map(function (c) { return [c[0], c[1]]; }),
        curRows.map(function (r) { return Object.assign({}, r, { source_closed: r.source_closed ? '已結' : '未結', owner: r.owner || '未指派' }); }),
        title);
    });
    UI.openModal(title, box, { footer: exportBtn });
    var rows;
    try { rows = await jget('/api/cases?' + qd(params || {})); }
    catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
    if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無案件' })); return; }
    curRows = rows;
    box.appendChild(U.el('p', { class: 'empty-hint', text: '共 ' + rows.length + ' 筆（點欄位排序）' }));
    var table = U.el('table', { class: 'tracking-table' });
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, cols.map(function (c) { return U.el('th', { text: c[1] }); }))]));
    var tb = U.el('tbody');
    rows.forEach(function (r) {
      tb.appendChild(U.el('tr', {}, cols.map(function (c) {
        var v = c[0] === 'source_closed' ? (r[c[0]] ? '已結' : '未結') : (r[c[0]] == null ? (c[0] === 'owner' ? '未指派' : '') : String(r[c[0]]));
        return U.el('td', { text: v });
      })));
    });
    table.appendChild(tb); box.appendChild(table); makeSortable(table);
  }

  // ---- 到期倒數（依倒數天數分桶：已逾期／30天內／31–60／61–90／90天以上）----
  // 可切「含申請提前量」：行動期限＝到期日 − 提前量(14天)，因為申請本身要時間，到期才動就來不及。
  var DUE_LEAD_DAYS = 14;
  var _dueLeadOn = true;   // 預設含提前量(主管要的追查角度)
  function leadToggle(onChange) {
    var wrap = U.el('label', { style: 'display:inline-flex;align-items:center;gap:6px;font-size:14px;cursor:pointer;margin:0 0 8px' });
    var chk = U.el('input', { type: 'checkbox' }); chk.checked = _dueLeadOn;
    chk.addEventListener('change', function () { _dueLeadOn = chk.checked; onChange(); });
    wrap.appendChild(chk);
    wrap.appendChild(U.el('span', { text: '含申請提前量（' + DUE_LEAD_DAYS + ' 天緩衝）——用「行動期限＝到期−' + DUE_LEAD_DAYS + '天」倒數' }));
    return wrap;
  }
  async function renderDueSoonInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    var lead = _dueLeadOn ? DUE_LEAD_DAYS : 0;
    var s;
    try { s = await jget('/api/summary?' + qd(lead ? { lead: lead } : {})); }
    catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料，請先匯入。' })); return; }
    var d = s.due_buckets || {};
    var basis = _dueLeadOn ? '行動期限（到期日 − ' + DUE_LEAD_DAYS + ' 天申請緩衝）' : '實際到期日';
    host.appendChild(U.el('p', { class: 'empty-hint', text: '依「' + basis + '」倒數分桶（未結案）。「已逾期」在含提前量時＝已過行動期限（再不動手，申請跑完就來不及）。無到期日的不在此（見待辦清單）。' }));
    host.appendChild(leadToggle(function () { renderDueSoonInto(host); }));
    var pm = function (extra) { var p = Object.assign({}, extra); if (lead) p.lead = lead; return p; };
    // 長條圖：各到期桶數量一眼看出近期壓力（紅＝已逾期），點長條下鑽該桶明細。
    var bparam = { overdue: { due_max: '-1' }, d30: { due_min: '0', due_max: '30' },
      d31_60: { due_min: '31', due_max: '60' }, d61_90: { due_min: '61', due_max: '90' }, d90plus: { due_min: '91' } };
    var blabel = { overdue: _dueLeadOn ? '已過行動期限' : '已逾期', d30: '30 天內', d31_60: '31–60 天', d61_90: '61–90 天', d90plus: '90 天以上' };
    hbarChart(host, dueBucketItems(d, function (key) {
      openFindings(blabel[key] + '（到期倒數）', pm(bparam[key]));
    }));
    renderTabs(host, [
      { label: '已逾期（' + (d.overdue || 0) + '）', render: function (c) { renderActionListInto(c, _dueLeadOn ? '已過行動期限' : '已逾期', pm({ due_max: '-1' })); } },
      { label: '30天內（' + (d.d30 || 0) + '）', render: function (c) { renderActionListInto(c, '30 天內' + (_dueLeadOn ? '要動手' : '到期'), pm({ due_min: '0', due_max: '30' })); } },
      { label: '31–60天（' + (d.d31_60 || 0) + '）', render: function (c) { renderActionListInto(c, '31–60 天', pm({ due_min: '31', due_max: '60' })); } },
      { label: '61–90天（' + (d.d61_90 || 0) + '）', render: function (c) { renderActionListInto(c, '61–90 天', pm({ due_min: '61', due_max: '90' })); } },
      { label: '90天以上（' + (d.d90plus || 0) + '）', render: function (c) { renderActionListInto(c, '90 天以上（較安全）', pm({ due_min: '91' })); } },
    ], 'duesoon');
  }

  // 視覺長條排行：橫條長度＝數量、紅段＝逾期，一眼看出誰多少(直白)。rows 需含 owner/total/overdue。
  function ensureBarStyle() {
    if (document.getElementById('webext-bar-style')) return;
    var st = U.el('style', { id: 'webext-bar-style' });
    st.textContent =
      '.obar-row{display:flex;align-items:center;gap:10px;margin:5px 0}'
      + '.obar-name{width:120px;flex:0 0 auto;text-align:right;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
      + '.obar-name.clk{color:#1a7f4b;cursor:pointer}'
      + '.obar-track{position:relative;flex:1;height:24px;background:#eef3f0;border-radius:5px;overflow:hidden;min-width:60px}'
      + '.obar-fill{position:absolute;left:0;top:0;height:100%;background:#1a7f4b}'
      + '.obar-od{position:absolute;left:0;top:0;height:100%;background:#c0392b}'
      + '.obar-num{flex:0 0 auto;min-width:130px;font-weight:700}'
      + '.obar-num .od{color:#c0392b}';
    document.head.appendChild(st);
  }
  function ownerBars(host, rows, onClick) {
    ensureBarStyle();
    if (!rows || !rows.length) { host.appendChild(U.el('p', { class: 'empty-hint', text: '無未結案。' })); return; }
    var max = Math.max.apply(null, rows.map(function (r) { return r.total || 0; })) || 1;
    rows.forEach(function (r) {
      var row = U.el('div', { class: 'obar-row' });
      var named = r.owner && r.owner !== '— 未指派';
      var name = U.el('div', { class: 'obar-name' + (named && onClick ? ' clk' : ''), text: r.owner, title: r.owner });
      var track = U.el('div', { class: 'obar-track' }, [
        U.el('div', { class: 'obar-fill', style: 'width:' + Math.max(Math.round((r.total || 0) / max * 100), 2) + '%' }),
        U.el('div', { class: 'obar-od', style: 'width:' + Math.round((r.overdue || 0) / max * 100) + '%' }),
      ]);
      var num = U.el('div', { class: 'obar-num' });
      num.appendChild(U.el('span', { text: String(r.total || 0) + ' 支' }));
      if (r.overdue) num.appendChild(U.el('span', { class: 'od', text: '（逾期 ' + r.overdue + '）' }));
      row.appendChild(name); row.appendChild(track); row.appendChild(num);
      if (named && onClick) { name.addEventListener('click', function () { onClick(r); }); track.style.cursor = 'pointer'; track.addEventListener('click', function () { onClick(r); }); }
      host.appendChild(row);
    });
  }

  // ===== 圖表元件（純 CSS/conic-gradient，穩健、可列印、重繪不漏）=====
  // 顏色表：嚴重度／到期倒數桶／處置階段（與系統既有色系一致）
  var SEV_COLOR = { Critical: '#b71c1c', High: '#e64a19', Medium: '#f9a825', Low: '#43a047', Unknown: '#90a4ae' };
  var DUE_COLOR = { overdue: '#c0392b', d30: '#e64a19', d31_60: '#f9a825', d61_90: '#7cb342', d90plus: '#90a4ae' };
  function ensureChartStyle() {
    if (document.getElementById('webext-chart-style')) return;
    var st = U.el('style', { id: 'webext-chart-style' });
    st.textContent =
      '.wx-chart{display:flex;align-items:center;gap:22px;flex-wrap:wrap;margin:8px 0 18px}'
      + '.wx-donut{width:150px;height:150px;border-radius:50%;flex:0 0 auto;position:relative}'
      + '.wx-donut::after{content:"";position:absolute;inset:30%;background:#fff;border-radius:50%}'
      + '.wx-donut .wx-mid{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:1;pointer-events:none}'
      + '.wx-donut .wx-mid b{font-size:26px;line-height:1;color:#1a1a1a}'
      + '.wx-donut .wx-mid span{font-size:12px;color:#777}'
      + '.wx-legend{display:flex;flex-direction:column;gap:5px;font-size:14px}'
      + '.wx-leg{display:flex;align-items:center;gap:8px;cursor:default}'
      + '.wx-leg.clk{cursor:pointer}.wx-leg.clk:hover{text-decoration:underline}'
      + '.wx-sw{width:14px;height:14px;border-radius:3px;flex:0 0 auto;display:inline-block}'
      + '.wx-leg .n{color:#777;margin-left:2px}'
      // 緊湊 KPI 卡片格（用格線：各組同寬、欄位對齊）
      + '.wx-kpi-grp{margin:12px 0 5px;font-size:13px;font-weight:700;color:#555;border-left:3px solid #1a7f4b;padding-left:7px}'
      + '.wx-kpi-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}'
      + '.wx-kpi{background:#f6f8f7;border:1px solid #e3e6ea;border-radius:8px;padding:7px 8px;text-align:center}'
      + '.wx-kpi .v{font-size:21px;font-weight:700;line-height:1.1;color:#1a1a1a}'
      + '.wx-kpi .v.danger{color:#c0392b}'
      + '.wx-kpi .l{font-size:11px;color:#667;margin-top:3px;line-height:1.25}'
      + '.wx-kpi.clk{cursor:pointer}.wx-kpi.clk .v{color:#1a7f4b}.wx-kpi.clk:hover{border-color:#1a7f4b;background:#eef5f1}'
      + '.wx-kpi.clk.danger .v{color:#c0392b}'
      // 白話總結句
      + '.wx-summary{background:#f0f6f3;border:1px solid #cfe3d8;border-radius:10px;padding:12px 16px;font-size:16px;line-height:1.9;margin:4px 0 12px}'
      + '.wx-summary .wx-hot{font-size:1.2em;color:#1a7f4b;padding:0 1px}'
      + '.wx-summary .wx-hot.danger{color:#c0392b}'
      + '.wx-summary .wx-hot.clk{cursor:pointer;text-decoration:underline dotted}'
      // 堆疊比例條（把母體關係用看的講清楚）
      + '.wx-sbar-wrap{margin:8px 0 14px}'
      + '.wx-sbar-head{display:flex;justify-content:space-between;align-items:baseline;font-size:13px;color:#555;font-weight:700;margin-bottom:4px}'
      + '.wx-sbar{display:flex;height:30px;border-radius:6px;overflow:hidden;background:#eef3f0}'
      + '.wx-sseg{display:flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:700;white-space:nowrap;min-width:2px;overflow:hidden}'
      + '.wx-sseg.clk{cursor:pointer}.wx-sseg.clk:hover{filter:brightness(1.08)}'
      + '.wx-sbar-legend{display:flex;gap:16px;margin-top:5px;font-size:13px;flex-wrap:wrap;color:#444}'
      + '@media print{.wx-donut,.wx-sseg{-webkit-print-color-adjust:exact;print-color-adjust:exact}}';
    document.head.appendChild(st);
  }
  // 甜甜圈：items=[{label,value,color,onClick?}]；中心顯示總數。總數 0 時顯示灰圈。
  function donutChart(host, items, centerLabel) {
    ensureChartStyle();
    var total = items.reduce(function (s, i) { return s + (i.value || 0); }, 0);
    var wrap = U.el('div', { class: 'wx-chart' });
    var stops = [], acc = 0;
    items.forEach(function (it) {
      if (!it.value) return;
      var frac = total ? it.value / total * 100 : 0;
      stops.push(it.color + ' ' + acc.toFixed(3) + '% ' + (acc + frac).toFixed(3) + '%');
      acc += frac;
    });
    var bg = stops.length ? 'conic-gradient(' + stops.join(',') + ')' : '#eef3f0';
    var donut = U.el('div', { class: 'wx-donut', style: 'background:' + bg });
    donut.appendChild(U.el('div', { class: 'wx-mid' }, [
      U.el('b', { text: String(total) }), U.el('span', { text: centerLabel || '總數' }),
    ]));
    wrap.appendChild(donut);
    var legend = U.el('div', { class: 'wx-legend' });
    items.forEach(function (it) {
      var pct = total ? Math.round((it.value || 0) / total * 100) : 0;
      var row = U.el('div', { class: 'wx-leg' + (it.onClick && it.value ? ' clk' : '') }, [
        U.el('span', { class: 'wx-sw', style: 'background:' + it.color }),
        U.el('span', { text: it.label }),
        U.el('span', { class: 'n', text: '　' + (it.value || 0) + '（' + pct + '%）' }),
      ]);
      if (it.onClick && it.value) row.addEventListener('click', it.onClick);
      legend.appendChild(row);
    });
    wrap.appendChild(legend);
    host.appendChild(wrap);
  }
  // 彩色水平長條：items=[{label,value,color,onClick?}]；長度＝數量占最大值比例。
  function hbarChart(host, items) {
    ensureBarStyle();
    var max = Math.max.apply(null, items.map(function (i) { return i.value || 0; })) || 1;
    var box = U.el('div', { style: 'margin:6px 0 16px' });
    items.forEach(function (it) {
      var row = U.el('div', { class: 'obar-row' });
      var name = U.el('div', { class: 'obar-name' + (it.onClick && it.value ? ' clk' : ''), text: it.label, title: it.label });
      var fill = U.el('div', { class: 'obar-fill', style: 'width:' + Math.max(Math.round((it.value || 0) / max * 100), it.value ? 2 : 0) + '%' + (it.color ? ';background:' + it.color : '') });
      var track = U.el('div', { class: 'obar-track' }, [fill]);
      var num = U.el('div', { class: 'obar-num', text: String(it.value || 0) + ' 支' });
      if (it.onClick && it.value) {
        name.addEventListener('click', it.onClick);
        track.style.cursor = 'pointer'; track.addEventListener('click', it.onClick);
      }
      row.appendChild(name); row.appendChild(track); row.appendChild(num);
      box.appendChild(row);
    });
    host.appendChild(box);
  }
  // 由 due_buckets 物件組出到期倒數的長條 items（含下鑽），basisLead 決定下鑽是否帶提前量。
  function dueBucketItems(d, onBucket) {
    d = d || {};
    return [
      { label: '已逾期', value: d.overdue || 0, color: DUE_COLOR.overdue, key: 'overdue' },
      { label: '30 天內', value: d.d30 || 0, color: DUE_COLOR.d30, key: 'd30' },
      { label: '31–60 天', value: d.d31_60 || 0, color: DUE_COLOR.d31_60, key: 'd31_60' },
      { label: '61–90 天', value: d.d61_90 || 0, color: DUE_COLOR.d61_90, key: 'd61_90' },
      { label: '90 天以上', value: d.d90plus || 0, color: DUE_COLOR.d90plus, key: 'd90plus' },
    ].map(function (it) { if (onBucket) it.onClick = function () { onBucket(it.key, it.label); }; return it; });
  }
  // 內嵌 SVG 折線圖：labels=X 軸字串；lines=[{name,color,values[]}]。可列印、無外部庫。
  // 1 點時只畫點；2 點以上連線。數字直接標在點上(主管一眼看走勢)。
  function svgLineChart(host, labels, lines, title) {
    ensureChartStyle();
    if (title) host.appendChild(U.el('div', { class: 'panel-head' }, [U.el('h3', { text: title })]));
    var n = labels.length;
    var W = 640, H = 190, padL = 44, padR = 14, padT = 16, padB = 34;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var maxV = 1; lines.forEach(function (ln) { ln.values.forEach(function (v) { if (v > maxV) maxV = v; }); });
    function xFor(i) { return n <= 1 ? padL + plotW / 2 : padL + i * plotW / (n - 1); }
    function yFor(v) { return padT + (1 - v / maxV) * plotH; }
    var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); };
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:680px;height:auto;font-family:inherit" xmlns="http://www.w3.org/2000/svg">';
    svg += '<line x1="' + padL + '" y1="' + yFor(0) + '" x2="' + (W - padR) + '" y2="' + yFor(0) + '" stroke="#e3e6ea"/>';
    svg += '<line x1="' + padL + '" y1="' + yFor(maxV) + '" x2="' + (W - padR) + '" y2="' + yFor(maxV) + '" stroke="#eef3f0"/>';
    svg += '<text x="' + (padL - 6) + '" y="' + (yFor(maxV) + 4) + '" text-anchor="end" font-size="11" fill="#999">' + maxV + '</text>';
    svg += '<text x="' + (padL - 6) + '" y="' + (yFor(0) + 4) + '" text-anchor="end" font-size="11" fill="#999">0</text>';
    labels.forEach(function (lb, i) { svg += '<text x="' + xFor(i) + '" y="' + (H - 12) + '" text-anchor="middle" font-size="11" fill="#777">' + esc(lb) + '</text>'; });
    lines.forEach(function (ln) {
      if (n >= 2) { var pts = ln.values.map(function (v, i) { return xFor(i) + ',' + yFor(v); }).join(' '); svg += '<polyline fill="none" stroke="' + ln.color + '" stroke-width="2.5" points="' + pts + '"/>'; }
      ln.values.forEach(function (v, i) {
        svg += '<circle cx="' + xFor(i) + '" cy="' + yFor(v) + '" r="3.5" fill="' + ln.color + '"/>';
        svg += '<text x="' + xFor(i) + '" y="' + (yFor(v) - 7) + '" text-anchor="middle" font-size="11" font-weight="700" fill="' + ln.color + '">' + v + '</text>';
      });
    });
    svg += '</svg>';
    var box = U.el('div', { style: 'margin:6px 0 8px' }); box.innerHTML = svg; host.appendChild(box);
    var lg = U.el('div', { class: 'wx-legend', style: 'flex-direction:row;gap:16px;margin-bottom:12px' });
    lines.forEach(function (ln) { lg.appendChild(U.el('div', { class: 'wx-leg' }, [U.el('span', { class: 'wx-sw', style: 'background:' + ln.color }), U.el('span', { text: ln.name })])); });
    host.appendChild(lg);
  }

  // 堆疊比例條：把「母體＝子項相加」用一條 bar 的分段畫出來，避免被誤加到別的母體。
  // segments=[{label,value,color,onClick?}]；寬度∝value；段夠寬就寫「標籤 值」，窄就只寫值。
  function stackedBar(host, title, total, segments) {
    ensureChartStyle();
    var wrap = U.el('div', { class: 'wx-sbar-wrap' });
    wrap.appendChild(U.el('div', { class: 'wx-sbar-head' }, [
      U.el('span', { text: title }), U.el('b', { text: String(total) }),
    ]));
    var sum = segments.reduce(function (s, x) { return s + (x.value || 0); }, 0) || 1;
    var bar = U.el('div', { class: 'wx-sbar' });
    segments.forEach(function (sg) {
      var w = (sg.value || 0) / sum * 100;
      var seg = U.el('div', { class: 'wx-sseg' + (sg.onClick && sg.value ? ' clk' : ''),
        style: 'width:' + w + '%;background:' + sg.color, title: sg.label + '：' + (sg.value || 0) });
      if (sg.value) seg.appendChild(U.el('span', { text: w >= 14 ? (sg.label.split('（')[0] + ' ' + sg.value) : String(sg.value) }));
      if (sg.onClick && sg.value) seg.addEventListener('click', sg.onClick);
      bar.appendChild(seg);
    });
    wrap.appendChild(bar);
    var lg = U.el('div', { class: 'wx-sbar-legend' });
    segments.forEach(function (sg) {
      lg.appendChild(U.el('span', { class: 'wx-leg' + (sg.onClick && sg.value ? ' clk' : '') }, [
        U.el('span', { class: 'wx-sw', style: 'background:' + sg.color }),
        U.el('span', { text: sg.label + ' ' + (sg.value || 0) }),
      ]));
    });
    wrap.appendChild(lg);
    host.appendChild(wrap);
  }

  // 由 severity 物件組出嚴重度圓餅 items（含下鑽）
  function severityItems(sev, onSev) {
    sev = sev || {};
    return ['Critical', 'High', 'Medium', 'Low'].map(function (k) {
      var it = { label: k, value: sev[k] || 0, color: SEV_COLOR[k] };
      if (onSev) it.onClick = function () { onSev(k); };
      return it;
    }).filter(function (it) { return it.value > 0; });
  }

  // 到期範圍膠囊頁籤：統一用「到期倒數」那款綠色 pill 風格(取代下拉)。onPick(value)；value ''＝全部。
  var RANGE_OPTS = [['14', '14 天內'], ['30', '30 天內'], ['60', '60 天內'], ['90', '90 天內'], ['', '全部']];
  function rangePills(current, onPick) {
    var nav = U.el('nav', { class: 'subtabs', style: 'margin:4px 0 12px' });
    var btns = [];
    RANGE_OPTS.forEach(function (o) {
      var b = U.el('button', { class: 'subtab-btn' + (o[0] === current ? ' active' : ''), text: o[1] });
      b.addEventListener('click', function () {
        btns.forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        onPick(o[0]);
      });
      btns.push(b); nav.appendChild(b);
    });
    return nav;
  }

  // ---- 負責人追蹤（主管角度：誰還有幾隻＋狀態分佈；可選到期範圍）----
  async function renderOwnerInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    host.appendChild(U.el('p', { class: 'empty-hint', text: '每位負責人「近期到期」還有幾隻（明年才到期的＝還沒到期，預設不看）。切到期範圍看各負責人怎麼變化；各範圍含已逾期。處置階段相加＝該範圍未結；等複掃/逾期為疊加。點負責人看他的全部。' }));
    // 到期範圍：統一用膠囊頁籤(同「到期倒數」)，預設聚焦近期 90 天
    var curRange = '90';
    host.appendChild(U.el('div', {}, [U.el('label', { text: '到期範圍　', style: 'font-size:14px' }),
      rangePills(curRange, function (v) { curRange = v; draw(); })]));
    host.appendChild(leadToggle(function () { draw(); }));   // 含申請提前量開關(與到期倒數共用狀態)
    var box = U.el('div'); host.appendChild(box);
    var cols = [['owner', '負責人'], ['department', '部門'], ['total', '未結'], ['original', '原始'],
      ['extension', '首次展延'], ['exception', '例外管理'], ['rescan', '等複掃'], ['overdue', '逾期']];
    async function draw() {
      box.innerHTML = '';
      var lead = _dueLeadOn ? DUE_LEAD_DAYS : 0;
      var p = {}; if (curRange) p.due_max = curRange; if (lead) p.lead = lead;
      var rows;
      try { rows = await jget('/api/owner-summary?' + qd(p)); }
      catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
      var label = curRange ? ('（' + curRange + ' 天內到期，' + rows.length + ' 人）') : ('（' + rows.length + ' 人）');
      box.appendChild(headWithExport('負責人追蹤' + label, cols, function () { return rows; }));
      if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: curRange ? '此範圍內無未結案。' : '無未結案。' })); return; }
      // 視覺排行：橫條長度＝未結數、紅段＝逾期，一眼看出誰多少(點進去看他全部)
      var barsBox = U.el('div', { style: 'margin:4px 0 16px' }); box.appendChild(barsBox);
      ownerBars(barsBox, rows, function (r) {
        var d = { owner: r.owner }; if (curRange) d.due_max = curRange; if (lead) d.lead = lead;
        openFindings(r.owner + ' · 全部未結', d);
      });
      box.appendChild(U.el('div', { class: 'panel-head' }, [U.el('h3', { text: '明細（點數字下鑽各狀態）' })]));
      // 每個數字欄對應的下鑽條件(帶 owner＋目前到期範圍/提前量)
      var DRILL = {
        total: {}, original: { stage: '原始修補期限' }, extension: { stage: '首次展延中' },
        exception: { stage: '例外管理中' }, rescan: { progress: '等複掃' }, overdue: { band: '已逾期' },
      };
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, cols.map(function (c) { return U.el('th', { text: c[1] }); }))]));
      var tb = U.el('tbody');
      rows.forEach(function (r) {
        var tds = cols.map(function (c) {
          var key = c[0];
          var td = U.el('td', { text: r[key] == null ? '' : String(r[key]) });
          var named = r.owner && r.owner !== '— 未指派';
          // owner 欄、以及所有數字欄(有值)都可下鑽
          if ((key === 'owner' || DRILL[key]) && named && (key === 'owner' || r[key])) {
            td.style.cursor = 'pointer'; td.style.fontWeight = '600';
            td.style.color = (key === 'overdue' && r.overdue) ? '#c0392b' : '#1a7f4b';
            td.title = '點我看明細';
            var base = Object.assign({ owner: r.owner }, DRILL[key] || {});
            if (curRange) base.due_max = curRange;
            if (lead) base.lead = lead;
            var ttl = r.owner + ' · ' + (key === 'owner' || key === 'total' ? '全部未結' : c[1]);
            td.addEventListener('click', (function (p, t) { return function () { openFindings(t, p); }; })(base, ttl));
          } else if (key === 'overdue' && r.overdue) {
            td.style.color = '#c0392b';
          }
          return td;
        });
        tb.appendChild(U.el('tr', {}, tds));
      });
      table.appendChild(tb); box.appendChild(table); makeSortable(table);
    }
    draw();
  }

  // 待辦清單的單一清單：搜尋 + 可改(負責人/預計完成日/備註) + 完整/簡易匯出
  var _TODO_COLS = [['host', '主機'], ['owner', '負責人'], ['severity', '嚴重度'], ['name', '弱點'],
    ['plugin_id', 'Plugin'], ['effective_due', '到期日'], ['overdue_days', '逾期天數'],
    ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
    ['department', '部門'], ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
  async function renderActionListInto(container, title, params) {
    container.innerHTML = '';
    var rows;
    try { rows = await jget('/api/findings?' + qd(Object.assign({ status: '未結案' }, params || {}))); }
    catch (e) { container.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
    var shown = rows.slice();
    container.appendChild(headWithListExport(title + '（' + rows.length + '）', function () { return shown; }));
    var search = U.el('input', { type: 'search', placeholder: '搜尋：負責人／主機 IP／弱點／逾期天數…（可空格分隔多字）' });
    search.style.cssText = 'width:100%;max-width:520px;margin:2px 0 8px;padding:8px 10px;border:1px solid #cdd5dd;border-radius:8px;font-size:14px';
    container.appendChild(search);
    var box = U.el('div'); container.appendChild(box);
    function matchRow(r, terms) {
      var hay = [r.owner, r.host, r.name, r.plugin_id, r.severity, r.effective_due,
        r.target_date, (r.overdue_days != null ? r.overdue_days : ''), r.track_note, r.department].join(' ').toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) >= 0; });
    }
    var refresh = function () { renderActionListInto(container, title, params); };
    function draw() {
      box.innerHTML = '';
      var terms = (search.value || '').toLowerCase().split(/\s+/).filter(function (t) { return t; });
      shown = terms.length ? rows.filter(function (r) { return matchRow(r, terms); }) : rows;
      if (!shown.length) {
        box.appendChild(U.el('p', { class: 'empty-hint', text: terms.length ? '找不到符合「' + search.value.trim() + '」的項目。' : '目前無項目 👍' }));
        return;
      }
      // 以負責人分組(主角度)：先列每位負責人幾隻，點名字展開明細
      var groups = {};
      shown.forEach(function (r) { var k = ((r.owner || '').trim()) || '— 未指派'; (groups[k] = groups[k] || []).push(r); });
      var keys = Object.keys(groups).sort(function (a, b) { return groups[b].length - groups[a].length; });
      box.appendChild(U.el('p', { class: 'empty-hint', text: '共 ' + keys.length + ' 位負責人、' + shown.length + ' 筆；點負責人展開明細。' }));
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, ['負責人', '筆數', '其中逾期'].map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      keys.forEach(function (k) {
        var grp = groups[k];
        var od = grp.filter(function (r) { return r.overdue_days != null && r.overdue_days > 0; }).length;
        var nameTd = U.el('td', { text: '▸ ' + k, style: 'font-weight:600;color:#1a7f4b;cursor:pointer' });
        var head = U.el('tr', { style: 'cursor:pointer' }, [nameTd,
          U.el('td', { text: String(grp.length) }),
          U.el('td', { text: od ? String(od) : '—', style: od ? 'color:#c0392b;font-weight:600' : '' })]);
        // 展開的明細(該負責人的清單，含 🔍/✏️)
        var inner = U.el('table', { class: 'tracking-table', style: 'margin:0' });
        var ih = _TODO_COLS.map(function (c) { return c[1]; }); ih.push('操作');
        inner.appendChild(U.el('thead', {}, [U.el('tr', {}, ih.map(function (h) { return U.el('th', { text: h }); }))]));
        var itb = U.el('tbody');
        grp.forEach(function (r) {
          var tds = _TODO_COLS.map(function (c) { return U.el('td', { text: r[c[0]] == null ? '' : String(r[c[0]]) }); });
          tds.push(opsCell(r, refresh));
          itb.appendChild(U.el('tr', {}, tds));
        });
        inner.appendChild(itb);
        var detail = U.el('tr', { class: 'hidden' }, [U.el('td', { colspan: '3', style: 'background:#f6f8f7;padding:6px' }, [inner])]);
        head.addEventListener('click', function () {
          var hid = detail.classList.toggle('hidden');
          nameTd.textContent = (hid ? '▸ ' : '▾ ') + k;
        });
        tb.appendChild(head); tb.appendChild(detail);
      });
      table.appendChild(tb); box.appendChild(table);
    }
    search.addEventListener('input', draw);
    draw();
  }

  // ---- 結案統計 ----
  async function renderCloseInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    var s;
    try { s = await jget('/api/close-stats?' + qd()); } catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料。' })); return; }
    function overviewTab(c0) {
      c0.appendChild(group('結案統計（快照比對）', [
        card(s.new_closed, '本期新結案'),
        card(s.source_confirmed, '來源(Excel)確認'),
        card(s.claimed_unconfirmed, '承辦聲稱未確認(可疑)', true),
      ]));
      c0.appendChild(U.el('p', { class: 'empty-hint', text: '「本期新結案」＝上期未結、這期變已結（快照比對）；「承辦聲稱未確認」＝承辦標完成但來源仍未結，可能自行浮報。' }));
    }
    function byCloserTab(c0) {
      if (!(s.by_closer && s.by_closer.length)) { c0.appendChild(U.el('p', { class: 'empty-hint', text: '本期無新結案。' })); return; }
      c0.appendChild(headWithExport('本期結案（依結案人）',
        [['name', '結案人'], ['closed', '本期結案']], function () { return s.by_closer; }));
      // 長條圖：誰本期結最多，一眼看出(由多到少)
      var sorted = s.by_closer.slice().sort(function (a, b) { return (b.closed || 0) - (a.closed || 0); });
      hbarChart(c0, sorted.map(function (r) { return { label: r.name || '（未填）', value: r.closed || 0, color: '#1a7f4b' }; }));
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, [U.el('th', { text: '結案人' }), U.el('th', { text: '本期結案' })])]));
      var tb = U.el('tbody');
      s.by_closer.forEach(function (r) { tb.appendChild(U.el('tr', {}, [U.el('td', { text: r.name }), U.el('td', { text: String(r.closed) })])); });
      table.appendChild(tb); c0.appendChild(table); makeSortable(table);
    }
    renderTabs(host, [
      { label: '結案總覽', render: overviewTab },
      { label: '依結案人（' + ((s.by_closer && s.by_closer.length) || 0) + '）', render: byCloserTab },
    ], 'close');
  }

  // ---- 對帳健檢（讓操作者不靠 AI 也能確認數字正確）----
  async function renderReconcileInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    var s;
    try { s = await jget('/api/reconcile?' + qd()); }
    catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料，請先匯入。' })); return; }
    var sum = null; try { sum = await jget('/api/summary?' + qd()); } catch (e) { }
    // 大橫幅：全綠或有問題
    var banner = U.el('div', { style: 'padding:12px 16px;border-radius:8px;font-size:16px;font-weight:700;margin:4px 0 12px;'
      + (s.all_ok ? 'background:#e8f5e9;color:#1a7f4b;border:1px solid #1a7f4b' : 'background:#fdecea;color:#c0392b;border:1px solid #c0392b') },
      [U.el('span', { text: s.all_ok ? '✅ 全部對帳一致——畫面數字彼此兜得起來，可放心。' : '⚠️ 發現不一致，請看下方紅色項目。' })]);
    host.appendChild(banner);
    // 來源
    host.appendChild(U.el('p', { class: 'empty-hint', text:
      '範圍：' + s.scope + '　·　來源檔：' + (s.source_file || '（未知）') + '　·　匯入時間：' + (s.imported_at || '').replace('T', ' ')
      + '　·　匯入列數：' + s.import_rows + '　·　目前資料列數：' + s.latest_rows }));
    // 對帳表
    var table = U.el('table', { class: 'tracking-table' });
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, ['對帳項目', '左邊', '＝?', '右邊（算式＝值）', '結果'].map(function (h) { return U.el('th', { text: h }); }))]));
    var tb = U.el('tbody');
    s.checks.forEach(function (c) {
      var res = U.el('td', { text: c.ok ? '✓ 一致' : '✗ 不一致' });
      res.style.cssText = 'font-weight:700;color:' + (c.ok ? '#1a7f4b' : '#c0392b');
      tb.appendChild(U.el('tr', {}, [
        U.el('td', { text: c.name }),
        U.el('td', { text: c.a_label + '＝' + c.a }),
        U.el('td', { text: '＝' }),
        U.el('td', { text: c.b_label + '＝' + c.b }),
        res,
      ]));
    });
    table.appendChild(tb); host.appendChild(table);
    host.appendChild(U.el('p', { class: 'empty-hint', text:
      '怎麼核到原始 Excel：① 每個統計數字都能點進去看清單、每列按 🔍 看原始整列；② 用各清單的「完整匯出 (CSV)」拉出來，跟資安那份 Excel 逐列比對。「匯入列數」應等於來源 Excel 的資料列數。' }));

    // 資料缺口（原在「待辦清單」，移來這裡集中「查」）：無人負責／無到期日，點數字看清單
    if (sum) {
      var g = sum.gaps || {};
      kpiCards(host, '資料缺口（應補齊；點數字看清單）', [
        { label: '無人負責', value: g.no_owner || 0, danger: (g.no_owner || 0) > 0, drill: function () { openFindings('無人負責', { no_owner: 'true' }); } },
        { label: '無到期日', value: g.no_due || 0, danger: (g.no_due || 0) > 0, drill: function () { openFindings('無到期日', { no_due: 'true' }); } },
      ]);
      if (!(g.no_owner || g.no_due)) host.appendChild(U.el('p', { class: 'empty-hint', text: '目前沒有缺口：每筆都有負責人與到期日。' }));
    }

    // 維護（需寫入權限）：清除來源已消失的追蹤紀錄（原在「送審進度」頁，移來這裡）
    if (canWrite()) {
      var m = U.el('div', { class: 'scope-info', style: 'margin-top:14px' }, [U.el('span', { text: '維護：來源已消失的追蹤紀錄可在此清除（不影響現有弱點）。' })]);
      var pb = U.el('button', { class: 'btn btn-sm', text: '清除已消失追蹤紀錄', style: 'margin-left:8px' });
      pb.addEventListener('click', function () { purgeOrphans(function () { renderReconcileInto(host); }); });
      m.firstChild.appendChild(document.createTextNode(' ')); m.firstChild.appendChild(pb);
      host.appendChild(m);
    }
  }

  // ---- D 查核（「查」：結案稽核＋對帳健檢兩分頁合一）----
  async function renderAuditInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    renderTabs(host, [
      { label: '結案稽核', render: renderCloseInto },   // 結案驗證／浮報
      { label: '對帳健檢', render: renderReconcileInto }, // 數字自我對帳＋資料缺口＋維護
    ], 'audit');
  }

  // ---- 主管週報（應申請未申請／已申請／預計完成彙總／落後；可列印存 PDF） ----
  var _lastReport = null;
  async function renderReportInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    var s;
    try { s = await jget('/api/report?' + qd()); }
    catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料，請先匯入。' })); return; }
    _lastReport = s;
    var scope = (s.department && s.department !== '全部') ? s.department : '全部門';

    // 標題列＋列印鈕
    var headRow = U.el('div', { class: 'panel-head', style: 'display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap' }, [
      U.el('h3', { text: '主管週報 — ' + scope + '（' + s.today + '）' }),
    ]);
    var btnWrap = U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' });
    var printBtn = U.el('button', { class: 'btn btn-primary btn-sm', text: '列印 / 存 PDF' });
    printBtn.addEventListener('click', function () { printReport(s); });
    btnWrap.appendChild(printBtn);
    // 一鍵發送：收進「報」這頁(原獨立項已併入)；沿用原 Email 設定流程
    var sendBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '一鍵發送' });
    sendBtn.addEventListener('click', function () { var b = document.getElementById('email-settings-btn'); if (b) b.click(); else UI.toast('找不到 Email 設定', 'error'); });
    btnWrap.appendChild(sendBtn);
    headRow.appendChild(btnWrap);
    host.appendChild(headRow);

    var fresh = (s.freshness.days_ago == null) ? '尚無匯入' : ('資料距今 ' + s.freshness.days_ago + ' 天');
    host.appendChild(U.el('p', { class: 'empty-hint', text: '產生時間 ' + (s.generated_at || '').replace('T', ' ') + '　·　' + fresh + '（彙總自系統內「預計完成日／追蹤備註」與備註申請紀錄）' }));

    // 選了特定部門 → 清單裡每列部門都一樣,「部門」欄多餘,隱藏(全部門時才顯示,用來分辨)
    var showDept = !(s.department && s.department !== '全部');

    function matchRow(r, terms) {
      var hay = [r.owner, r.host, r.name, r.plugin_id, r.severity, r.effective_due,
        r.target_date, (r.overdue_days != null ? r.overdue_days : ''), r.track_note,
        r.department].join(' ').toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) >= 0; });
    }
    // 一個清單頁籤：自帶搜尋框(找人/IP/弱點/逾期天數,空白分隔 AND)，即時過濾該清單
    function listTab(container, title, rows) {
      var search = U.el('input', { type: 'search', placeholder: '搜尋：負責人／主機 IP／弱點／逾期天數…（可空格分隔多字）' });
      search.style.cssText = 'width:100%;max-width:520px;margin:2px 0 8px;padding:8px 10px;border:1px solid #cdd5dd;border-radius:8px;font-size:14px';
      container.appendChild(search);
      var box = U.el('div'); container.appendChild(box);
      function draw() {
        box.innerHTML = '';
        var terms = (search.value || '').toLowerCase().split(/\s+/).filter(function (t) { return t; });
        var list = terms.length ? rows.filter(function (r) { return matchRow(r, terms); }) : rows;
        var hit = reportTable(box, title, list, renderReportInto, showDept);
        if (!hit) box.appendChild(U.el('p', { class: 'empty-hint',
          text: terms.length ? '找不到符合「' + search.value.trim() + '」的項目。' : '目前無項目。' }));
      }
      search.addEventListener('input', draw);
      draw();
    }

    // Excel 式頁籤：總覽(KPI 三表) / 處置落點 / 應申請未申請 / 落後，一次一張、不用長捲
    renderTabs(host, [
      { label: '總覽', render: function (c) {
          var notApply = Math.max(s.unresolved - s.apply_universe, 0);   // 還不急：未結但還沒到該申請的時機
          var pg = s.progress || {};
          // ① 一句白話總結（粗體數字，重點可點下鑽）
          function hot(v, danger, drill) {
            var e = U.el('b', { class: 'wx-hot' + (danger ? ' danger' : '') + (drill ? ' clk' : ''), text: String(v) });
            if (drill) { e.title = '點看明細'; e.addEventListener('click', drill); }
            return e;
          }
          var sp = U.el('div', { class: 'wx-summary' });
          [U.el('span', { text: scope + '還有 ' }), hot(s.unresolved, false, function () { openFindings('未結案', {}); }),
           U.el('span', { text: ' 支未結：' }), hot(s.overdue, true, function () { openFindings('已逾期', { band: '已逾期' }); }),
           U.el('span', { text: ' 已逾期、' }), hot(s.high_risk, true, function () { openFindings('高風險（Critical/High）', { risk: 'high' }); }),
           U.el('span', { text: ' 高風險（其中 ' }), hot(s.high_risk_overdue, true, function () { openFindings('高風險且逾期', { risk: 'high', band: '已逾期' }); }),
           U.el('span', { text: ' 又逾期，最急）。需要申請展延／例外的 ' }), hot(s.apply_universe, false, function () { openFindings('需申請母體', { apply_universe: 'true' }); }),
           U.el('span', { text: ' 支，其中 ' }), hot(s.need_apply_count, true, function () { openFindings('應申請未申請', { should_apply: 'true' }); }),
           U.el('span', { text: ' 支還沒去申請（要催）、' }), hot(s.target.no_target, true, function () { openFindings('未報預計完成日', { no_target: 'true' }); }),
           U.el('span', { text: ' 支還沒回報預計完成日。' })].forEach(function (n) { sp.appendChild(n); });
          c.appendChild(sp);

          // ② 分解條：把母體關係用「看」的講清楚（避免被誤加）
          stackedBar(c, '未結 ' + s.unresolved + '　＝　要申請 ＋ 還不急', s.unresolved, [
            { label: '要申請', value: s.apply_universe, color: '#1a7f4b', onClick: function () { openFindings('需申請母體', { apply_universe: 'true' }); } },
            { label: '還不急（到期還遠，直接修即可）', value: notApply, color: '#b0bec5', onClick: function () { openFindings('還不急（未到申請時機）', { not_apply: 'true' }); } },
          ]);
          stackedBar(c, '其中「要申請」' + s.apply_universe + '　＝　未申請 ＋ 已申請', s.apply_universe, [
            { label: '應申請未申請（要催）', value: s.need_apply_count, color: '#c0392b', onClick: function () { openFindings('應申請未申請', { should_apply: 'true' }); } },
            { label: '已申請處置中', value: s.applied_count, color: '#1a7f4b', onClick: function () { openFindings('已申請處置中', { applied: 'true' }); } },
          ]);

          // ③ 只留「要催的」關鍵數字（紅），一排對齊
          kpiCards(c, '要催的（最該盯）', [
            { label: '已逾期', value: s.overdue, danger: true, drill: function () { openFindings('落後（已逾期）', { band: '已逾期' }); } },
            { label: '高風險且逾期', value: s.high_risk_overdue, danger: true, drill: function () { openFindings('高風險且逾期', { risk: 'high', band: '已逾期' }); } },
            { label: '應申請未申請', value: s.need_apply_count, danger: true, drill: function () { openFindings('應申請未申請', { should_apply: 'true' }); } },
            { label: '未報預計完成日', value: s.target.no_target, danger: true, drill: function () { openFindings('未報預計完成日', { no_target: 'true' }); } },
            { label: '需追查 ⚠️', value: pg.flagged || 0, danger: true, drill: function () { openFindings('需追查（說要申請/做完卻未反映）', { flagged: 'true' }); } },
          ]);

          // ④ 視覺：嚴重度圓餅 + 到期倒數長條（皆可點下鑽）
          var vis = U.el('div', { style: 'display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start;margin:10px 0' });
          var pie = U.el('div', { style: 'flex:1 1 300px;min-width:280px' });
          pie.appendChild(U.el('div', { class: 'panel-head' }, [U.el('h3', { text: '嚴重度分佈（未結案）' })]));
          var sevItems = severityItems(s.severity, function (k) { openFindings(k + '（嚴重度）', { severity: k }); });
          if (sevItems.length) donutChart(pie, sevItems, '未結'); else pie.appendChild(U.el('p', { class: 'empty-hint', text: '無未結案。' }));
          var bar = U.el('div', { style: 'flex:1 1 300px;min-width:280px' });
          bar.appendChild(U.el('div', { class: 'panel-head' }, [U.el('h3', { text: '到期倒數（未結案·真正到期日）' })]));
          var bp = { overdue: { band: '已逾期' }, d30: { due_min: '0', due_max: '30' },
            d31_60: { due_min: '31', due_max: '60' }, d61_90: { due_min: '61', due_max: '90' }, d90plus: { due_min: '91' } };
          var bl = { overdue: '已逾期', d30: '30 天內', d31_60: '31–60 天', d61_90: '61–90 天', d90plus: '90 天以上' };
          hbarChart(bar, dueBucketItems(s.due_buckets, function (key) { openFindings(bl[key] + '（到期倒數）', bp[key]); }));
          vis.appendChild(pie); vis.appendChild(bar);
          c.appendChild(vis);

          // ⑤ 未結趨勢折線 + 本週變化一句話
          var trendBox = U.el('div'); c.appendChild(trendBox);
          var tp = {}; if (s.department && s.department !== '全部') tp.department = s.department;
          jget('/api/trend?' + qd(tp)).then(function (tr) {
            if (!tr || tr.length < 2) return;
            var labels = tr.map(function (x) { return (x.date || '').slice(5); });
            svgLineChart(trendBox, labels, [
              { name: '未結', color: '#1a7f4b', values: tr.map(function (x) { return x.open; }) },
              { name: '其中逾期', color: '#c0392b', values: tr.map(function (x) { return x.overdue; }) },
            ], '未結趨勢（每次匯入）');
          }).catch(function () { });
          if (s.change && s.change.has_prev) {
            c.appendChild(U.el('p', { class: 'empty-hint', text: '本週（本批 vs 上批）：新增 ' + s.change.new + '、解決 ' + s.change.resolved + '，淨變化 ' + (s.change.delta > 0 ? '+' : '') + s.change.delta + '。' }));
          }

          // ⑥ 處理進度（管理人標註，次要資訊）
          kpiCards(c, '處理進度（管理人標註；結論仍以資安 Excel 為主）', [
            { label: '要申請展延', value: pg.apply_ext || 0, drill: function () { openFindings('要申請展延', { progress: '要申請展延' }); } },
            { label: '要申請例外', value: pg.apply_exc || 0, drill: function () { openFindings('要申請例外', { progress: '要申請例外' }); } },
            { label: '處理中', value: pg.wip || 0, drill: function () { openFindings('處理中', { progress: '處理中' }); } },
            { label: '等複掃', value: pg.rescan || 0, drill: function () { openFindings('等複掃', { progress: '等複掃' }); } },
          ]);
      } },
      { label: '負責人', render: function (c) { renderOwnerInto(c); } },       // 併入：誰還有幾支＋狀態(每格下鑽)
      { label: '到期倒數', render: function (c) { renderDueSoonInto(c); } },    // 併入：14/30/60/90 分桶(含 14 天申請行動線)
      { label: '處置落點', render: function (c) { renderStageLanding(c, s); } },
      { label: '應申請未申請（' + s.need_apply_list.length + '）', render: function (c) {
          listTab(c, '應申請未申請清單（主管要催承辦去提例外／展延）', s.need_apply_list);
      } },
      { label: '要申請·送審中（' + (s.apply_intent_list || []).length + '）', render: function (c) {
          listTab(c, '要申請·送審中（管理人標了要申請、Excel 尚未反映）', s.apply_intent_list || []);
      } },
      { label: '需追查⚠️（' + (s.flagged_list || []).length + '）', render: function (c) {
          listTab(c, '需追查（說要申請/做完卻未反映：待查或可疑）', s.flagged_list || []);
      } },
      { label: '落後（' + s.overdue_list.length + '）', render: function (c) {
          listTab(c, '落後清單（已逾真正到期日）', s.overdue_list);
      } },
    ], 'report');
  }

  // KPI 指標表：統一成與「處置落點」同款綠底表格(項目｜數值)；數值可標紅、可下鑽；整表可匯出。
  function kpiTable(host, title, rows) {
    host.appendChild(headWithExport(title, [['label', '項目'], ['value', '數值']], function () { return rows; }));
    var table = U.el('table', { class: 'tracking-table' });   // 滿版(與其他表一致)
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, [U.el('th', { text: '項目' }), U.el('th', { text: '數值' })])]));
    var tb = U.el('tbody');
    rows.forEach(function (r) {
      var v = U.el('td', { text: (r.value == null ? '—' : String(r.value)) });
      if (r.danger && r.value) v.style.color = '#c0392b';
      if (r.drill && r.value) {
        v.style.cursor = 'pointer'; v.style.fontWeight = '600'; v.title = '點我看明細';
        if (!r.danger) v.style.color = '#1a7f4b';
        v.addEventListener('click', r.drill);
      }
      tb.appendChild(U.el('tr', {}, [U.el('td', { text: r.label }), v]));
    });
    table.appendChild(tb); host.appendChild(table);
  }

  // 緊湊 KPI 卡片格：一組小標 + 一排數字磚(數字大、標籤小)，取代一疊 2 欄表，省約 4/5 空間。
  // items=[{label,value,danger,drill}]；danger→紅、drill→可點(綠、hover)。
  function kpiCards(host, title, items) {
    ensureChartStyle();
    host.appendChild(U.el('div', { class: 'wx-kpi-grp', text: title }));
    var row = U.el('div', { class: 'wx-kpi-row' });
    items.forEach(function (it) {
      var clk = it.drill && it.value;
      var tile = U.el('div', { class: 'wx-kpi' + (clk ? ' clk' : '') + (it.danger && it.value ? ' danger' : '') });
      if (clk) { tile.style.cursor = 'pointer'; tile.title = '點我看明細'; tile.addEventListener('click', it.drill); }
      tile.appendChild(U.el('div', { class: 'v' + (it.danger && it.value ? ' danger' : ''), text: (it.value == null ? '—' : String(it.value)) }));
      tile.appendChild(U.el('div', { class: 'l', text: it.label }));
      row.appendChild(tile);
    });
    host.appendChild(row);
  }

  // 匯出此清單(CSV)：把「眼前這份子集」依目前欄位匯出(非全量)。cols=[[key,表頭],…]。
  function exportRowsCSV(cols, rows, title) {
    var headers = cols.map(function (c) { return c[1]; });
    var lines = [headers].concat(rows.map(function (r) {
      return cols.map(function (c) { return r[c[0]] == null ? '' : r[c[0]]; });
    }));
    var csv = lines.map(function (arr) {
      return arr.map(function (v) {
        var sVal = String(v);
        if (/[",\n]/.test(sVal)) sVal = '"' + sVal.replace(/"/g, '""') + '"';
        return sVal;
      }).join(',');
    }).join('\r\n');
    var blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });  // BOM 讓 Excel 認 UTF-8
    var url = URL.createObjectURL(blob);
    var a = U.el('a', { href: url, download: (title || '弱點清單').replace(/[\\\/:*?"<>|]/g, '_') + '.csv' });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    if (UI && UI.toast) UI.toast('已匯出此清單 CSV', 'success');
  }

  // 匯出「原始整列」：每筆帶回原始 Excel 的全部欄位(原欄名、原欄序)，只是列是眼前子集。
  // 欄序＝各筆 raw 鍵的有序聯集(raw 保留匯入時的原欄序)。不幫使用者篩欄。
  function exportRawCSV(rows, title) {
    rows = rows || [];
    var cols = [], seen = {};
    rows.forEach(function (r) {
      var raw = r && r.raw ? r.raw : null;
      if (raw) Object.keys(raw).forEach(function (k) { if (!seen[k]) { seen[k] = 1; cols.push([k, k]); } });
    });
    if (!cols.length) {   // 沒有 raw(理論上不會)→退回顯示欄,至少匯得出東西
      if (UI && UI.toast) UI.toast('這份清單沒有原始欄位可匯出', 'error');
      return;
    }
    exportRowsCSV(cols, rows.map(function (r) { return (r && r.raw) ? r.raw : {}; }), title);
  }

  // 弱點明細清單的「簡易匯出」欄位(約 8 欄,各資料來源都有這些鍵)
  var SIMPLE_FINDING_COLS = [['host', '主機'], ['owner', '負責人'], ['severity', '嚴重度'],
    ['name', '弱點'], ['plugin_id', 'Plugin'], ['effective_due', '到期日'],
    ['overdue_days', '逾期天數'], ['department', '部門']];

  // 弱點明細清單的雙匯出鈕：完整(原始整欄,~28) + 簡易(精簡 8 欄)。回傳 [完整鈕, 簡易鈕]。
  function listExportButtons(getRows, title) {
    function guard(fn) {
      return function () {
        var rows = getRows() || [];
        if (!rows.length) { if (UI && UI.toast) UI.toast('沒有可匯出的資料', 'error'); return; }
        fn(rows);
      };
    }
    var full = U.el('button', { class: 'btn btn-secondary btn-sm', text: '完整匯出 (CSV)', title: '原始 Excel 全部欄位' });
    full.addEventListener('click', guard(function (rows) { exportRawCSV(rows, title + '_完整'); }));
    var simple = U.el('button', { class: 'btn btn-secondary btn-sm', text: '簡易匯出 (CSV)', title: '只匯常用幾欄' });
    simple.addEventListener('click', guard(function (rows) { exportRowsCSV(SIMPLE_FINDING_COLS, rows, title + '_簡易'); }));
    return [full, simple];
  }

  // 標題 + 弱點清單雙匯出鈕(完整/簡易)
  function headWithListExport(title, getRows) {
    var head = U.el('div', { class: 'panel-head', style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap' },
      [U.el('h3', { text: title })]);
    listExportButtons(getRows, title).forEach(function (b) { head.appendChild(b); });
    return head;
  }

  // 通用「標題 + 匯出此清單」：任何統計/清單都掛得上。getRows 回傳當下要匯出的資料(子集)。
  function headWithExport(title, cols, getRows) {
    var head = U.el('div', { class: 'panel-head', style: 'display:flex;align-items:center;gap:10px;flex-wrap:wrap' },
      [U.el('h3', { text: title })]);
    var b = U.el('button', { class: 'btn btn-secondary btn-sm', text: '匯出此清單 (CSV)' });
    b.addEventListener('click', function () {
      var rows = getRows() || [];
      if (!rows.length) { if (UI && UI.toast) UI.toast('沒有可匯出的資料', 'error'); return; }
      exportRowsCSV(cols, rows, title);
    });
    head.appendChild(b);
    return head;
  }

  // 處置落點：目前未結案各筆的「真正到期日」落在哪一關,幾筆、到期日區間、其中逾期(可點下鑽看每筆)
  function renderStageLanding(host, s) {
    var st = s.stages || {};
    var rows = [
      { key: '原始修補期限', label: '原始修補期限', b: st.original },
      { key: '首次展延中', label: '首次展延', b: st.extension },
      { key: '例外管理中', label: '例外管理（列管）', b: st.exception },
    ];
    if (st.other && st.other.count) rows.push({ key: null, label: '未定／其他', b: st.other });
    var expRows = rows.map(function (r) {
      var b = r.b || {}; return { 處置階段: r.label, 筆數: b.count || 0, 其中逾期: (b.overdue == null ? '' : b.overdue),
        最早落點: b.earliest_due || '', 最晚落點: b.latest_due || '' };
    });
    // 圓餅：三關占比一眼看結構（原始＝還沒去申請、展延/例外＝已列管），點圖例下鑽。
    var STG_COLOR = { '原始修補期限': '#e64a19', '首次展延中': '#f9a825', '例外管理中': '#1a7f4b', '其他': '#90a4ae' };
    var pieItems = rows.map(function (r) {
      var b = r.b || {};
      return { label: r.label, value: b.count || 0, color: STG_COLOR[r.key] || '#90a4ae',
        onClick: r.key ? function () { openFindings(r.label + '（處置落點）', { stage: r.key }); } : null };
    }).filter(function (it) { return it.value > 0; });
    if (pieItems.length) donutChart(host, pieItems, '未結');
    host.appendChild(headWithExport('處置落點（未結案；點筆數看每筆到期日）',
      [['處置階段', '處置階段'], ['筆數', '筆數'], ['其中逾期', '其中逾期'], ['最早落點', '最早落點（到期日）'], ['最晚落點', '最晚落點（到期日）']],
      function () { return expRows; }));
    var table = U.el('table', { class: 'tracking-table' });
    table.appendChild(U.el('thead', {}, [U.el('tr', {},
      ['處置階段', '筆數', '其中逾期', '最早落點（到期日）', '最晚落點（到期日）'].map(function (h) { return U.el('th', { text: h }); }))]));
    var tb = U.el('tbody');
    rows.forEach(function (r) {
      var b = r.b || { count: 0 };
      var cntCell = U.el('td', { text: String(b.count || 0) });
      if (r.key && b.count) {
        cntCell.style.cssText = 'color:#1a7f4b;cursor:pointer;font-weight:600';
        cntCell.title = '點我看這關的每筆到期日';
        cntCell.addEventListener('click', function () { openFindings(r.label + '（處置落點）', { stage: r.key }); });
      }
      tb.appendChild(U.el('tr', {}, [
        U.el('td', { text: r.label }),
        cntCell,
        U.el('td', { text: (b.overdue == null ? '—' : String(b.overdue)) }),
        U.el('td', { text: b.earliest_due || '—' }),
        U.el('td', { text: b.latest_due || '—' }),
      ]));
    });
    table.appendChild(tb); host.appendChild(table); makeSortable(table);
  }

  // 週報用的清單表（含預計完成日／追蹤備註，可改）。showDept=false 時隱藏部門欄。回傳是否有畫出表格。
  function reportTable(host, title, rows, refresh, showDept) {
    if (!rows || !rows.length) return false;
    host.appendChild(headWithListExport(title + '（' + rows.length + '）', function () { return rows; }));
    var heads = ['負責人', '弱點', '嚴重度', '主機', '到期日', '逾期天數', '處理進度', '對帳狀態', '預計完成日', '追蹤備註'];
    if (showDept) heads.push('部門');
    heads.push('操作');   // 固定有(🔍看原始；可寫入再加 ✏️)
    var table = U.el('table', { class: 'tracking-table' });
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, heads.map(function (h) { return U.el('th', { text: h }); }))]));
    var tb = U.el('tbody');
    rows.forEach(function (r) {
      var td = r.target_date || '—';
      if (r.target_overdue) td += '（已過）';
      var tds = [
        U.el('td', { text: r.owner || '未指派' }), U.el('td', { text: r.name || r.plugin_id || '' }),
        U.el('td', { text: r.severity || '' }), U.el('td', { text: r.host || '' }),
        U.el('td', { text: r.effective_due || '—' }),
        U.el('td', { text: (r.overdue_days != null && r.overdue_days > 0) ? String(r.overdue_days) : '—' }),
        U.el('td', { text: r.progress || '' }), U.el('td', { text: r.progress_state || '' }),
        U.el('td', { text: td }), U.el('td', { text: r.track_note || '' }),
      ];
      if (showDept) tds.push(U.el('td', { text: r.department || '' }));
      tds.push(opsCell(r, function () { if (refresh) refresh(document.getElementById('webext-view-body')); }));
      tb.appendChild(U.el('tr', {}, tds));
    });
    table.appendChild(tb); host.appendChild(table); makeSortable(table);
    return true;
  }

  // 列印／存 PDF：開乾淨視窗、純文字表格、觸發瀏覽器列印（可選「另存為 PDF」）
  function printReport(s) {
    var scope = (s.department && s.department !== '全部') ? s.department : '全部門';
    function esc(x) { return String(x == null ? '' : x).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); }
    function tbl(title, rows) {
      if (!rows || !rows.length) return '<h3>' + esc(title) + '（0）</h3><p class="muted">無。</p>';
      var h = '<h3>' + esc(title) + '（' + rows.length + '）</h3><table><thead><tr>'
        + ['負責人', '弱點', '嚴重度', '主機', '到期日', '逾期天數', '處理進度', '對帳狀態', '預計完成日', '追蹤備註', '部門']
          .map(function (x) { return '<th>' + x + '</th>'; }).join('') + '</tr></thead><tbody>';
      rows.forEach(function (r) {
        var td = r.target_date || '—'; if (r.target_overdue) td += '（已過）';
        h += '<tr>' + [r.owner || '未指派', r.name || r.plugin_id || '', r.severity || '', r.host || '',
          r.effective_due || '—', (r.overdue_days != null && r.overdue_days > 0) ? r.overdue_days : '—',
          r.progress || '', r.progress_state || '', td, r.track_note || '', r.department || '']
          .map(function (x) { return '<td>' + esc(x) + '</td>'; }).join('') + '</tr>';
      });
      return h + '</tbody></table>';
    }
    var kv = function (k, v) { return '<span class="kv"><b>' + esc(v) + '</b> ' + esc(k) + '</span>'; };
    var html = '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
      + '<title>主管週報_' + esc(scope) + '_' + esc(s.today) + '</title><style>'
      + 'body{font-family:"Microsoft JhengHei","PingFang TC",sans-serif;color:#1a1a1a;margin:28px;font-size:13px}'
      + 'h1{font-size:20px;margin:0 0 4px}h3{margin:18px 0 6px;border-left:4px solid #1a7f4b;padding-left:8px}'
      + '.muted{color:#777}.sub{color:#555;margin:0 0 12px}'
      + '.kv{display:inline-block;margin:0 16px 6px 0}.kv b{font-size:16px;color:#1a7f4b}'
      + '.block{background:#f6f8f7;border:1px solid #e3e6ea;border-radius:8px;padding:10px 12px;margin:8px 0}'
      + 'table{border-collapse:collapse;width:100%;margin:4px 0 10px}'
      + 'th,td{border:1px solid #d6dbdf;padding:4px 7px;text-align:left;vertical-align:top}'
      + 'th{background:#eef3f0}@media print{button{display:none}}'
      + '</style></head><body>'
      + '<h1>弱點修補 主管週報</h1>'
      + '<p class="sub">範圍：' + esc(scope) + '　|　基準日：' + esc(s.today)
      + '　|　產生：' + esc((s.generated_at || '').replace('T', ' '))
      + (s.freshness.days_ago == null ? '' : '　|　資料距今 ' + s.freshness.days_ago + ' 天') + '</p>'
      + ((s.change && s.change.has_prev) ? ('<div class="block"><b>本週變化（本批 vs 上批）</b><br>'
          + kv('上批未結', s.change.prev) + kv('本批未結', s.change.now)
          + kv('淨變化', (s.change.delta > 0 ? '+' : '') + s.change.delta)
          + kv('本週新增', s.change.new) + kv('本週解決', s.change.resolved) + '</div>') : '')
      + '<div class="block"><b>本期概況（未結案）</b><br>'
      + kv('未結案', s.unresolved) + kv('落後(逾期)', s.overdue) + kv('如期', s.on_track)
      + kv('高風險', s.high_risk) + kv('高風險且逾期', s.high_risk_overdue) + '</div>'
      + '<div class="block"><b>申請進度</b><br>'
      + kv('需申請母體', s.apply_universe) + kv('應申請未申請', s.need_apply_count) + kv('已申請處置中', s.applied_count) + '</div>'
      + '<div class="block"><b>預計完成彙總</b><br>'
      + kv('已回報預計日', s.target.with_target) + kv('未回報(要催)', s.target.no_target)
      + kv('已過預計日', s.target.target_overdue) + kv('預計30天內完成', s.target.target_soon) + '</div>'
      + '<div class="block"><b>處理進度分佈（管理人標註）</b><br>'
      + kv('要申請展延', (s.progress || {}).apply_ext || 0) + kv('要申請例外', (s.progress || {}).apply_exc || 0)
      + kv('處理中', (s.progress || {}).wip || 0) + kv('等複掃', (s.progress || {}).rescan || 0)
      + kv('需追查⚠️', (s.progress || {}).flagged || 0) + '</div>'
      + tbl('應申請未申請清單', s.need_apply_list)
      + tbl('要申請·送審中（Excel 尚未反映）', s.apply_intent_list || [])
      + tbl('需追查⚠️（說要申請/做完卻未反映）', s.flagged_list || [])
      + tbl('落後清單', s.overdue_list)
      + '<p class="muted" style="margin-top:16px">結論以資安 Excel 為主；「處理進度」為管理人追蹤標註。彙總自系統「預計完成日／追蹤備註」與備註申請紀錄（例外管理／展延 iForm）。</p>'
      + '</body></html>';
    var w = window.open('', '_blank');
    if (!w) { UI.toast('瀏覽器擋了新視窗，請允許彈出視窗後再試', 'error'); return; }
    w.document.open(); w.document.write(html); w.document.close();
    setTimeout(function () { try { w.focus(); w.print(); } catch (e) {} }, 300);
  }

  // ===== 左側第二大項「承辦管線」（與「總覽」並列，綠色），底下放全部新功能 =====
  // 架構＝四個角度：看（負責人追蹤）／做（到期倒數）／報（主管週報，含一鍵發送）／查（查核：結案稽核＋對帳健檢＋資料缺口）
  var GOV_ITEMS = [
    // 收斂：主管週報一站看完(負責人/到期倒數都併成其分頁，下鑽亦以負責人為主)；查核(查帳)獨立留。
    // 開發期暫加 A/B 代號方便對話指稱；開發完畢再拿掉(搜 'DEV-LETTER' 一次清)
    { key: 'report', label: 'A. 主管週報', render: renderReportInto },    // 看＋報：總覽/負責人/到期倒數/處置落點/各清單(含一鍵發送鈕)
    { key: 'audit', label: 'B. 查核', render: renderAuditInto },          // 查：結案稽核＋對帳健檢＋資料缺口(查帳,非報告)
    // 一鍵發送＝動作；放左側好找，沿用原 Email 設定流程。主管週報內也有同鈕。
    { key: 'email', label: '📧 一鍵發送', action: function () {
        var b = document.getElementById('email-settings-btn'); if (b) b.click(); else UI.toast('找不到 Email 設定', 'error');
      } },
  ];

  // 全站匯出統一成「原封 Excel」：攔截所有匯出鈕(原本各表的匯出CSV等)→改下載原封 xlsx。
  // 用捕獲階段委派，涵蓋動態產生的按鈕，且不改原檔。
  function wireUnifiedExport() {
    document.addEventListener('click', function (e) {
      var b = e.target && e.target.closest ? e.target.closest('button, a') : null;
      if (!b) return;
      var t = (b.textContent || '').replace(/\s/g, '');
      // 「匯出此清單」是下鑽視窗的『只匯出眼前這份子集』(原生 exportCSV，client 端)——
      // 不可攔；攔了會變成匯出原封全量(幾千筆)。只統一其餘『整表/總覽匯出』→ 原封 xlsx。
      if (/此清單/.test(t)) return;
      if (/匯出/.test(t) && /(CSV|清單|Excel|匯出$)/.test(t) && b.id.indexOf('webext') !== 0) {
        e.preventDefault(); e.stopImmediatePropagation();
        window.location = '/api/export?' + qd();   // 原封 1:1 xlsx（帶目前部門）
      }
    }, true);
  }

  var currentGov = null;
  function hideWebextView() {
    var v = document.getElementById('webext-view'); if (v) v.classList.add('hidden');
    currentGov = null;
    document.querySelectorAll('#sheet-nav .webext-navitem').forEach(function (b) { b.classList.remove('active'); });
  }
  function refreshGov() {  // 重繪目前開著的承辦小項（登入狀態改變時用）
    var v = document.getElementById('webext-view');
    if (currentGov && v && !v.classList.contains('hidden')) currentGov.render(document.getElementById('webext-view-body'));
  }
  function showWebextView(item, btn) {
    // 蓋掉原本兩個檢視（原檔用 .hidden 切換，這裡比照）
    var sum = document.getElementById('summary-view'); if (sum) sum.classList.add('hidden');
    var sv = document.getElementById('sheet-view'); if (sv) sv.classList.add('hidden');
    var v = document.getElementById('webext-view'); if (v) v.classList.remove('hidden');
    document.getElementById('webext-view-title').textContent = item.label;
    currentGov = item;
    // 原本 nav 的 active 拿掉，改點亮我的小項
    document.querySelectorAll('#sheet-nav .sheet-item').forEach(function (b) { b.classList.remove('active'); });
    if (btn) btn.classList.add('active');
    item.render(document.getElementById('webext-view-body'));
  }

  function injectNavGroup() {
    var nav = document.getElementById('sheet-nav');
    if (!nav || nav.querySelector('.webext-navgroup')) return;      // 已注入就不重複
    if (!nav.querySelector('.nav-summary')) return;                  // 原本 nav 還沒建好，等下次
    // 原本每個項目（總覽＋各表）被點時，隱藏我的檢視、還原原本流程
    nav.querySelectorAll('.sheet-item:not(.webext-navitem)').forEach(function (b) {
      if (!b._webextHooked) { b._webextHooked = true; b.addEventListener('click', hideWebextView); }
    });
    // 大項標頭（綠色，比照 nav-summary 風格）；點標頭＝收合/展開底下小項
    var head = U.el('button', { class: 'sheet-item nav-summary webext-navgroup', title: '承辦管線（點我收合／展開）' },
      [U.el('span', { class: 'sheet-name', text: '承辦管線' }), U.el('span', { class: 'webext-chev' })]);
    nav.appendChild(head);
    // 小項
    GOV_ITEMS.forEach(function (item) {
      var b = U.el('button', { class: 'sheet-item webext-navitem', title: item.label },
        [U.el('span', { class: 'sheet-name', text: '　• ' + item.label })]);
      b.addEventListener('click', function () {
        if (item.action) { item.action(); return; }
        showWebextView(item, b);
      });
      nav.appendChild(b);
    });
    head.addEventListener('click', function () {
      setNavState('navGov', !navState('navGov', true)); applyCollapse();
    });
    decorateNav();
  }

  // ===== 大項收合／展開（總覽的 10 張表、承辦管線的小項），狀態記 localStorage =====
  function navState(key, def) {
    try { var v = localStorage.getItem('webext.' + key); return v === null ? def : v === '1'; }
    catch (e) { return def; }
  }
  function setNavState(key, val) { try { localStorage.setItem('webext.' + key, val ? '1' : '0'); } catch (e) {} }

  function applyCollapse() {
    var sheetsOpen = navState('navSheets', true);
    var govOpen = navState('navGov', true);
    document.querySelectorAll('#sheet-nav .sheet-item').forEach(function (b) {
      if (b.classList.contains('nav-summary') || b.classList.contains('webext-navgroup')) return;
      if (b.classList.contains('webext-navitem')) b.style.display = govOpen ? '' : 'none';
      else b.style.display = sheetsOpen ? '' : 'none';   // 原本 10 張表
    });
    var s = document.querySelector('#sheet-nav .nav-summary:not(.webext-navgroup) .webext-chev');
    if (s) s.textContent = sheetsOpen ? ' ▾' : ' ▸';
    var g = document.querySelector('#sheet-nav .webext-navgroup .webext-chev');
    if (g) g.textContent = govOpen ? ' ▾' : ' ▸';
  }

  function decorateNav() {
    // 給「總覽」大項加收合箭頭（點箭頭收 10 張表，點總覽本身仍開總覽頁）
    var sum = document.querySelector('#sheet-nav .nav-summary:not(.webext-navgroup)');
    if (sum && !sum.querySelector('.webext-chev')) {
      var c = U.el('span', { class: 'webext-chev' });
      c.addEventListener('click', function (e) {
        e.stopPropagation();
        setNavState('navSheets', !navState('navSheets', true)); applyCollapse();
      });
      sum.appendChild(c);
    }
    applyCollapse();
  }

  function wireNewFeatures() {
    var lb = document.getElementById('webext-login-btn');
    if (lb) lb.addEventListener('click', function () { me.authenticated ? doLogout() : openLogin(); });
    // 「Email 設定」移到承辦管線→「一鍵發送」，從「其他功能」選單隱藏(功能仍靠此按鈕觸發)
    var eb = document.getElementById('email-settings-btn'); if (eb) eb.style.display = 'none';
    wireUnifiedExport();
    injectNavGroup();
    // main.js 會在載入資料/切部門時重建 #sheet-nav → 用 observer 重新注入我的大項
    var nav = document.getElementById('sheet-nav');
    if (nav && window.MutationObserver) {
      new MutationObserver(function () {
        // 原本重建 nav(換部門/結案狀態)會清掉我的大項→此時收起我的檢視、還原原本流程，再重新注入
        if (!nav.querySelector('.webext-navgroup')) { hideWebextView(); injectNavGroup(); }
      }).observe(nav, { childList: true });
    }
    refreshMe();
  }

  function start() {
    wirePersist();
    wireNewFeatures();
    // 防「上傳畫面一閃」：靜態遮罩(index.html 第一幀就蓋)在載完伺服器快照後才移除
    var up = document.getElementById('upload-section');
    if (up) up.classList.add('hidden');   // 先藏上傳區(有資料時不會閃出來)
    function dropBoot() { var b = document.getElementById('webext-boot'); if (b && b.parentNode) b.parentNode.removeChild(b); }
    loadFromServer().then(function (ok) {   // 有伺服器資料就自動載入
      if (!ok && up) up.classList.remove('hidden');   // 沒資料→回到上傳畫面
      dropBoot();
    }).catch(function () { if (up) up.classList.remove('hidden'); dropBoot(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})(window);

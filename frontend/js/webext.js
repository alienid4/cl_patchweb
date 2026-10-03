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
      ['plugin_id', 'Plugin'], ['effective_due', '到期日'], ['overdue_days', '逾期天數'],
      ['stage', '處置階段'], ['progress', '處理進度'], ['rescan_state', '複掃狀態'],
      ['department', '部門'], ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
    var curRows = [];   // 載入後填入,供「匯出」用(匯的是眼前這份子集)
    var footer = U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
      listExportButtons(function () { return curRows; }, title));   // 完整/簡易 兩顆
    UI.openModal(title, box, { footer: footer });
    var rows;
    try { rows = await jget('/api/findings?' + qd(Object.assign({ status: '未結案' }, params || {}))); }
    catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
    if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無資料' })); return; }
    curRows = rows;
    var hint = '共 ' + rows.length + ' 筆（點欄位排序）';
    if (canWrite()) hint += '；負責人欄可點「改」重新指派（存系統、重匯不會被蓋掉）';
    box.appendChild(U.el('p', { class: 'empty-hint', text: hint }));
    var heads = cols.map(function (c) { return c[1]; });
    if (canWrite()) heads.push('操作');
    var table = U.el('table', { class: 'tracking-table' });
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, heads.map(function (h) { return U.el('th', { text: h }); }))]));
    var tb = U.el('tbody');
    rows.forEach(function (r) {
      var tds = cols.map(function (c) { return U.el('td', { text: r[c[0]] == null ? '' : String(r[c[0]]) }); });
      if (canWrite()) {
        var btn = U.el('button', { class: 'btn btn-sm', text: '改' });
        btn.addEventListener('click', function () { editOwner(r, function () { openFindings(title, params); }); });
        tds.push(U.el('td', {}, [btn]));
      }
      tb.appendChild(U.el('tr', {}, tds));
    });
    table.appendChild(tb); box.appendChild(table); makeSortable(table);
  }

  // 管理員編輯一筆弱點的可寫欄位(負責人＋追蹤備註)：開視窗、存系統疊加層、重匯不洗掉、不動 Excel
  function editOverlay(row, done) {
    var title = (row.host || '') + ' / ' + (row.name || row.plugin_id || '');
    var owner = U.el('input', { type: 'text', value: row.owner || '' });
    var target = U.el('input', { type: 'date', value: row.target_date || '' });
    var progress = U.el('select', {}, [
      U.el('option', { value: '', text: '未標記' }),
      U.el('option', { value: '處理中', text: '處理中' }),
      U.el('option', { value: '等複掃', text: '等複掃（承辦回報做完、等資安複掃）' }),
    ]);
    progress.value = row.progress || '';
    var note = U.el('textarea', { rows: '4' });
    note.value = row.track_note || '';
    [owner, target, progress, note].forEach(function (i) { i.style.cssText = 'display:block;width:100%;margin:4px 0 12px;padding:8px;border:1px solid #e3e6ea;border-radius:6px;font-size:14px;font-family:inherit'; });
    var body = U.el('div', {}, [
      U.el('p', { class: 'empty-hint', text: title }),
      U.el('label', { text: '負責人（留空＝取消覆蓋、回到 Excel 值）' }), owner,
      U.el('label', { text: '處理進度（系統內自己標，不碰 Excel；結論仍以資安 Excel 為主）' }), progress,
      U.el('label', { text: '預計完成日（承辦回報預計哪天做完；留空＝清除。供主管週報彙總）' }), target,
      U.el('label', { text: '追蹤備註（承辦回報：何時做什麼動作。只存系統，不會動到 Excel 原備註）' }), note,
    ]);
    var save = U.el('button', { class: 'btn btn-primary', text: '存檔' });
    save.addEventListener('click', async function () {
      var r = await fetch('/api/findings/' + row.id + '/overlay', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ set_owner: true, owner: owner.value, set_target: true, target_date: target.value, set_progress: true, progress: progress.value, set_note: true, note: note.value })
      });
      if (!r.ok) { var e = await r.json().catch(function () { return {}; }); UI.toast('存失敗：' + (e.detail || r.status), 'error'); return; }
      UI.closeModal(); UI.toast('已更新', 'success'); if (done) done();
    });
    UI.openModal('編輯弱點（承辦管理）', body, { footer: save });
    setTimeout(function () { owner.focus(); }, 0);
  }
  var editOwner = editOverlay;  // 相容舊呼叫名

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
  // 送審進度＝「已申請處置中」：備註已有申請紀錄(例外/展延)的弱點，追它們的預計完成日。
  // 狀態改成看備註自動算(方案A)：不再手動推關卡、不列全量案件。
  async function renderCasesInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    var s;
    try { s = await jget('/api/report?' + qd()); }
    catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料，請先匯入。' })); return; }
    var exc = (s.stages && s.stages.exception) ? s.stages.exception.count : 0;
    var ext = (s.stages && s.stages.extension) ? s.stages.extension.count : 0;
    var total = (s.applied_count != null) ? s.applied_count : (exc + ext);

    host.appendChild(U.el('p', { class: 'empty-hint',
      text: '「已申請處置中」＝備註已有申請紀錄（例外／展延）的弱點，在這裡追它們的預計完成日有沒有如期。承辦去 iForm 申請、下次匯入後會自動進來，狀態由備註自動判定，不需手動改。' }));

    renderTabs(host, [
      { label: '全部處理中（' + total + '）', render: function (c) { renderActionListInto(c, '已申請處置中（例外／展延）', { applied: 'true' }); } },
      { label: '例外管理（' + exc + '）', render: function (c) { renderActionListInto(c, '例外管理中', { stage: '例外管理中' }); } },
      { label: '首次展延（' + ext + '）', render: function (c) { renderActionListInto(c, '首次展延中', { stage: '首次展延中' }); } },
    ], 'cases');

    // 維護：清除舊/測試資料殘留、來源已消失的追蹤紀錄(orphan case overlay)
    if (canWrite()) {
      var m = U.el('div', { class: 'scope-info', style: 'margin-top:14px' },
        [U.el('span', { text: '維護：舊／測試資料殘留、來源已消失的追蹤紀錄可在此清除。' })]);
      var pb = U.el('button', { class: 'btn btn-sm', text: '清除已消失追蹤紀錄', style: 'margin-left:8px' });
      pb.addEventListener('click', function () { purgeOrphans(); });
      m.firstChild.appendChild(document.createTextNode(' '));
      m.firstChild.appendChild(pb);
      host.appendChild(m);
    }
  }

  // 清除「已消失」案件（來源已無的 orphan case）；需寫入權限，後端 require_write_role
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

  // ---- 待辦清單（「做」：要處理的清單都集中在這。待申請／已逾期／無人負責／無到期日）----
  async function renderTodoInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    var s;
    try { s = await jget('/api/summary?' + qd()); }
    catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料，請先匯入。' })); return; }
    renderTabs(host, [
      { label: '待申請（' + s.should_apply + '）', render: function (c) { renderActionListInto(c, '待申請（應提例外／展延未提）', { should_apply: 'true' }); } },
      { label: '已逾期（' + s.overdue + '）', render: function (c) { renderActionListInto(c, '已逾期', { band: '已逾期' }); } },
      { label: '無人負責（' + s.gaps.no_owner + '）', render: function (c) { renderActionListInto(c, '無人負責', { no_owner: 'true' }); } },
      { label: '無到期日（' + s.gaps.no_due + '）', render: function (c) { renderActionListInto(c, '無到期日', { no_due: 'true' }); } },
    ], 'todo');
  }

  // 待辦清單的單一清單：搜尋 + 可改(負責人/預計完成日/備註) + 完整/簡易匯出
  var _TODO_COLS = [['host', '主機'], ['owner', '負責人'], ['severity', '嚴重度'], ['name', '弱點'],
    ['plugin_id', 'Plugin'], ['effective_due', '到期日'], ['overdue_days', '逾期天數'],
    ['stage', '處置階段'], ['progress', '處理進度'], ['rescan_state', '複掃狀態'],
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
    function draw() {
      box.innerHTML = '';
      var terms = (search.value || '').toLowerCase().split(/\s+/).filter(function (t) { return t; });
      shown = terms.length ? rows.filter(function (r) { return matchRow(r, terms); }) : rows;
      if (!shown.length) {
        box.appendChild(U.el('p', { class: 'empty-hint', text: terms.length ? '找不到符合「' + search.value.trim() + '」的項目。' : '目前無項目 👍' }));
        return;
      }
      var w = canWrite();
      var heads = _TODO_COLS.map(function (c) { return c[1]; });
      if (w) heads.push('操作');
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, heads.map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      shown.forEach(function (r) {
        var tds = _TODO_COLS.map(function (c) { return U.el('td', { text: r[c[0]] == null ? '' : String(r[c[0]]) }); });
        if (w) {
          var b = U.el('button', { class: 'btn btn-sm', text: '改' });
          b.addEventListener('click', function () { editOverlay(r, function () { renderActionListInto(container, title, params); }); });
          tds.push(U.el('td', {}, [b]));
        }
        tb.appendChild(U.el('tr', {}, tds));
      });
      table.appendChild(tb); box.appendChild(table); makeSortable(table);
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
    var printBtn = U.el('button', { class: 'btn btn-primary btn-sm', text: '列印 / 存 PDF' });
    printBtn.addEventListener('click', function () { printReport(s); });
    headRow.appendChild(printBtn);
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
          kpiTable(c, '本期概況（未結案）', [
            { label: '未結案', value: s.unresolved },
            { label: '落後（已逾期）', value: s.overdue, danger: true, drill: function () { openFindings('落後（已逾期）', { band: '已逾期' }); } },
            { label: '如期（未逾期）', value: s.on_track },
            { label: '高風險（Critical/High）', value: s.high_risk, danger: true },
          ]);
          kpiTable(c, '申請進度', [
            { label: '需申請母體', value: s.apply_universe },
            { label: '應申請未申請（要催）', value: s.need_apply_count, danger: true, drill: function () { openFindings('應申請未申請', { should_apply: 'true' }); } },
            { label: '已申請處置中（例外/展延）', value: s.applied_count },
          ]);
          kpiTable(c, '預計完成彙總（需申請母體）', [
            { label: '已回報預計完成日', value: s.target.with_target },
            { label: '未回報預計完成日（要催）', value: s.target.no_target, danger: true },
            { label: '已過預計完成日', value: s.target.target_overdue, danger: true },
            { label: '預計 30 天內完成', value: s.target.target_soon },
          ]);
      } },
      { label: '處置落點', render: function (c) { renderStageLanding(c, s); } },
      { label: '應申請未申請（' + s.need_apply_list.length + '）', render: function (c) {
          listTab(c, '應申請未申請清單（主管要催承辦去提例外／展延）', s.need_apply_list);
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
    var w = canWrite();
    var heads = ['負責人', '弱點', '嚴重度', '主機', '到期日', '逾期天數', '預計完成日', '追蹤備註'];
    if (showDept) heads.push('部門');
    if (w) heads.push('操作');
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
        U.el('td', { text: td }), U.el('td', { text: r.track_note || '' }),
      ];
      if (showDept) tds.push(U.el('td', { text: r.department || '' }));
      if (w) {
        var b = U.el('button', { class: 'btn btn-sm', text: '改' });
        b.addEventListener('click', function () { editOverlay(r, function () { if (refresh) refresh(document.getElementById('webext-view-body')); }); });
        tds.push(U.el('td', {}, [b]));
      }
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
        + ['負責人', '弱點', '嚴重度', '主機', '到期日', '逾期天數', '預計完成日', '追蹤備註', '部門']
          .map(function (x) { return '<th>' + x + '</th>'; }).join('') + '</tr></thead><tbody>';
      rows.forEach(function (r) {
        var td = r.target_date || '—'; if (r.target_overdue) td += '（已過）';
        h += '<tr>' + [r.owner || '未指派', r.name || r.plugin_id || '', r.severity || '', r.host || '',
          r.effective_due || '—', (r.overdue_days != null && r.overdue_days > 0) ? r.overdue_days : '—',
          td, r.track_note || '', r.department || '']
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
      + '<div class="block"><b>本期概況（未結案）</b><br>'
      + kv('未結案', s.unresolved) + kv('落後(逾期)', s.overdue) + kv('如期', s.on_track) + kv('高風險', s.high_risk) + '</div>'
      + '<div class="block"><b>申請進度</b><br>'
      + kv('需申請母體', s.apply_universe) + kv('應申請未申請', s.need_apply_count) + kv('已申請處置中', s.applied_count) + '</div>'
      + '<div class="block"><b>預計完成彙總</b><br>'
      + kv('已回報預計日', s.target.with_target) + kv('未回報(要催)', s.target.no_target)
      + kv('已過預計日', s.target.target_overdue) + kv('預計30天內完成', s.target.target_soon) + '</div>'
      + tbl('應申請未申請清單', s.need_apply_list)
      + tbl('落後清單', s.overdue_list)
      + '<p class="muted" style="margin-top:16px">本報告彙總自系統「預計完成日／追蹤備註」與備註申請紀錄（例外管理／展延 iForm）。</p>'
      + '</body></html>';
    var w = window.open('', '_blank');
    if (!w) { UI.toast('瀏覽器擋了新視窗，請允許彈出視窗後再試', 'error'); return; }
    w.document.open(); w.document.write(html); w.document.close();
    setTimeout(function () { try { w.focus(); w.print(); } catch (e) {} }, 300);
  }

  // ===== 左側第二大項「承辦管線」（與「總覽」並列，綠色），底下放全部新功能 =====
  // 新功能小項：render=渲染進主區；action=直接動作(如下載)不切畫面
  // 架構：看(總覽,原生)／做(待辦清單+送審進度)／報(主管週報)／查(結案稽核)
  var GOV_ITEMS = [
    { key: 'todo', label: '待辦清單', render: renderTodoInto },     // 做：要處理的清單都在這
    { key: 'cases', label: '送審進度', render: renderCasesInto },   // 做：例外/展延申請跑簽到核准
    { key: 'report', label: '主管週報', render: renderReportInto }, // 報：給主管的固定報告
    { key: 'closestat', label: '結案稽核', render: renderCloseInto }, // 查：結案驗證/浮報
    // 一鍵發送＝沿用原本「Email 設定」流程(開原設定視窗)；日後 B(自動寄週報)再接進主管週報
    { key: 'email', label: '一鍵發送', action: function () {
        var b = document.getElementById('email-settings-btn'); if (b) b.click();
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
    loadFromServer();  // 有伺服器資料就自動載入；沒有則維持原本上傳畫面
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})(window);

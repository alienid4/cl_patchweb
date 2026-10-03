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
      ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
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
    if (canWrite()) hint += '；點 ✏️ 可編輯負責人／進度／預計完成日／備註（存系統、重匯不會被蓋掉）';
    box.appendChild(U.el('p', { class: 'empty-hint', text: hint }));
    var heads = cols.map(function (c) { return c[1]; });
    heads.push('操作');   // 固定有(🔍看原始；可寫入再加 ✏️)
    var table = U.el('table', { class: 'tracking-table' });
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, heads.map(function (h) { return U.el('th', { text: h }); }))]));
    var tb = U.el('tbody');
    rows.forEach(function (r) {
      var tds = cols.map(function (c) { return U.el('td', { text: r[c[0]] == null ? '' : String(r[c[0]]) }); });
      tds.push(opsCell(r, function () { openFindings(title, params); }));
      tb.appendChild(U.el('tr', {}, tds));
    });
    table.appendChild(tb); box.appendChild(table); makeSortable(table);
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
    var pg = s.progress || {};
    var intent = (pg.apply_ext || 0) + (pg.apply_exc || 0);   // 管理人標「要申請」的(送審中)

    host.appendChild(U.el('p', { class: 'empty-hint',
      text: '送審進度＝整個申請流程的勾稽。「要申請（送審中）」是你標了要申請、但資安 Excel 還沒反映的（對帳狀態看送審中/待查）；資安核准、備註出現後會自動移到「已申請處置中」。結論一律以資安 Excel 為主。' }));

    renderTabs(host, [
      { label: '要申請·送審中（' + intent + '）', render: function (c) { renderActionListInto(c, '要申請（送審中：我標了要申請，Excel 尚未反映）', { apply_intent: 'true' }); } },
      { label: '已申請處置中（' + total + '）', render: function (c) { renderActionListInto(c, '已申請處置中（例外／展延，Excel 已反映）', { applied: 'true' }); } },
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
    var tabs = [
      { label: '待申請（' + s.should_apply + '）', render: function (c) { renderActionListInto(c, '待申請（應提例外／展延未提）', { should_apply: 'true' }); } },
      { label: '已逾期（' + s.overdue + '）', render: function (c) { renderActionListInto(c, '已逾期', { band: '已逾期' }); } },
    ];
    // 無人負責／無到期日：有資料才顯示該頁籤(為 0 時是空的，收起來少雜訊)
    if (s.gaps.no_owner) tabs.push({ label: '無人負責（' + s.gaps.no_owner + '）', render: function (c) { renderActionListInto(c, '無人負責', { no_owner: 'true' }); } });
    if (s.gaps.no_due) tabs.push({ label: '無到期日（' + s.gaps.no_due + '）', render: function (c) { renderActionListInto(c, '無到期日', { no_due: 'true' }); } });
    renderTabs(host, tabs, 'todo');
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

  // ---- 負責人追蹤（主管角度：誰還有幾隻＋狀態分佈；可選到期範圍）----
  async function renderOwnerInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    host.appendChild(U.el('p', { class: 'empty-hint', text: '每位負責人未結案「幾隻＋狀態」。處置階段(原始／首次展延／例外管理)相加＝未結；等複掃＝自行回報做完·等資安複審(疊加)；逾期(疊加)。點負責人看他的全部。' }));
    // 到期範圍選擇：只看近期到期的
    var sel = U.el('select', { style: 'padding:6px 10px;border:1px solid #cdd5dd;border-radius:8px;font-size:14px;margin:0 0 10px' }, [
      U.el('option', { value: '', text: '到期範圍：全部' }),
      U.el('option', { value: '15', text: '15 天內到期（含逾期）' }),
      U.el('option', { value: '30', text: '30 天內到期（含逾期）' }),
      U.el('option', { value: '60', text: '60 天內到期（含逾期）' }),
      U.el('option', { value: '90', text: '90 天內到期（含逾期）' }),
    ]);
    host.appendChild(U.el('div', {}, [U.el('label', { text: '到期範圍　', style: 'font-size:14px' }), sel]));
    host.appendChild(leadToggle(function () { draw(); }));   // 含申請提前量開關(與到期倒數共用狀態)
    var box = U.el('div'); host.appendChild(box);
    var cols = [['owner', '負責人'], ['department', '部門'], ['total', '未結'], ['original', '原始'],
      ['extension', '首次展延'], ['exception', '例外管理'], ['rescan', '等複掃'], ['overdue', '逾期']];
    async function draw() {
      box.innerHTML = '';
      var lead = _dueLeadOn ? DUE_LEAD_DAYS : 0;
      var p = {}; if (sel.value) p.due_max = sel.value; if (lead) p.lead = lead;
      var rows;
      try { rows = await jget('/api/owner-summary?' + qd(p)); }
      catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
      var label = sel.value ? ('（' + sel.value + ' 天內到期，' + rows.length + ' 人）') : ('（' + rows.length + ' 人）');
      box.appendChild(headWithExport('負責人追蹤' + label, cols, function () { return rows; }));
      if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: sel.value ? '此範圍內無未結案。' : '無未結案。' })); return; }
      // 視覺排行：橫條長度＝未結數、紅段＝逾期，一眼看出誰多少(點進去看他全部)
      var barsBox = U.el('div', { style: 'margin:4px 0 16px' }); box.appendChild(barsBox);
      ownerBars(barsBox, rows, function (r) {
        var d = { owner: r.owner }; if (sel.value) d.due_max = sel.value; if (lead) d.lead = lead;
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
            if (sel.value) base.due_max = sel.value;
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
    sel.addEventListener('change', draw);
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
          if (s.change && s.change.has_prev) {
            kpiTable(c, '本週變化（本批 vs 上批匯入）', [
              { label: '上批未結', value: s.change.prev },
              { label: '本批未結', value: s.change.now },
              { label: '淨變化', value: (s.change.delta > 0 ? '+' : '') + s.change.delta, danger: s.change.delta > 0 },
              { label: '本週新增', value: s.change.new, danger: s.change.new > 0 },
              { label: '本週解決', value: s.change.resolved },
            ]);
          }
          kpiTable(c, '本期概況（未結案）', [
            { label: '未結案', value: s.unresolved },
            { label: '落後（已逾期）', value: s.overdue, danger: true, drill: function () { openFindings('落後（已逾期）', { band: '已逾期' }); } },
            { label: '如期（未逾期）', value: s.on_track },
            { label: '高風險（Critical/High）', value: s.high_risk, danger: true },
            { label: '高風險且逾期（最急）', value: s.high_risk_overdue, danger: true },
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
          var pg = s.progress || {};
          kpiTable(c, '處理進度分佈（管理人標註；結論仍以資安 Excel 為主）', [
            { label: '要申請展延', value: pg.apply_ext || 0 },
            { label: '要申請例外', value: pg.apply_exc || 0 },
            { label: '處理中（修補中）', value: pg.wip || 0 },
            { label: '等複掃（回報做完）', value: pg.rescan || 0 },
            { label: '需追查 ⚠️（說要申請/做完卻未反映）', value: pg.flagged || 0, danger: true },
          ]);
      } },
      { label: '負責人', render: function (c) {
          c.appendChild(U.el('p', { class: 'empty-hint', text: '每位負責人未結數（橫條長度＝數量，紅段＝逾期）。點負責人看他的全部。' }));
          jget('/api/owner-summary?' + qd()).then(function (rows) {
            ownerBars(c, rows, function (r) { openFindings(r.owner + ' · 全部未結', { owner: r.owner }); });
          }).catch(function () { c.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); });
      } },
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
  // 新功能小項：render=渲染進主區；action=直接動作(如下載)不切畫面
  // 架構：看(總覽,原生)／做(待辦清單+送審進度)／報(主管週報)／查(結案稽核)
  var GOV_ITEMS = [
    { key: 'byowner', label: '負責人追蹤', render: renderOwnerInto }, // 主角度：誰還有幾隻＋狀態(每格可下鑽)
    { key: 'todo', label: '待辦清單', render: renderTodoInto },     // 做：要處理的清單都在這
    { key: 'duesoon', label: '到期倒數', render: renderDueSoonInto }, // 做：依距到期天數看 30/60/90
    { key: 'cases', label: '送審進度', render: renderCasesInto },   // 做：例外/展延申請跑簽到核准
    { key: 'report', label: '主管週報', render: renderReportInto }, // 報：給主管的固定報告
    { key: 'closestat', label: '結案稽核', render: renderCloseInto }, // 查：結案驗證/浮報
    { key: 'reconcile', label: '對帳健檢', render: renderReconcileInto }, // 查：數字自我對帳(不靠AI)
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

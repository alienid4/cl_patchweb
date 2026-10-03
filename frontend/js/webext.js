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

  // 讓表格可點欄位排序（數字欄按數值、其餘按字串；再點反向）
  function makeSortable(table) {
    var ths = table.tHead ? table.tHead.rows[0].cells : [];
    for (var i = 0; i < ths.length; i++) (function (ci, th) {
      th.style.cursor = 'pointer'; th.title = '點我排序';
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
        asc = !asc;
        rows.forEach(function (r) { tb.appendChild(r); });
      });
    })(i, ths[i]);
  }

  // 下鑽：開視窗顯示 /api/findings 篩出的明細（可排序）
  async function openFindings(title, params) {
    var box = U.el('div');
    UI.openModal(title, box);
    var rows;
    try { rows = await jget('/api/findings?' + qd(Object.assign({ status: '未結案' }, params || {}))); }
    catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
    if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無資料' })); return; }
    var hint = '共 ' + rows.length + ' 筆（點欄位排序）';
    if (canWrite()) hint += '；負責人欄可點「改」重新指派（存系統、重匯不會被蓋掉）';
    box.appendChild(U.el('p', { class: 'empty-hint', text: hint }));
    var cols = [['host', '主機'], ['owner', '負責人'], ['severity', '嚴重度'], ['name', '弱點'],
      ['plugin_id', 'Plugin'], ['effective_due', '到期日'], ['overdue_days', '逾期天數'],
      ['stage', '處置階段'], ['department', '部門']];
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

  // 管理員改負責人：跳提示輸入新名字 → 存系統(疊加層)
  async function editOwner(row, done) {
    var cur = row.owner || '';
    var name = window.prompt('改負責人（' + (row.host || '') + ' / ' + (row.name || row.plugin_id || '') + '）\n原：' + cur + '\n輸入新負責人（留空＝取消覆蓋，回到 Excel 值）：', cur);
    if (name === null) return;  // 取消
    var r = await fetch('/api/findings/' + row.id + '/owner', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ owner: name })
    });
    if (!r.ok) { var e = await r.json().catch(function () { return {}; }); UI.toast('改失敗：' + (e.detail || r.status), 'error'); return; }
    var j = await r.json();
    UI.toast('已改負責人（同弱點 ' + j.updated + ' 筆）', 'success');
    if (done) done();
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
  async function renderCasesInto(host) {
    if (!host) return;
    host.innerHTML = '';
    var s, cases;
    try { s = await jget('/api/summary?' + qd()); cases = await jget('/api/cases?' + qd()); }
    catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料，請先匯入。' })); return; }
    // 行動線 / 缺口 / 新鮮度（數字可下鑽）
    host.appendChild(group('行動線與缺口示警', [
      card(s.should_apply, '應提申請未提', true, function () { openFindings('應提申請未提', { should_apply: 'true' }); }),
      card(s.gaps.no_owner, '無人負責', true, function () { openFindings('無人負責', { no_owner: 'true' }); }),
      card(s.gaps.no_due, '無到期日', true, function () { openFindings('無到期日', { no_due: 'true' }); }),
      card((s.freshness.days_ago == null ? '—' : s.freshness.days_ago), '資料距今(天)'),
    ]));
    // 管線關卡（可下鑽該關卡案件）
    var cnt = {}; PIPE.forEach(function (k) { cnt[k] = 0; });
    var suspect = 0, orphan = 0;
    cases.forEach(function (c) { if (c.status in cnt) cnt[c.status]++; if (c.suspect) suspect++; if (c.is_orphan) orphan++; });
    host.appendChild(group('申請流程管線', PIPE.map(function (k) {
      return card(cnt[k], k, k === '未申請' || k === '退回補件',
        function () { openCasesModal(k + ' 案件', { status: k }); });
    })));
    var hint = U.el('div', { class: 'scope-info' }, [U.el('span', {
      html: '可疑聲稱 <b>' + suspect + '</b>　·　來源已消失 <b>' + orphan + '</b>　·　'
        + (me.open_write ? '（目前免登入模式，可直接推進狀態）'
           : me.authenticated ? '（登入身分：' + (me.display_name || me.username) + '，可推進狀態）'
           : '（登入後可推進狀態）') })]);
    host.appendChild(hint);
    // 案件表
    if (!cases.length) { host.appendChild(U.el('p', { class: 'empty-hint', text: '無案件。' })); return; }
    var table = U.el('table', { class: 'tracking-table' });
    var thead = U.el('tr', {}, ['主機', 'Plugin', '負責人', '部門', '狀態', '來源', '操作'].map(function (h) { return U.el('th', { text: h }); }));
    table.appendChild(U.el('thead', {}, [thead]));
    var tb = U.el('tbody');
    cases.slice(0, 300).forEach(function (c) {
      var ops = U.el('td');
      if (canWrite()) {
        (NEXT[c.status] || []).forEach(function (to) {
          var b = U.el('button', { class: 'btn btn-sm', text: '→' + to, style: 'margin:1px' });
          b.addEventListener('click', function () { transition(c.id, to, function () { renderCasesInto(host); }); });
          ops.appendChild(b);
        });
        if (!(NEXT[c.status] || []).length) ops.textContent = '—';
      } else { ops.textContent = ''; }
      var stTxt = c.status + (c.suspect ? '（可疑）' : '') + (c.is_orphan ? '（已消失）' : '');
      tb.appendChild(U.el('tr', {}, [
        U.el('td', { text: c.host || '' }), U.el('td', { text: c.plugin_id || '' }),
        U.el('td', { text: c.owner || '未指派' }), U.el('td', { text: c.department || '' }),
        U.el('td', { text: stTxt }), U.el('td', { text: c.source_closed ? '已結' : '未結' }), ops,
      ]));
    });
    table.appendChild(tb);
    host.appendChild(table);
    makeSortable(table);
  }

  // 下鑽：開視窗顯示 /api/cases 某關卡的案件（可排序）
  async function openCasesModal(title, params) {
    var box = U.el('div');
    UI.openModal(title, box);
    var rows;
    try { rows = await jget('/api/cases?' + qd(params || {})); }
    catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
    if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無案件' })); return; }
    box.appendChild(U.el('p', { class: 'empty-hint', text: '共 ' + rows.length + ' 筆（點欄位排序）' }));
    var cols = [['host', '主機'], ['plugin_id', 'Plugin'], ['owner', '負責人'], ['department', '部門'], ['status', '狀態'], ['source_closed', '來源']];
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

  // ---- 缺口示警（行動線／缺口／新鮮度＋最急 Top） ----
  async function renderGapsInto(host) {
    if (!host) return;
    host.innerHTML = '';
    var s, top;
    try {
      s = await jget('/api/summary?' + qd());
      top = await jget('/api/findings?' + qd({ status: '未結案', should_apply: 'true' }));
    } catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料，請先匯入。' })); return; }
    host.appendChild(group('行動線與缺口示警', [
      card(s.should_apply, '應提申請未提', true, function () { openFindings('應提申請未提', { should_apply: 'true' }); }),
      card(s.gaps.no_owner, '無人負責', true, function () { openFindings('無人負責', { no_owner: 'true' }); }),
      card(s.gaps.no_due, '無到期日', true, function () { openFindings('無到期日', { no_due: 'true' }); }),
      card(s.due_soon, '近期到期(30天)', true, function () { openFindings('近期到期(30天)', { band: '30天內' }); }),
      card(s.overdue, '已逾期', true, function () { openFindings('已逾期', { band: '已逾期' }); }),
      card((s.freshness.days_ago == null ? '—' : s.freshness.days_ago), '資料距今(天)'),
    ]));
    top.sort(function (a, b) { return (b.overdue_days == null ? -1e9 : b.overdue_days) - (a.overdue_days == null ? -1e9 : a.overdue_days); });
    if (top.length) {
      host.appendChild(U.el('div', { class: 'panel-head' }, [U.el('h3', { text: '最急（應提申請未提，前 10）' })]));
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, ['負責人', '弱點', '嚴重度', '主機', '逾期天數', '部門'].map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      top.slice(0, 10).forEach(function (r) {
        tb.appendChild(U.el('tr', {}, [
          U.el('td', { text: r.owner || '未指派' }), U.el('td', { text: r.name || r.plugin_id || '' }),
          U.el('td', { text: r.severity || '' }), U.el('td', { text: r.host || '' }),
          U.el('td', { text: (r.overdue_days != null && r.overdue_days > 0) ? String(r.overdue_days) : '—' }),
          U.el('td', { text: r.department || '' }),
        ]));
      });
      table.appendChild(tb); host.appendChild(table); makeSortable(table);
    } else {
      host.appendChild(U.el('p', { class: 'empty-hint', text: '目前無「應提申請未提」的急件。' }));
    }
  }

  // ---- 結案統計 ----
  async function renderCloseInto(host) {
    if (!host) return;
    host.innerHTML = '';
    var s;
    try { s = await jget('/api/close-stats?' + qd()); } catch (e) { host.appendChild(U.el('p', { class: 'empty-hint', text: '尚無資料。' })); return; }
    host.appendChild(group('結案統計（快照比對）', [
      card(s.new_closed, '本期新結案'),
      card(s.source_confirmed, '來源(Excel)確認'),
      card(s.claimed_unconfirmed, '承辦聲稱未確認(可疑)', true),
    ]));
    if (s.by_closer && s.by_closer.length) {
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, [U.el('th', { text: '結案人' }), U.el('th', { text: '本期結案' })])]));
      var tb = U.el('tbody');
      s.by_closer.forEach(function (r) { tb.appendChild(U.el('tr', {}, [U.el('td', { text: r.name }), U.el('td', { text: String(r.closed) })])); });
      table.appendChild(tb); host.appendChild(table); makeSortable(table);
    }
    host.appendChild(U.el('p', { class: 'empty-hint', text: '「本期新結案」＝上期未結、這期變已結（快照比對）；「承辦聲稱未確認」＝承辦標完成但來源仍未結，可能自行浮報。' }));
  }

  // ===== 左側第二大項「承辦管線」（與「總覽」並列，綠色），底下放全部新功能 =====
  // 新功能小項：render=渲染進主區；action=直接動作(如下載)不切畫面
  var GOV_ITEMS = [
    { key: 'gaps', label: '缺口示警', render: renderGapsInto },
    { key: 'cases', label: '申請流程管線', render: renderCasesInto },
    { key: 'closestat', label: '結案統計', render: renderCloseInto },
    // 一鍵發送＝沿用原本「Email 設定」流程(開原設定視窗)，移到此、改名；原選單項已隱藏
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

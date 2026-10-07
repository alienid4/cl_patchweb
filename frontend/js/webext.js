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
      sheet_key: r.sheet, plugin_id: r.pluginId || null, name: r.name || null,
      host: (r.hostRaw !== undefined ? r.hostRaw : r.host) || null,   // 存真實主機；畫面顯示的資產名稱不存進 host
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
  // 由 snapshot 組出「workbook 物件」(不寫成二進位)；伺服器版直接餵這個進解析，省掉 XLSX.write+read
  function snapshotToWb(snap) {
    var wb = XLSX.utils.book_new();
    snap.sheets.forEach(function (s) {
      var ws = XLSX.utils.json_to_sheet(s.rows, { header: s.columns });
      XLSX.utils.book_append_sheet(wb, ws, String(s.name || 'sheet').slice(0, 31));
    });
    return wb;
  }
  // 舊路徑(相容備援)：組成 .xlsx File 走 file-input→XLSX.read
  function snapshotToFile(snap) {
    var out = XLSX.write(snapshotToWb(snap), { type: 'array', bookType: 'xlsx' });
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

  // 右上角資訊堆疊：上＝資料檔(版本)，下＝登入者／角色／目前登入人數
  var _dataFile = '';
  function _headInfo() {
    var ha = document.querySelector('.header-actions'); if (!ha) return null;
    var stack = document.getElementById('webext-headinfo');
    if (!stack) {
      stack = U.el('div', { id: 'webext-headinfo', style: 'display:flex;flex-direction:column;align-items:flex-end;gap:3px;align-self:center' });
      var df = U.el('span', { id: 'webext-datafile', title: '目前載入的資料檔（版本）',
        style: 'font-weight:700;color:#fff;background:rgba(255,255,255,.18);padding:5px 12px;border-radius:8px;font-size:14px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' });
      var who = U.el('span', { id: 'webext-whoami', style: 'color:#eaf5ef;font-size:12.5px;white-space:nowrap' });
      stack.appendChild(df); stack.appendChild(who); ha.appendChild(stack);
    }
    return stack;
  }
  function showDataFile(name) {
    if (name) _dataFile = name;
    if (!_headInfo()) return;
    var slot = document.getElementById('webext-datafile');
    slot.textContent = _dataFile ? ('📄 ' + _dataFile) : '';
    slot.title = _dataFile ? ('目前資料檔：' + _dataFile) : '';
  }
  function _roleLabel(role) {
    return role === 'super_admin' ? '最高管理員' : role === 'dept_admin' ? '部門窗口' : role === 'user' ? '一般使用者' : '使用者';
  }

  // ── snapshot 快取（IndexedDB，依匯入批次為鍵；資料沒換就不重抓 5MB）──
  var _IDB_NAME = 'wxsnap', _IDB_STORE = 'snap';
  function idbOpen() {
    return new Promise(function (res, rej) {
      if (!global.indexedDB) return rej(new Error('no idb'));
      var r = indexedDB.open(_IDB_NAME, 1);
      r.onupgradeneeded = function () { try { r.result.createObjectStore(_IDB_STORE); } catch (e) {} };
      r.onsuccess = function () { res(r.result); };
      r.onerror = function () { rej(r.error); };
    });
  }
  function idbGet(key) {
    return idbOpen().then(function (db) {
      return new Promise(function (res) {
        try { var q = db.transaction(_IDB_STORE, 'readonly').objectStore(_IDB_STORE).get(key);
          q.onsuccess = function () { res(q.result || null); }; q.onerror = function () { res(null); };
        } catch (e) { res(null); }
      });
    }).catch(function () { return null; });
  }
  function idbPutOnly(key, val) {   // 只留最新一份：先清空再寫，自動汰舊
    return idbOpen().then(function (db) {
      return new Promise(function (res) {
        try { var os = db.transaction(_IDB_STORE, 'readwrite').objectStore(_IDB_STORE);
          os.clear(); var p = os.put(val, key);
          p.onsuccess = function () { res(true); }; p.onerror = function () { res(false); };
        } catch (e) { res(false); }
      });
    }).catch(function () { return false; });
  }

  async function loadFromServer() {
    try {
      // 先問輕量 meta：資料換了沒（免下載整包）
      var meta = null;
      try { var mr = await fetch('/api/snapshot-meta'); if (mr.ok) meta = await mr.json(); } catch (e) {}
      // 快取鍵含換人指紋：系統上改了負責人／部門，總覽才會跟著更新（2026-10-06 總覽停在 300、週報 294）
      var key = (meta && meta.batch_id != null) ? ('b' + meta.batch_id + '|' + (meta.imported_at || '') + '|' + (meta.ov_sig || '')) : null;
      var snap = null;
      if (key) { try { snap = await idbGet(key); } catch (e) {} }   // 命中快取＝秒開，不下載
      if (!snap) {
        var r = await fetch('/api/snapshot');
        if (!r.ok) return false;
        snap = await r.json();
        if (key && snap && snap.sheets && snap.sheets.length) { idbPutOnly(key, snap); }   // 背景存，不 await
      }
      if (!snap || !snap.sheets || !snap.sheets.length) return false;
      showDataFile(snap.source_file);
      // 快路徑：直接把伺服器資料組成 workbook 物件餵進解析，跳過 XLSX.write+read（大量列省近 1 秒）
      if (global.App && global.App.importWorkbookObj) {
        global.App.importWorkbookObj(snapshotToWb(snap), snap.source_file);
      } else {
        feedToApp(snapshotToFile(snap), true);   // 相容備援：舊 main.js 沒有新入口時走原路
      }
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
        showDataFile(file.name);   // 上傳新檔→右上角版本即時更新
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
  // 任何登入者都可寫(範圍由後端把關：super 全部／dept_admin 限部門／user 限自己)；免登入＝可寫
  function canWrite() { return !!(me.open_write || me.authenticated); }
  function isSuper() { return !!me.is_super; }   // AD/權限設定、SMTP、資料管理只給 Super Admin
  function isAdminRole() { return !!(me.is_super || me.role === 'dept_admin'); }   // super 或 部門窗口(可發送報告)
  async function jget(u) { var r = await fetch(u); if (!r.ok) throw new Error(u + ' ' + r.status); return r.json(); }
  // 讀「左側目前選的部門」（沿用原本 #my-dept-select），承辦管線各面板都要吃它
  // 週報上方可另外指定部門／工作表（記在這台瀏覽器）。部門沒指定時沿用左側選單；左側選單一換就以左側為準。
  function _lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function _lsSet(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { } }
  var _deptPick = _lsGet('wx_dept');     // null＝跟左側；'__all__'＝全部門；其他＝部門名
  var _sheetPick = _lsGet('wx_sheet');   // null／''＝全部工作表
  function curDept() {
    if (_deptPick) return _deptPick === '__all__' ? null : _deptPick;
    var s = document.getElementById('my-dept-select'); var v = s && s.value; return (v && v !== '__all__') ? v : null;
  }
  function curSheet() { return _sheetPick || null; }
  function qd(params) {
    var d = curDept(); if (d) (params = params || {}).department = d;
    var sh = curSheet(); if (sh) (params = params || {}).sheet = sh;
    return new URLSearchParams(params || {}).toString();
  }
  document.addEventListener('change', function (e) {
    if (e.target && e.target.id === 'my-dept-select' && _deptPick) { _deptPick = null; _lsSet('wx_dept', null); }
  }, true);

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

  // 表格／匯出的欄位顯示值。逾期天數＝今天−真正到期日，後端對「還沒到期」回負數（例如例外核准到年底＝-87），
  // 直接顯示會被看成「逾期 -87 天」甚至以為算錯。只有 > 0 才是逾期，其餘一律顯示「—」（與單機版一致：未逾期不列天數）。
  function cellVal(r, key) {
    var v = r ? r[key] : null;
    // 處理進度存的值是「等複掃」，畫面／匯出顯示「結案申請中」（使用者 2026-10-06 改名，存值不動、舊資料免轉換）
    if (key === 'progress' && v === '等複掃') return '結案申請中';
    if (key === 'overdue_days') return (v != null && v > 0) ? String(v) : '—';
    return v == null ? '' : String(v);
  }

  // ⚠ 待補進度：追蹤備註有寫、處理進度沒設（2026-10-06：寫了「修補完畢」卻忘了設等複掃，狀態沒轉卻以為轉了）
  var PROGRESS_SET = ['處理中', '要申請展延', '要申請例外', '等複掃'];
  function noteNoProgress(r) { return !!(r && String(r.track_note || '').trim() && PROGRESS_SET.indexOf(r.progress) < 0); }

  // 一組弱點的處置統計（下鑽頂端與「依負責人」共用，同一套定義）。兩個維度分開看：
  //   ① Excel 官方階段：展延＝首次展延中；例外＝例外管理中（其餘是原始修補期限）
  //   ② 承辦處理進度（四類加起來＝總筆數）：尚未修補＝沒回報（維持原來狀態）；修補中＝處理中；
  //      已送審＝要申請展延／例外；結案申請中＝等複掃（使用者 2026-10-06 改名；存的值仍是「等複掃」）
  function dispoStats(rs) {
    var st = { total: rs.length, overdue: 0, notdue: 0, orig: 0, ext: 0, exc: 0, todo: 0, wip: 0, sub: 0, subext: 0, subexc: 0, rescan: 0, notewarn: 0 };
    rs.forEach(function (r) {
      if (noteNoProgress(r)) st.notewarn++;   // 提醒用旗標，不屬於三組互斥
      // 時間（互斥，相加＝總）：逾期＝已過真正到期日；未逾期＝其餘（還在期限內／無到期日）
      if (r.overdue_days != null && r.overdue_days > 0) st.overdue++;
      else st.notdue++;
      // 處置階段（互斥，相加＝總）：展延＝首次展延中；例外＝例外管理中；其餘＝原始修補（目前狀態）
      if (r.stage === '首次展延中') st.ext++;
      else if (r.stage === '例外管理中') st.exc++;
      else st.orig++;
      // 承辦進度（互斥，相加＝總）
      if (r.progress === '處理中') st.wip++;
      else if (r.progress === '等複掃') st.rescan++;
      else if (r.progress === '要申請展延' || r.progress === '要申請例外') {
        st.sub++;
        if (r.progress === '要申請展延') st.subext++; else st.subexc++;
      }
      else st.todo++;
    });
    return st;
  }

  // 統計分組定義：key→{label, 所屬組, 判定函式}；供「數字可點下鑽」與各處共用，與 dispoStats 同一套。
  // 三組各自互斥、各自相加＝總（同一批從三個角度看，不要跨組相加）：time／stage／prog
  var STAT_DEFS = {
    overdue: { label: '逾期', grp: 'time', test: function (r) { return r.overdue_days != null && r.overdue_days > 0; } },
    notdue: { label: '未逾期', grp: 'time', test: function (r) { return !(r.overdue_days != null && r.overdue_days > 0); } },
    orig: { label: '原始修補', grp: 'stage', test: function (r) { return r.stage !== '首次展延中' && r.stage !== '例外管理中'; } },
    ext: { label: '展延', grp: 'stage', test: function (r) { return r.stage === '首次展延中'; } },
    exc: { label: '例外', grp: 'stage', test: function (r) { return r.stage === '例外管理中'; } },
    todo: { label: '尚未修補', grp: 'prog', test: function (r) { return r.progress !== '處理中' && r.progress !== '等複掃' && r.progress !== '要申請展延' && r.progress !== '要申請例外'; } },
    wip: { label: '修補中', grp: 'prog', test: function (r) { return r.progress === '處理中'; } },
    sub: { label: '送審中', grp: 'prog', test: function (r) { return r.progress === '要申請展延' || r.progress === '要申請例外'; } },
    rescan: { label: '結案申請中', grp: 'prog', test: function (r) { return r.progress === '等複掃'; } },
    subext: { label: '展延送審', grp: 'prog', test: function (r) { return r.progress === '要申請展延'; } },
    subexc: { label: '例外送審', grp: 'prog', test: function (r) { return r.progress === '要申請例外'; } },
    notewarn: { label: '⚠ 待補進度', grp: 'warn', test: noteNoProgress },
  };

  // 表格儲存格：Excel 有填但備註沒有申請紀錄的展延上限／例外核准期限＝不算數，標灰並說明
  // （使用者 2026-10-05：看得出是原始資料的問題，不會誤以為已經展延）
  function cellTd(r, key) {
    var td = U.el('td', { text: cellVal(r, key) });
    var off = (key === 'first_extension_due' && r.first_extension_due && r.ext_applied === false) ||
              (key === 'exception_due' && r.exception_due && r.exc_applied === false);
    if (off) {
      td.style.color = '#b0b8b4'; td.style.textDecoration = 'line-through';
      td.title = 'Excel 有填，但備註沒有' + (key === 'exception_due' ? '例外' : '展延') + '的申請紀錄，系統不採用這個日期（請確認原始資料）';
    }
    // 一列固定一行（使用者 2026-10-06）：長文字截斷成「…」，滑鼠移上去看完整；日期、階段等短欄不折行
    var CLIP = { name: 280, remark: 170, track_note: 170, progress_state: 110, change_note: 260 };
    if (key === 'change_note') { td.style.color = '#0f5f35'; td.style.fontWeight = '600'; }
    td.style.whiteSpace = 'nowrap';
    if (CLIP[key]) {
      td.style.maxWidth = CLIP[key] + 'px'; td.style.overflow = 'hidden'; td.style.textOverflow = 'ellipsis'; td.style.textAlign = 'left';
      if (!td.title) td.title = td.textContent;
      // 點一下切換「整段顯示／截斷」（tooltip 要停住才出現，點的比較直覺）
      td.style.cursor = 'pointer';
      td.addEventListener('click', function () {
        var full = td.style.whiteSpace === 'nowrap';
        td.style.whiteSpace = full ? 'normal' : 'nowrap';
        td.style.overflow = full ? 'visible' : 'hidden';
      });
    }
    if (key === 'progress' && noteNoProgress(r)) {
      td.textContent = '⚠ 未設'; td.style.color = '#b9770e'; td.style.fontWeight = '600';
      td.title = '追蹤備註有寫（' + String(r.track_note).slice(0, 40) + '），但處理進度沒設——狀態還是「尚未修補」。點 ✏️ 補上（做完了就設「結案申請中」）';
    }
    return td;
  }

  // 分頁：tbody 裡標 wx-pgu 的列，每頁 size 筆（群組列的展開明細 _detail 跟著它顯示/隱藏）。
  // 用顯示/隱藏而不是只畫一頁：排序照全部資料排、勾選跨頁保留；總計列不標 wx-pgu＝永遠顯示。
  var PAGE_SIZE = 20;
  function pager(table, size) {
    size = size || PAGE_SIZE;
    var tb = table.tBodies[0], page = 0;
    var bar = U.el('div', { style: 'display:none;gap:10px;align-items:center;justify-content:center;margin:6px 0;font-size:14px' });
    function units() { return Array.prototype.filter.call(tb.rows, function (r) { return r.classList.contains('wx-pgu'); }); }
    function render() {
      var us = units(), n = us.length, pages = Math.max(1, Math.ceil(n / size));
      if (page >= pages) page = pages - 1;
      us.forEach(function (r, i) {
        var on = i >= page * size && i < (page + 1) * size;
        r.style.display = on ? '' : 'none';
        if (r._detail) r._detail.style.display = on ? '' : 'none';
      });
      bar.innerHTML = '';
      if (n <= size) { bar.style.display = 'none'; return; }
      bar.style.display = 'flex';
      function btn(t, to, dis) {
        var b = U.el('button', { class: 'btn btn-secondary btn-sm', text: t });
        b.disabled = dis; b.addEventListener('click', function (e) { e.stopPropagation(); page = to; render(); });
        return b;
      }
      bar.appendChild(btn('« 第一頁', 0, page === 0));
      bar.appendChild(btn('‹ 上一頁', page - 1, page === 0));
      bar.appendChild(U.el('span', { text: '第 ' + (page + 1) + ' / ' + pages + ' 頁（共 ' + n + ' 筆，每頁 ' + size + '）' }));
      bar.appendChild(btn('下一頁 ›', page + 1, page >= pages - 1));
      bar.appendChild(btn('最後一頁 »', pages - 1, page >= pages - 1));
    }
    table._afterSort = function () { page = 0; render(); };
    render();
    return bar;
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
        if (table._afterSort) table._afterSort();   // 有分頁就回到第一頁重新分
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

  // 下鑽視窗放到接近滿版（不改原檔 CSS，注入一次；只作用在掛了 .wx-drillmax 的下鑽視窗）
  function ensureDrillMaxStyle() {
    if (document.getElementById('wx-drillmax-style')) return;
    var s = document.createElement('style'); s.id = 'wx-drillmax-style';
    s.textContent = '.modal.wx-drillmax{max-width:100%;width:100%;max-height:97vh}' +
      '#modal-overlay.show:has(.wx-drillmax){padding:8px}';
    document.head.appendChild(s);
  }

  // 下鑽：開視窗顯示 /api/findings 篩出的明細（可排序、可匯出此清單）
  async function openFindings(title, params) {
    var box = U.el('div');
    var cols = [['host', '主機'], ['owner', '負責人'], ['severity', '嚴重度'], ['name', '弱點'],
      ['plugin_id', 'Plugin'], ['remediation_due', '原始期限'], ['first_extension_due', '展延上限'], ['exception_due', '例外核准期限'], ['effective_due', '到期日'], ['remark', '備註(Excel)'], ['overdue_days', '逾期天數'],
      ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
      ['department', '部門'], ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
    var curRows = [];   // 載入後填入,供「匯出」用(匯的是眼前這份子集)
    // 匯出：有勾選＝只匯出勾的那幾筆；沒勾＝匯出眼前這份（2026-10-06 使用者勾了 16 筆要匯出，卻匯出整個視窗）
    var expBtns = listExportButtons(function () {
      var ids = (typeof selIds === 'function') ? selIds() : [];
      if (ids.length) {
        var set = {}; ids.forEach(function (id) { set[id] = 1; });
        return (allRows || []).filter(function (r) { return set[r.id]; });
      }
      return curRows;
    }, title);
    var footer = U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, expBtns);   // 完整/簡易 兩顆
    UI.openModal(title, box, { footer: footer, wide: true, noBackdropClose: true });   // 寬版＋點外面不關(只 ✕／Esc)
    var rows;
    var _q = Object.assign({ status: '未結案' }, params || {}); var _stat0 = _q._stat; delete _q._stat;   // _stat＝開窗先選好某統計類（不送後端）
    var _src = _q._src || '/api/findings'; delete _q._src;   // _src＝改向別的清單來源要資料（如「與上次匯入比較」）
    try { rows = await jget(_src + '?' + qd(_q)); }
    catch (e) { box.appendChild(U.el('p', { class: 'empty-hint', text: '讀取失敗' })); return; }
    if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無資料' })); return; }
    var allRows = rows;   // 這個視窗的完整清單；rows 是套用搜尋後「眼前這份」，三種檢視、全選、匯出都吃 rows
    // 比較清單帶「變了什麼」：三種檢視的明細都在弱點後面多這一欄
    var hasNote = rows.some(function (r) { return r.change_note; });
    function withNote(cs) {
      if (!hasNote) return cs;
      var i = cs.findIndex(function (c) { return c[0] === 'name'; });
      cs = cs.slice(); cs.splice(i >= 0 ? i + 1 : 1, 0, ['change_note', '變了什麼']); return cs;
    }
    cols = withNote(cols);
    curRows = rows;
    var self = function () { openFindings(title, params); };
    function nPluginOf(rs) { var s = {}; rs.forEach(function (r) { s[(r.plugin_id || '') + '|' + (r.name || '')] = 1; }); return Object.keys(s).length; }
    ensureDrillMaxStyle();
    var _mEl = document.querySelector('#modal-overlay .modal'); if (_mEl) _mEl.classList.add('wx-drillmax');   // 下鑽放到接近滿版
    var statKey = (_stat0 && STAT_DEFS[_stat0]) ? _stat0 : null;   // 點統計數字下鑽：只顯示該類（null＝全部）
    var shown = rows;          // 眼前真正顯示的列（rows 再套 statKey）

    // ── 頂端固定區（往下捲也留在上面）：頁籤＋筆數、搜尋、批次列 ──
    // .modal-body 有 20px 內距，top 用 -20px 才會貼齊視窗頂邊
    var stickyTop = U.el('div', { style: 'position:sticky;top:-20px;z-index:5;background:#fff;margin:-20px -22px 8px;padding:8px 22px 6px;border-bottom:1px solid #e3ebe7;box-shadow:0 4px 8px -6px rgba(0,0,0,.18)' });
    box.appendChild(stickyTop);
    // 切換：依負責人(預設，追人) / 依弱點彙總 / 依主機明細；筆數放同一列右側，不再獨佔一大塊空白
    var tgl = U.el('nav', { class: 'subtabs', style: 'margin:0' });
    var bO = U.el('button', { class: 'subtab-btn active', text: '依負責人' });
    var bP = U.el('button', { class: 'subtab-btn', text: '依弱點彙總' });
    var bH = U.el('button', { class: 'subtab-btn', text: '依主機（明細）' });
    tgl.appendChild(bO); tgl.appendChild(bP); tgl.appendChild(bH);
    var sumEl = U.el('span', { style: 'color:#6b7a73;font-size:14.5px;margin-left:auto' });
    // 收合統計／到期，騰出空間給清單勾選（記在本機，下次沿用）
    var collapsed = false; try { collapsed = localStorage.getItem('wx.drillCollapse') === '1'; } catch (e) {}
    var tgBtn = U.el('button', { class: 'btn btn-secondary btn-sm', title: '收合／展開 統計與到期篩選，騰出空間勾選' });
    stickyTop.appendChild(U.el('div', { style: 'display:flex;align-items:center;gap:12px;flex-wrap:wrap' }, [tgl, sumEl, tgBtn]));
    // 處置統計：總筆數／逾期／展延／例外／修補中／已送審／待複掃（跟著搜尋結果變）
    var statsEl = U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-top:5px' });
    stickyTop.appendChild(statsEl);
    // 到期篩選（依真正到期日、今天起算），可複選。每顆是一段，互不重疊：
    //   已逾期(<0)｜14 天內(0–14)｜30 天內(15–30)｜60 天內(31–60)｜90(61–90)｜120(91–120)｜180(121–180)｜360 天內(181–360)（使用者 2026-10-06 加後四段）
    //   各段獨立：點哪段＝只選那段，再點＝取消；要累加就多點幾段；「全部」＝清掉所有選擇
    var DUE_SEG = [['overdue', '已逾期', null, -1], [14, '14 天內', 0, 14], [30, '30 天內', 15, 30], [60, '60 天內', 31, 60],
      [90, '90 天內', 61, 90], [120, '120 天內', 91, 120], [180, '180 天內', 121, 180], [360, '360 天內', 181, 360], [9999, '360 天以上', 361, 99999]];
    var dueOn = {};          // 段 key → true
    // 預設看 30 天內（含已逾期）——使用者 2026-10-06：主要目的是抓快到期的。
    // 但下鑽本身已帶到期範圍（到期倒數分桶、30/60/90 累計、逾期等）就不再疊預設，否則那一桶會被藏掉。
    var _pp = params || {};
    // 這幾類下鑽依定義就是「到期日還遠」（已申請處置中＝展延/例外期限通常在數月後、暫無需申請、高風險未結不含近期），
    // 疊預設 30 天會整批藏掉（2026-10-07：已申請處置中 69 支點進去 0 筆）→ 不疊預設
    var _farByDef = _pp.applied || _pp.not_apply || _pp.risk === 'high_only' || _pp._src;   // 比較清單也不疊預設
    if (_pp.due_min == null && _pp.due_max == null && !_pp.band && !_farByDef) { dueOn = { overdue: true, 14: true, 30: true }; }
    var dueBar = U.el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:5px' },
      [U.el('span', { text: '到期（可複選）：', style: 'color:#6b7a73;font-size:14px' })]);
    var bAll = U.el('button', { class: 'subtab-btn', text: '全部', style: 'padding:3px 12px;font-size:14px' });
    bAll.addEventListener('click', function () { dueOn = {}; paintDue(); applyFilter(); draw(); });
    dueBar.appendChild(bAll);
    var dueBtns = DUE_SEG.map(function (sg, i) {
      var b = U.el('button', { class: 'subtab-btn', text: sg[1], style: 'padding:3px 12px;font-size:14px',
        title: sg[0] === 'overdue' ? '已過真正到期日' : ('距到期 ' + sg[2] + '–' + sg[3] + ' 天') });
      b.addEventListener('click', function () {
        if (dueOn[sg[0]]) delete dueOn[sg[0]];   // 已亮→取消；沒亮→加選（各段獨立，可複選累加）
        else dueOn[sg[0]] = true;
        paintDue(); applyFilter(); draw();
      });
      dueBar.appendChild(b); return b;
    });
    function paintDue() {
      var any = Object.keys(dueOn).length > 0;
      bAll.classList.toggle('active', !any);
      dueBtns.forEach(function (b, i) { b.classList.toggle('active', !!dueOn[DUE_SEG[i][0]]); });
    }
    paintDue();
    stickyTop.appendChild(dueBar);
    function applyCollapse() {
      statsEl.style.display = collapsed ? 'none' : 'flex';
      dueBar.style.display = collapsed ? 'none' : 'flex';
      tgBtn.textContent = collapsed ? '展開統計／到期 ▾' : '收合統計／到期 ▴';
    }
    tgBtn.addEventListener('click', function () {
      collapsed = !collapsed; try { localStorage.setItem('wx.drillCollapse', collapsed ? '1' : '0'); } catch (e) {}
      applyCollapse();
    });
    applyCollapse();
    var _today = (function () { var d = new Date(); d.setHours(0, 0, 0, 0); return d; })();
    function daysToDue(r) {
      if (!r.effective_due) return null;
      var p = String(r.effective_due).slice(0, 10).split('-');
      var d = new Date(+p[0], +p[1] - 1, +p[2]);
      return Math.round((d - _today) / 86400000);
    }
    function passDue(r) {
      if (!Object.keys(dueOn).length) return true;
      var n = daysToDue(r);
      if (n == null) return false;
      return DUE_SEG.some(function (sg) {
        if (!dueOn[sg[0]]) return false;
        return sg[0] === 'overdue' ? n < 0 : (n >= sg[2] && n <= sg[3]);
      });
    }

    // 搜尋：可一次貼一整欄 IP（從 Excel 複製，換行／空白／逗號分隔都行）
    //   IP → 跟主機欄「完全相同」才算（避免 .2 誤中 .22）；其他字 → 主機／負責人／弱點／Plugin／部門／處理進度／追蹤備註／預計完成日 包含即中
    // 用 textarea 不用 input：單行 input 貼上時會把換行直接吃掉，Excel 一欄 IP 會黏成一串
    var qInput = U.el('textarea', { rows: '1', placeholder: '搜尋：可直接貼上 Excel 一整欄 IP（多筆）；或輸入負責人、弱點、Plugin、部門、處理進度（如 處理中）、追蹤備註',
      style: 'flex:1;min-width:280px;padding:7px 10px;border:1px solid #cfd8d3;border-radius:6px;font-size:15px;resize:vertical;max-height:120px;line-height:1.4;font-family:inherit' });
    function fitQ() { qInput.style.height = 'auto'; qInput.style.height = Math.min(qInput.scrollHeight + 2, 120) + 'px'; }
    var qClear = U.el('button', { class: 'btn btn-secondary btn-sm', text: '清除' });
    var qInfo = U.el('div', { style: 'font-size:14px;margin-top:4px;color:#6b7a73' });
    stickyTop.appendChild(U.el('div', { style: 'display:flex;gap:8px;align-items:center;margin-top:10px' }, [qInput, qClear]));
    stickyTop.appendChild(qInfo);
    var batchHost = U.el('div', { style: 'margin-top:8px' }); stickyTop.appendChild(batchHost);
    var listBox = U.el('div'); box.appendChild(listBox);

    var IP_RE = /^\d{1,3}(\.\d{1,3}){3}$/;
    function hostIps(h) { return String(h || '').match(/\d{1,3}(?:\.\d{1,3}){3}/g) || []; }
    function applyFilter() {
      var toks = qInput.value.split(/[\s,;，；、]+/).map(function (t) { return t.trim(); }).filter(Boolean);
      var uniq = {}; toks = toks.filter(function (t) { var k = t.toLowerCase(); if (uniq[k]) return false; uniq[k] = 1; return true; });
      var base = allRows.filter(passDue);
      if (!toks.length) { rows = base; qInfo.textContent = ''; return; }
      var ips = toks.filter(function (t) { return IP_RE.test(t); });
      var words = toks.filter(function (t) { return !IP_RE.test(t); }).map(function (t) { return t.toLowerCase(); });
      var ipSet = {}; ips.forEach(function (ip) { ipSet[ip] = 1; });
      var hitTok = {};
      rows = base.filter(function (r) {
        var ok = false;
        hostIps(r.host).forEach(function (ip) { if (ipSet[ip]) { ok = true; hitTok[ip] = 1; } });
        if (words.length) {
          var hay = [r.host, r.owner, r.name, r.plugin_id, r.department, r.progress, cellVal(r, 'progress'), r.progress_state, r.track_note, r.target_date, r.stage].map(function (v) { return String(v == null ? '' : v).toLowerCase(); }).join('\n');
          words.forEach(function (w) { if (hay.indexOf(w) >= 0) { ok = true; hitTok[w] = 1; } });
        }
        return ok;
      });
      // 查無的要講出來：「沒找到」跟「沒查」看起來一樣是 0，必須分得出來
      var miss = ips.filter(function (ip) { return !hitTok[ip]; }).concat(words.filter(function (w) { return !hitTok[w]; }));
      qInfo.innerHTML = '';
      qInfo.appendChild(U.el('span', { text: '查了 ' + toks.length + ' 個條件，符合 ' + rows.length + ' 筆。' }));
      if (miss.length) {
        qInfo.appendChild(U.el('span', { style: 'color:#c0392b;margin-left:8px',
          text: '查無 ' + miss.length + ' 個：' + miss.slice(0, 30).join('、') + (miss.length > 30 ? ' …' : '') +
            '（這個視窗只含目前的篩選範圍，例如未結案、所選部門）' }));
      }
    }
    var _qTimer = null;
    qInput.addEventListener('input', function () { fitQ(); clearTimeout(_qTimer); _qTimer = setTimeout(function () { applyFilter(); draw(); }, 200); });
    qInput.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) e.preventDefault(); });   // Enter 不換行（貼上的換行照收）
    qClear.addEventListener('click', function () { qInput.value = ''; fitQ(); applyFilter(); draw(); qInput.focus(); });
    var mode = 'byowner';
    function setMode(m, btn) { mode = m; [bO, bP, bH].forEach(function (b) { b.classList.remove('active'); }); btn.classList.add('active'); draw(); }
    bO.addEventListener('click', function () { setMode('byowner', bO); });
    bP.addEventListener('click', function () { setMode('byplugin', bP); });
    bH.addEventListener('click', function () { setMode('byhost', bH); });

    // ── 跨檢視共用的批次勾選（依負責人／依弱點／依主機都能勾，共用一組 selected）──
    var writable = canWrite();
    var selected = {};          // finding id -> true
    var cntEl = null;           // 每次 draw 重建的「已選 N 筆」標籤
    function selIds() { return Object.keys(selected).filter(function (k) { return selected[k]; }).map(Number); }
    function updCount() {
      var ids = selIds(), n = ids.length;
      // 搜尋換了範圍，先前勾的不會自動取消；不在眼前的要講出來，免得批次動作改到看不到的那幾筆
      var vis = {}; shown.forEach(function (r) { vis[r.id] = 1; });
      var hidden = ids.filter(function (id) { return !vis[id]; }).length;
      if (cntEl) {
        cntEl.textContent = '已選 ' + n + ' 筆' + (hidden ? '（其中 ' + hidden + ' 筆不在目前篩選結果中）' : '');
        cntEl.style.color = hidden ? '#c0392b' : '';
      }
      // 匯出鈕跟著勾選改字：看得出這次會匯出什麼
      if (expBtns) {
        expBtns[0].textContent = n ? ('完整匯出已選 ' + n + ' 筆 (CSV)') : '完整匯出 (CSV)';
        expBtns[1].textContent = n ? ('簡易匯出已選 ' + n + ' 筆 (CSV)') : '簡易匯出 (CSV)';
        expBtns.forEach(function (b) { b.classList.toggle('btn-primary', !!n); b.classList.toggle('btn-secondary', !n); });
      }
    }
    function rowCb(id) {
      var cb = U.el('input', { type: 'checkbox', class: 'wx-rowcb' });
      cb.checked = !!selected[id];
      cb.addEventListener('change', function () { selected[id] = cb.checked; updCount(); });
      return cb;
    }
    function groupCb(ids) {   // 群組勾選框：勾一個群組＝選到它底下全部
      var cb = U.el('input', { type: 'checkbox', class: 'wx-gcb' });
      cb.checked = ids.length > 0 && ids.every(function (id) { return selected[id]; });
      cb.addEventListener('click', function (e) { e.stopPropagation(); });   // 不要觸發整列的展開
      cb.addEventListener('change', function () { ids.forEach(function (id) { selected[id] = cb.checked; }); updCount(); });
      return cb;
    }
    // 有搜尋（合計 300 筆內）或只剩一組時自動展開，搜到的那幾筆直接看得到（2026-10-06 使用者：搜尋後下方沒出現）
    function autoExpand() {
      var searching = !!String(qInput.value || '').trim();
      var nGroups = 0, seen = {};
      shown.forEach(function (r) { var g = mode === 'byplugin' ? ((r.plugin_id || '') + '|' + (r.name || '')) : (((r.owner || '').trim()) || '— 未指派'); if (!seen[g]) { seen[g] = 1; nGroups++; } });
      return (searching && shown.length <= 300) || nGroups === 1;
    }
    // 展開明細的表頭：「選」欄是勾選框＝只選這一組（這個負責人／這個弱點底下）的列，不會選到別人
    function innerHead(icols, grp, inner) {
      var tr = U.el('tr', {});
      if (writable) {
        var ids = grp.map(function (x) { return x.id; });
        var hcb = U.el('input', { type: 'checkbox', title: '只選這一組的 ' + ids.length + ' 筆（含其他頁；不會選到其他負責人）' });
        hcb.checked = ids.length > 0 && ids.every(function (id) { return selected[id]; });
        hcb.addEventListener('click', function (e) { e.stopPropagation(); });
        hcb.addEventListener('change', function () {
          ids.forEach(function (id) { selected[id] = hcb.checked; });
          inner.querySelectorAll('tbody .wx-rowcb').forEach(function (cb) { cb.checked = hcb.checked; });
          updCount();
        });
        tr.appendChild(U.el('th', { style: 'white-space:nowrap' }, [hcb, U.el('span', { text: ' 選', style: 'font-size:12px' })]));
      }
      icols.forEach(function (c) { tr.appendChild(U.el('th', { text: c[1] })); });
      tr.appendChild(U.el('th', { text: '操作' }));
      return U.el('thead', {}, [tr]);
    }
    function batchBar() {
      ensureLists();
      cntEl = U.el('span', { text: '已選 0 筆', style: 'font-weight:600;min-width:72px' });
      function onDone() { self(); }
      var bStatus = U.el('button', { class: 'btn btn-primary btn-sm', text: '批次改狀態' });
      var bAttach = U.el('button', { class: 'btn btn-secondary btn-sm', text: '批次上傳佐證' });
      bStatus.addEventListener('click', function () { var ids = selIds(); if (!ids.length) { UI.toast('請先勾選項目', 'error'); return; } openBatchStatus(ids, onDone); });
      bAttach.addEventListener('click', function () { var ids = selIds(); if (!ids.length) { UI.toast('請先勾選項目', 'error'); return; } openBatchAttach(ids, onDone); });
      var bSelAll = U.el('button', { class: 'btn btn-secondary btn-sm', text: '全選全部（' + shown.length + ' 筆）',
        title: '選目前畫面上全部負責人的 ' + shown.length + ' 筆。只要選某一個人的：展開他，勾明細表頭的「選」' });
      var bSelNone = U.el('button', { class: 'btn btn-secondary btn-sm', text: '全不選' });
      bSelAll.addEventListener('click', function () { shown.forEach(function (r) { selected[r.id] = true; }); draw(); });
      bSelNone.addEventListener('click', function () { selected = {}; draw(); });
      return U.el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:nowrap;overflow-x:auto;padding:6px 12px;background:#f0f6f3;border:1px solid #cfe3d8;border-radius:8px', title: '三種檢視都能勾（勾「弱點」或「負責人」＝選到其底下全部）→ 一次改狀態或掛同一份佐證' }, [
        bSelAll, bSelNone, cntEl, bStatus, bAttach,
        U.el('span', { class: 'empty-hint', style: 'margin:0;white-space:nowrap', text: '勾負責人／弱點＝選其底下全部' }),
      ]);
    }

    // 群組列的展開明細，可篩成只看某一類（key＝STAT_DEFS 的鍵，null＝全部）。
    // 點群組列＝展開／收合全部；點列上的數字＝只看那一類，再點同一個數字收合。
    function filterableDetail(grp, icols, colspan, onState) {
      var box = U.el('td', { colspan: String(colspan), style: 'background:#f6f8f7;padding:6px' });
      var detail = U.el('tr', { class: 'hidden' }, [box]);
      var cur = null, built = false;
      function fill(key) {
        cur = key; built = true;
        var rows = key ? grp.filter(STAT_DEFS[key].test) : grp;
        box.innerHTML = '';
        if (key) {
          var all = U.el('button', { class: 'btn btn-sm', text: '顯示全部 ' + grp.length + ' 筆' });
          all.addEventListener('click', function (e) { e.stopPropagation(); fill(null); });
          box.appendChild(U.el('div', { style: 'display:flex;gap:10px;align-items:center;margin:0 0 6px 2px;font-size:13.5px' }, [
            U.el('span', { text: '只看「' + STAT_DEFS[key].label + '」' + rows.length + ' 筆', style: 'font-weight:700;color:#0f5f35' }), all]));
        }
        var inner = U.el('table', { class: 'tracking-table', style: 'margin:0' });
        inner.appendChild(innerHead(icols, rows, inner));
        var itb = U.el('tbody');
        rows.forEach(function (r) {
          var tds = writable ? [U.el('td', {}, [rowCb(r.id)])] : [];
          icols.forEach(function (c) { tds.push(cellTd(r, c[0])); });
          tds.push(opsCell(r, self));
          itb.appendChild(U.el('tr', { class: 'wx-pgu' }, tds));
        });
        inner.appendChild(itb); makeSortable(inner);
        box.appendChild(inner); box.appendChild(pager(inner));
      }
      function isOpen() { return !detail.classList.contains('hidden'); }
      function open(key) { if (!built || cur !== key) fill(key); detail.classList.remove('hidden'); onState(true); }
      function close() { detail.classList.add('hidden'); onState(false); }
      return {
        detail: detail, open: open,
        toggleAll: function () { if (isOpen() && cur === null) close(); else open(null); },
        wireCell: function (td, key) {
          if (!td || td.textContent === '—' || td.textContent === '0') return;
          td.style.cursor = 'pointer'; td.style.textDecoration = 'underline dotted';
          td.title = '點我展開，只看「' + STAT_DEFS[key].label + '」';
          td.addEventListener('click', function (e) {
            e.stopPropagation();
            if (isOpen() && cur === key) close(); else open(key);
          });
        },
      };
    }

    // 依負責人：誰還有幾支＋其中逾期(主角度，追人不追 IP)；點名字展開他的明細
    // 欄位分三段（直線＋表頭標題）：時間(旗標逾期)｜處置階段 原始修補/展延/例外(互斥)｜承辦進度 尚未修補/修補中/送審中/待複掃(互斥)
    function drawOwner() {
      var groups = {};
      shown.forEach(function (r) { var k = ((r.owner || '').trim()) || '— 未指派'; (groups[k] = groups[k] || []).push(r); });
      var keys = Object.keys(groups).sort(function (a, b) { return groups[b].length - groups[a].length; });
      var BLH = ';border-left:2px solid #1a7f4b', BLB = ';border-left:2px solid #e3ebe7', BLT = ';border-left:2px solid #ccd9d1';
      var lead = (writable ? 1 : 0) + 3;   // 選?＋負責人＋部門＋總筆數
      var table = U.el('table', { class: 'tracking-table' });
      // 第一列：分組標題（時間／處置階段／承辦進度）
      var GH = ';color:#fff;font-weight:700;font-size:13px;background:#0f5f35';   // 分組標題：白字＋深綠底（彩字在綠底上看不到）
      var ghr = U.el('tr', {}, [
        U.el('th', { colspan: String(lead), style: GH.slice(1) }),
        U.el('th', { text: '時間（互斥）', colspan: '2', style: GH.slice(1) + ';border-left:2px solid #fff' }),
        U.el('th', { text: '承辦進度（互斥）', colspan: '5', style: GH.slice(1) + ';border-left:2px solid #fff' }),
        U.el('th', { style: GH.slice(1) }),
      ]);
      // 第二列：欄名
      function th(t, bl) { return U.el('th', { text: t, style: bl || '' }); }
      var hr = U.el('tr', {}, (writable ? [th('選')] : []).concat([
        th('負責人'), th('部門'), th('總筆數'),
        th('逾期', BLH.slice(1)), th('未逾期'),
        th('尚未修補', BLH.slice(1)), th('修補中'), th('展延送審'), th('例外送審'), th('結案申請中'),
        th('附件')]));
      table.appendChild(U.el('thead', {}, [ghr, hr]));
      var tb = U.el('tbody');
      var icols = [['host', '主機'], ['severity', '嚴重度'], ['name', '弱點'], ['plugin_id', 'Plugin'],
        ['remediation_due', '原始期限'], ['first_extension_due', '展延上限'], ['exception_due', '例外核准期限'], ['effective_due', '到期日'], ['remark', '備註(Excel)'], ['overdue_days', '逾期天數'],
        ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
        ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
      icols = withNote(icols);
      var colspanAll = lead + 2 + 5 + 1;   // 全表欄數（給明細展開列用）：時間2＋承辦5＋附件1
      function numTd(n, extra) { return U.el('td', { text: n ? String(n) : '—', style: (n ? 'font-weight:600' : 'color:#9aa5a0') + (extra || '') }); }
      keys.forEach(function (k) {
        var grp = groups[k], r0 = grp[0];
        var od = grp.filter(function (x) { return x.overdue_days != null && x.overdue_days > 0; }).length;
        var att = grp.reduce(function (s, x) { return s + (x.att_count || 0); }, 0);
        var ds = dispoStats(grp);
        var nameTd = U.el('td', { text: '▸ ' + k, style: 'font-weight:600;color:#1a7f4b;cursor:pointer;text-align:left;min-width:120px' });
        var cells = [];
        if (writable) cells.push(U.el('td', {}, [groupCb(grp.map(function (x) { return x.id; }))]));
        cells = cells.concat([nameTd,
          U.el('td', { text: r0.department || '' }),
          U.el('td', { text: String(grp.length), style: 'font-weight:700' }),
          U.el('td', { text: od ? String(od) : '—', style: (od ? 'color:#c0392b;font-weight:600' : 'color:#9aa5a0') + BLB }),
          numTd(ds.notdue),
          U.el('td', { text: ds.todo ? String(ds.todo) : '—', style: (ds.todo ? 'font-weight:600;color:#b9770e' : 'color:#9aa5a0') + BLB }),
          numTd(ds.wip), numTd(ds.subext), numTd(ds.subexc), numTd(ds.rescan),
          U.el('td', { text: att ? ('📎' + att) : '' })]);
        var head = U.el('tr', { style: 'cursor:pointer' }, cells);
        var fd = filterableDetail(grp, icols, colspanAll, function (open) { nameTd.textContent = (open ? '▾ ' : '▸ ') + k; });
        // 數字格可點＝展開時只看那一類（使用者 2026-10-06：點 31 就只看那 31 筆）
        var off = writable ? 1 : 0;
        [['overdue', 3], ['notdue', 4], ['todo', 5], ['wip', 6], ['subext', 7], ['subexc', 8], ['rescan', 9]].forEach(function (p) {
          fd.wireCell(cells[off + p[1]], p[0]);
        });
        head.addEventListener('click', fd.toggleAll);
        if (autoExpand()) fd.open(null);
        head.classList.add('wx-pgu'); head._detail = fd.detail;
        tb.appendChild(head); tb.appendChild(fd.detail);
      });
      // 總計列（表尾；每欄加總＝目前顯示的全部）
      var T = dispoStats(shown);
      var attT = shown.reduce(function (s, x) { return s + (x.att_count || 0); }, 0);
      function totTd(n, extra, danger) { return U.el('td', { text: String(n), style: 'font-weight:700' + (danger && n ? ';color:#c0392b' : '') + (extra || '') }); }
      var totCells = [];
      if (writable) totCells.push(U.el('td', {}));
      totCells = totCells.concat([
        U.el('td', { text: '總計', style: 'font-weight:700' }),
        U.el('td', {}),
        U.el('td', { text: String(T.total), style: 'font-weight:700' }),
        totTd(T.overdue, BLT, true), totTd(T.notdue),
        totTd(T.todo, BLT), totTd(T.wip), totTd(T.subext), totTd(T.subexc), totTd(T.rescan),
        U.el('td', { text: attT ? ('📎' + attT) : '', style: 'font-weight:700' })]);
      tb.appendChild(U.el('tr', { style: 'background:#f0f6f3;border-top:2px solid #1a7f4b' }, totCells));
      table.appendChild(tb); listBox.appendChild(table); listBox.appendChild(pager(table));
    }

    function drawHost() {
      var heads = (writable ? ['選'] : []).concat(cols.map(function (c) { return c[1]; })); heads.push('操作');
      var table = U.el('table', { class: 'tracking-table' });
      table.appendChild(U.el('thead', {}, [U.el('tr', {}, heads.map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      shown.forEach(function (r) {
        var tds = [];
        if (writable) tds.push(U.el('td', {}, [rowCb(r.id)]));
        cols.forEach(function (c) {
          var td = cellTd(r, c[0]);
          tds.push(td);
        });
        tds.push(opsCell(r, self));
        tb.appendChild(U.el('tr', { class: 'wx-pgu' }, tds));
      });
      table.appendChild(tb); listBox.appendChild(table); listBox.appendChild(pager(table));
    }
    // 依弱點彙總的排序狀態（點表頭切換；預設台數多→少）
    var SEV_RANK = { Critical: 4, High: 3, Medium: 2, Low: 1 };
    var _pSort = { key: 'count', dir: -1 };
    function drawPlugin() {
      var groups = {};
      shown.forEach(function (r) { var k = (r.plugin_id || '') + '|' + (r.name || ''); (groups[k] = groups[k] || []).push(r); });
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
          stage: r0.stage || '', plugin: r0.plugin_id || '', ds: dispoStats(grp) };
      });
      // 承辦進度各類筆數攤平到聚合值上，表頭可直接點來排序
      var PROG_KEYS = ['todo', 'wip', 'subext', 'subexc', 'rescan'];
      aggs.forEach(function (a) { PROG_KEYS.forEach(function (k) { a[k] = a.ds[k]; }); });
      aggs.sort(function (a, b) {
        var kf = _pSort.key, va = a[kf], vb = b[kf];
        if (va < vb) return -1 * _pSort.dir; if (va > vb) return 1 * _pSort.dir;
        return b.count - a.count;   // 同值再以台數多者在前
      });
      var table = U.el('table', { class: 'tracking-table' });
      // 可排序表頭：[顯示字, 排序鍵]；點擊切換升降序
      var HEADS = [['弱點', 'name'], ['Plugin', 'plugin'], ['嚴重度', 'sev'], ['台數', 'count'],
        ['原始期限', 'origMin'], ['到期日', 'effMin'], ['處置階段', 'stage'], ['其中逾期', 'od'],
        ['尚未修補', 'todo'], ['修補中', 'wip'], ['展延送審', 'subext'], ['例外送審', 'subexc'], ['結案申請中', 'rescan']];
      var NUM_SORT = ['count', 'sev', 'od'].concat(PROG_KEYS);   // 數字欄預設多→少
      var BLH = 'border-left:2px solid #1a7f4b', BLB = ';border-left:2px solid #e3ebe7';
      // 第一列分組標題（跟「依負責人」同一套：承辦進度互斥，五欄相加＝台數）
      var GH = 'color:#fff;font-weight:700;font-size:13px;background:#0f5f35';
      var ghr = U.el('tr', {}, [
        U.el('th', { colspan: String(HEADS.length - PROG_KEYS.length + (writable ? 1 : 0)), style: GH }),
        U.el('th', { text: '承辦進度（互斥）', colspan: String(PROG_KEYS.length), style: GH + ';border-left:2px solid #fff' })]);
      var htr = U.el('tr', {});
      if (writable) htr.appendChild(U.el('th', { text: '選' }));
      HEADS.forEach(function (h) {
        var arrow = _pSort.key === h[1] ? (_pSort.dir === 1 ? ' ▲' : ' ▼') : '';
        var th = U.el('th', { text: h[0] + arrow, style: 'cursor:pointer;user-select:none' + (h[1] === 'todo' ? ';' + BLH : '') });
        th.title = '點擊依此欄排序';
        th.addEventListener('click', function () {
          if (_pSort.key === h[1]) _pSort.dir *= -1;
          else { _pSort.key = h[1]; _pSort.dir = NUM_SORT.indexOf(h[1]) >= 0 ? -1 : 1; }
          draw();
        });
        htr.appendChild(th);
      });
      table.appendChild(U.el('thead', {}, [ghr, htr]));
      function numTd(n, extra) { return U.el('td', { text: n ? String(n) : '—', style: (n ? 'font-weight:600' : 'color:#9aa5a0') + (extra || '') }); }
      function progTds(ds) {
        return [U.el('td', { text: ds.todo ? String(ds.todo) : '—', style: (ds.todo ? 'font-weight:600;color:#b9770e' : 'color:#9aa5a0') + BLB }),
          numTd(ds.wip), numTd(ds.subext), numTd(ds.subexc), numTd(ds.rescan)];
      }
      var pColspan = HEADS.length + (writable ? 1 : 0);
      var tb = U.el('tbody');
      var icols = [['host', '主機'], ['remediation_due', '原始期限'], ['first_extension_due', '展延上限'], ['exception_due', '例外核准期限'], ['effective_due', '到期日'], ['remark', '備註(Excel)'], ['overdue_days', '逾期天數'],
        ['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
        ['target_date', '預計完成日'], ['track_note', '追蹤備註']];
      icols = withNote(icols);
      aggs.forEach(function (a) {
        var grp = a.grp, r0 = a.r0;
        // 一列一行：弱點名稱太長截斷，滑鼠移上去看完整
        var nameTd = U.el('td', { text: '▸ ' + a.name, title: a.name, style: 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:320px;font-weight:600;color:#1a7f4b;cursor:pointer;min-width:240px;text-align:left' });
        var pcells = [];
        if (writable) pcells.push(U.el('td', {}, [groupCb(grp.map(function (x) { return x.id; }))]));
        pcells = pcells.concat([nameTd,
          U.el('td', { text: a.plugin }), U.el('td', { text: r0.severity || '' }),
          U.el('td', { text: String(a.count), style: 'font-weight:700' }),
          U.el('td', { text: a.origTxt }), U.el('td', { text: a.effTxt }), U.el('td', { text: a.stage }),
          U.el('td', { text: a.od ? String(a.od) : '—', style: a.od ? 'color:#c0392b;font-weight:600' : '' })].concat(progTds(a.ds)));
        pcells.forEach(function (td) { td.style.whiteSpace = 'nowrap'; });   // 日期、階段不折行
        var head = U.el('tr', { style: 'cursor:pointer' }, pcells);
        // 展開：這個弱點影響的主機；展開時名稱整段顯示（折行），收合回一行截斷（使用者 2026-10-06：點了要看得到全名）
        function nameState(open) { nameTd.textContent = (open ? '▾ ' : '▸ ') + a.name; nameTd.style.whiteSpace = open ? 'normal' : 'nowrap'; }
        var fd = filterableDetail(grp, icols, pColspan, nameState);
        // 其中逾期＋承辦進度五欄可點＝只看那一類
        var off = writable ? 1 : 0;
        [['overdue', 7], ['todo', 8], ['wip', 9], ['subext', 10], ['subexc', 11], ['rescan', 12]].forEach(function (p) {
          fd.wireCell(pcells[off + p[1]], p[0]);
        });
        head.addEventListener('click', fd.toggleAll);
        if (autoExpand()) fd.open(null);
        head.classList.add('wx-pgu'); head._detail = fd.detail;
        tb.appendChild(head); tb.appendChild(fd.detail);
      });
      // 總計列：台數＝目前顯示總筆數、其中逾期＝逾期總數（與頂端統計一致）
      var pOd = shown.filter(function (x) { return x.overdue_days != null && x.overdue_days > 0; }).length;
      var ptc = [];
      if (writable) ptc.push(U.el('td', {}));
      ptc = ptc.concat([
        U.el('td', { text: '總計', style: 'font-weight:700' }),
        U.el('td', {}), U.el('td', {}),
        U.el('td', { text: String(shown.length), style: 'font-weight:700' }),
        U.el('td', {}), U.el('td', {}), U.el('td', {}),
        U.el('td', { text: pOd ? String(pOd) : '—', style: 'font-weight:700' + (pOd ? ';color:#c0392b' : '') })]);
      var PT = dispoStats(shown);
      PROG_KEYS.forEach(function (k, i) { ptc.push(U.el('td', { text: String(PT[k]), style: 'font-weight:700' + (i === 0 ? BLB : '') })); });
      tb.appendChild(U.el('tr', { style: 'background:#f0f6f3;border-top:2px solid #1a7f4b' }, ptc));
      table.appendChild(tb); listBox.appendChild(table); listBox.appendChild(pager(table));
    }
    // 統計分組顯示（時間旗標／處置階段互斥／承辦進度互斥），每個數字可點＝下鑽只看那類
    var STAT_GROUPS = [
      { title: '時間（互斥）', color: '#a3342d', keys: ['overdue', 'notdue'] },
      { title: '承辦進度（互斥）', color: '#1a7f4b', keys: ['todo', 'wip', 'subext', 'subexc', 'rescan'] },
      { title: '提醒', color: '#b9770e', keys: ['notewarn'], hideZero: true },   // 備註有寫、進度沒設（另一把尺，不跟上面相加）
    ];
    function renderStats(st) {
      statsEl.innerHTML = '';
      // 總筆數
      statsEl.appendChild(U.el('span', { style: 'display:inline-flex;gap:5px;align-items:baseline;margin-right:4px' }, [
        U.el('span', { text: '總筆數', style: 'color:#6b7a73;font-size:13.5px' }),
        U.el('b', { text: String(st.total), style: 'font-size:17px;color:#1a7f4b' })]));
      STAT_GROUPS.forEach(function (g) {
        if (g.hideZero && !g.keys.some(function (k) { return st[k] || statKey === k; })) return;
        statsEl.appendChild(U.el('span', { style: 'width:1px;height:22px;background:#cfd8d3;margin:0 2px' }));
        statsEl.appendChild(U.el('span', { text: g.title, style: 'color:' + g.color + ';font-size:12px;font-weight:600;align-self:center' }));
        g.keys.forEach(function (key) {
          var active = statKey === key;
          var chip = U.el('span', { title: '點我只看「' + STAT_DEFS[key].label + '」（再點取消）',
            style: 'display:inline-flex;gap:4px;align-items:baseline;padding:3px 10px;border:1px solid ' + (active ? g.color : '#dfe8e3') + ';border-radius:999px;cursor:pointer;background:' + (active ? g.color : '#f7faf8') }, [
            U.el('span', { text: STAT_DEFS[key].label, style: 'font-size:13.5px;color:' + (active ? '#fff' : '#6b7a73') }),
            U.el('b', { text: String(st[key]), style: 'font-size:15px;color:' + (active ? '#fff' : (key === 'overdue' ? '#c0392b' : key === 'notewarn' ? '#b9770e' : '#2a3430')) })]);
          chip.addEventListener('click', function () { statKey = (statKey === key) ? null : key; draw(); });
          statsEl.appendChild(chip);
        });
      });
      if (statKey) {
        statsEl.appendChild(U.el('button', { class: 'btn btn-secondary btn-sm', style: 'margin-left:6px', text: '清除下鑽（' + STAT_DEFS[statKey].label + '）',
          onclick: function () { statKey = null; draw(); } }));
      }
    }
    function draw() {
      listBox.innerHTML = '';
      batchHost.innerHTML = '';
      var st = dispoStats(rows);                 // 統計一律以「搜尋/到期篩選後」為母數，數字穩定不跳
      shown = statKey ? rows.filter(STAT_DEFS[statKey].test) : rows;   // 點了統計數字就只顯示那類
      curRows = shown;                            // 匯出＝眼前這份
      var extra = statKey ? ('　·　下鑽：' + STAT_DEFS[statKey].label + ' ' + shown.length + ' 筆') : '';
      sumEl.textContent = (rows.length < allRows.length ? '篩選結果 ' + rows.length + ' / ' : '共 ') +
        allRows.length + ' 筆、' + nPluginOf(rows) + ' 種弱點' + extra;
      renderStats(st);
      if (writable) batchHost.appendChild(batchBar());
      if (!shown.length) listBox.appendChild(U.el('p', { class: 'empty-hint', text: statKey ? '這一類目前沒有項目' : '沒有符合搜尋的項目' }));
      else (mode === 'byowner' ? drawOwner : mode === 'byplugin' ? drawPlugin : drawHost)();
      updCount();
    }
    // 保險：預設的到期篩選若把這份清單整批濾光（有資料卻 0 筆），自動改看全部，不讓人以為沒資料
    if (Object.keys(dueOn).length && allRows.length && !allRows.some(passDue)) { dueOn = {}; paintDue(); }
    applyFilter();   // 先套預設的到期篩選再畫
    draw();
  }

  var PROGRESS_OPTS = [['', '（不變）'], ['處理中', '處理中'], ['要申請展延', '要申請展延'],
    ['要申請例外', '要申請例外'], ['等複掃', '結案申請中'], ['__clear__', '清除進度']];

  // 批次改狀態：對已勾選 N 筆套同一組疊加欄（負責人／部門／進度／預計完成日／備註）。詳細預覽後才套。
  function openBatchStatus(ids, onDone) {
    ensureLists();
    var box = U.el('div');
    box.appendChild(U.el('p', { class: 'empty-hint', text: '只勾「要改」的欄位才會套用；其餘保持不動。已選 ' + ids.length + ' 筆。' }));
    var FS = 'padding:6px 10px;border:1px solid #cdd5dd;border-radius:6px;font-size:14px;width:100%';
    function rowField(labelText, inputEl) {
      var use = U.el('input', { type: 'checkbox' });
      inputEl.style.cssText = FS;
      var r = U.el('div', { style: 'display:grid;grid-template-columns:20px 120px 1fr;gap:8px;align-items:center;margin:6px 0' }, [
        use, U.el('span', { text: labelText, style: 'font-weight:600' }), inputEl]);
      box.appendChild(r);
      return { use: use, el: inputEl };
    }
    var fOwner = rowField('負責人', U.el('input', { list: 'webext-owners', placeholder: '改負責人為…（可打新名字）' }));
    var fDept = rowField('部門', U.el('input', { list: 'webext-depts', placeholder: '改部門為…' }));
    var progSel = U.el('select'); PROGRESS_OPTS.forEach(function (o) { progSel.appendChild(U.el('option', { value: o[0], text: o[1] })); });
    var fProg = rowField('處理進度', progSel);
    var fTarget = rowField('預計完成日', U.el('input', { type: 'date' }));
    var fNote = rowField('追蹤備註', U.el('input', { placeholder: '管理追蹤備註' }));
    box.appendChild(_datalist('webext-owners', _ownerList));
    box.appendChild(_datalist('webext-depts', _deptList));
    var preview = U.el('div', { class: 'empty-hint', style: 'margin-top:8px' });
    box.appendChild(preview);

    function collect() {
      var body = { ids: ids };
      var changes = [];
      if (fOwner.use.checked) { body.set_owner = true; body.owner = fOwner.el.value.trim(); changes.push('負責人 → ' + (body.owner || '（清除）')); }
      if (fDept.use.checked) { body.set_department = true; body.department = fDept.el.value.trim(); changes.push('部門 → ' + (body.department || '（清除）')); }
      if (fProg.use.checked) { body.set_progress = true; body.progress = progSel.value === '__clear__' ? '' : progSel.value; changes.push('處理進度 → ' + (body.progress || '（清除）')); }
      if (fTarget.use.checked) { body.set_target = true; body.target_date = fTarget.el.value; changes.push('預計完成日 → ' + (body.target_date || '（清除）')); }
      if (fNote.use.checked) { body.set_note = true; body.note = fNote.el.value; changes.push('追蹤備註 → ' + (body.note || '（清除）')); }
      return { body: body, changes: changes };
    }
    function refresh() {
      var c = collect();
      preview.innerHTML = '';
      if (!c.changes.length) { preview.textContent = '尚未勾選要改的欄位。'; return; }
      preview.appendChild(U.el('div', { style: 'font-weight:600;color:#1a7f4b' }, [U.el('span', { text: '即將對 ' + ids.length + ' 筆套用：' })]));
      c.changes.forEach(function (t) { preview.appendChild(U.el('div', { text: '· ' + t })); });
      preview.appendChild(U.el('div', { class: 'empty-hint', style: 'margin-top:4px', text: '（無權限的筆數會自動略過並列出）' }));
    }
    [fOwner, fDept, fProg, fTarget, fNote].forEach(function (f) { f.use.addEventListener('change', refresh); f.el.addEventListener('input', refresh); f.el.addEventListener('change', refresh); });
    refresh();

    var apply = U.el('button', { class: 'btn btn-primary', text: '確認套用' });
    apply.addEventListener('click', async function () {
      var c = collect();
      if (!c.changes.length) { UI.toast('請至少勾一個要改的欄位', 'error'); return; }
      if (!window.confirm('對 ' + ids.length + ' 筆套用：\n' + c.changes.join('\n') + '\n\n確定？')) return;
      apply.disabled = true; apply.textContent = '套用中…';
      try {
        var r = await fetch('/api/findings/bulk-overlay', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(c.body) });
        var j = await r.json();
        if (!r.ok) { UI.toast(j.detail || '失敗', 'error'); apply.disabled = false; apply.textContent = '確認套用'; return; }
        UI.toast('已套用 ' + j.applied + ' 筆' + (j.skipped.length ? ('，略過 ' + j.skipped.length + '（無權限）') : '') + (j.failed.length ? ('，失敗 ' + j.failed.length) : ''), j.failed.length ? 'error' : 'success');
        UI.closeModal(); if (onDone) onDone();
      } catch (e) { UI.toast('套用時發生錯誤', 'error'); apply.disabled = false; apply.textContent = '確認套用'; }
    });
    UI.openModal('批次改狀態', box, { footer: apply, stack: true, sticky: true });
  }

  // 批次上傳佐證：一份檔案掛已勾選的 N 筆（實體檔去重只存一份）。
  function openBatchAttach(ids, onDone) {
    var box = U.el('div');
    box.appendChild(U.el('p', { class: 'empty-hint', text: '選一個檔案，掛到已勾選的 ' + ids.length + ' 筆弱點（只上傳一次、系統建 ' + ids.length + ' 筆關聯、硬碟只存一份）。' }));
    var kindSel = U.el('select', { style: 'padding:6px 10px;border:1px solid #cdd5dd;border-radius:6px;font-size:14px' });
    ATTACH_KINDS.forEach(function (k) { kindSel.appendChild(U.el('option', { value: k, text: k })); });
    var fileInp = U.el('input', { type: 'file' });
    box.appendChild(U.el('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0' }, [
      U.el('span', { text: '類型', style: 'font-weight:600' }), kindSel, fileInp]));
    var apply = U.el('button', { class: 'btn btn-primary', text: '上傳並掛到 ' + ids.length + ' 筆' });
    apply.addEventListener('click', async function () {
      var f = fileInp.files && fileInp.files[0];
      if (!f) { UI.toast('請先選檔案', 'error'); return; }
      if (!window.confirm('把「' + f.name + '」掛到已勾選的 ' + ids.length + ' 筆？')) return;
      apply.disabled = true; apply.textContent = '上傳中…';
      try {
        var buf = await f.arrayBuffer();
        var url = '/api/attachments/bulk?ids=' + ids.join(',') + '&name=' + encodeURIComponent(f.name) + '&kind=' + encodeURIComponent(kindSel.value);
        var r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: buf });
        var j = await r.json();
        if (!r.ok) { UI.toast(j.detail || '上傳失敗', 'error'); apply.disabled = false; apply.textContent = '上傳並掛到 ' + ids.length + ' 筆'; return; }
        UI.toast('已掛到 ' + j.applied + ' 筆' + (j.skipped.length ? ('，略過 ' + j.skipped.length + '（無權限）') : '') + (j.failed.length ? ('，失敗 ' + j.failed.length) : ''), j.failed.length ? 'error' : 'success');
        UI.closeModal(); if (onDone) onDone();
      } catch (e) { UI.toast('上傳時發生錯誤', 'error'); apply.disabled = false; apply.textContent = '上傳並掛到 ' + ids.length + ' 筆'; }
    });
    UI.openModal('批次上傳佐證', box, { footer: apply, stack: true, sticky: true });
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
      U.el('option', { value: '等複掃', text: '結案申請中（承辦回報做完、等資安複掃確認結案）' }),
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
    // 防呆：這筆已在首次展延中又選「要申請展延」→ 展延只能一次，下一步應申請例外（使用者 2026-10-06）
    var stageHint = U.el('div', { style: 'display:none;margin:-6px 0 12px;padding:8px 10px;border:1px solid #f0c27b;background:#fff8ec;border-radius:6px;font-size:13.5px;text-align:left' });
    function stageConflict() {
      if (progress.value === '要申請展延' && row.stage === '首次展延中') return 'ext';
      if ((progress.value === '要申請展延' || progress.value === '要申請例外') && row.stage === '例外管理中') return 'exc';
      return '';
    }
    function updStageHint() {
      var c = stageConflict(); stageHint.innerHTML = '';
      if (!c) { stageHint.style.display = 'none'; return; }
      stageHint.style.display = 'block';
      if (c === 'ext') {
        stageHint.appendChild(U.el('span', { text: '⚠ 這筆已在「首次展延中」（Excel 已有展延紀錄）。展延只能一次，下一步應申請例外。 ', style: 'color:#9a5b00;font-weight:600' }));
        var fix = U.el('button', { class: 'btn btn-primary btn-sm', text: '改成「要申請例外」' });
        fix.addEventListener('click', function () { progress.value = '要申請例外'; updAttHint(); updStageHint(); });
        stageHint.appendChild(fix);
      } else {
        stageHint.appendChild(U.el('span', { text: '⚠ 這筆已在「例外管理中」（Excel 已有例外紀錄），通常不需要再申請。確定的話照常存檔即可。', style: 'color:#9a5b00;font-weight:600' }));
      }
    }
    progress.addEventListener('change', updStageHint);
    body.insertBefore(stageHint, progress.nextSibling); updStageHint();
    var _stageAck = false;   // 已提醒過一次；再按存檔就照使用者的選擇存
    body.appendChild(U.el('label', { text: '申請佐證文件（展延／例外的 WBS、理由說明等；掛在此弱點、重匯不洗；下載需登入）' }));
    body.appendChild(attHint);
    body.appendChild(attachPanel(row));
    var save = U.el('button', { class: 'btn btn-primary', text: '存檔' });
    // 防呆：追蹤備註有寫、處理進度空白 → 先提醒（寫了「修補完畢」卻忘了設等複掃，狀態就不會轉）
    var warnBox = U.el('div', { style: 'display:none;margin:4px 0 12px;padding:10px 12px;border:1px solid #f0c27b;background:#fff8ec;border-radius:8px;text-align:left' });
    body.insertBefore(warnBox, body.firstChild.nextSibling);
    var DONE_RE = /完畢|完成|已修補|修補好|已修復|已更新|已升級|已關閉|已處理|處理完/;
    function showWarn() {
      warnBox.innerHTML = '';
      var looksDone = DONE_RE.test(note.value);
      warnBox.appendChild(U.el('div', { style: 'font-weight:600;color:#9a5b00;margin-bottom:6px', text: '⚠ 追蹤備註有寫，但處理進度是空白。這樣狀態會一直是「尚未修補」，要不要順手設？' }));
      if (looksDone) warnBox.appendChild(U.el('div', { style: 'font-size:13px;color:#6b7a73;margin-bottom:6px', text: '備註看起來是「已經做完」→ 建議設「結案申請中」（等資安複掃確認結案）。' }));
      var bR = U.el('button', { class: 'btn ' + (looksDone ? 'btn-primary' : 'btn-secondary') + ' btn-sm', text: '設為「結案申請中」並存檔' + (looksDone ? '（建議）' : '') });
      var bW = U.el('button', { class: 'btn btn-secondary btn-sm', text: '設為「處理中」並存檔' });
      var bK = U.el('button', { class: 'btn btn-secondary btn-sm', text: '不用，照原樣存檔' });
      bR.addEventListener('click', function () { progress.value = '等複掃'; updAttHint(); doSave(); });
      bW.addEventListener('click', function () { progress.value = '處理中'; updAttHint(); doSave(); });
      bK.addEventListener('click', function () { doSave(); });
      warnBox.appendChild(U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, [bR, bW, bK]));
      warnBox.style.display = 'block';
      warnBox.scrollIntoView({ block: 'nearest' });
    }
    save.addEventListener('click', function () {
      // 部門只能選現有(避免打錯多出部門)；留空＝清除回 Excel 值
      var dv = (dept.value || '').trim();
      if (dv && (_deptList || []).indexOf(dv) < 0) {
        UI.toast('部門「' + dv + '」不在清單中。請從既有部門選擇（避免打錯新增部門）；留空＝回到 Excel 值。', 'error');
        dept.focus(); return;
      }
      if (stageConflict() === 'ext' && !_stageAck) {
        _stageAck = true; updStageHint(); stageHint.scrollIntoView({ block: 'nearest' });
        UI.toast('這筆已在展延中，建議改成「要申請例外」；確定要照原樣存，再按一次存檔', 'error');
        return;
      }
      if (String(note.value || '').trim() && !progress.value) { showWarn(); return; }
      doSave();
    });
    async function doSave() {
      var r = await fetch('/api/findings/' + row.id + '/overlay', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ set_owner: true, owner: owner.value, set_department: true, department: dept.value, set_target: true, target_date: target.value, set_progress: true, progress: progress.value, set_note: true, note: note.value })
      });
      if (!r.ok) { var e = await r.json().catch(function () { return {}; }); UI.toast('存失敗：' + (e.detail || r.status), 'error'); return; }
      UI.closeModal(); UI.toast('已更新', 'success'); if (done) done();
    }
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
    // 這筆的歷程（匯入變化＋系統操作），放在原始資料上面（2026-10-07）
    if (row && row.id && !row.readonly) {
      var hb = U.el('div', { style: 'margin-bottom:12px' }, [U.el('div', { text: '歷程', style: 'font-weight:700;color:#0f5f35;margin-bottom:4px' }),
        U.el('div', { text: '讀取中…', style: 'color:#6b7a73' })]);
      box.insertBefore(hb, box.firstChild);
      jget('/api/findings/' + row.id + '/history').then(function (h) {
        hb.removeChild(hb.lastChild);
        var t = U.el('table', { class: 'tracking-table', style: 'margin:0' });
        var tb = U.el('tbody');
        (h.items || []).forEach(function (it) {
          tb.appendChild(U.el('tr', {}, [U.el('td', { text: (it.at || '').replace('T', ' '), style: 'white-space:nowrap' }),
            U.el('td', { text: it.src, style: 'white-space:nowrap;color:' + (it.src === '系統' ? '#1565c0' : '#6b7a73') }),
            U.el('td', { text: it.text, style: 'text-align:left' })]));
        });
        if (!(h.items || []).length) tb.appendChild(U.el('tr', {}, [U.el('td', { text: '沒有紀錄' })]));
        t.appendChild(tb); hb.appendChild(t);
      }).catch(function () { hb.lastChild.textContent = '讀取失敗'; });
    }
    UI.openModal('原始資料 — ' + (row.host || '') + ' / ' + (row.name || row.plugin_id || ''), box, { stack: true });
  }
  // 一列「操作」欄：固定有 🔍(看原始)，可寫入時再加 ✏️(編輯)
  function opsCell(row, onEditDone) {
    var ops = U.el('td', { style: 'white-space:nowrap' });   // 🔍 ✏️ 並排，不疊兩行
    if (row && row.att_count) {   // 有申請佐證文件：顯示 📎N（點 ✏️ 進編輯看/下載）
      ops.appendChild(U.el('span', { text: '📎' + row.att_count, title: row.att_count + ' 份申請佐證文件（點 ✏️ 查看／下載）', style: 'margin-right:4px;font-size:13px;color:#1a7f4b;font-weight:700' }));
    }
    var rawBtn = U.el('button', { class: 'btn btn-sm', text: '🔍', title: '看原始資料', style: 'margin-right:4px' });
    rawBtn.addEventListener('click', function () { showRawModal(row); });
    ops.appendChild(rawBtn);
    if (canWrite() && !(row && row.readonly)) {   // readonly＝只存在上一批的列（目前資料沒有它），不能編輯
      var eb = U.el('button', { class: 'btn btn-sm', text: '✏️', title: '編輯' });
      eb.addEventListener('click', function () { editOverlay(row, onEditDone); });
      ops.appendChild(eb);
    }
    return ops;
  }

  // ---- 登入 ----
  async function refreshMe() {
    try { me = await jget('/api/me'); } catch (e) { me = { authenticated: false }; }
    if (me.authenticated && me.department && _lsGet('wx_dept') === null && !_deptPick) { _deptPick = me.department; }
    _headInfo();
    var who = document.getElementById('webext-whoami');
    var span = document.getElementById('webext-user');   // 原生 header 的使用者區(保留，隱藏)
    var btn = document.getElementById('webext-login-btn');
    if (span) span.classList.add('hidden');              // 改用 whoami 行，原 span 不用
    var online = (me.online != null) ? ('　·　目前登入 ' + me.online + ' 人') : '';
    if (who) {
      if (me.authenticated) {
        // 看得到的範圍跟著角色（後端 VIEW_SCOPE 擋），這裡講清楚，免得以為資料少了
        var scopeTxt = me.is_super ? '' : (me.role === 'dept_admin' ? '；只顯示 ' + (me.department || '（未設部門）') + ' 的弱點'
          : '；只顯示你負責的弱點');
        who.textContent = '歡迎 ' + (me.display_name || me.username) + '（' + _roleLabel(me.role) + '）登入' + scopeTxt + online;
      } else if (me.open_write) {
        who.textContent = '免登入模式（最高權限）' + online;
      } else {
        who.textContent = '尚未登入，請先登入' + online;
      }
    }
    if (btn) {
      // 已登入→顯示登出；其餘(含免登入模式，方便切換帳號測試)→顯示登入
      btn.classList.remove('hidden');
      btn.textContent = me.authenticated ? '登出' : '登入';
    }
  }
  function openLogin() {
    var user = U.el('input', { type: 'text', id: 'wx-user', placeholder: '帳號', autocomplete: 'username' });
    var pass = U.el('input', { type: 'password', id: 'wx-pass', placeholder: '密碼', autocomplete: 'current-password' });
    var err = U.el('p', { class: 'empty-hint', style: 'color:#c0392b;display:none;font-size:15px;margin:6px 0 0' });
    // 小視窗、大字（使用者 2026-10-06：只有兩欄，不用拉滿版）
    [user, pass].forEach(function (i) { i.style.cssText = 'display:block;width:100%;margin:4px 0 12px;padding:10px 12px;border:1px solid #cdd5dd;border-radius:6px;font-size:17px;box-sizing:border-box'; });
    var LBL = 'font-size:16px;font-weight:600';
    var body = U.el('div', {}, [U.el('label', { text: '帳號（員編）', style: LBL }), user, U.el('label', { text: '密碼', style: LBL }), pass, err]);
    var submit = U.el('button', { class: 'btn btn-primary', text: '登入' });
    async function doLogin() {
      var r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user.value.trim(), password: pass.value }) });
      if (!r.ok) {
        var e = await r.json().catch(function () { return {}; });
        // 沒帶原因（多半是伺服器出錯 5xx）也要講出狀態碼，不要只寫「登入失敗」讓人猜（2026-10-06 同事 AD 登入只看到這四個字）
        err.textContent = e.detail || ('登入失敗（' + (r.status >= 500 ? '伺服器錯誤 ' + r.status + '，請通知管理員查服務紀錄' : '狀態碼 ' + r.status) + '）');
        err.style.display = 'block'; return;
      }
      UI.closeModal(); await refreshMe();
      // 登入後部門預設成自己的部門（使用者 2026-10-06）；部門名要跟 Excel 對得上才選得到，對不上就維持原樣
      var dsel = document.getElementById('my-dept-select');
      if (me.department && dsel && [].some.call(dsel.options, function (o) { return o.value === me.department; })) {
        _deptPick = null; _lsSet('wx_dept', null);
        dsel.value = me.department; dsel.dispatchEvent(new Event('change', { bubbles: true }));
      }
      refreshGov(); UI.toast('已登入', 'success');
    }
    submit.addEventListener('click', doLogin);
    pass.addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
    submit.style.cssText = 'font-size:16px;padding:8px 28px';
    UI.openModal('登入', body, { footer: submit, narrow: true });
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
  var _dueLeadOn = false;  // 預設不含提前量：「30 天內」就是字面上實際到期日 30 天內（2026-10-05 使用者要求）；要追申請時機再勾
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
    host.appendChild(U.el('p', { class: 'empty-hint', text: '依「' + basis + '」倒數分桶（未結案）；無到期日者不列。' }));
    host.appendChild(leadToggle(function () { renderDueSoonInto(host); }));
    var pm = function (extra) { var p = Object.assign({}, extra); if (lead) p.lead = lead; return p; };
    // 累計三顆：30／60／90 天內（從今天起算，不含已逾期）。下面的分段頁籤是互斥的(31–60 不含 30 內)，這三顆才是「N 天內全部」
    var cum = [[30, d.d30 || 0], [60, (d.d30 || 0) + (d.d31_60 || 0)], [90, (d.d30 || 0) + (d.d31_60 || 0) + (d.d61_90 || 0)]];
    var cumRow = U.el('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0 14px' },
      [U.el('span', { style: 'font-weight:600', text: '累計（今天起，不含已逾期）：' })]);
    cum.forEach(function (c) {
      var b = U.el('button', { class: 'btn btn-primary', text: c[0] + ' 天內（' + c[1] + '）', style: 'min-width:130px' });
      if (!c[1]) b.disabled = true;
      b.addEventListener('click', function () {
        openFindings(c[0] + ' 天內到期（' + (_dueLeadOn ? '依行動期限' : '依實際到期日') + '，累計）', pm({ due_min: '0', due_max: String(c[0]) }));
      });
      cumRow.appendChild(b);
    });
    host.appendChild(cumRow);
    // 長條圖：各到期桶數量一眼看出近期壓力（紅＝已逾期），點長條下鑽該桶明細。
    var bparam = { overdue: { due_max: '-1' }, d30: { due_min: '0', due_max: '30' },
      d31_60: { due_min: '31', due_max: '60' }, d61_90: { due_min: '61', due_max: '90' }, d90plus: { due_min: '91' } };
    var blabel = { overdue: _dueLeadOn ? '已過行動期限' : '已逾期', d30: '30 天內', d31_60: '31–60 天', d61_90: '61–90 天', d90plus: '90 天以上' };
    hbarChart(host, dueBucketItems(d, function (key) {
      openFindings(blabel[key] + '（到期倒數）', pm(bparam[key]));
    }));
    renderTabs(host, [
      { label: '已逾期（' + (d.overdue || 0) + '）', render: function (c) { renderActionListInto(c, _dueLeadOn ? '已過行動期限' : '已逾期', pm({ due_max: '-1' })); } },
      { label: '30天內（' + (d.d30 || 0) + '）', render: function (c) { renderActionListInto(c, '30 天內', pm({ due_min: '0', due_max: '30' })); } },
      { label: '31–60天（' + (d.d31_60 || 0) + '）', render: function (c) { renderActionListInto(c, '31–60 天', pm({ due_min: '31', due_max: '60' })); } },
      { label: '61–90天（' + (d.d61_90 || 0) + '）', render: function (c) { renderActionListInto(c, '61–90 天', pm({ due_min: '61', due_max: '90' })); } },
      { label: '90天以上（' + (d.d90plus || 0) + '）', render: function (c) { renderActionListInto(c, '90 天以上', pm({ due_min: '91' })); } },
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
    host.appendChild(U.el('p', { class: 'empty-hint', text: '各負責人於所選到期範圍內的未結數（含逾期）。點負責人查看明細。' }));
    // 到期範圍：統一用膠囊頁籤(同「到期倒數」)，預設聚焦近期 90 天
    var curRange = '90';
    host.appendChild(U.el('div', {}, [U.el('label', { text: '到期範圍　', style: 'font-size:14px' }),
      rangePills(curRange, function (v) { curRange = v; draw(); })]));
    host.appendChild(leadToggle(function () { draw(); }));   // 含申請提前量開關(與到期倒數共用狀態)
    var box = U.el('div'); host.appendChild(box);
    var cols = [['owner', '負責人'], ['department', '部門'], ['total', '未結'], ['original', '原始'],
      ['extension', '首次展延'], ['exception', '例外管理'], ['rescan', '結案申請中'], ['overdue', '逾期']];
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
        r.target_date, cellVal(r, 'overdue_days'), r.track_note, r.department].join(' ').toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) >= 0; });
    }
    var refresh = function () { renderActionListInto(container, title, params); };
    function draw() {
      box.innerHTML = '';
      var terms = (search.value || '').toLowerCase().split(/\s+/).filter(function (t) { return t; });
      shown = terms.length ? rows.filter(function (r) { return matchRow(r, terms); }) : rows;
      if (!shown.length) {
        box.appendChild(U.el('p', { class: 'empty-hint', text: terms.length ? '查無符合「' + search.value.trim() + '」的項目。' : '目前無項目。' }));
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
          var tds = _TODO_COLS.map(function (c) { return U.el('td', { text: cellVal(r, c[0]) }); });
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
        card(s.source_confirmed, '來源（Excel）確認'),
        card(s.claimed_unconfirmed, '聲稱完成·來源未確認', true),
      ]));
      c0.appendChild(U.el('p', { class: 'empty-hint', text: '本期新結案＝上期未結、本期已結（快照比對）；聲稱完成·來源未確認＝已標記完成但來源仍未結案，需查核。' }));
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
    // 總覽（瀏覽器端照原始列算）vs 週報（伺服器算）：四個數字要一樣（使用者 2026-10-06：數字要對得上才有說服力）
    try {
      var rep = await jget('/api/report?' + qd());
      var st = global.App && global.App.getState && global.App.getState();
      var shs = (st && st.sheets) || [];
      var sh = curSheet(); if (sh) shs = shs.filter(function (x) { return x.name === sh; });
      if (shs.length && global.Summary && global.Summary.overall) {
        var ov = global.Summary.overall(shs, curDept() || '__all__').totals;
        [['未結案', ov.open, rep.unresolved], ['已逾期', ov.overdue, rep.overdue],
         ['近期到期（30 天內）', ov.soon, rep.soon], ['高風險未結（不含逾期、近期）', ov.high, rep.high_risk_only]].forEach(function (x) {
          var ok = x[1] === x[2];
          s.checks.push({ name: '總覽 ＝ 主管週報：' + x[0], a_label: '總覽', a: x[1], b_label: '主管週報', b: x[2], ok: ok });
          if (!ok) s.all_ok = false;
        });
      }
    } catch (e) { }
    // 大橫幅：全綠或有問題
    var banner = U.el('div', { style: 'padding:12px 16px;border-radius:8px;font-size:16px;font-weight:700;margin:4px 0 12px;'
      + (s.all_ok ? 'background:#e8f5e9;color:#1a7f4b;border:1px solid #1a7f4b' : 'background:#fdecea;color:#c0392b;border:1px solid #c0392b') },
      [U.el('span', { text: s.all_ok ? '✅ 對帳一致：各項數字相符。' : '⚠️ 發現不一致，詳見下方紅色項目。' })]);
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
      '核對方式：點數字展開清單、每列「🔍」查看原始整列；或以「完整匯出 (CSV)」與來源 Excel 逐列比對（匯入列數應等於來源資料列數）。' }));

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

  // ---- AD 登入設定（Super Admin）：所有欄位用填的、存 DB、可隨時改；含測試連線 ----
  async function openAdSettings() {
    var cfg;
    try { cfg = await jget('/api/ad-settings'); }
    catch (e) { UI.toast('讀取失敗（需最高權限）', 'error'); return; }
    var box = U.el('div');
    var FS = 'display:block;width:100%;margin:0;padding:7px 8px;border:1px solid #cdd5dd;border-radius:6px;font-size:14px;font-family:inherit';
    // 區塊標題 + 回傳一個兩欄網格容器
    function section(title, hint) {
      box.appendChild(U.el('div', { style: 'margin:16px 0 6px;font-weight:700;font-size:15px;color:#1a7f4b;border-left:3px solid #1a7f4b;padding-left:8px' }, [U.el('span', { text: title })]));
      if (hint) box.appendChild(U.el('p', { class: 'empty-hint', style: 'margin:0 0 6px' , text: hint }));
      var g = U.el('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:10px 16px' });
      box.appendChild(g); return g;
    }
    function fld(g, labelText, el, full) {
      var cell = U.el('div', full ? { style: 'grid-column:1 / -1' } : {});
      cell.appendChild(U.el('label', { text: labelText, style: 'display:block;margin:0 0 2px;font-weight:600;font-size:13px;color:#445' }));
      el.style.cssText = FS; cell.appendChild(el); g.appendChild(cell); return el;
    }
    // 啟用
    var enabled = U.el('input', { type: 'checkbox' }); enabled.checked = !!cfg.enabled;
    box.appendChild(U.el('label', { style: 'display:flex;gap:8px;align-items:center;margin:2px 0;font-weight:600' }, [enabled, U.el('span', { text: '啟用 AD 登入（關閉＝維持目前本地／免登入）' })]));

    // ① 站點預設（可編輯，存 DB）
    var pg = section('站點預設（填一次，之後「使用站點」可選；IP 存你伺服器 DB，不進公開程式碼）');
    var presetInputs = [];
    (cfg.presets || []).forEach(function (p) {
      var ip = fld(pg, p.site + ' — LDAP IP（逗號分隔）', U.el('input', { value: (p.ips || []).join(', ') }));
      presetInputs.push({ site: p.site, el: ip });
    });

    // ② 連線
    var cg = section('連線');
    var useSel = fld(cg, '使用站點（選了帶入下面伺服器）', U.el('select'));
    useSel.appendChild(U.el('option', { value: '', text: '— 選站點 —' }));
    presetInputs.forEach(function (p) { useSel.appendChild(U.el('option', { value: p.site, text: p.site })); });
    var enc = fld(cg, '加密', U.el('select'));
    ['none', 'starttls', 'ldaps'].forEach(function (o) { enc.appendChild(U.el('option', { value: o, text: o })); }); enc.value = cfg.encryption || 'none';
    var servers = fld(cg, 'LDAP 伺服器（實際使用，逗號分隔，可手改）', U.el('input', { value: (cfg.servers || []).join(', ') }), true);
    useSel.addEventListener('change', function () {
      var p = presetInputs.filter(function (x) { return x.site === useSel.value; })[0];
      if (p && p.el.value.trim()) servers.value = p.el.value.trim();
    });

    // ③ 綁定
    var bg = section('綁定方式');
    var bindStyle = fld(bg, '方式', U.el('select'));
    [['upn', 'UPN（員編@後綴）'], ['nt', 'NT（網域\\員編）'], ['dn', 'DN 樣板']].forEach(function (o) { bindStyle.appendChild(U.el('option', { value: o[0], text: o[1] })); }); bindStyle.value = cfg.bind_style || 'upn';
    var upn = fld(bg, 'UPN 後綴（UPN 用，例 corp.example.com）', U.el('input', { value: cfg.upn_suffix || '' }));
    var ntd = fld(bg, 'NT 網域（NT 用，例 corp）', U.el('input', { value: cfg.nt_domain || '' }));

    // ④ 讀取屬性
    var ag = section('讀取屬性（綁定後向 AD 讀取）');
    var baseDn = fld(ag, '搜尋基準 base_dn', U.el('input', { value: cfg.base_dn || '' }), true);
    var nameAttr = fld(ag, '顯示名屬性（對 Excel 負責人名）', U.el('input', { value: cfg.name_attr || 'displayName' }));
    var deptAttr = fld(ag, '部門屬性', U.el('input', { value: cfg.dept_attr || 'department' }));
    var loginAttr = fld(ag, '員編比對屬性（搜尋用）', U.el('input', { value: cfg.login_attr || 'sAMAccountName' }));

    // ⑤ 權限 / 對照
    var sg = section('權限與對照');
    var supers = fld(sg, 'Super Admin 員編（逗號分隔）', U.el('input', { value: (cfg.super_admins || []).join(', ') }), true);
    var empmap = fld(sg, '員編↔負責人名 對照（顯示名對不上時補；每行一筆 員編=負責人名）', U.el('textarea', { rows: '3' }), true);
    empmap.value = Object.keys(cfg.emp_to_owner || {}).map(function (k) { return k + '=' + cfg.emp_to_owner[k]; }).join('\n');

    function collect() {
      var m = {}; empmap.value.split(/\n/).forEach(function (l) { var i = l.indexOf('='); if (i > 0) { var k = l.slice(0, i).trim(), v = l.slice(i + 1).trim(); if (k) m[k] = v; } });
      return { enabled: enabled.checked, servers: servers.value.split(/[,\s]+/).filter(Boolean),
        encryption: enc.value, bind_style: bindStyle.value, upn_suffix: upn.value.trim(), nt_domain: ntd.value.trim(),
        base_dn: baseDn.value.trim(), name_attr: nameAttr.value.trim(), dept_attr: deptAttr.value.trim(), login_attr: loginAttr.value.trim(),
        super_admins: supers.value.split(/[,\s]+/).filter(Boolean), emp_to_owner: m,
        presets: presetInputs.map(function (p) { return { site: p.site, ips: p.el.value.split(/[,\s]+/).filter(Boolean) }; }) };
    }

    // ⑥ 測試連線
    var tg = section('測試連線（不會儲存密碼；先測成功再啟用）');
    var tLogin = fld(tg, '員編', U.el('input', { placeholder: '員編' }));
    var tPw = fld(tg, '密碼', U.el('input', { type: 'password', placeholder: '只用於測試' }));
    var tBtn = U.el('button', { class: 'btn btn-secondary', text: '測試連線' });
    var tOut = U.el('span', { style: 'margin-left:10px;font-size:13px' });
    tBtn.addEventListener('click', async function () {
      tOut.textContent = '測試中…'; tOut.style.color = '#666';
      try {
        var r = await (await fetch('/api/ad-test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ login: tLogin.value, password: tPw.value, config: collect() }) })).json();
        tOut.textContent = (r.ok ? '✅ ' : '⚠️ ') + (r.message || ''); tOut.style.color = r.ok ? '#1a7f4b' : '#c0392b';
      } catch (e) { tOut.textContent = '測試失敗'; tOut.style.color = '#c0392b'; }
    });
    box.appendChild(U.el('div', { style: 'display:flex;align-items:center;margin:8px 0' }, [tBtn, tOut]));

    var save = U.el('button', { class: 'btn btn-primary', text: '儲存設定' });
    save.addEventListener('click', async function () {
      var r = await fetch('/api/ad-settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(collect()) });
      if (r.ok) { UI.toast('已儲存 AD 設定', 'success'); UI.closeModal(); } else { UI.toast('儲存失敗', 'error'); }
    });
    UI.openModal('AD 登入設定（Super Admin）', box, { footer: save, sticky: true, wide: true });
  }

  // ---- 帳號與權限（Super Admin）：指定部門窗口及其部門 ----
  async function openUserAdmin() {
    var users;
    try { users = await jget('/api/users'); }
    catch (e) { UI.toast('讀取失敗（需最高權限）', 'error'); return; }
    var box = U.el('div');
    box.appendChild(U.el('p', { class: 'empty-hint', text: '新增：下方「新增／重設本地帳號」；修改：改該列欄位後按「儲存」；刪除：該列「刪除」。AD 員工登入一次會自動建立，可再在此調整角色/部門。信箱供一鍵發送用，可手補。' }));

    // 收件人涵蓋率：從 AD 一次撈齊所有負責人信箱（很多負責人從沒登入、沒信箱）
    var fetchOut = U.el('span', { style: 'margin-left:10px;font-size:13px' });
    var fetchBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '從 AD 補負責人信箱' });
    fetchBtn.addEventListener('click', function () {
      var fb = U.el('div');
      var li = U.el('input', { placeholder: '員編', style: 'padding:6px 10px;border:1px solid #cdd5dd;border-radius:6px;width:100%;margin:4px 0' });
      var pw = U.el('input', { type: 'password', placeholder: '密碼（只用於這次 AD 查詢，不儲存）', style: 'padding:6px 10px;border:1px solid #cdd5dd;border-radius:6px;width:100%;margin:4px 0' });
      fb.appendChild(U.el('p', { class: 'empty-hint', text: '用你的 AD 帳密綁定，依負責人顯示名向 AD 搜 mail，補進對應帳號。' }));
      fb.appendChild(li); fb.appendChild(pw);
      var go = U.el('button', { class: 'btn btn-primary', text: '開始補' });
      go.addEventListener('click', async function () {
        go.disabled = true; go.textContent = '查詢中…';
        try {
          var r = await fetch('/api/users/fetch-ad-mails', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ login: li.value, password: pw.value }) });
          var j = await r.json();
          if (!r.ok) { UI.toast(j.detail || '查詢失敗', 'error'); go.disabled = false; go.textContent = '開始補'; return; }
          UI.toast('負責人 ' + j.names + '：查到 ' + j.found + '，更新 ' + j.updated + '、新建 ' + j.created, 'success');
          UI.closeModal(); openUserAdmin();
        } catch (e) { UI.toast('查詢失敗', 'error'); go.disabled = false; go.textContent = '開始補'; }
      });
      UI.openModal('從 AD 補負責人信箱', fb, { footer: go, stack: true, sticky: true });
    });

    // 本地測試帳號：Super Admin 自建，供測試不同角色（AD 啟用時也能用；本地優先）
    function openLocalUserForm() {
      var fb = U.el('div');
      fb.appendChild(U.el('p', { class: 'empty-hint', text: '建立／重設一個本地帳號（有密碼）。同帳號再存＝更新密碼／角色。本地帳號即使啟用 AD 也能登入。' }));
      var FS = 'padding:6px 10px;border:1px solid #cdd5dd;border-radius:6px;font-size:14px;width:100%;margin:4px 0';
      var un = U.el('input', { placeholder: '帳號（如 user / admin / superadmin）' }); un.style.cssText = FS;
      var pw = U.el('input', { type: 'text', placeholder: '密碼（至少 4 碼）' }); pw.style.cssText = FS;
      var dn = U.el('input', { placeholder: '顯示名（一般使用者要對應某負責人就填那個人名，如 王小明）' }); dn.style.cssText = FS;
      var dp = U.el('input', { placeholder: '部門（部門窗口要管的部門，需同 Excel 短名）' }); dp.style.cssText = FS;
      var nt = U.el('input', { placeholder: '註解（員編_姓名_部門_用途，例 01000000_某某_某部_patchweb）' }); nt.style.cssText = FS;
      var rl = U.el('select'); rl.style.cssText = FS;
      [['user', '一般'], ['dept_admin', '部門窗口'], ['super_admin', 'Super Admin']].forEach(function (o) { rl.appendChild(U.el('option', { value: o[0], text: o[1] })); });
      fb.appendChild(U.el('label', { text: '帳號', style: 'font-weight:600' })); fb.appendChild(un);
      fb.appendChild(U.el('label', { text: '密碼', style: 'font-weight:600' })); fb.appendChild(pw);
      fb.appendChild(U.el('label', { text: '角色', style: 'font-weight:600' })); fb.appendChild(rl);
      fb.appendChild(U.el('label', { text: '顯示名', style: 'font-weight:600' })); fb.appendChild(dn);
      fb.appendChild(U.el('label', { text: '部門', style: 'font-weight:600' })); fb.appendChild(dp);
      fb.appendChild(U.el('label', { text: '註解', style: 'font-weight:600' })); fb.appendChild(nt);
      var go = U.el('button', { class: 'btn btn-primary', text: '建立／重設' });
      go.addEventListener('click', async function () {
        if (!un.value.trim() || pw.value.length < 4) { UI.toast('帳號必填、密碼至少 4 碼', 'error'); return; }
        try {
          var r = await fetch('/api/users/local', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: un.value.trim(), password: pw.value, role: rl.value, display_name: dn.value, department: dp.value, note: nt.value }) });
          var j = await r.json();
          if (!r.ok) { UI.toast(j.detail || '失敗', 'error'); return; }
          UI.toast((j.created ? '已建立 ' : '已更新 ') + j.username + '（' + rl.value + '）', 'success');
          UI.closeModal(); openUserAdmin();
        } catch (e) { UI.toast('失敗', 'error'); }
      });
      UI.openModal('新增／重設本地帳號', fb, { footer: go, stack: true, sticky: true });
    }

    var localBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '新增／重設本地帳號' });
    localBtn.addEventListener('click', openLocalUserForm);
    var seedBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '一鍵建三個測試帳號' });
    seedBtn.addEventListener('click', async function () {
      var pwd = window.prompt('設定測試帳號（superadmin / admin / user）的共同密碼。\n※已存在的帳號會「略過、不覆蓋」，只建缺的。', 'test-1234');
      if (!pwd) return;
      if (pwd.length < 4) { UI.toast('密碼至少 4 碼', 'error'); return; }
      try {
        var r = await fetch('/api/users/seed-test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pwd }) });
        var j = await r.json();
        if (!r.ok) { UI.toast(j.detail || '失敗', 'error'); return; }
        var exists = (j.accounts || []).filter(function (a) { return a.status === 'exists'; }).map(function (a) { return a.username; });
        var msg = '新建 ' + j.created + ' 個' + (exists.length ? ('；已建立（略過）：' + exists.join('、')) : '');
        if (j.created) msg += '，密碼：' + pwd;
        UI.toast(msg, 'success');
        openUserAdmin();
      } catch (e) { UI.toast('失敗', 'error'); }
    });
    box.appendChild(U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 10px' }, [fetchBtn, localBtn, seedBtn, fetchOut]));

    if (!users.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '尚無帳號（AD 登入後會自動建立）。' })); UI.openModal('帳號與權限（Super Admin）', box, { sticky: true, wide: true }); return; }
    var table = U.el('table', { class: 'tracking-table' });
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, ['員編', '顯示名', '角色', '部門', '信箱', '註解', '啟用', '操作'].map(function (h) { return U.el('th', { text: h }); }))]));
    var tb = U.el('tbody');
    users.forEach(function (u) {
      var roleSel = U.el('select', {}, [['super_admin', 'Super Admin'], ['dept_admin', '部門窗口'], ['user', '一般']].map(function (o) { return U.el('option', { value: o[0], text: o[1] }); }));
      roleSel.value = u.role;
      var deptInp = U.el('input', { type: 'text', value: u.department || '', style: 'padding:5px;border:1px solid #cdd5dd;border-radius:5px;width:110px' });
      var mailInp = U.el('input', { type: 'text', value: u.email || '', placeholder: '（無）', style: 'padding:5px;border:1px solid #cdd5dd;border-radius:5px;width:160px' });
      var noteInp = U.el('input', { type: 'text', value: u.note || '', placeholder: '員編_姓名_部門_用途', style: 'padding:5px;border:1px solid #cdd5dd;border-radius:5px;width:200px' });
      var act = U.el('input', { type: 'checkbox' }); act.checked = u.is_active;
      var sv = U.el('button', { class: 'btn btn-sm btn-primary', text: '儲存' });
      sv.addEventListener('click', async function () {
        var r = await fetch('/api/users/' + u.id + '/role', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role: roleSel.value, department: deptInp.value, email: mailInp.value, note: noteInp.value, is_active: act.checked }) });
        UI.toast(r.ok ? '已更新 ' + u.username : '更新失敗', r.ok ? 'success' : 'error');
      });
      var del = U.el('button', { class: 'btn btn-sm', text: '刪除', style: 'margin-left:6px;background:#fff;border:1px solid #d9534f;color:#c0392b' });
      del.addEventListener('click', async function () {
        if (!window.confirm('確定刪除帳號「' + u.username + (u.display_name ? '（' + u.display_name + '）' : '') + '」？\nAD 帳號刪掉後下次登入會自動重建（回預設一般角色）。')) return;
        var r = await fetch('/api/users/' + u.id, { method: 'DELETE' });
        var j = await r.json().catch(function () { return {}; });
        if (!r.ok) { UI.toast(j.detail || '刪除失敗', 'error'); return; }
        UI.toast('已刪除 ' + u.username, 'success'); openUserAdmin();
      });
      tb.appendChild(U.el('tr', {}, [U.el('td', { text: u.username }), U.el('td', { text: u.display_name || '' }),
        U.el('td', {}, [roleSel]), U.el('td', {}, [deptInp]), U.el('td', {}, [mailInp]), U.el('td', {}, [noteInp]), U.el('td', {}, [act]), U.el('td', {}, [sv, del])]));
    });
    table.appendChild(tb); box.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
    UI.openModal('帳號與權限（Super Admin）', box, { sticky: true, wide: true });
  }

  // ---- Email／SMTP 設定（Super Admin）：伺服器端寄信設定，存 DB、免重部署 ----
  // ---- 承辦狀態備份／還原（Super Admin）：把一台機器上標過的狀態帶到另一台 ----
  function openCaseBackup() {
    var box = U.el('div', { style: 'text-align:left;line-height:1.7' });
    box.appendChild(U.el('p', { text: '只帶「有人動過」的承辦資料：處理進度、預計完成日、追蹤備註、改過的負責人／部門。不含 Excel 內容與附件檔。', style: 'margin:0 0 10px' }));
    box.appendChild(U.el('p', { text: '⚠ 備份檔含負責人姓名與備註，屬公司內部資料，請依公司規定保管與傳遞。', style: 'margin:0 0 14px;color:#b9770e' }));
    // 1) 下載
    var bDl = U.el('button', { class: 'btn btn-primary', text: '下載備份檔' });
    bDl.addEventListener('click', async function () {
      bDl.disabled = true; bDl.textContent = '產生中…';
      try {
        var r = await fetch('/api/cases/backup');
        if (!r.ok) throw new Error(r.status);
        var blob = await r.blob();
        var cd = r.headers.get('Content-Disposition') || '';
        var m = cd.match(/filename\*=UTF-8''([^;]+)/);
        var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
        a.download = m ? decodeURIComponent(m[1]) : '承辦狀態備份.json'; a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
        UI.toast('已下載備份檔', 'success');
      } catch (e) { UI.toast('下載失敗（需 Super Admin）', 'error'); }
      bDl.disabled = false; bDl.textContent = '下載備份檔';
    });
    box.appendChild(U.el('div', { style: 'margin-bottom:18px' }, [U.el('b', { text: '① 備份：' }), bDl]));
    // 2) 還原：選檔 → 預覽 → 確認
    var fi = U.el('input', { type: 'file', accept: '.json,application/json' });
    var ow = U.el('input', { type: 'checkbox' });
    var res = U.el('div', { style: 'margin-top:10px' });
    var bApply = U.el('button', { class: 'btn btn-primary', text: '確認匯入' }); bApply.disabled = true;
    var data = null;
    async function run(apply) {
      var r = await fetch('/api/cases/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: data, overwrite: ow.checked, apply: apply }) });
      var j = await r.json().catch(function () { return {}; });
      if (!r.ok) { res.innerHTML = ''; res.appendChild(U.el('p', { text: '✗ ' + (j.detail || ('失敗 ' + r.status)), style: 'color:#c0392b' })); bApply.disabled = true; return; }
      res.innerHTML = '';
      if (j.backup_source_file !== j.here_source_file) {
        res.appendChild(U.el('p', { style: 'color:#b9770e', text: '⚠ 兩台的資料檔不同（備份檔：' + (j.backup_source_file || '?') + '；這台：' + (j.here_source_file || '?') + '），對不到的會比較多。' }));
      }
      var lines = [['備份檔內筆數', j.total], ['會新增（這台還沒標過）', j.new], ['兩邊一樣（不動）', j.same],
        ['跟這台現有資料衝突', j.conflict], [ow.checked ? '其中會覆蓋' : '其中保留這台現有的', ow.checked ? j.overwritten : j.skipped_conflict],
        ['這台對不到這一列（不匯入）', j.unmatched]];
      var t = U.el('table', { class: 'tracking-table', style: 'max-width:520px' });
      var tb = U.el('tbody');
      lines.forEach(function (l) { tb.appendChild(U.el('tr', {}, [U.el('td', { text: l[0], style: 'text-align:left' }), U.el('td', { text: String(l[1] || 0), style: 'font-weight:700' })])); });
      t.appendChild(tb); res.appendChild(t);
      if (j.unmatched_samples && j.unmatched_samples.length) res.appendChild(U.el('p', { style: 'font-size:13px;color:#6b7a73', text: '對不到的例子：' + j.unmatched_samples.slice(0, 8).join('、') }));
      if (j.applied) {
        res.appendChild(U.el('p', { style: 'color:#1a7f4b;font-weight:600', text: '✅ 已匯入。匯入前的資料庫已備份：' + (j.db_backup || '（非 SQLite，未備份）') }));
        bApply.disabled = true;
      } else {
        res.appendChild(U.el('p', { style: 'font-size:13px;color:#6b7a73', text: '以上是預覽，尚未寫入。確認無誤再按「確認匯入」。' }));
        bApply.disabled = !(j.new || (ow.checked && j.overwritten));
      }
    }
    fi.addEventListener('change', async function () {
      data = null; bApply.disabled = true; res.innerHTML = '';
      var f = fi.files && fi.files[0]; if (!f) return;
      try { data = JSON.parse(await f.text()); } catch (e) { res.appendChild(U.el('p', { text: '✗ 不是有效的備份檔', style: 'color:#c0392b' })); return; }
      run(false);
    });
    ow.addEventListener('change', function () { if (data) run(false); });
    bApply.addEventListener('click', function () {
      if (!data) return;
      if (!confirm('確定要把備份檔的承辦狀態匯入這台？（匯入前會自動備份資料庫）')) return;
      bApply.disabled = true; run(true);
    });
    box.appendChild(U.el('div', {}, [U.el('b', { text: '② 還原：' }), fi]));
    box.appendChild(U.el('label', { style: 'display:inline-flex;gap:6px;align-items:center;margin-top:8px;cursor:pointer' },
      [ow, U.el('span', { text: '跟這台現有資料衝突時，用備份檔覆蓋（預設保留這台的）' })]));
    box.appendChild(res);
    UI.openModal('承辦狀態備份／還原（Super Admin）', box, { footer: bApply, sticky: true, wide: true });
  }

  async function openEmailSettings() {
    var cfg;
    try { cfg = await jget('/api/email-settings'); }
    catch (e) { UI.toast('讀取失敗（需最高權限）', 'error'); return; }
    var box = U.el('div');
    var FS = 'display:block;width:100%;margin:0;padding:7px 8px;border:1px solid #cdd5dd;border-radius:6px;font-size:14px;font-family:inherit';
    function section(title, hint) {
      box.appendChild(U.el('div', { style: 'margin:16px 0 6px;font-weight:700;font-size:15px;color:#1a7f4b;border-left:3px solid #1a7f4b;padding-left:8px' }, [U.el('span', { text: title })]));
      if (hint) box.appendChild(U.el('p', { class: 'empty-hint', style: 'margin:0 0 6px', text: hint }));
      var g = U.el('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:10px 16px' });
      box.appendChild(g); return g;
    }
    function fld(g, labelText, el, full) {
      var cell = U.el('div', full ? { style: 'grid-column:1 / -1' } : {});
      cell.appendChild(U.el('label', { text: labelText, style: 'display:block;margin:0 0 2px;font-weight:600;font-size:13px;color:#445' }));
      el.style.cssText = FS; cell.appendChild(el); g.appendChild(cell); return el;
    }
    function chk(labelText, on) {
      var c = U.el('input', { type: 'checkbox' }); c.checked = !!on;
      box.appendChild(U.el('label', { style: 'display:flex;gap:8px;align-items:center;margin:4px 0;font-weight:600' }, [c, U.el('span', { text: labelText })]));
      return c;
    }

    var enabled = chk('啟用一鍵發送（關閉＝發送鈕擋下）', cfg.enabled);

    var sg = section('SMTP relay（公司內部免認證主機；不需帳密）');
    var host = fld(sg, 'SMTP 主機', U.el('input', { value: cfg.smtp_host || '', placeholder: '例 mailrelay.公司內網' }), true);
    var port = fld(sg, '埠', U.el('input', { type: 'number', value: String(cfg.smtp_port || 25) }));
    var tls = U.el('input', { type: 'checkbox' }); tls.checked = !!cfg.use_tls;
    var tlsCell = U.el('div'); tlsCell.appendChild(U.el('label', { style: 'display:flex;gap:8px;align-items:center;margin-top:22px;font-weight:600' }, [tls, U.el('span', { text: 'STARTTLS 加密（內部 relay 多免）' })])); sg.appendChild(tlsCell);

    var fg = section('寄件與內容');
    var fromDef = fld(fg, '系統預設寄件人（操作者本人無信箱時用）', U.el('input', { value: cfg.from_default || '', placeholder: 'noreply@公司' }), true);
    var subj = fld(fg, '主旨前綴', U.el('input', { value: cfg.subject_prefix || '' }));
    var copyTo = fld(fg, '系統總備份信箱（每封密件備份一份，可核對有無寄出）', U.el('input', { value: cfg.copy_to || '', placeholder: '選填，建議填一個留底信箱' }));
    var siteUrl = fld(fg, '系統網址（每週報告信裡放直達連結用）', U.el('input', { value: cfg.site_url || '', placeholder: '例 http://10.30.0.1:3100' }), true);
    var ccSelf = U.el('input', { type: 'checkbox' }); ccSelf.checked = !!cfg.cc_self;
    var ccCell = U.el('div', { style: 'grid-column:1 / -1' }); ccCell.appendChild(U.el('label', { style: 'display:flex;gap:8px;align-items:center;font-weight:600' }, [ccSelf, U.el('span', { text: '每封副本給操作者本人（登入者才有信箱）' })])); fg.appendChild(ccCell);

    var rg = section('納入範圍');
    var incOver = U.el('input', { type: 'checkbox' }); incOver.checked = cfg.include_overdue !== false;
    var incSoon = U.el('input', { type: 'checkbox' }); incSoon.checked = !!cfg.include_soon;
    var oCell = U.el('div'); oCell.appendChild(U.el('label', { style: 'display:flex;gap:8px;align-items:center;font-weight:600' }, [incOver, U.el('span', { text: '逾期未結' })])); rg.appendChild(oCell);
    var sCell = U.el('div'); sCell.appendChild(U.el('label', { style: 'display:flex;gap:8px;align-items:center;font-weight:600' }, [incSoon, U.el('span', { text: '近期到期' })])); rg.appendChild(sCell);
    var soon = fld(rg, '近期到期天數門檻', U.el('input', { type: 'number', value: String(cfg.soon_days || 30) }));
    var gfb = fld(rg, '查無負責人與窗口信箱時，統一轉寄給（選填）', U.el('input', { value: cfg.global_fallback || '', placeholder: '選填' }), true);

    function collect() {
      return { enabled: enabled.checked, smtp_host: host.value.trim(), smtp_port: parseInt(port.value, 10) || 25,
        use_tls: tls.checked, from_default: fromDef.value.trim(), subject_prefix: subj.value,
        copy_to: copyTo.value.trim(), site_url: siteUrl.value.trim(),
        cc_self: ccSelf.checked, include_overdue: incOver.checked, include_soon: incSoon.checked,
        soon_days: parseInt(soon.value, 10) || 30, global_fallback: gfb.value.trim() };
    }

    var tg = section('測試寄信（確認伺服器連得上 relay；預設寄給自己）');
    var tTo = fld(tg, '收件人（留空＝寄給自己）', U.el('input', { placeholder: '可留空' }), true);
    var tBtn = U.el('button', { class: 'btn btn-secondary', text: '寄測試信' });
    var tOut = U.el('span', { style: 'margin-left:10px;font-size:13px' });
    tBtn.addEventListener('click', async function () {
      tOut.textContent = '儲存設定並寄送中…'; tOut.style.color = '#666';
      try {
        await fetch('/api/email-settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(collect()) });
        var r = await (await fetch('/api/email-test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to: tTo.value.trim() || null }) })).json();
        tOut.textContent = (r.ok ? '✅ ' : '⚠️ ') + (r.message || ''); tOut.style.color = r.ok ? '#1a7f4b' : '#c0392b';
      } catch (e) { tOut.textContent = '測試失敗'; tOut.style.color = '#c0392b'; }
    });
    box.appendChild(U.el('div', { style: 'display:flex;align-items:center;margin:8px 0' }, [tBtn, tOut]));

    var save = U.el('button', { class: 'btn btn-primary', text: '儲存設定' });
    save.addEventListener('click', async function () {
      var r = await fetch('/api/email-settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(collect()) });
      if (r.ok) { UI.toast('已儲存 Email 設定', 'success'); UI.closeModal(); } else { UI.toast('儲存失敗', 'error'); }
    });
    UI.openModal('Email／SMTP 設定（Super Admin）', box, { footer: save, sticky: true, wide: true });
  }

  // ---- 一鍵發送（Super Admin／部門窗口）：伺服器端寄催辦信。先預覽計畫，確認才寄 ----
  async function openSendReminders() {
    var data;
    try { data = await jget('/api/send-reminders/preview?' + qd()); }
    catch (e) {
      var st = (e && e.message || '').indexOf(' 403') >= 0;
      UI.toast(st ? '需最高權限或部門窗口才能發送' : '讀取失敗', 'error'); return;
    }
    var box = U.el('div');
    var plan = data.plan || [];
    var sendable = plan.filter(function (p) { return p.mode !== 'skip'; });
    var nFall = plan.filter(function (p) { return p.mode === 'fallback'; }).length;
    var nSkip = plan.filter(function (p) { return p.mode === 'skip'; }).length;

    // 狀態提示：未設定／未啟用時說清楚，並擋住送出
    var blocked = !data.configured || !data.enabled;
    if (!data.configured) {
      box.appendChild(U.el('div', { class: 'scope-info', style: 'color:#c0392b' }, [U.el('span', { text: '尚未設定寄信主機。請最高管理員到「其他功能 → Email／SMTP 設定」填寫並啟用。以下僅為預覽。' })]));
    } else if (!data.enabled) {
      box.appendChild(U.el('div', { class: 'scope-info', style: 'color:#c0392b' }, [U.el('span', { text: '一鍵發送尚未啟用。請最高管理員到「Email／SMTP 設定」開啟。以下僅為預覽。' })]));
    }
    box.appendChild(U.el('p', { class: 'empty-hint', text:
      '範圍：' + (data.scope || '全部') + '　·　寄件者：' + (data['from'] || '（未設定）') +
      (data.cc_self ? '（副本給自己）' : '') }));

    if (!plan.length) {
      box.appendChild(U.el('p', { class: 'empty-hint', text: '目前無逾期／近期到期的待催辦項目。' }));
      UI.openModal('一鍵發送', box, { sticky: true, wide: true }); return;
    }

    var checks = {};
    var sampleBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '看範例信' });
    sampleBtn.addEventListener('click', async function () {
      var first = plan.filter(function (p) { return p.mode !== 'skip'; })[0] || plan[0];
      try {
        var s = await jget('/api/send-reminders/sample?' + qd({ owner: first.owner }));
        if (!s.ok) { UI.toast(s.message || '無法產生範例', 'error'); return; }
        var sb = U.el('div', {}, [
          U.el('p', { class: 'empty-hint', text: '收件人：' + (s.to || '（查無，將轉窗口／略過）') + '　·　' + s.owner }),
          U.el('div', { style: 'font-weight:700;margin:4px 0' }, [U.el('span', { text: s.subject })]),
          U.el('pre', { style: 'white-space:pre-wrap;background:#f6f8f7;padding:10px;border-radius:6px;font-family:inherit;font-size:13px', text: s.body }),
        ]);
        UI.openModal('範例信（' + s.owner + '）', sb, { stack: true, sticky: true });
      } catch (e) { UI.toast('讀取範例失敗', 'error'); }
    });
    var tools = U.el('div', { class: 'batch-tools', style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap' }, [
      U.el('span', { class: 'batch-tools-label', text: '勾選要寄的負責人（可寄 ' + sendable.length + '　轉窗口 ' + nFall + '　查無信箱 ' + nSkip + '）' }),
      U.el('button', { class: 'btn btn-secondary btn-sm', text: '全選', onclick: function () { Object.keys(checks).forEach(function (k) { checks[k].checked = true; }); } }),
      U.el('button', { class: 'btn btn-secondary btn-sm', text: '全不選', onclick: function () { Object.keys(checks).forEach(function (k) { checks[k].checked = false; }); } }),
      sampleBtn,
    ]);
    box.appendChild(tools);

    var list = U.el('div', { style: 'max-height:46vh;overflow:auto;margin-top:8px' });
    plan.forEach(function (p) {
      var row = U.el('div', { style: 'display:flex;gap:10px;align-items:center;padding:5px 2px;border-bottom:1px solid #eef2f5' });
      var label;
      if (p.mode === 'send') label = '→ ' + p.to;
      else if (p.mode === 'fallback') label = '→ 轉窗口 ' + p.to + '（查無本人信箱）';
      else label = '查無信箱，略過';
      if (p.mode !== 'skip') {
        var cb = U.el('input', { type: 'checkbox' }); cb.checked = true; checks[p.owner] = cb;
        row.appendChild(cb);
      } else {
        row.appendChild(U.el('span', { style: 'width:13px' }));
      }
      row.appendChild(U.el('span', { style: 'font-weight:600;min-width:7em', text: p.owner }));
      row.appendChild(U.el('span', { class: 'empty-hint', style: 'margin:0;min-width:4em', text: p.count + ' 筆' }));
      row.appendChild(U.el('span', { class: 'empty-hint', style: 'margin:0;color:' + (p.mode === 'skip' ? '#c0392b' : '#445'), text: label }));
      list.appendChild(row);
    });
    box.appendChild(list);
    var result = U.el('div', { style: 'margin-top:10px' });
    box.appendChild(result);

    var sending = false;
    var sendBtn = U.el('button', { class: 'btn btn-primary', text: '確認寄出' });
    if (blocked) { sendBtn.disabled = true; sendBtn.title = '需先設定並啟用寄信'; }
    sendBtn.addEventListener('click', async function () {
      if (sending || blocked) return;
      var owners = Object.keys(checks).filter(function (k) { return checks[k].checked; });
      if (!owners.length) { UI.toast('請至少勾選一位', 'error'); return; }
      if (!confirm('確認寄出催辦信給 ' + owners.length + ' 位負責人？')) return;
      sending = true; sendBtn.disabled = true; sendBtn.textContent = '寄送中…';
      try {
        var r = await fetch('/api/send-reminders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ owners: owners }) });
        var j = await r.json();
        if (!r.ok) { UI.toast(j.detail || '寄送失敗', 'error'); return; }
        result.innerHTML = '';
        result.appendChild(U.el('div', { class: 'scope-info' + (j.failed > 0 ? '' : ''), style: 'font-weight:600' }, [U.el('span', { text:
          '完成：寄出 ' + j.sent + '　轉窗口 ' + j.fallback + '　略過 ' + j.skipped + '　失敗 ' + j.failed })]));
        (j.details || []).forEach(function (d) {
          var txt = d.mode === 'skip' ? (d.owner + '：略過（' + (d.error || '查無信箱') + '）')
                  : d.error ? (d.owner + '：失敗 ' + d.error)
                  : (d.owner + ' → ' + d.to + (d.mode === 'fallback' ? '（轉窗口）' : ''));
          result.appendChild(U.el('div', { class: 'empty-hint', style: 'margin:2px 0;color:' + (d.error ? '#c0392b' : '#445'), text: txt }));
        });
        UI.toast(j.failed > 0 ? ('有 ' + j.failed + ' 封失敗') : ('已寄出 ' + j.sent + ' 封'), j.failed > 0 ? 'error' : 'success');
        sendBtn.textContent = '已寄出';
      } catch (e) { UI.toast('寄送時發生錯誤', 'error'); sendBtn.disabled = false; sendBtn.textContent = '確認寄出'; }
      finally { sending = false; }
    });
    UI.openModal('一鍵發送（催辦信）', box, { footer: sendBtn, sticky: true, wide: true });
  }

  // ---- 我的操作紀錄（本人自查：何時申請/上傳/變更了什麼）----
  async function openMyActivity() {
    var data;
    try { data = await jget('/api/my-activity'); }
    catch (e) { UI.toast('讀取失敗', 'error'); return; }
    var box = U.el('div');
    box.appendChild(U.el('p', { class: 'empty-hint', text: '範圍：' + (data.scope || '本人') + '　·　最近 ' + (data.items || []).length + ' 筆（新到舊）。' }));
    if (!data.items || !data.items.length) {
      box.appendChild(U.el('p', { class: 'empty-hint', text: '尚無紀錄。' }));
      UI.openModal('我的操作紀錄', box, { sticky: true, wide: true }); return;
    }
    var showUser = data.scope !== '本人';
    var table = U.el('table', { class: 'tracking-table' });
    var heads = ['時間'].concat(showUser ? ['員編'] : []).concat(['動作', '對象', '內容']);
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, heads.map(function (h) { return U.el('th', { text: h }); }))]));
    var tb = U.el('tbody');
    data.items.forEach(function (a) {
      var tds = [U.el('td', { text: (a.at || '').replace('T', ' '), style: 'white-space:nowrap' })];
      if (showUser) tds.push(U.el('td', { text: a.username || '' }));
      tds.push(U.el('td', { text: a.action_label || a.action }));
      tds.push(U.el('td', { text: a.target || '', style: 'white-space:normal;text-align:left' }));
      tds.push(U.el('td', { text: a.detail || '', style: 'white-space:normal;text-align:left;color:#667' }));
      tb.appendChild(U.el('tr', {}, tds));
    });
    table.appendChild(tb); makeSortable(table);
    box.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
    UI.openModal('我的操作紀錄', box, { sticky: true, wide: true });
  }

  // ---- 發信紀錄（Super Admin）：逐封寄送結果 ----
  async function openMailLog() {
    var rows;
    try { rows = await jget('/api/mail-log'); }
    catch (e) { UI.toast('讀取失敗（需最高權限）', 'error'); return; }
    var box = U.el('div');
    if (!rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '尚無發信紀錄。' })); UI.openModal('發信紀錄', box, { sticky: true, wide: true }); return; }
    var table = U.el('table', { class: 'tracking-table' });
    var cols = [['sent_at', '時間'], ['sender', '操作者'], ['owner', '負責人'], ['to', '收件人'], ['mode', '方式'], ['status', '狀態'], ['count', '筆數'], ['error', '錯誤']];
    table.appendChild(U.el('thead', {}, [U.el('tr', {}, cols.map(function (c) { return U.el('th', { text: c[1] }); }))]));
    var tb = U.el('tbody');
    var modeLabel = { send: '寄本人', fallback: '轉窗口', skip: '略過', weekly: '每週' };
    rows.forEach(function (m) {
      var tr = U.el('tr', { class: (m.status === 'failed' ? 'row-overdue' : '') });
      cols.forEach(function (c) {
        var v = m[c[0]];
        if (c[0] === 'sent_at') v = (v || '').replace('T', ' ');
        if (c[0] === 'mode') v = modeLabel[v] || v;
        var td = U.el('td', { text: v == null ? '' : String(v) });
        if (c[0] === 'error' || c[0] === 'to') { td.style.whiteSpace = 'normal'; td.style.textAlign = 'left'; }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    table.appendChild(tb); makeSortable(table);
    box.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
    UI.openModal('發信紀錄（' + rows.length + ' 筆）', box, { sticky: true, wide: true });
  }

  // ---- 系統設定（Super Admin）：session 時數、每週排程試跑 ----
  async function openGeneralSettings() {
    var cfg;
    try { cfg = await jget('/api/general-settings'); }
    catch (e) { UI.toast('讀取失敗（需最高權限）', 'error'); return; }
    var box = U.el('div');
    var FS = 'padding:6px 10px;border:1px solid #cdd5dd;border-radius:6px;font-size:14px;width:120px';
    box.appendChild(U.el('div', { style: 'margin:8px 0 6px;font-weight:700;color:#1a7f4b' }, [U.el('span', { text: '登入有效時數' })]));
    var ttl = U.el('input', { type: 'number', min: '1', max: '720', value: String(cfg.session_ttl_hours || 12) }); ttl.style.cssText = FS;
    box.appendChild(U.el('div', { style: 'display:flex;gap:10px;align-items:center' }, [ttl, U.el('span', { class: 'empty-hint', style: 'margin:0', text: '小時（改完只影響「新登入」；1～720）' })]));
    var save = U.el('button', { class: 'btn btn-primary btn-sm', text: '儲存', style: 'margin-top:8px' });
    save.addEventListener('click', async function () {
      var r = await fetch('/api/general-settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_ttl_hours: parseInt(ttl.value, 10) || 12 }) });
      UI.toast(r.ok ? '已儲存' : '儲存失敗', r.ok ? 'success' : 'error');
    });
    box.appendChild(save);

    box.appendChild(U.el('div', { style: 'margin:18px 0 6px;font-weight:700;color:#1a7f4b;border-top:1px solid #eef2f5;padding-top:12px' }, [U.el('span', { text: '每週部門週報（排程）' })]));
    box.appendChild(U.el('p', { class: 'empty-hint', text: '平時由伺服器 cron 每週一 08:00 自動寄給「有開啟」的部門窗口。可在此手動試跑一次。' }));
    var runOut = U.el('span', { style: 'margin-left:10px;font-size:13px' });
    var runBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '立即試跑一次' });
    runBtn.addEventListener('click', async function () {
      if (!window.confirm('立即寄一次每週週報給所有「有開啟」的部門窗口？')) return;
      runOut.textContent = '寄送中…'; runOut.style.color = '#666';
      try {
        var r = await fetch('/api/send-weekly', { method: 'POST' });
        var j = await r.json();
        if (!r.ok) { runOut.textContent = '⚠️ ' + (j.detail || '失敗'); runOut.style.color = '#c0392b'; return; }
        runOut.textContent = '✅ 寄出 ' + j.sent + '　略過 ' + j.skipped + '　失敗 ' + j.failed; runOut.style.color = '#1a7f4b';
      } catch (e) { runOut.textContent = '試跑失敗'; runOut.style.color = '#c0392b'; }
    });
    box.appendChild(U.el('div', { style: 'display:flex;align-items:center' }, [runBtn, runOut]));
    UI.openModal('系統設定（Super Admin）', box, { sticky: true });
  }

  // ---- 其他功能（移到左側成一項）：卡片式分類、全部靠左對齊 ----
  function renderMoreInto(host) {
    if (!host) return;
    host.innerHTML = ''; host.classList.remove('webext-rpt');
    var ft = document.getElementById('file-name-tag');
    var fn = _dataFile || (ft && ft.textContent.trim()) || '（未知）';
    host.appendChild(U.el('div', { class: 'scope-info', style: 'margin:0 0 14px;text-align:left' }, [U.el('span', { text: '目前資料檔：' + fn })]));

    function trigger(id) { return function () { var el = document.getElementById(id); if (el) el.click(); else UI.toast('找不到此功能', 'error'); }; }
    // 分類卡片容器
    var wrap = U.el('div', { style: 'display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start' });
    host.appendChild(wrap);
    function card(title) {
      var c = U.el('div', { style: 'flex:1 1 320px;min-width:300px;max-width:460px;border:1px solid #e1e7ec;border-radius:10px;padding:12px 14px;background:#fff' });
      c.appendChild(U.el('div', { style: 'font-weight:700;font-size:15px;color:#1a7f4b;text-align:left;margin:0 0 10px;padding-left:8px;border-left:3px solid #1a7f4b' }, [U.el('span', { text: title })]));
      wrap.appendChild(c); return c;
    }
    // 靠左的功能列：粗體標題＋灰色說明，整顆按鈕 justify 靠左
    function item(c, label, desc, onClick) {
      var inner = U.el('span', { style: 'display:flex;flex-direction:column;align-items:flex-start;gap:1px;text-align:left;line-height:1.3' }, [
        U.el('span', { text: label, style: 'font-weight:600' }),
      ]);
      if (desc) inner.appendChild(U.el('span', { text: desc, style: 'font-size:12px;color:#8a97a3' }));
      var b = U.el('button', { class: 'btn btn-secondary', style: 'display:flex;justify-content:flex-start;text-align:left;width:100%;margin:0 0 8px;padding:9px 12px' }, [inner]);
      b.addEventListener('click', onClick); c.appendChild(b); return b;
    }

    // ① 說明文件（所有人）
    var cDoc = card('說明文件');
    item(cDoc, '使用說明', '系統怎麼用', trigger('help-btn'));
    item(cDoc, '資安說明', '弱點與處置邏輯', trigger('security-btn'));

    // ② 我的（登入者；免登入時也顯示我的紀錄）
    var cMine = card('我的');
    item(cMine, '我的操作紀錄', '查我何時申請／上傳／變更了什麼', openMyActivity);
    if (me.authenticated && me.role === 'dept_admin') {
      var wcb = U.el('input', { type: 'checkbox' }); wcb.checked = !!me.weekly_report;
      wcb.addEventListener('change', async function () {
        try {
          var r = await fetch('/api/my-weekly', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: wcb.checked }) });
          if (r.ok) { me.weekly_report = wcb.checked; UI.toast(wcb.checked ? '已開啟：每週一自動寄你部門週報' : '已關閉每週週報', 'success'); }
          else { wcb.checked = !wcb.checked; UI.toast('設定失敗', 'error'); }
        } catch (e) { wcb.checked = !wcb.checked; UI.toast('設定失敗', 'error'); }
      });
      cMine.appendChild(U.el('label', { style: 'display:flex;gap:8px;align-items:flex-start;text-align:left;cursor:pointer;padding:9px 12px;border:1px dashed #cfe3d8;border-radius:8px' }, [
        wcb, U.el('span', { style: 'display:flex;flex-direction:column;gap:1px' }, [
          U.el('span', { text: '每週一自動收我部門的週報', style: 'font-weight:600' }),
          U.el('span', { text: '方便向上報告；可隨時關', style: 'font-size:12px;color:#8a97a3' })]) ]));
    }

    if (!isSuper()) return;
    // ③ 資料管理（Super Admin）
    var cData = card('資料管理（Super Admin）');
    item(cData, '上傳新的彙總表', '重新選擇弱點彙總 Excel', trigger('reload-btn'));
    item(cData, '清除暫存資料', '清掉本機暫存', trigger('clear-btn'));
    item(cData, '功能開關', '前端功能的開關', trigger('features-btn'));
    item(cData, '承辦狀態備份／還原', '處理進度／預計完成日／備註 匯出成檔，或從檔案匯回', openCaseBackup);

    // ④ 通知（Super Admin）
    var cMail = card('通知 Email（Super Admin）');
    item(cMail, 'Email／SMTP 設定', 'relay 主機／寄件人／備份信箱／每週排程', openEmailSettings);
    item(cMail, '發信紀錄', '逐封寄送結果（成功／失敗／轉窗口）', openMailLog);

    // ⑤ 權限與系統（Super Admin）
    var cSys = card('權限與系統（Super Admin）');
    item(cSys, '帳號與權限', '指定部門窗口／補信箱／從 AD 補 mail', openUserAdmin);
    item(cSys, 'AD 登入設定', 'LDAP／員編／測試連線', openAdSettings);
    item(cSys, '系統設定', '登入時數／每週排程試跑', openGeneralSettings);
  }

  // 週報範圍：部門＋工作表兩個下拉（選了就整份週報、各頁籤、下鑽都只算這個範圍）
  function reportFilterBar(onChange) {
    var bar = U.el('div', { style: 'display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:4px 0 12px;padding:8px 12px;background:#f0f6f3;border:1px solid #cfe3d8;border-radius:8px' });
    var dSel = U.el('select', { style: 'padding:5px 8px;font-size:14.5px;border:1px solid #cfd8d3;border-radius:6px;min-width:160px' });
    var sSel = U.el('select', { style: 'padding:5px 8px;font-size:14.5px;border:1px solid #cfd8d3;border-radius:6px;min-width:220px' });
    dSel.appendChild(U.el('option', { value: '__all__', text: '全部門' }));
    sSel.appendChild(U.el('option', { value: '', text: '全部工作表（一起算）' }));
    bar.appendChild(U.el('b', { text: '範圍：' }));
    bar.appendChild(U.el('label', { style: 'display:inline-flex;gap:6px;align-items:center' }, [U.el('span', { text: '部門' }), dSel]));
    bar.appendChild(U.el('label', { style: 'display:inline-flex;gap:6px;align-items:center' }, [U.el('span', { text: '工作表' }), sSel]));
    bar.appendChild(U.el('span', { style: 'color:#6b7a73;font-size:13px', text: '（記在這台瀏覽器；下次開啟沿用）' }));
    Promise.all([jget('/api/departments').catch(function () { return []; }), jget('/api/sheets').catch(function () { return []; })]).then(function (r) {
      (r[0] || []).forEach(function (d) { dSel.appendChild(U.el('option', { value: d, text: d })); });
      (r[1] || []).forEach(function (x) { sSel.appendChild(U.el('option', { value: x.sheet_key, text: x.sheet_key + '（未結 ' + x.open + '）' })); });
      dSel.value = curDept() || '__all__';
      sSel.value = curSheet() || '';
      if (sSel.value !== (curSheet() || '')) { _sheetPick = null; _lsSet('wx_sheet', null); }   // 選過的表已不在這份報告
    });
    dSel.addEventListener('change', function () { _deptPick = dSel.value; _lsSet('wx_dept', dSel.value); onChange(); });
    sSel.addEventListener('change', function () { _sheetPick = sSel.value || null; _lsSet('wx_sheet', sSel.value || null); onChange(); });
    return bar;
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
    var scope = ((s.department && s.department !== '全部') ? s.department : '全部門') + (curSheet() ? '・' + curSheet() : '');

    // 標題列＋列印鈕
    var headRow = U.el('div', { class: 'panel-head', style: 'display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap' }, [
      U.el('h3', { text: '主管週報 — ' + scope + '（' + s.today + '）' }),
    ]);
    var btnWrap = U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' });
    var printBtn = U.el('button', { class: 'btn btn-primary btn-sm', text: '列印 / 存 PDF' });
    printBtn.addEventListener('click', function () { printReport(s); });
    btnWrap.appendChild(printBtn);
    var qBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '季度總覽（列印 / PDF）' });
    qBtn.addEventListener('click', function () { printQuarter(s); });
    btnWrap.appendChild(qBtn);
    // 一鍵發送：Super Admin 或 部門窗口(dept_admin) 可發；SMTP 設定本身仍只給 super
    if (isAdminRole()) {
      var sendBtn = U.el('button', { class: 'btn btn-secondary btn-sm', text: '一鍵發送' });
      sendBtn.addEventListener('click', openSendReminders);
      btnWrap.appendChild(sendBtn);
    }
    headRow.appendChild(btnWrap);
    host.appendChild(headRow);
    host.appendChild(reportFilterBar(function () { renderReportInto(host); }));


    // 選了特定部門 → 清單裡每列部門都一樣,「部門」欄多餘,隱藏(全部門時才顯示,用來分辨)
    var showDept = !(s.department && s.department !== '全部');

    function matchRow(r, terms) {
      var hay = [r.owner, r.host, r.name, r.plugin_id, r.severity, r.effective_due,
        r.target_date, cellVal(r, 'overdue_days'), r.track_note,
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
          var notApply = Math.max(s.unresolved - s.apply_universe, 0);   // 暫無需申請：未結、尚未達申請時機
          var pg = s.progress || {};
          // ① 分解表放最前面（使用者 2026-10-07）：未結 → 需申請／暫無需申請 → 應申請未申請／已核准 → 承辦進度
          applyTree(c, s, notApply);
          jget('/api/compare?' + qd(_cmpPrev ? { prev: _cmpPrev } : {})).then(function (cp) { _lastCompare = cp; }).catch(function () { });   // 列印週報要帶比較數字（不必先點那一頁）
          // ② 概況 KPI 卡（每個數字可點下鑽）
          kpiCards(c, '概況', [
            { label: '未結', value: s.unresolved, drill: function () { openFindings('未結案', {}); } },
            // 跟總覽同一套：已逾期／近期到期／高風險未結 三者互斥（使用者 2026-10-06：數字要對得上才有說服力）
            { label: '已逾期', value: s.overdue, danger: true, drill: function () { openFindings('已逾期', { band: '已逾期' }); } },
            { label: '近期到期（30 天內）', value: s.soon, drill: function () { openFindings('近期到期（30 天內）', { due_min: '0', due_max: '30' }); } },
            { label: '高風險未結（不含逾期、近期）', value: s.high_risk_only, danger: true, drill: function () { openFindings('高風險未結（不含已逾期、近期到期）', { risk: 'high_only' }); } },
            { label: '其中高風險且逾期', value: s.high_risk_overdue, danger: true, drill: function () { openFindings('高風險且逾期', { risk: 'high', band: '已逾期' }); } },
          ]);
          // 需申請／應申請未申請／已核准 已在最上面的分解表，這裡只留表上沒有的
          kpiCards(c, '申請追蹤', [
            { label: '未回報預計完成日', value: s.target.no_target, danger: true, drill: function () { openFindings('未回報預計完成日', { no_target: 'true' }); } },
            { label: '待追查', value: pg.flagged || 0, danger: true, drill: function () { openFindings('待追查（聲稱申請或完成，來源未反映）', { flagged: 'true' }); } },
          ]);


          // ③ 視覺：嚴重度圓餅 + 到期倒數長條（皆可點下鑽）
          var vis = U.el('div', { style: 'display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start;margin:10px 0' });
          var pie = U.el('div', { style: 'flex:1 1 300px;min-width:280px' });
          pie.appendChild(U.el('div', { class: 'panel-head' }, [U.el('h3', { text: '嚴重度分佈（未結案）' })]));
          var sevItems = severityItems(s.severity, function (k) { openFindings(k + '（嚴重度）', { severity: k }); });
          if (sevItems.length) donutChart(pie, sevItems, '未結'); else pie.appendChild(U.el('p', { class: 'empty-hint', text: '無未結案。' }));
          var bar = U.el('div', { style: 'flex:1 1 300px;min-width:280px' });
          bar.appendChild(U.el('div', { class: 'panel-head' }, [U.el('h3', { text: '到期倒數（未結案·真正到期日）' })]));
          // 分段跟下鑽的到期篩選同一套（使用者 2026-10-07）：已逾期／14／30／60／90／120／180／360／360 以上／無到期日
          var FINE = [['overdue', '已逾期', { band: '已逾期' }, '#c0392b'], ['d14', '14 天內', { due_min: '0', due_max: '14' }, '#d84315'],
            ['d30', '15–30 天', { due_min: '15', due_max: '30' }, '#ef6c00'], ['d60', '31–60 天', { due_min: '31', due_max: '60' }, '#f9a825'],
            ['d90', '61–90 天', { due_min: '61', due_max: '90' }, '#c0ca33'], ['d120', '91–120 天', { due_min: '91', due_max: '120' }, '#7cb342'],
            ['d180', '121–180 天', { due_min: '121', due_max: '180' }, '#43a047'], ['d360', '181–360 天', { due_min: '181', due_max: '360' }, '#26a69a'],
            ['d360plus', '360 天以上', { due_min: '361' }, '#90a4ae'], ['no_due', '無到期日', { no_due: 'true' }, '#cfd8dc']];
          var df = s.due_fine || {};
          hbarChart(bar, FINE.filter(function (x) { return x[0] !== 'no_due' || df.no_due; }).map(function (x) {
            return { label: x[1], value: df[x[0]] || 0, color: x[3], key: x[0], onClick: function () { openFindings(x[1] + '（到期倒數）', x[2]); } };
          }));
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
            c.appendChild(U.el('p', { class: 'empty-hint', text: '本期變化（本批 vs 上批）：新增 ' + s.change.new + '、解決 ' + s.change.resolved + '，淨變化 ' + (s.change.delta > 0 ? '+' : '') + s.change.delta + '。' }));
          }

          // ⑥ 處理進度（管理人標註，次要資訊）
          kpiCards(c, '處理進度（管理人標註；結論以資安 Excel 為準）', [
            { label: '⚠ 備註有寫、進度未設', value: pg.note_no_progress || 0, danger: true, drill: function () { openFindings('⚠ 待補處理進度（追蹤備註有寫、處理進度沒設）', { note_no_progress: 'true' }); } },
            { label: '已回報（進度／預計完成／備註任一）', value: pg.reported || 0, drill: function () { openFindings('已回報的弱點（處理進度／預計完成日／追蹤備註）', { reported: 'true' }); } },
            { label: '要申請展延', value: pg.apply_ext || 0, drill: function () { openFindings('要申請展延', { progress: '要申請展延' }); } },
            { label: '要申請例外', value: pg.apply_exc || 0, drill: function () { openFindings('要申請例外', { progress: '要申請例外' }); } },
            { label: '處理中', value: pg.wip || 0, drill: function () { openFindings('處理中', { progress: '處理中' }); } },
            { label: '結案申請中', value: pg.rescan || 0, drill: function () { openFindings('結案申請中', { progress: '等複掃' }); } },
          ]);
      } },
      { label: '與上次比較', render: function (c) { compareSection(c); } },   // 2026-10-07：獨立一頁
      { label: '負責人', render: function (c) { renderOwnerInto(c); } },       // 併入：誰還有幾支＋狀態(每格下鑽)
      { label: '到期倒數', render: function (c) { renderDueSoonInto(c); } },    // 併入：14/30/60/90 分桶(含 14 天申請行動線)
      { label: '處置落點', render: function (c) { renderStageLanding(c, s); } },
      { label: '應申請未申請（' + s.need_apply_list.length + '）', render: function (c) {
          listTab(c, '應申請未申請清單', s.need_apply_list);
      } },
      { label: '要申請·送審中（' + (s.apply_intent_list || []).length + '）', render: function (c) {
          listTab(c, '要申請·送審中（已標註申請，來源尚未反映）', s.apply_intent_list || []);
      } },
      { label: '待追查（' + (s.flagged_list || []).length + '）', render: function (c) {
          listTab(c, '待追查清單（聲稱申請或完成，來源未反映）', s.flagged_list || []);
      } },
      { label: '逾期（' + s.overdue_list.length + '）', render: function (c) {
          listTab(c, '逾期清單（已逾到期日）', s.overdue_list);
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
  // ── 與上次匯入比較（2026-10-07）：主管看數字；每個數字可點，開的是同一個下鑽視窗（多一欄「變了什麼」）──
  // 差額顏色：變好綠、變差紅。哪個方向算好看指標性質（未結/逾期變少＝好；核准/送審變多＝好）。
  var CMP_GOOD_DOWN = { unresolved: 1, overdue: 1, soon: 1, high_only: 1, apply_universe: 1, need_apply: 1, todo: 1 };
  var CMP_GOOD_UP = { applied: 1, wip: 1, subext: 1, subexc: 1, rescan: 1 };
  var _cmpPrev = null;   // 使用者選的「跟哪一次比」（null＝前一批）
  var _lastCompare = null;
  // 列印版：只放數字（主管看），不放清單
  function cmpPrintBlock(esc) {
    var cp = _lastCompare;
    if (!cp || !cp.has_prev) return '';
    var rows = cp.metrics.map(function (m) {
      return '<tr><td style="text-align:left">' + esc(m.label) + '</td><td>' + (m.prev == null ? '未記錄' : m.prev) + '</td><td>' + m.cur
        + '</td><td>' + (m.delta == null ? '—' : (m.delta > 0 ? '+' : '') + m.delta) + '</td></tr>';
    }).join('');
    var ev = cp.events.filter(function (e) { return e.n; }).map(function (e) { return esc(e.label) + ' ' + e.n; }).join('　·　');
    return '<div class="block"><b>與上次匯入比較（上次＝' + esc((cp.prev.asof || '').replace('T', ' ')) + '）</b><br>' + esc(cp.summary)
      + '<table><thead><tr><th>項目</th><th>上次</th><th>本次</th><th>差額</th></tr></thead><tbody>' + rows + '</tbody></table>'
      + (ev ? '<div>本期變動：' + ev + '</div>' : '') + '</div>';
  }
  function compareSection(host) {
    var wrap = U.el('div', { style: 'margin:12px 0 16px;border:1px solid #cfe0d6;border-radius:10px;padding:10px 12px;background:#fbfdfc' });
    host.appendChild(wrap);
    function render() {
      wrap.innerHTML = '';
      var head = U.el('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px' },
        [U.el('span', { text: '與上次匯入比較', style: 'font-weight:700;font-size:16px;color:#0f5f35' })]);
      wrap.appendChild(head);
      var body = U.el('div', { text: '讀取中…', style: 'color:#6b7a73' }); wrap.appendChild(body);
      var q = {}; if (_cmpPrev) q.prev = _cmpPrev;
      jget('/api/compare?' + qd(q)).then(function (cp) {
        _lastCompare = cp;   // 列印週報時帶這份
        body.innerHTML = '';
        if (!cp.has_prev) { body.appendChild(U.el('p', { class: 'empty-hint', text: cp.reason || '無法比較' })); return; }
        // 選「跟哪一次比」
        var sel = U.el('select', { style: 'padding:3px 8px;border:1px solid #cdd5dd;border-radius:6px;font-size:13.5px' });
        (cp.batches || []).filter(function (b) { return !b.is_latest; }).forEach(function (b) {
          var o = U.el('option', { value: String(b.id), text: (b.imported_at || '').replace('T', ' ') + '　' + (b.source_file || '') + '（' + b.row_count + ' 列）' });
          if (cp.prev && b.id === cp.prev.id) o.selected = true;
          sel.appendChild(o);
        });
        sel.addEventListener('change', function () { _cmpPrev = sel.value; render(); });
        head.appendChild(U.el('span', { text: '跟', style: 'color:#6b7a73;font-size:13.5px' })); head.appendChild(sel);
        head.appendChild(U.el('span', { text: '比；上次＝那批被換掉當下（' + ((cp.prev.asof || '').replace('T', ' ')) + '）的狀態，本次＝現在', style: 'color:#6b7a73;font-size:13px' }));
        if (isSuper()) {
          var mg = U.el('button', { class: 'btn btn-sm', text: '匯入批次管理', style: 'margin-left:auto' });
          mg.addEventListener('click', function () { openBatchManager(render); });
          head.appendChild(mg);
        }
        body.appendChild(U.el('div', { text: cp.summary, style: 'font-weight:700;font-size:15px;margin:2px 0 8px;color:#2a3430' }));
        var lay = U.el('div', { style: 'display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start' });
        body.appendChild(lay);
        // 數字表
        var t = U.el('table', { class: 'tracking-table', style: 'width:auto;min-width:420px;margin:0' });
        t.appendChild(U.el('thead', {}, [U.el('tr', {}, ['項目', '上次', '本次', '差額'].map(function (h) { return U.el('th', { text: h }); }))]));
        var tb = U.el('tbody');
        function numCell(txt, onClick, style) {
          var td = U.el('td', { text: txt, style: 'font-weight:700;font-size:15px;' + (style || '') });
          if (onClick) { td.style.cursor = 'pointer'; td.style.textDecoration = 'underline dotted'; td.title = '點我看清單'; td.addEventListener('click', onClick); }
          return td;
        }
        function open(label, extra) {
          var pm = Object.assign({ _src: '/api/compare/rows' }, extra); if (_cmpPrev) pm.prev = _cmpPrev;
          openFindings(label, pm);
        }
        cp.metrics.forEach(function (m) {
          var sub = /^　/.test(m.label);
          var unk = m.prev == null;
          var dcol = '#6b7a73';
          if (!unk && m.delta) {
            var good = CMP_GOOD_DOWN[m.key] ? m.delta < 0 : (CMP_GOOD_UP[m.key] ? m.delta > 0 : null);
            dcol = good == null ? '#6b7a73' : (good ? '#1a7f4b' : '#c0392b');
          }
          tb.appendChild(U.el('tr', {}, [
            U.el('td', { text: m.label, style: 'text-align:left;' + (sub ? 'color:#55625c' : 'font-weight:600') }),
            unk ? U.el('td', { text: '未記錄', title: '這批是補算的舊資料，當時的承辦進度沒有拍下來', style: 'color:#9aa5a0' })
              : numCell(String(m.prev), m.prev ? function () { open(m.label.trim() + '（上次）', { metric: m.key, side: 'prev' }); } : null),
            numCell(String(m.cur), m.cur ? function () { open(m.label.trim() + '（本次）', { metric: m.key, side: 'cur' }); } : null),
            unk ? U.el('td', { text: '—', style: 'color:#9aa5a0' })
              : numCell((m.delta > 0 ? '+' : '') + m.delta, function () { open(m.label.trim() + '（進出明細）', { metric: m.key, side: 'both' }); }, 'color:' + dcol),
          ]));
        });
        t.appendChild(tb); lay.appendChild(t);
        // 本期變動
        var evBox = U.el('div', { style: 'flex:1 1 280px;min-width:260px' });
        evBox.appendChild(U.el('div', { text: '本期變動（點數字看是哪幾筆）', style: 'font-weight:700;color:#0f5f35;margin-bottom:6px' }));
        var TONE = { good: '#1a7f4b', bad: '#c0392b', info: '#55625c' };
        cp.events.forEach(function (e) {
          evBox.appendChild(U.el('div', { style: 'display:flex;justify-content:space-between;gap:10px;padding:4px 8px;border-bottom:1px solid #edf2ef' }, [
            U.el('span', { text: e.label }),
            numCell(String(e.n), e.n ? function () { open(e.label, { event: e.key }); } : null, 'color:' + (e.n ? TONE[e.tone] : '#9aa5a0'))]));
        });
        lay.appendChild(evBox);
        // 對帳等式
        var eq = cp.equation;
        body.appendChild(U.el('div', { text: '對帳：' + eq.text + (eq.ok ? '　✓ 等於本次未結 ' + eq.cur_open : '　✗ 對不上（本次未結 ' + eq.cur_open + '），請回報'),
          style: 'margin-top:8px;font-size:13px;color:' + (eq.ok ? '#1a7f4b' : '#c0392b') }));
        if (!cp.progress_recorded) body.appendChild(U.el('div', { class: 'empty-hint', text: '「上次」的承辦進度未記錄：這批是補算的舊資料。從下一次匯入起會自動記錄。' }));
      }).catch(function () { body.textContent = '讀取失敗'; });
    }
    render();
  }

  // 匯入批次管理（Super Admin）：列出每次匯入，可刪匯錯的那批（刪前伺服器會先備份 DB）
  function openBatchManager(onDone) {
    var box = U.el('div', { text: '讀取中…' });
    UI.openModal('匯入批次管理', box, { stack: true });
    jget('/api/batches').then(function (bs) {
      box.innerHTML = '';
      box.appendChild(U.el('p', { class: 'empty-hint', text: '匯錯檔可以刪掉那一批：連同它造成的變化紀錄一起刪；刪的是最新批，就回到前一批。刪之前伺服器會自動備份資料庫。承辦填的進度、備註不受影響。' }));
      var t = U.el('table', { class: 'tracking-table' });
      t.appendChild(U.el('thead', {}, [U.el('tr', {}, ['匯入時間', '檔名', '列數', '', ''].map(function (h) { return U.el('th', { text: h }); }))]));
      var tb = U.el('tbody');
      bs.forEach(function (b) {
        var del = U.el('button', { class: 'btn btn-sm', text: '刪除這批', style: 'color:#c0392b' });
        if (bs.length < 2) del.disabled = true;
        del.addEventListener('click', async function () {
          var ok = window.prompt('確定刪除 ' + (b.imported_at || '').replace('T', ' ') + ' 匯入的「' + (b.source_file || '') + '」？要刪請輸入：刪除');
          if (ok !== '刪除') return;
          var r = await fetch('/api/batches/' + b.id, { method: 'DELETE' });
          var j = await r.json().catch(function () { return {}; });
          if (!r.ok) { UI.toast('刪除失敗：' + (j.detail || r.status), 'error'); return; }
          UI.toast('已刪除；資料庫備份在 ' + (j.db_backup || '（無）'), 'success');
          UI.closeModal(); if (onDone) onDone();
        });
        tb.appendChild(U.el('tr', {}, [U.el('td', { text: (b.imported_at || '').replace('T', ' ') }), U.el('td', { text: b.source_file || '' }),
          U.el('td', { text: String(b.row_count) }), U.el('td', { text: b.is_latest ? '目前使用中' : '', style: 'color:#1a7f4b;font-weight:700' }),
          U.el('td', {}, [del])]));
      });
      t.appendChild(tb); box.appendChild(t);
    }).catch(function () { box.textContent = '讀取失敗'; });
  }

  // 申請面分解樹：每一層相加＝上一層，每個數字可點下鑽；最底層「應申請未申請」依承辦進度拆
  // （52 筆展延送審＝已送出、只是 Excel 還沒更新；真正沒動的是「尚未修補」那格，只有它用紅字）
  function applyTree(host, s, notApply) {
    ensureTreeStyle();
    var g = U.el('div', { class: 'wx-tree' });
    host.appendChild(U.el('div', { class: 'wx-tree-wrap' }, [U.el('div', { class: 'wx-tree-cap', text: '未結弱點分解（點任一數字看清單）' }), g]));
    function node(label, value, cls, area, drill, hint) {
      var n = U.el('div', { class: 'wx-node ' + cls + (drill && value ? ' clk' : ''), style: 'grid-area:' + area, title: hint || (drill ? '點我看清單' : '') }, [
        U.el('div', { class: 'wx-node-lb', text: label }), U.el('div', { class: 'wx-node-n', text: value == null ? '…' : String(value) })]);
      if (drill && value) n.addEventListener('click', drill);
      g.appendChild(n); return n;
    }
    node('未結', s.unresolved, 'n-root', 'root', function () { openFindings('未結案', {}); });
    node('需申請', s.apply_universe, 'n-need', 'need', function () { openFindings('需申請母體', { apply_universe: 'true' }); }, '已進入到期前 30 天、或已經是展延／例外');
    node('暫無需申請（期限內）', notApply, 'n-not', 'not', function () { openFindings('暫無需申請（期限內）', { not_apply: 'true' }); }, '離到期還超過 30 天，時間到了會自動算進「應申請未申請」');
    node('應申請未申請', s.need_apply_count, 'n-sa', 'sa', function () { openFindings('應申請未申請', { should_apply: 'true' }); });
    node('已核准展延／例外', s.applied_count, 'n-app', 'app', function () { openFindings('已核准展延／例外（期限內，仍待修補）', { applied: 'true' }); }, '資安已核准、期限還沒到；仍待修補');
    var KS = ['todo', 'wip', 'subext', 'subexc', 'rescan'];
    var leaf = {};
    KS.forEach(function (k, i) { leaf[k] = node(STAT_DEFS[k].label, null, 'n-leaf' + (k === 'todo' ? ' n-todo' : ''), 'p' + i, null); });
    jget('/api/findings?' + qd({ status: '未結案', should_apply: 'true' })).then(function (rows) {
      var st = dispoStats(rows);
      KS.forEach(function (k) {
        var n = leaf[k], v = st[k];
        n.querySelector('.wx-node-n').textContent = String(v);
        if (v) {
          n.classList.add('clk'); n.title = '點我看清單';
          n.addEventListener('click', function () { openFindings('應申請未申請・' + STAT_DEFS[k].label, { should_apply: 'true', _stat: k }); });
        } else n.classList.add('zero');
      });
    }).catch(function () { KS.forEach(function (k) { leaf[k].querySelector('.wx-node-n').textContent = '—'; }); });
  }
  function ensureTreeStyle() {
    if (document.getElementById('wx-tree-style')) return;
    var st = document.createElement('style'); st.id = 'wx-tree-style';
    st.textContent =
      '.wx-tree-wrap{margin:6px 0 16px}' +
      '.wx-tree-cap{font-weight:700;color:#0f5f35;font-size:15px;margin:0 0 8px;padding-left:8px;border-left:4px solid #1a7f4b}' +
      '.wx-tree{display:grid;gap:8px;grid-template-columns:repeat(5,minmax(90px,1fr)) minmax(150px,1.3fr) minmax(150px,1.3fr);' +
      'grid-template-areas:"root root root root root root root" "need need need need need need not" "sa sa sa sa sa app not" "p0 p1 p2 p3 p4 app not"}' +
      '.wx-node{border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;' +
      'border:1px solid transparent;transition:transform .12s,box-shadow .12s}' +
      '.wx-node.clk{cursor:pointer}.wx-node.clk:hover{transform:translateY(-2px);box-shadow:0 6px 14px -6px rgba(0,0,0,.35)}' +
      '.wx-node-lb{font-size:13px;font-weight:600;opacity:.9}.wx-node-n{font-size:26px;font-weight:800;line-height:1.2;margin-top:2px}' +
      '.n-root{background:linear-gradient(135deg,#0f5f35,#1a7f4b);color:#fff}.n-root .wx-node-n{font-size:32px}' +
      '.n-need{background:#e3f1e8;border-color:#b7d9c4;color:#0f5f35}' +
      '.n-not{background:#eef1f3;border-color:#d3dade;color:#55625c}' +
      '.n-sa{background:#fdecea;border-color:#f3c3bd;color:#a3342d}' +
      '.n-app{background:#e6f2fb;border-color:#bcd9ef;color:#1d5f8f}' +
      '.n-leaf{background:#fff;border-color:#e3d9ec;color:#4a3d57}.n-leaf .wx-node-n{font-size:22px}' +
      '.n-leaf.n-todo{background:#fff5f4;border-color:#f3c3bd;color:#c0392b}' +
      '.n-leaf.zero{opacity:.55}' +
      '@media (max-width:760px){.wx-tree{grid-template-columns:repeat(5,1fr);grid-template-areas:"root root root root root" "need need need need need" "sa sa sa sa sa" "p0 p1 p2 p3 p4" "app app app not not"}}';
    document.head.appendChild(st);
  }

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
      return cols.map(function (c) { return cellVal(r, c[0]); });
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
    // 原始欄之後接上承辦填的欄位（使用者 2026-10-06：匯出要看得到處理進度）；欄名加「承辦_」避免跟原始欄撞名
    var caseCols = CASE_EXPORT_COLS.slice(1);   // 處置階段原始 Excel 已有，不重複
    var extra = caseCols.map(function (c) { return ['\u0000' + c[0], '承辦_' + c[1]]; });
    exportRowsCSV(cols.concat(extra), rows.map(function (r) {
      var o = {}, raw = (r && r.raw) ? r.raw : {};
      Object.keys(raw).forEach(function (k) { o[k] = raw[k]; });
      caseCols.forEach(function (c) { o['\u0000' + c[0]] = r ? r[c[0]] : ''; });
      return o;
    }), title);
  }

  // 承辦疊加層的欄位（系統上填的，原始 Excel 沒有）：簡易／完整匯出都帶
  var CASE_EXPORT_COLS = [['stage', '處置階段'], ['progress', '處理進度'], ['progress_state', '對帳狀態'],
    ['target_date', '預計完成日'], ['track_note', '追蹤備註']];

  // 弱點明細清單的「簡易匯出」欄位：常用 8 欄＋承辦欄位
  var SIMPLE_FINDING_COLS = [['host', '主機'], ['owner', '負責人'], ['severity', '嚴重度'],
    ['name', '弱點'], ['plugin_id', 'Plugin'], ['effective_due', '到期日'],
    ['overdue_days', '逾期天數'], ['department', '部門']].concat(CASE_EXPORT_COLS);

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
    // 標記成「清單匯出」：全站統一匯出(wireUnifiedExport)看到這個標記就放行，不改成下載整份原封 Excel
    full.setAttribute('data-wx-list-export', '1'); simple.setAttribute('data-wx-list-export', '1');
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
        U.el('td', { text: cellVal(r, 'progress') }), U.el('td', { text: r.progress_state || '' }),
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
          cellVal(r, 'progress'), r.progress_state || '', td, r.track_note || '', r.department || '']
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
      + cmpPrintBlock(esc)
      + '<div class="block"><b>本期概況（未結案）</b><br>'
      + kv('未結案', s.unresolved) + kv('逾期', s.overdue) + kv('如期', s.on_track)
      + kv('近期到期', s.soon) + kv('高風險未結(不含逾期、近期)', s.high_risk_only) + kv('其中高風險且逾期', s.high_risk_overdue) + '</div>'
      + '<div class="block"><b>申請進度</b><br>'
      + kv('需申請母體', s.apply_universe) + kv('應申請未申請', s.need_apply_count) + kv('已核准展延／例外', s.applied_count) + '</div>'
      + '<div class="block"><b>預計完成彙總</b><br>'
      + kv('已回報預計日', s.target.with_target) + kv('未回報', s.target.no_target)
      + kv('已過預計日', s.target.target_overdue) + kv('預計30天內完成', s.target.target_soon) + '</div>'
      + '<div class="block"><b>處理進度分佈（管理人標註）</b><br>'
      + kv('要申請展延', (s.progress || {}).apply_ext || 0) + kv('要申請例外', (s.progress || {}).apply_exc || 0)
      + kv('處理中', (s.progress || {}).wip || 0) + kv('結案申請中', (s.progress || {}).rescan || 0)
      + kv('待追查', (s.progress || {}).flagged || 0) + '</div>'
      + tbl('應申請未申請清單', s.need_apply_list)
      + tbl('要申請·送審中（來源尚未反映）', s.apply_intent_list || [])
      + tbl('待追查（聲稱申請或完成，來源未反映）', s.flagged_list || [])
      + tbl('逾期清單', s.overdue_list)
      + '<p class="muted" style="margin-top:16px">結論以資安 Excel 為準；「處理進度」為管理人追蹤標註。</p>'
      + '</body></html>';
    var w = window.open('', '_blank');
    if (!w) { UI.toast('瀏覽器擋了新視窗，請允許彈出視窗後再試', 'error'); return; }
    w.document.open(); w.document.write(html); w.document.close();
    setTimeout(function () { try { w.focus(); w.print(); } catch (e) {} }, 300);
  }

  // 季度總覽（可列印／存 PDF）：本期概況 ＋ 近一季趨勢（每批未結/逾期）＋ 本期新結案。供季度報告直接用。
  async function printQuarter(s) {
    var scope = (s.department && s.department !== '全部') ? s.department : '全部門';
    function esc(x) { return String(x == null ? '' : x).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); }
    var trend = [], close = null;
    try { trend = await jget('/api/trend?' + qd({ limit: 13 })); } catch (e) {}
    try { close = await jget('/api/close-stats?' + qd()); } catch (e) {}
    var kv = function (k, v) { return '<span class="kv"><b>' + esc(v) + '</b> ' + esc(k) + '</span>'; };
    var trendRows = (trend || []).map(function (t) {
      return '<tr><td>' + esc(t.date || '') + '</td><td>' + esc(t.open) + '</td><td>' + esc(t.overdue) + '</td></tr>';
    }).join('');
    var closerRows = (close && close.by_closer || []).map(function (c) {
      return '<tr><td>' + esc(c.name) + '</td><td>' + esc(c.closed) + '</td></tr>';
    }).join('');
    var html = '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
      + '<title>季度總覽_' + esc(scope) + '_' + esc(s.today) + '</title><style>'
      + 'body{font-family:"Microsoft JhengHei","PingFang TC",sans-serif;color:#1a1a1a;margin:28px;font-size:13px}'
      + 'h1{font-size:20px;margin:0 0 4px}h3{margin:18px 0 6px;border-left:4px solid #1a7f4b;padding-left:8px}'
      + '.muted{color:#777}.sub{color:#555;margin:0 0 12px}'
      + '.kv{display:inline-block;margin:0 16px 6px 0}.kv b{font-size:16px;color:#1a7f4b}'
      + '.block{background:#f6f8f7;border:1px solid #e3e6ea;border-radius:8px;padding:10px 12px;margin:8px 0}'
      + 'table{border-collapse:collapse;width:100%;margin:4px 0 10px}'
      + 'th,td{border:1px solid #d6dbdf;padding:4px 7px;text-align:left}th{background:#eef3f0}@media print{button{display:none}}'
      + '</style></head><body>'
      + '<h1>弱點修補 季度總覽</h1>'
      + '<p class="sub">範圍：' + esc(scope) + '　|　基準日：' + esc(s.today) + '</p>'
      + '<div class="block"><b>本期概況（未結案）</b><br>'
      + kv('未結案', s.unresolved) + kv('逾期', s.overdue) + kv('如期', s.on_track)
      + kv('近期到期', s.soon) + kv('高風險未結(不含逾期、近期)', s.high_risk_only) + kv('其中高風險且逾期', s.high_risk_overdue) + '</div>'
      + '<div class="block"><b>申請進度</b><br>'
      + kv('需申請母體', s.apply_universe) + kv('應申請未申請', s.need_apply_count) + kv('已核准展延／例外', s.applied_count) + '</div>'
      + '<h3>近一季未結趨勢（每次匯入）</h3>'
      + (trendRows ? ('<table><thead><tr><th>日期</th><th>未結</th><th>其中逾期</th></tr></thead><tbody>' + trendRows + '</tbody></table>') : '<p class="muted">尚無足夠歷史。</p>')
      + '<h3>本期新結案' + (close ? '（' + esc(close.new_closed) + '）' : '') + '</h3>'
      + (closerRows ? ('<table><thead><tr><th>結案人</th><th>結案數</th></tr></thead><tbody>' + closerRows + '</tbody></table>') : '<p class="muted">本期無新結案或尚無上一批可比。</p>')
      + '<p class="muted" style="margin-top:16px">結論以資安 Excel 為準。</p>'
      + '</body></html>';
    var w = window.open('', '_blank');
    if (!w) { UI.toast('瀏覽器擋了新視窗，請允許彈出視窗後再試', 'error'); return; }
    w.document.open(); w.document.write(html); w.document.close();
    setTimeout(function () { try { w.focus(); w.print(); } catch (e) {} }, 400);
  }

  // ===== 左側第二大項「承辦管線」（與「總覽」並列，綠色），底下放全部新功能 =====
  // 架構＝四個角度：看（負責人追蹤）／做（到期倒數）／報（主管週報，含一鍵發送）／查（查核：結案稽核＋對帳健檢＋資料缺口）
  var GOV_ITEMS = [
    // 收斂：主管週報一站看完(負責人/到期倒數都併成其分頁，下鑽亦以負責人為主)；查核(查帳)獨立留。
    // 開發期暫加 A/B 代號方便對話指稱；開發完畢再拿掉(搜 'DEV-LETTER' 一次清)
    { key: 'report', label: 'A. 主管週報', render: renderReportInto },    // 看＋報：總覽/負責人/到期倒數/處置落點/各清單(含一鍵發送鈕)
    { key: 'audit', label: 'B. 查核', render: renderAuditInto },          // 查：結案稽核＋對帳健檢＋資料缺口(查帳,非報告)
    // 一鍵發送＝動作；super 或 部門窗口可用；伺服器端寄催辦信（預覽→確認→寄）。
    { key: 'email', label: 'C. 一鍵發送', adminOnly: true, action: function () {
        openSendReminders();
      } },
    // 其他功能：原右上角選單移來這(資料管理/系統設定)；右上角改顯示資料版本
    { key: 'more', label: 'D. 其他功能', render: renderMoreInto },
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
      // 下鑽／清單自己的匯出鈕（匯眼前或已勾選的子集）一律放行。2026-10-06 修：按鈕改名成「完整匯出 (CSV)」後
      // 被這裡誤攔，使用者勾 16 筆匯出卻拿到整份 400 多筆
      if (b.getAttribute && b.getAttribute('data-wx-list-export')) return;
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
    var head = U.el('button', { class: 'sheet-item nav-summary webext-navgroup', title: '承辦作業（點我收合／展開）' },
      [U.el('span', { class: 'sheet-name', text: '承辦作業' }), U.el('span', { class: 'webext-chev' })]);
    nav.appendChild(head);
    // 小項
    GOV_ITEMS.forEach(function (item) {
      if (item.superOnly && !isSuper()) return;   // 僅 Super Admin
      if (item.adminOnly && !isAdminRole()) return;   // super 或 部門窗口(如一鍵發送)
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
    // 「其他功能」整個移到左側(成為一個項目)；右上角原位置改顯示資料版本(showDataFile)
    var mb = document.getElementById('more-btn'); if (mb) mb.style.display = 'none';
    showDataFile('');   // 先建右上角版本槽(hide more-btn 後補位)；檔名於載入/上傳時填
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

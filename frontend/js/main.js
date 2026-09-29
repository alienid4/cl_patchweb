/* ============================================================
 * js/main.js
 * 主控：檔案上傳/拖放、解析、驅動分頁渲染、分頁切換、錯誤提示。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var UI = global.UI;

  var state = { result: null, fileName: '', sheets: null, activeIdx: 0, mode: 'summary', myDept: '__all__', closeStatus: 'open' };

  function $(id) { return document.getElementById(id); }

  /* -------- 入口綁定 -------- */
  function init() {
    // 版本號(右上角)
    var vEl = $('app-version');
    if (vEl && global.APP_VERSION) {
      vEl.textContent = global.APP_VERSION;
      if (global.APP_VERSION_DATE) vEl.title = '版本 ' + global.APP_VERSION + '（' + global.APP_VERSION_DATE + '）';
    }
    var vUp = $('app-version-up');
    if (vUp && global.APP_VERSION) vUp.textContent = global.APP_VERSION;

    // 子分頁（總覽/例外統計內，點選切換，免下拉）
    initSubtabs();

    // 檢查相依函式庫
    if (typeof XLSX === 'undefined') {
      showError('缺少 SheetJS(XLSX) 函式庫，請確認網路可連 CDN，或依 assets/vendor/README 放置離線檔。');
    }

    var fileInput = $('file-input');
    var dropZone = $('drop-zone');
    var pickBtn = $('pick-btn');

    pickBtn.addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function (e) {
      if (e.target.files && e.target.files[0]) handleFile(e.target.files[0]);
    });

    // 拖放
    ['dragenter', 'dragover'].forEach(function (ev) {
      dropZone.addEventListener(ev, function (e) {
        e.preventDefault(); e.stopPropagation();
        dropZone.classList.add('drag-over');
      });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      dropZone.addEventListener(ev, function (e) {
        e.preventDefault(); e.stopPropagation();
        dropZone.classList.remove('drag-over');
      });
    });
    dropZone.addEventListener('drop', function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files[0]) handleFile(files[0]);
    });

    // 分頁切換
    document.querySelectorAll('.tab-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
    });

    // Modal 關閉
    $('modal-close').addEventListener('click', UI.closeModal);
    // sticky 的 modal（Email 設定、催辦內容等有輸入的流程）不吃這兩種關法，
    // 否則填到一半誤點畫面空白處就整組消失、得從頭來過。只能按右上 ✕。
    $('modal-overlay').addEventListener('click', function (e) {
      if (e.target === this && !UI.isModalSticky()) UI.closeModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !UI.isModalSticky()) UI.closeModal();
    });

    // 「其他功能」下拉選單
    function closeMore() { var d = $('more-dropdown'); if (d) d.classList.add('hidden'); }
    if ($('more-btn')) $('more-btn').addEventListener('click', function (e) {
      e.stopPropagation();
      $('more-dropdown').classList.toggle('hidden');
    });
    document.addEventListener('click', function (e) {
      var d = $('more-dropdown'), b = $('more-btn');
      if (d && !d.classList.contains('hidden') && !d.contains(e.target) && e.target !== b) closeMore();
    });

    // 換一個檔案(保留記憶，匯入新檔才覆蓋)
    $('reload-btn').addEventListener('click', function () {
      closeMore();
      $('file-input').value = '';
      resetToUpload();
    });
    // 清除記憶(移除 localStorage 並回上傳畫面)
    var clearBtn = $('clear-btn');
    if (clearBtn) clearBtn.addEventListener('click', function () {
      closeMore();
      clearState();
      $('file-input').value = '';
      resetToUpload();
      UI.toast('已清除暫存資料', 'success');
    });

    // 使用說明（開啟圖文手冊）
    if ($('help-btn')) $('help-btn').addEventListener('click', function () {
      closeMore();
      window.open('docs/使用說明.html', '_blank');
    });

    // 資安說明（資料保護、寄信權杖、發信紀錄、注意事項）
    if ($('security-btn')) $('security-btn').addEventListener('click', function () {
      closeMore();
      window.open('docs/資安說明.html', '_blank');
    });

    // 功能開關（其他功能 → 設定面板）
    if ($('features-btn')) $('features-btn').addEventListener('click', function () {
      closeMore();
      if (global.Features) global.Features.openSettings(refreshView);
    });

    // 複製指標摘要(整理成一段可讀文字 → 剪貼簿，方便貼進 email)
    var copyBtn = $('copy-summary-btn');
    if (copyBtn) copyBtn.addEventListener('click', function () {
      closeMore();
      if (!state.result) return;
      UI.copyText(buildSummaryText(state.result));
    });

    // 記住「我的部門」與「結案狀態」(全域，下次進來預設)
    state.myDept = loadMyDept();
    state.closeStatus = loadCloseStatus();

    // 常駐查詢框（若在總覽，先進入目前項目再查）
    if ($('global-search-input')) $('global-search-input').addEventListener('input', function () {
      if (state.mode === 'summary' && state.sheets) selectSheet(state.activeIdx);
      global.Search.onInput();
    });
    if ($('global-search-clear')) $('global-search-clear').addEventListener('click', function () { global.Search.clear(); });

    // 自動匯入設定（其他功能 → 自動匯入設定）
    if ($('autoimport-btn')) $('autoimport-btn').addEventListener('click', function () {
      closeMore();
      if (global.AutoImport) global.AutoImport.openSettings();
    });

    // 還原上次匯入(若有)
    tryRestore();

    // 自動匯入：若已設定來源資料夾且小幫手在跑，開啟時抓最新（抓到新檔才覆蓋，同檔跳過）。
    // 放在還原之後：沒設定就完全不動作，行為與今天相同。
    if (global.AutoImport) global.AutoImport.run();
  }

  function todayStr() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate());
  }
  function todayKey() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  /* 把關鍵指標整理成一段可讀文字(數字取自與指標卡相同來源) */
  function buildSummaryText(result) {
    // 總覽模式：全部項目彙總
    if (state.mode === 'summary' && state.sheets && global.Summary) {
      var o = global.Summary.overall(state.sheets, state.myDept);
      var t = o.totals;
      var hot = o.rows.filter(function (r) { return r.overdue > 0; })
        .sort(function (a, b) { return b.overdue - a.overdue; }).slice(0, 5)
        .map(function (r) { return r.name + '（逾期 ' + r.overdue + '）'; });
      var L = [];
      L.push('【弱點追蹤總覽】　' + todayStr());
      L.push('全部 ' + state.sheets.length + ' 個項目：未結案 ' + U.num(t.open) + ' 筆、已逾期 ' + U.num(t.overdue) +
             ' 筆、近期到期 ' + U.num(t.soon) + ' 筆、高風險未結 ' + U.num(t.high) + ' 筆，整體結案率 ' + t.rate + '%。');
      if (hot.length) L.push('逾期集中：' + hot.join('、') + '。');
      return L.join('\n');
    }
    var s = result.summary;
    var dept = (state.sheets && state.sheets[state.activeIdx]) ? state.sheets[state.activeIdx].name : '';
    var repairRate = (result.severityRepair && result.severityRepair.totals)
      ? Math.round(result.severityRepair.totals.rate * 1000) / 10 : 0;
    var today = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    var dateStr = today.getFullYear() + '/' + p(today.getMonth() + 1) + '/' + p(today.getDate());

    var lines = [];
    lines.push('【資安弱點指標摘要】' + (dept ? '（' + dept + '）' : '') + '　' + dateStr);
    lines.push('未結案 ' + U.num(s.total) + ' 筆；其中已逾期 ' + U.num(s.bands.overdue) +
               ' 筆、30 天內到期 ' + U.num(s.bands.d30) + ' 筆。');
    lines.push('高風險：Critical ' + U.num(s.critical) + ' 筆、High ' + U.num(s.high) + ' 筆。');
    lines.push('整體結案率：' + repairRate + '%。');
    return lines.join('\n');
  }

  /* -------- 檔案處理（多工作表） -------- */
  function handleFile(file) {
    if (!/\.(xlsx|xlsm|xls|csv)$/i.test(file.name)) {
      UI.toast('請選擇 Excel 檔（.xlsx / .xls / .csv）', 'error');
      return;
    }
    if (typeof XLSX === 'undefined') {
      showError('試算表函式庫未載入。請確認 <b>assets/vendor/xlsx.full.min.js</b> 存在，然後重新整理（Ctrl+F5）。');
      UI.toast('函式庫未載入', 'error');
      return;
    }
    state.fileName = file.name;
    setLoading(true);
    hideError();

    readArrayBuffer(file).then(function (buf) {
      loadWorkbook(buf, file.name);
      saveWorkbook(buf, file.name);
      // 快照失敗不該影響主流程：畫面其實已正確渲染，原本卻會被外層 catch 誤報成「解析失敗」
      if (global.History) {
        try { global.History.record(state.sheets, file.name, todayKey()); }
        catch (e) { UI.toast('趨勢快照未能記錄（不影響本次解析）', 'error'); }
      }
      setLoading(false);
      UI.toast('解析完成：' + file.name + '（' + state.sheets.length + ' 張表）', 'success');
    }).catch(function (err) {
      setLoading(false);
      showError('解析失敗：' + U.esc(err && err.message || err));
      UI.toast('解析失敗', 'error');
    });
  }

  /* 由 ArrayBuffer 直接匯入（自動匯入用；手動走 handleFile）。回傳成功與否。 */
  function importArrayBuffer(buf, fileName, opts) {
    opts = opts || {};
    if (typeof XLSX === 'undefined') { UI.toast('函式庫未載入', 'error'); return false; }
    state.fileName = fileName;
    setLoading(true); hideError();
    try {
      loadWorkbook(buf, fileName);
      saveWorkbook(buf, fileName);
      if (global.History) {
        try { global.History.record(state.sheets, fileName, todayKey()); } catch (e) {}
      }
      setLoading(false);
      if (opts.source === 'auto') {
        UI.toast('已自動匯入：' + fileName + (opts.modified ? '（' + opts.modified + '）' : '') +
                 ' · ' + state.sheets.length + ' 張表', 'success');
      } else {
        UI.toast('解析完成：' + fileName + '（' + state.sheets.length + ' 張表）', 'success');
      }
      return true;
    } catch (err) {
      setLoading(false);
      showError('解析失敗：' + U.esc(err && err.message || err));
      UI.toast('解析失敗', 'error');
      return false;
    }
  }

  function readArrayBuffer(file) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function (e) { resolve(e.target.result); };
      r.onerror = function () { reject(new Error('檔案讀取失敗（可能正被 Excel 鎖住，請先關閉）')); };
      r.readAsArrayBuffer(file);
    });
  }

  /* buf → 建各表 result、渲染左側導覽、預設落在「總覽」首頁 */
  function loadWorkbook(buf, fileName) {
    var sheets = global.Multi.buildAll(buf);
    // 訊息寫在例外裡：原本先 showError 再 throw，外層 catch 會用「解析失敗：無數字工作表」
    // 蓋掉這句能直接指出命名規則的提示
    if (!sheets.length) throw new Error('找不到「數字-」開頭的工作表（例如「1-系統弱點掃描弱點」）。請確認檔案或工作表命名。');
    state.sheets = sheets;
    state.fileName = fileName || state.fileName;
    if (state.activeIdx >= sheets.length) state.activeIdx = 0;
    state.result = sheets[state.activeIdx].result;   // 供查詢/複製摘要有預設對象
    renderSheetNav();
    showDashboard();
    showSummary();                                   // 主管首頁：預設看總覽
  }

  /* -------- 左側導覽：總覽 + 各項目 -------- */
  /* 所有工作表出現過的部門(去重、排序) */
  function allDepartments(sheets) {
    var set = {};
    (sheets || []).forEach(function (s) {
      (s.records || []).forEach(function (r) { set[r.unit || '(未填)'] = 1; });
    });
    return Object.keys(set).sort();
  }
  /* 某表在指定部門 + 結案狀態下的筆數 */
  function deptCount(s, dept, close) {
    return (s.records || []).filter(function (r) {
      var okDept = (dept === '__all__' || (r.unit || '(未填)') === dept);
      var okClose = (close === 'all') || (r.closeBucket === close);
      return okDept && okClose;
    }).length;
  }
  var CLOSE_LABELS = { open: '未結', closed: '已結', all: '筆' };

  function renderSheetNav() {
    var nav = $('sheet-nav');
    nav.innerHTML = '';

    // 部門選擇器(全站；記住的部門若此檔沒有則退回全部)
    var depts = allDepartments(state.sheets);
    if (state.myDept !== '__all__' && depts.indexOf(state.myDept) < 0) state.myDept = '__all__';
    var sel = U.el('select', { class: 'dept-select', id: 'my-dept-select' });
    var oAll = document.createElement('option'); oAll.value = '__all__'; oAll.textContent = '全部部門'; sel.appendChild(oAll);
    depts.forEach(function (u) { var o = document.createElement('option'); o.value = u; o.textContent = u; sel.appendChild(o); });
    sel.value = state.myDept;
    sel.addEventListener('change', function () { setMyDept(sel.value); });
    nav.appendChild(U.el('div', { class: 'dept-picker' }, [
      U.el('span', { class: 'dept-picker-label', text: '部門' }), sel,
    ]));

    // 結案狀態選擇器(全站，接在部門下面)
    var csel = U.el('select', { class: 'dept-select', id: 'my-close-select' });
    [['open', '未結案'], ['closed', '已結案'], ['all', '全部']].forEach(function (o) {
      var op = document.createElement('option'); op.value = o[0]; op.textContent = o[1]; csel.appendChild(op);
    });
    csel.value = state.closeStatus;
    csel.addEventListener('change', function () { setCloseStatus(csel.value); });
    // 總覽頁的表格本來就同時列「未結案／已結案」兩欄，是完整全貌，
    // 不隨此篩選變動；若不停用，使用者會看到左側計數變了但右側數字不動而以為壞掉
    var isSummary = (state.mode === 'summary');
    csel.disabled = isSummary;
    var closeRow = U.el('div', { class: 'dept-picker close-picker' }, [
      U.el('span', { class: 'dept-picker-label', text: '結案狀態' }), csel,
    ]);
    if (isSummary) {
      closeRow.appendChild(U.el('span', { class: 'close-picker-note', text: '總覽已含未結／已結全貌' }));
    }
    nav.appendChild(closeRow);

    // 總覽置頂
    nav.appendChild(U.el('button', {
      class: 'sheet-item nav-summary' + (state.mode === 'summary' ? ' active' : ''),
      onclick: showSummary,
    }, [U.el('span', { class: 'sheet-name', text: '總覽' })]));

    // 各項目(未結數依目前部門)
    state.sheets.forEach(function (s, i) {
      var item = U.el('button', {
        class: 'sheet-item' + (state.mode === 'sheet' && i === state.activeIdx ? ' active' : ''),
        title: s.name,
        onclick: (function (idx) { return function () { selectSheet(idx); saveActiveIdx(idx); }; })(i),
      }, [
        U.el('span', { class: 'sheet-name', text: s.name }),
        U.el('span', { class: 'sheet-count', text: U.num(deptCount(s, state.myDept, state.closeStatus)) + ' ' + CLOSE_LABELS[state.closeStatus] }),
      ]);
      nav.appendChild(item);
    });
  }

  /* 切換「我的部門」：記住、重繪導覽與目前畫面 */
  function setMyDept(v) {
    state.myDept = v;
    saveMyDept(v);
    renderSheetNav();
    if (state.mode === 'summary') showSummary();
    else applyFilters();
  }

  /* 切換「結案狀態」(全站)：記住、重繪導覽計數與目前畫面 */
  function setCloseStatus(v) {
    state.closeStatus = v;
    saveCloseStatus(v);
    renderSheetNav();
    if (state.mode === 'sheet') applyFilters();
  }

  function setNavActive() {
    var nav = $('sheet-nav'); if (!nav) return;
    var sum = nav.querySelector('.nav-summary');
    if (sum) sum.classList.toggle('active', state.mode === 'summary');
    var sheetItems = nav.querySelectorAll('.sheet-item:not(.nav-summary)');
    Array.prototype.forEach.call(sheetItems, function (el, idx) {
      el.classList.toggle('active', state.mode === 'sheet' && idx === state.activeIdx);
    });
  }

  /* 顯示總覽首頁 */
  function showSummary() {
    state.mode = 'summary';
    $('summary-view').classList.remove('hidden');
    $('sheet-view').classList.add('hidden');
    renderSheetNav();          // 重繪以更新「結案狀態」選單的停用狀態
    setNavActive();
    global.Summary.render(state.sheets, function (i) { selectSheet(i); saveActiveIdx(i); }, state.myDept);
    if ($('file-name-tag')) $('file-name-tag').textContent = state.fileName + '　(總覽)';
    if ($('copy-summary-btn')) $('copy-summary-btn').disabled = !state.sheets;
  }

  /* 進入某項目(工作表)細項 */
  function selectSheet(i) {
    state.mode = 'sheet';
    state.activeIdx = i;
    var s = state.sheets[i];
    global.Summary.destroyChart();
    if (global.History) global.History.destroyChart();
    $('summary-view').classList.add('hidden');
    $('sheet-view').classList.remove('hidden');
    renderSheetNav();          // 重繪以恢復「結案狀態」選單可用
    setNavActive();
    // 部門與結案狀態沿用左側全站設定(不重設)
    applyFilters();
  }

  /* 依目前選的表 + 兩個篩選，重算母體並重繪 */
  function applyFilters() {
    if (!state.sheets) return;
    var s = state.sheets[state.activeIdx];
    var deptSel = state.myDept || '__all__';
    var closeSel = state.closeStatus || 'open';
    var recs = s.records;
    var deptFiltered = (deptSel === '__all__') ? recs : recs.filter(function (r) { return (r.unit || '(未填)') === deptSel; });
    var scoped = (closeSel === 'all') ? deptFiltered : deptFiltered.filter(function (r) { return r.closeBucket === closeSel; });
    var result = global.Analysis.assembleResult(deptFiltered, scoped, { allCount: recs.length });
    result.caps = s.caps;
    // 母體標籤：面板不要再寫死「未結案」，否則選「已結案」時標題與內容矛盾
    result.closeLabel = { open: '未結案', closed: '已結案', all: '全部狀態' }[closeSel] || '未結案';
    state.result = result;
    renderResult(result, s.name, { dept: deptSel, close: closeSel, deptCount: deptFiltered.length });
  }

  function renderResult(result, sheetName, opts) {
    opts = opts || {};
    applyTabVisibility(result.caps || {});
    $('file-name-tag').textContent = state.fileName + '　(工作表：' + sheetName + ')';
    renderQuality(result);
    // 關掉的分頁不再白白計算與建立 Chart（原本只隱藏按鈕，內容照樣渲染）
    var F = global.Features;
    function on(id) { return !F || !F.isOn || F.isOn(id); }
    global.Dashboard.render(result);
    if (on('tab-tracking')) global.Tracking.render(result);
    if (on('tab-matrix'))   global.Matrix.render(result);
    if (on('tab-stats'))    global.Stats.render(result);
    if (on('tab-search'))   global.Search.render(result);
    // scope-info 反映目前篩選
    var deptLabel = (opts.dept && opts.dept !== '__all__') ? opts.dept : '全部部門';
    var closeLabel = result.closeLabel || { open: '未結案', closed: '已結案', all: '全部狀態' }[opts.close || 'open'];
    var scope = $('scope-info');
    scope.innerHTML = '';
    scope.appendChild(U.el('span', { html:
      '<b>' + U.esc(sheetName) + '</b>　·　部門：<b>' + U.esc(deptLabel) + '</b>　·　狀態：<b>' + closeLabel +
      '</b>　|　顯示 <b>' + U.num(result.summary.total) + '</b> 筆（該部門全部 ' + U.num(opts.deptCount != null ? opts.deptCount : result.allCount) + ' 筆）' }));
    if ($('copy-summary-btn')) $('copy-summary-btn').disabled = !state.result;
  }

  /* -------- 記住上次匯入(localStorage：存整份檔 base64 + 目前選的表) -------- */
  var STORAGE_KEY = 'vulnDashboard.v2';

  function abToB64(buf) {
    var bytes = new Uint8Array(buf), bin = '', chunk = 0x8000;
    for (var i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }
  function b64ToAb(b64) {
    var bin = atob(b64), len = bin.length, bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }
  function saveWorkbook(buf, fileName) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        v: 2, b64: abToB64(buf), fileName: fileName,
        activeIdx: state.activeIdx, savedAt: new Date().toISOString(),
      }));
    } catch (e) {
      UI.toast('檔案較大，未能記住（下次需重新匯入）', 'error');
    }
  }
  /* activeIdx 獨立成一個 key：原本為了改一個整數，要把含數 MB base64 的整包
   * parse→stringify→寫回，大檔時每點一次工作表都會卡頓 */
  var ACTIVE_KEY = 'vulnDashboard.activeIdx';
  function saveActiveIdx(i) {
    try { localStorage.setItem(ACTIVE_KEY, String(i)); } catch (e) {}
  }
  function loadActiveIdx(fallback) {
    try {
      var v = localStorage.getItem(ACTIVE_KEY);
      var n = parseInt(v, 10);
      return isNaN(n) ? (fallback || 0) : n;
    } catch (e) { return fallback || 0; }
  }
  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var p = JSON.parse(raw);
      return (p && p.b64) ? p : null;
    } catch (e) { return null; }
  }
  function clearState() { try { localStorage.removeItem(STORAGE_KEY); } catch (e) {} }

  /* 「我的部門」記憶(獨立於檔案，跨檔沿用) */
  var DEPT_KEY = 'vulnDashboard.dept';
  function loadMyDept() { try { return localStorage.getItem(DEPT_KEY) || '__all__'; } catch (e) { return '__all__'; } }
  function saveMyDept(v) { try { localStorage.setItem(DEPT_KEY, v); } catch (e) {} }

  /* 「結案狀態」記憶(全站，跨檔沿用) */
  var CLOSE_KEY = 'vulnDashboard.close';
  function loadCloseStatus() { try { var v = localStorage.getItem(CLOSE_KEY); return (v === 'closed' || v === 'all') ? v : 'open'; } catch (e) { return 'open'; } }
  function saveCloseStatus(v) { try { localStorage.setItem(CLOSE_KEY, v); } catch (e) {} }

  function fmtSavedAt(iso) {
    if (!iso) return '';
    var d = new Date(iso); if (isNaN(d.getTime())) return '';
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  /* -------- 跨午夜自動重算 --------
   * 逾期天數是在「解析當下」算的，看板長時間開著跨過午夜就會停在昨天的基準。
   * 偵測到日期變更時，用已存的檔案重新解析一次，並保留目前的檢視位置。 */
  var dayKey = todayKey();
  function checkDayRollover() {
    var now = todayKey();
    if (now === dayKey) return;
    dayKey = now;
    if (!state.sheets) return;
    var saved = loadState();
    if (!saved || !saved.b64) return;
    var keepMode = state.mode, keepIdx = state.activeIdx;
    try {
      loadWorkbook(b64ToAb(saved.b64), saved.fileName);
      state.activeIdx = keepIdx;
      if (keepMode === 'sheet') selectSheet(keepIdx); else showSummary();
      UI.toast('已跨日，逾期天數以今日（' + now + '）重新計算', 'info');
    } catch (e) { /* 重算失敗就維持原畫面，不影響使用 */ }
  }
  // 回到分頁時檢查一次；另每 10 分鐘檢查，涵蓋一直開著沒切走的情況
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) checkDayRollover();
  });
  window.addEventListener('focus', checkDayRollover);
  setInterval(checkDayRollover, 10 * 60 * 1000);

  function tryRestore() {
    var saved = loadState();
    if (!saved) return;
    try {
      state.activeIdx = loadActiveIdx(saved.activeIdx || 0);   // 新 key 優先，相容舊存檔
      loadWorkbook(b64ToAb(saved.b64), saved.fileName);
      var when = fmtSavedAt(saved.savedAt);
      UI.toast('已還原上次匯入：' + (saved.fileName || '') + (when ? '（' + when + '）' : ''), 'success');
    } catch (e) {
      clearState(); // 還原失敗就清掉，回到上傳畫面
    }
  }

  /* 資料品質檢核橫幅(全域，位於分頁上方) */
  function renderQuality(result) {
    var banner = $('quality-banner');
    if (!banner) return;
    banner.innerHTML = '';
    var q = result.quality;

    // 解析警告(欄位對不上/重複表頭)與「未分類結案狀態」優先顯示：
    // 這兩種會讓資料看起來正常卻其實不完整，必須讓使用者知道
    var sheet = state.sheets && state.sheets[state.activeIdx];
    var parseWarns = (sheet && sheet.warnings) || [];
    var otherRecs = ((sheet && sheet.records) || []).filter(function (r) { return r.closeBucket === 'other'; });
    if (parseWarns.length || otherRecs.length) {
      banner.className = 'quality-banner warn';
      banner.appendChild(U.el('span', { class: 'q-icon', text: '⚠' }));
      var msgs = parseWarns.slice();
      if (otherRecs.length) msgs.push('有 ' + otherRecs.length + ' 筆的結案狀態無法歸類為未結案或已結案，未被計入任何統計。');
      banner.appendChild(U.el('span', { class: 'q-text', text: msgs.join('　') }));
      if (otherRecs.length) {
        banner.appendChild(U.el('button', {
          class: 'btn btn-secondary btn-sm', text: '看未分類清單',
          onclick: function () { UI.openDetail('未分類結案狀態（' + otherRecs.length + ' 筆）', otherRecs); },
        }));
      }
      return;
    }

    if (!q || q.count === 0) {
      banner.className = 'quality-banner ok';
      banner.appendChild(U.el('span', { text: '✅ 資料品質檢核通過，未發現異常。' }));
      return;
    }

    banner.className = 'quality-banner warn';
    var summary = q.issues.map(function (i) { return i.label.replace(/（.*）/, '') + ' ' + i.records.length; }).join('　·　');
    banner.appendChild(U.el('span', { class: 'q-icon', text: '⚠' }));
    banner.appendChild(U.el('span', { class: 'q-text', html: '資料品質：發現 <b>' + q.count + '</b> 項異常（' + U.esc(summary) + '）' }));
    banner.appendChild(U.el('button', {
      class: 'btn btn-secondary btn-sm', text: '檢視明細',
      onclick: function () { openQuality(q); },
    }));
  }

  function openQuality(q) {
    var wrap = U.el('div', { class: 'quality-detail' });
    q.issues.forEach(function (i) {
      wrap.appendChild(U.el('div', { class: 'q-issue-head' }, [
        U.el('span', { class: 'q-badge', text: String(i.records.length) }),
        U.el('span', { text: i.label }),
        U.el('button', { class: 'btn btn-secondary btn-sm q-view', text: '看清單',
          onclick: function () { UI.openDetail(i.label + '（' + i.records.length + ' 筆）', i.records); } }),
      ]));
    });
    UI.openModal('資料品質檢核（' + q.count + ' 項異常）', wrap);
  }

  /* -------- 版面狀態 -------- */
  function setLoading(on) {
    $('loading').classList.toggle('show', !!on);
  }
  function showDashboard() {
    $('upload-section').classList.add('hidden');
    $('main-content').classList.remove('hidden');
    if ($('more-btn')) $('more-btn').classList.remove('hidden');
    if ($('header-search')) $('header-search').classList.remove('hidden');
    if ($('copy-summary-btn')) $('copy-summary-btn').disabled = !state.result;
    switchTab('dashboard');
  }
  function resetToUpload() {
    state.result = null;
    state.sheets = null;
    state.activeIdx = 0;
    state.mode = 'summary';
    if ($('sheet-nav')) $('sheet-nav').innerHTML = '';
    if ($('summary-view')) $('summary-view').innerHTML = '';
    global.Summary.destroyChart();
    if (global.History) global.History.destroyChart();
    global.Dashboard.destroyCharts();
    global.Stats.destroyCharts();
    $('main-content').classList.add('hidden');
    $('upload-section').classList.remove('hidden');
    if ($('more-btn')) $('more-btn').classList.add('hidden');
    if ($('header-search')) $('header-search').classList.add('hidden');
    if ($('more-dropdown')) $('more-dropdown').classList.add('hidden');
    if ($('copy-summary-btn')) $('copy-summary-btn').disabled = true;
    $('file-name-tag').textContent = '';
    hideError();
  }
  function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    document.querySelectorAll('.tab-panel').forEach(function (p) {
      p.classList.toggle('active', p.id === 'tab-' + tab);
    });
  }

  /* 分頁顯示：面板自適應(caps) + 功能開關(Features) 任一不通就隱藏；當前分頁被藏則退回總覽 */
  function applyTabVisibility(caps) {
    caps = caps || {};
    var F = global.Features;
    var defs = [
      { tab: 'tracking', ok: true },
      { tab: 'matrix',   ok: caps.severity !== false },
      { tab: 'stats',    ok: caps.stagePanel !== false },
      { tab: 'search',   ok: true },
    ];
    defs.forEach(function (d) {
      var btn = document.querySelector('.tab-btn[data-tab="' + d.tab + '"]');
      if (!btn) return;
      var visible = d.ok && (!F || F.isOn('tab-' + d.tab));
      btn.style.display = visible ? '' : 'none';
      if (!visible && btn.classList.contains('active')) switchTab('dashboard');
    });
  }

  /* 功能開關變更後重繪目前畫面 */
  function refreshView() {
    if (!state.sheets) return;
    if (state.mode === 'summary') showSummary();
    else applyFilters();
  }
  function showError(html) {
    var box = $('error-box');
    box.innerHTML = html;
    box.classList.add('show');
  }
  function hideError() {
    var box = $('error-box');
    box.classList.remove('show');
    box.innerHTML = '';
  }

  /* -------- 子分頁（subtabs）：同一分頁內點選切換，取代往下捲 -------- */
  function initSubtabs() {
    document.querySelectorAll('.subtab-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { activateSubtab(btn); });
    });
  }
  function activateSubtab(btn) {
    var panel = btn.closest('.tab-panel'); if (!panel) return;
    var sub = btn.dataset.sub;
    Array.prototype.forEach.call(panel.querySelectorAll('.subtab-btn'), function (b) {
      b.classList.toggle('active', b === btn);
    });
    Array.prototype.forEach.call(panel.querySelectorAll('.subpanel'), function (p) {
      p.classList.toggle('active', p.dataset.sub === sub);
    });
    // Chart.js 4 在 display:none 下建立的圖表 canvas 為 0×0 且 resize 無效；
    // 切到含未成形圖表的子分頁時，重繪目前畫面，讓圖表在「可見」狀態下重新建立
    var activePanel = panel.querySelector('.subpanel.active');
    if (activePanel) {
      var cvs = activePanel.querySelectorAll('canvas');
      var needsRender = Array.prototype.some.call(cvs, function (c) { return c.width === 0; });
      if (needsRender) refreshView();
    }
  }
  /* 依面板開關顯示/隱藏子分頁；確保永遠有一個可見子分頁被選中 */
  function setSubtabVisible(tabId, map) {
    var panel = document.getElementById('tab-' + tabId); if (!panel) return;
    Object.keys(map).forEach(function (sub) {
      var b = panel.querySelector('.subtab-btn[data-sub="' + sub + '"]');
      var p = panel.querySelector('.subpanel[data-sub="' + sub + '"]');
      var vis = !!map[sub];
      if (b) b.style.display = vis ? '' : 'none';
      if (!vis && b) b.classList.remove('active');
      if (!vis && p) p.classList.remove('active');
    });
    var btns = Array.prototype.slice.call(panel.querySelectorAll('.subtab-btn'));
    var activeOk = btns.some(function (b) { return b.classList.contains('active') && b.style.display !== 'none'; });
    if (!activeOk) {
      var first = btns.filter(function (b) { return b.style.display !== 'none'; })[0];
      if (first) activateSubtab(first);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  global.App = { getState: function () { return state; }, setSubtabVisible: setSubtabVisible, importArrayBuffer: importArrayBuffer };
})(window);

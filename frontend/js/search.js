/* ============================================================
 * js/search.js
 * 查詢：全文搜尋 + 快速篩選（可複選）。搜尋框常駐於分頁上方，
 * 快速篩選與結果顯示於「查詢」分頁。結果可排序、匯出、另開新分頁。
 * 多選規則：同一類（到期狀態／嚴重度／處置階段）內為 OR，跨類為 AND。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var UI = global.UI;

  var state = { result: null, sel: {} };   // sel: 已選的篩選鍵集合（不含 all）
  var chipEls = {};
  var resultsEl = null;

  /* 快速篩選定義：對應 Analysis.Filters */
  var QUICK = [
    { key: 'all',            label: '全部' },
    { key: 'overdue',        label: '已逾期' },
    { key: 'soon',           label: '近期到期' },
    { key: 'todayTrack',     label: '今日待追蹤' },
    { key: 'sixMonths',      label: '六個月到期' },
    { key: 'critical',       label: 'Critical' },
    { key: 'high',           label: 'High' },
    { key: 'medium',         label: 'Medium' },
    { key: 'stageException', label: '例外管理中' },
    { key: 'stageExtension', label: '首次展延中' },
    { key: 'safeException',  label: '例外核准未到期' },
    { key: 'chronic',        label: '反覆展延／例外' },
  ];

  /* 只有「互斥／同維度」的鍵歸同一類（類內 OR）；未列出者各自獨立（跨類 AND） */
  var OR_GROUP = {
    overdue: 'due', soon: 'due', todayTrack: 'due', sixMonths: 'due',
    critical: 'sev', high: 'sev', medium: 'sev',
    stageException: 'stage', stageExtension: 'stage',
  };

  /* 把一筆紀錄攤平成可搜尋字串 */
  function recordText(r) {
    return [
      r.host, r.owner, r.name, r.pluginId, r.risk, r.severity,
      r.systemCategory, r.assetName, r.protocol, r.port, r.year,
      U.fmtDate(r.fixDeadline), U.fmtDate(r.firstExtension),
      U.fmtDate(r.exceptionApproval), U.fmtDate(r.realDue),
    ].join(' ').toLowerCase();
  }

  function getInput() { return document.getElementById('global-search-input'); }
  function labelOf(key) {
    for (var i = 0; i < QUICK.length; i++) if (QUICK[i].key === key) return QUICK[i].label;
    return key;
  }
  function selKeys() { return Object.keys(state.sel); }

  /* 點 chip：all 清空；其餘切換選取 */
  function toggleChip(key) {
    if (key === 'all') { state.sel = {}; }
    else if (state.sel[key]) { delete state.sel[key]; }
    else { state.sel[key] = true; }
    syncChips();
    run();
  }

  /* 建立「查詢」分頁內容：快速篩選 chips + 結果區（搜尋框在分頁外，常駐） */
  function render(result) {
    state.result = result;
    var box = document.getElementById('search-body');
    if (!box) return;
    box.innerHTML = '';

    var chipWrap = U.el('div', { class: 'search-chips' });
    chipEls = {};
    QUICK.forEach(function (q) {
      var chip = U.el('button', {
        class: 'search-chip',
        text: q.label,
        onclick: (function (k) { return function () { toggleChip(k); }; })(q.key),
      });
      chipEls[q.key] = chip;
      chipWrap.appendChild(chip);
    });
    box.appendChild(chipWrap);

    resultsEl = U.el('div', { id: 'search-results', class: 'search-results' });
    box.appendChild(resultsEl);

    syncChips();
    run();
  }

  function syncChips() {
    var anySel = selKeys().length > 0;
    Object.keys(chipEls).forEach(function (k) {
      var on = (k === 'all') ? !anySel : !!state.sel[k];
      chipEls[k].classList.toggle('active', on);
    });
  }

  /* 依已選鍵分組：類內 OR、跨類 AND */
  function buildPredicate() {
    var Filters = global.Analysis.Filters;
    var groups = {};
    selKeys().forEach(function (k) {
      var d = OR_GROUP[k] || k;                 // 未分組者各自成一類
      (groups[d] = groups[d] || []).push(k);
    });
    var dims = Object.keys(groups);
    if (!dims.length) return function () { return true; };
    return function (r) {
      return dims.every(function (d) {
        return groups[d].some(function (k) {
          var fn = Filters[k];
          return fn ? fn(r) : true;
        });
      });
    };
  }

  function run() {
    if (!state.result || !resultsEl) return;
    var input = getInput();
    var text = input ? input.value : '';
    var records = state.result.records;
    var pass = buildPredicate();
    var terms = text.trim().toLowerCase().split(/\s+/).filter(Boolean);

    var list = records.filter(pass).filter(function (r) {
      if (!terms.length) return true;
      var t = recordText(r);
      return terms.every(function (term) { return t.indexOf(term) >= 0; });
    });

    resultsEl.innerHTML = '';
    var labels = selKeys().map(labelOf);
    var title = '查詢結果' +
      (labels.length ? '（' + labels.join(' ＋ ') + '）' : '') +
      (terms.length ? '（關鍵字：' + terms.join(' ') + '）' : '');
    var bar = U.el('div', { class: 'search-resultbar' }, [
      U.el('span', { class: 'search-count', text: '共 ' + list.length + ' 筆' }),
      U.el('button', { class: 'btn btn-secondary btn-sm', text: '另開新分頁',
        onclick: function () { UI.popOutTable(title, list); }, disabled: list.length ? null : 'disabled' }),
      U.el('button', { class: 'btn btn-secondary btn-sm', text: '匯出 CSV',
        onclick: function () { UI.exportCSV(list, title); }, disabled: list.length ? null : 'disabled' }),
    ]);
    resultsEl.appendChild(bar);
    resultsEl.appendChild(UI.buildDetailTable(list));
  }

  /* 全域搜尋框輸入：有關鍵字就切到「查詢」分頁並執行 */
  function onInput() {
    var input = getInput();
    var txt = input ? input.value.trim() : '';
    var tab = document.getElementById('tab-search');
    if (txt && tab && !tab.classList.contains('active')) {
      var btn = document.querySelector('.tab-btn[data-tab="search"]');
      if (btn) btn.click();
    }
    run();
  }

  function clear() {
    var input = getInput(); if (input) input.value = '';
    state.sel = {};
    syncChips();
    run();
  }

  global.Search = { render: render, onInput: onInput, clear: clear };
})(window);

/* ============================================================
 * js/dashboard.js
 * TAB1 儀表板：指標卡(可點擊 drill-down)、圖表、AI摘要。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var CFG = global.APP_CONFIG;
  var UI = global.UI;

  var charts = {}; // 保存 Chart 實例以便重繪時銷毀

  function destroyCharts() {
    Object.keys(charts).forEach(function (k) {
      if (charts[k]) { charts[k].destroy(); charts[k] = null; }
    });
  }

  /* 指標卡分組定義：到期採「互斥」時間帶(相加 = 未結案，可驗算)。
   * 回傳 [{title, cards:[{label,value,cls,filter}]}] */
  function metricGroups(result) {
    var s = result.summary;
    var A = global.Analysis;
    var bandCls = { overdue: 'm-overdue', d30: 'm-warn', d90: 'm-warn2', d180: 'm-info', over180: 'm-info', noDue: 'm-none', closed: 'm-done' };
    function sevFilter(sev) { return function (r) { return r.severity === sev; }; }

    // 嚴重度卡(依序 + 其他未歸類)
    var sevCls = { Critical: 'm-critical', High: 'm-high', Medium: 'm-medium', Low: 'm-info', Info: 'm-none' };
    var sevCards = [];
    CFG.severityOrder.forEach(function (sev) {
      var v = s.severityDist[sev] || 0;
      if (v > 0) sevCards.push({ label: sev, value: v, cls: sevCls[sev] || 'm-info', filter: sevFilter(sev) });
    });
    Object.keys(s.severityDist).forEach(function (sev) {
      if (CFG.severityOrder.indexOf(sev) < 0 && s.severityDist[sev] > 0) {
        sevCards.push({ label: sev, value: s.severityDist[sev], cls: 'm-none', filter: sevFilter(sev) });
      }
    });

    var groups = [
      { title: '總覽', cards: [{ label: (result.closeLabel || '未結案'), value: s.total, cls: 'm-total', filter: function () { return true; } }] },
      { title: '到期時間帶', cards: A.BANDS.map(function (b) {
          return { label: b.label, value: s.bands[b.key], cls: bandCls[b.key] || 'm-info',
            filter: (function (key) { return function (r) { return A.bandOf(r) === key; }; })(b.key) };
        }) },
    ];
    // 面板自適應：該表無嚴重度欄則不放嚴重度卡
    if (!(result.caps && result.caps.severity === false)) groups.push({ title: '嚴重度', cards: sevCards });
    return groups;
  }

  function render(result) {
    var s = result.summary;
    var records = result.records;
    var Filters = global.Analysis.Filters;

    /* ---- 資料涵蓋摘要 ---- */
    var scope = document.getElementById('scope-info');
    scope.innerHTML = '';
    scope.appendChild(U.el('span', { html:
      '分析條件：<b>負責單位 = ' + U.esc(CFG.filter.department) + '</b>　且　<b>結案狀態 = ' +
      U.esc(CFG.filter.openStatus) + '</b>　|　符合單位共 ' + U.num(result.deptAllCount) +
      ' 筆，其中未結案 <b>' + U.num(s.total) + '</b> 筆　|　全檔 ' + U.num(result.allCount) + ' 筆' }));

    /* ---- 指標卡 ---- */
    var grid = document.getElementById('metric-grid');
    grid.innerHTML = '';
    metricGroups(result).forEach(function (g) {
      var section = U.el('div', { class: 'metric-group' });
      section.appendChild(U.el('div', { class: 'metric-group-title', text: g.title }));
      var cardsWrap = U.el('div', { class: 'metric-cards' });
      g.cards.forEach(function (m) {
        var card = U.el('div', {
          class: 'metric-card ' + m.cls,
          onclick: function () {
            var list = records.filter(m.filter);
            UI.openDetail(m.label + '　明細（' + list.length + ' 筆）', list);
          },
        }, [
          U.el('div', { class: 'metric-value', text: U.num(m.value) }),
          U.el('div', { class: 'metric-label', text: m.label }),
        ]);
        cardsWrap.appendChild(card);
      });
      section.appendChild(cardsWrap);
      grid.appendChild(section);
    });

    /* ---- 各面板：依「功能開關」決定顯示/渲染 ---- */
    var F = global.Features;
    function feat(id) { return !F || F.isOn(id); }
    function gate(elId, on) { var el = document.getElementById(elId); if (el) el.style.display = on ? '' : 'none'; }

    // 各嚴重度結案進度
    gate('sev-repair', feat('panel-sev-repair'));
    if (feat('panel-sev-repair')) renderSevRepair(result);

    // 優先處理清單
    gate('today-actions', feat('panel-today-actions'));
    if (feat('panel-today-actions')) renderTodayActions(result);

    // 風險排序
    gate('risk-top', feat('panel-risk-top'));
    if (feat('panel-risk-top')) renderRiskTop(result);

    // 圖表（嚴重度／到期分布）
    destroyCharts();
    var chartsRow = document.querySelector('#tab-dashboard .charts-row');
    if (chartsRow) chartsRow.style.display = feat('panel-charts') ? '' : 'none';
    if (feat('panel-charts')) {
      var noSev = (result.caps && result.caps.severity === false);
      var sevCanvas = document.getElementById('chart-severity');
      var sevCard = sevCanvas && sevCanvas.closest ? sevCanvas.closest('.chart-card') : null;
      if (sevCard) sevCard.style.display = noSev ? 'none' : '';
      if (!noSev) renderSeverityChart(s, result.records);
      renderDueChart(s, result.records);
    }

    // 子分頁顯示：面板被關掉(功能開關/面板自適應)就連該子分頁一起藏
    if (global.App && global.App.setSubtabVisible) {
      var caps = result.caps || {};
      global.App.setSubtabVisible('dashboard', {
        sevrepair: feat('panel-sev-repair') && caps.severity !== false,
        today: feat('panel-today-actions'),
        risk: feat('panel-risk-top'),
        charts: feat('panel-charts'),
      });
    }
  }

  /* 嚴重度 × 修復狀態（給主管：哪些已修復 / 未修復 + 修復率） */
  function renderSevRepair(result) {
    var box = document.getElementById('sev-repair');
    if (!box) return;
    box.innerHTML = '';
    // 面板自適應：無嚴重度欄則整塊不顯示
    if (result.caps && result.caps.severity === false) { box.style.display = 'none'; return; }
    box.style.display = '';
    var sr = result.severityRepair;

    // 本表固定涵蓋全部結案狀態(算結案率必需)，與上方指標卡母體不同 → 明確標示避免誤解
    box.appendChild(U.el('div', { class: 'panel-bar' }, [
      U.el('h3', { text: '各嚴重度結案進度' }),
      U.el('span', { class: 'panel-bar-note', text: '共 ' + U.num(sr.totals.total) + ' 筆　·　此表固定涵蓋全部結案狀態（用於計算結案率）' }),
    ]));

    if (!sr.rows.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無資料。' })); return; }

    var showOther = sr.hasOther;
    var table = U.el('table', { class: 'tracking-table sevrepair-table' });
    var thead = U.el('thead'); var htr = U.el('tr');
    var heads = ['嚴重度', '未結案', '已結案'];
    if (showOther) heads.push('其他');
    heads = heads.concat(['合計', '結案率']);
    heads.forEach(function (h, i) { htr.appendChild(U.el('th', { class: i === 0 ? '' : 'num-cell', text: h })); });
    thead.appendChild(htr); table.appendChild(thead);

    function cell(n, recs, title, cls) {
      var td = U.el('td', { class: 'num-cell ' + (cls || '') + (n > 0 ? ' clickable' : ' zero'), text: n ? U.num(n) : '·' });
      if (n > 0) td.addEventListener('click', function () { UI.openDetail(title + '（' + n + ' 筆）', recs); });
      return td;
    }
    function rateBar(rate) {
      var pct = Math.round(rate * 1000) / 10;
      var wrap = U.el('td', { class: 'rate-cell' });
      var bar = U.el('div', { class: 'rate-bar' }, [U.el('div', { class: 'rate-fill', text: pct + '%' })]);
      bar.querySelector('.rate-fill').style.width = Math.max(pct, 12) + '%';
      wrap.appendChild(bar);
      return wrap;
    }

    var tbody = U.el('tbody');
    sr.rows.forEach(function (r) {
      var tr = U.el('tr');
      tr.appendChild(U.el('td', { class: 'owner-cell' }, [U.el('span', { class: 'sev-badge sev-' + r.sev, text: r.sev })]));
      tr.appendChild(cell(r.openN, r.open, r.sev + ' 未結案', 'has-overdue'));
      tr.appendChild(cell(r.closedN, r.closed, r.sev + ' 已結案', 'rep-done'));
      if (showOther) tr.appendChild(cell(r.otherN, r.other, r.sev + ' 其他狀態', ''));
      tr.appendChild(cell(r.total, r.records, r.sev + ' 全部', 'total-cell'));
      tr.appendChild(rateBar(r.rate));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    var t = sr.totals;
    var tfoot = U.el('tfoot'); var ftr = U.el('tr', { class: 'total-row' });
    ftr.appendChild(U.el('td', { class: 'owner-cell', text: '合計' }));
    ftr.appendChild(cell(t.openN, t.open, '全部 未結案', 'has-overdue'));
    ftr.appendChild(cell(t.closedN, t.closed, '全部 已結案', 'rep-done'));
    if (showOther) ftr.appendChild(cell(t.otherN, t.other, '全部 其他狀態', ''));
    ftr.appendChild(cell(t.total, t.open.concat(t.closed).concat(t.other), '全部（含各狀態）', 'total-cell'));
    ftr.appendChild(rateBar(t.rate));
    tfoot.appendChild(ftr); table.appendChild(tfoot);

    box.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
  }

  /* 狀態文字(逾期/今日/尚餘/例外將到) */
  function statusText(r) {
    if (r.realDue === null) return '無到期日';
    if (r.daysLeft < 0) return '逾期 ' + r.overdueDays + ' 天';
    if (r.daysLeft === 0) return '今日到期';
    if (r.stage === 'exception') return '例外核准 ' + r.daysLeft + ' 天內到期';
    return '距到期 ' + r.daysLeft + ' 天';
  }

  function sevBadge(r) {
    return U.el('span', { class: 'sev-badge sev-' + (r.severity || 'Unknown'), text: r.severity || '-' });
  }

  /* 今日行動清單：逾期 + 今日到期 + 例外將到，依風險分排序 */
  function renderTodayActions(result) {
    var box = document.getElementById('today-actions');
    box.innerHTML = '';
    var list = result.todayActions || [];

    var head = U.el('div', { class: 'panel-bar' }, [
      U.el('h3', { html: '優先處理清單　<span class="count-pill">' + list.length + '</span>' }),
    ]);
    box.appendChild(head);

    if (!list.length) {
      box.appendChild(U.el('p', { class: 'empty-hint', text: '目前無須立即處理的項目。' }));
      return;
    }

    var cols = ['', '負責人', '嚴重度', '弱點', '主機', '真正到期日', '狀態', '風險分數'];
    var table = U.el('table', { class: 'mini-table' });
    var thead = U.el('thead'); var htr = U.el('tr');
    cols.forEach(function (c) { htr.appendChild(U.el('th', { text: c })); });
    thead.appendChild(htr); table.appendChild(thead);
    var tbody = U.el('tbody');
    list.slice(0, 20).forEach(function (r, i) {
      var tr = U.el('tr', { class: r.overdue ? 'row-overdue' : '' });
      tr.appendChild(U.el('td', { class: 'rank', text: String(i + 1) }));
      tr.appendChild(U.el('td', { text: r.owner }));
      var sevTd = U.el('td'); sevTd.appendChild(sevBadge(r)); tr.appendChild(sevTd);
      tr.appendChild(U.el('td', { class: 'col-name', text: r.name || '-', title: r.name }));
      tr.appendChild(U.el('td', { text: r.host || '-' }));
      tr.appendChild(U.el('td', { class: 'col-due', text: U.fmtDate(r.realDue) }));
      tr.appendChild(U.el('td', { class: r.overdue ? 'st-overdue' : 'st-soon', text: statusText(r) }));
      tr.appendChild(U.el('td', { class: 'risk-score', text: U.num(r.riskScore) }));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(U.el('div', { class: 'table-scroll' }, [table]));

    var actions = U.el('div', { class: 'panel-actions' }, [
      U.el('button', { class: 'btn btn-secondary btn-sm', text: '查看全部（' + list.length + ' 筆）',
        onclick: function () { UI.openDetail('優先處理清單（' + list.length + ' 筆）', list); } }),
      U.el('button', { class: 'btn btn-primary btn-sm', text: '產生全部催辦',
        onclick: function () { global.Tracking && document.querySelector('[data-tab="tracking"]').click();
          // 直接開批次
          var m = global.Reminder.buildAll(result.owners);
          openMail('批次催辦（' + m.included + ' 人）', m); } }),
    ]);
    box.appendChild(actions);
  }

  /* 借用 tracking 的郵件 modal(避免重複) */
  function openMail(title, mail) {
    var content = U.el('div', { class: 'reminder-box' });
    content.appendChild(U.el('div', { class: 'reminder-field' }, [
      U.el('label', { text: '主旨' }),
      U.el('input', { type: 'text', class: 'reminder-subject', value: mail.subject, readonly: 'readonly' }),
    ]));
    var ta = U.el('textarea', { class: 'reminder-body', rows: '18' }); ta.value = mail.body;
    content.appendChild(U.el('div', { class: 'reminder-field' }, [U.el('label', { text: '內容' }), ta]));
    var footer = U.el('div', { class: 'reminder-actions' }, [
      U.el('button', { class: 'btn btn-primary', text: '複製整封（主旨+內容）',
        onclick: function () { UI.copyText('主旨：' + mail.subject + '\n\n' + ta.value); } }),
      U.el('button', { class: 'btn btn-secondary', text: '只複製內容', onclick: function () { UI.copyText(ta.value); } }),
    ]);
    UI.openModal(title, content, { footer: footer });
  }

  /* 風險加權 Top N */
  function renderRiskTop(result) {
    var box = document.getElementById('risk-top');
    box.innerHTML = '';
    var list = result.riskRanking || [];
    var head = U.el('div', { class: 'panel-bar' }, [
      U.el('h3', { html: '風險排序（前 ' + list.length + ' 名）' }),
    ]);
    box.appendChild(head);
    if (!list.length) { box.appendChild(U.el('p', { class: 'empty-hint', text: '無資料。' })); return; }

    var cols = ['#', '風險分數', '嚴重度', '弱點', '主機', '負責人', '真正到期日', '狀態'];
    var table = U.el('table', { class: 'mini-table' });
    var thead = U.el('thead'); var htr = U.el('tr');
    cols.forEach(function (c) { htr.appendChild(U.el('th', { text: c })); });
    thead.appendChild(htr); table.appendChild(thead);
    var tbody = U.el('tbody');
    list.forEach(function (r, i) {
      var tr = U.el('tr', { class: r.overdue ? 'row-overdue' : '' });
      tr.appendChild(U.el('td', { class: 'rank', text: String(i + 1) }));
      tr.appendChild(U.el('td', { class: 'risk-score strong', text: U.num(r.riskScore) }));
      var sevTd = U.el('td'); sevTd.appendChild(sevBadge(r)); tr.appendChild(sevTd);
      tr.appendChild(U.el('td', { class: 'col-name', text: r.name || '-', title: r.name }));
      tr.appendChild(U.el('td', { text: r.host || '-' }));
      tr.appendChild(U.el('td', { text: r.owner }));
      tr.appendChild(U.el('td', { class: 'col-due', text: U.fmtDate(r.realDue) }));
      tr.appendChild(U.el('td', { class: r.overdue ? 'st-overdue' : '', text: statusText(r) }));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
    // 「查看全部」原本只把同一份前 10 名再開一次；改為即時算出完整排序
    var allRecs = result.records || [];
    box.appendChild(U.el('div', { class: 'panel-actions' }, [
      U.el('button', { class: 'btn btn-secondary btn-sm', text: '查看全部（' + allRecs.length + ' 筆）',
        onclick: function () {
          var full = global.Analysis.riskRanking(allRecs, allRecs.length);
          UI.openDetail('風險排序（全部 ' + full.length + ' 筆）', full);
        } }),
    ]));
  }

  function renderSeverityChart(s, records) {
    var canvas = document.getElementById('chart-severity');
    if (!canvas || typeof Chart === 'undefined') return;
    var order = CFG.severityOrder;
    var labels = [], data = [], colors = [];
    order.forEach(function (sev) {
      var v = s.severityDist[sev] || 0;
      if (v > 0) { labels.push(sev); data.push(v); colors.push(CFG.severityColors[sev]); }
    });
    // 其他未歸類
    Object.keys(s.severityDist).forEach(function (sev) {
      if (order.indexOf(sev) < 0 && s.severityDist[sev] > 0) {
        labels.push(sev); data.push(s.severityDist[sev]);
        colors.push(CFG.severityColors[sev] || CFG.severityColors.Unknown);
      }
    });
    if (!data.length) { labels = ['無資料']; data = [1]; colors = ['#cfd8dc']; }

    var drill = UI.drillEvents(function (i) {
      var sev = labels[i];
      var list = (records || []).filter(function (r) { return (r.severity || 'Unknown') === sev; });
      if (list.length) UI.openDetail('嚴重度 ' + sev + '（' + list.length + ' 筆）', list);
    });
    charts.severity = new Chart(canvas.getContext('2d'), {
      type: 'doughnut',
      data: { labels: labels, datasets: [{ data: data, backgroundColor: colors, borderWidth: 2, borderColor: '#fff' }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        onClick: drill.onClick, onHover: drill.onHover,
        plugins: {
          legend: { position: 'right' },
          title: { display: true, text: '嚴重度分布', font: { size: 14 } },
        },
      },
    });
  }

  function renderDueChart(s, records) {
    var canvas = document.getElementById('chart-due');
    if (!canvas || typeof Chart === 'undefined') return;
    var A = global.Analysis;
    var labels = A.BANDS.map(function (b) { return b.label; });
    var data = A.BANDS.map(function (b) { return s.bands[b.key]; });
    var colors = ['#b71c1c', '#e64a19', '#f9a825', '#1976d2', '#546e7a', '#b0bec5'];

    var drill = UI.drillEvents(function (i) {
      var band = A.BANDS[i]; if (!band) return;
      var list = (records || []).filter(function (r) { return A.bandOf(r) === band.key; });
      if (list.length) UI.openDetail('到期時間帶：' + band.label + '（' + list.length + ' 筆）', list);
    });
    charts.due = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: { labels: labels, datasets: [{ label: '筆數', data: data, backgroundColor: colors, borderRadius: 6 }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        onClick: drill.onClick, onHover: drill.onHover,
        plugins: {
          legend: { display: false },
          title: { display: true, text: '到期時間帶分布', font: { size: 14 } },
        },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
  }

  global.Dashboard = { render: render, destroyCharts: destroyCharts };
})(window);

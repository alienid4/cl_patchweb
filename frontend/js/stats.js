/* ============================================================
 * js/stats.js
 * TAB3 例外/展延統計：處置階段(例外管理中 / 首次展延中 / 原始修補期限)
 * 統計、安全名單、占比圖、到期時間帶堆疊圖。數字可 drill-down。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var CFG = global.APP_CONFIG;
  var UI = global.UI;

  var charts = {};
  function destroyCharts() {
    Object.keys(charts).forEach(function (k) { if (charts[k]) { charts[k].destroy(); charts[k] = null; } });
  }

  var STAGE_COLORS = {
    exception: '#6a1b9a',  // 例外管理中 — 紫
    extension: '#1565c0',  // 首次展延中 — 藍
    original:  '#2e7d32',  // 原始修補期限 — 綠
    none:      '#78909c',  // 未定期限 — 灰
  };
  var BAND_LABELS = {
    overdue: '已逾期', d30: '30天內', d90: '31–90天', d180: '91–180天',
    over180: '180天以上', noDue: '無到期日',
  };

  function render(result) {
    // 面板自適應：該表無例外/展延欄 → 此分頁不適用(主控已隱藏分頁鈕)，不渲染
    if (result.caps && result.caps.stagePanel === false) {
      destroyCharts();
      ['stage-grid', 'gov-radar', 'stats-scope'].forEach(function (id) { var el = document.getElementById(id); if (el) el.innerHTML = ''; });
      return;
    }
    var ss = result.stageStats;
    var records = result.records;
    var F = global.Analysis.Filters;
    var soon = ss.soonDays;

    /* ---- 摘要列 ---- */
    var info = document.getElementById('stats-scope');
    info.innerHTML = '';
    // 用動態母體標籤：選「已結案」時不該再寫「未結案 N 筆」
    info.appendChild(U.el('span', { html:
      U.esc(result.closeLabel || '未結案') + ' <b>' + U.num(ss.total) + '</b> 筆，依處置階段分類。' }));

    /* ---- 階段卡片 ---- */
    var grid = document.getElementById('stage-grid');
    grid.innerHTML = '';

    // 安全名單卡(置頂強調)
    var safe = ss.safeList;
    // 0 筆時不綁點擊(比照其他可點數字)，避免點開空白 modal
    var safeCard = U.el('div', {
      class: 'stage-card stage-safe' + (safe.length ? '' : ' not-clickable'),
      onclick: safe.length
        ? function () { UI.openDetail('例外核准未到期（' + safe.length + ' 筆）', safe); }
        : null,
    }, [
      U.el('div', { class: 'stage-title', text: '例外核准未到期' }),
      U.el('div', { class: 'stage-big', text: U.num(safe.length) }),
      U.el('div', { class: 'stage-sub', text: '例外核准期限尚未到' }),
    ]);
    grid.appendChild(safeCard);

    ss.stages.forEach(function (st) {
      if (st.key === 'none' && st.total === 0) return; // 無「未定期限」則不顯示該卡
      var pct = ss.total ? Math.round(st.total / ss.total * 1000) / 10 : 0;
      var stageRecs = records.filter(function (r) { return r.stage === st.key; });

      var card = U.el('div', { class: 'stage-card', dataset: { stage: st.key } });
      card.style.borderTopColor = STAGE_COLORS[st.key];

      var head = U.el('div', { class: 'stage-title', text: st.label });
      var big = U.el('div', { class: 'stage-big', text: U.num(st.total) });
      big.style.color = STAGE_COLORS[st.key];
      var pctEl = U.el('div', { class: 'stage-sub', text: '占' + (result.closeLabel || '未結案') + ' ' + pct + '%' });

      // 可點的子指標
      var subs = U.el('div', { class: 'stage-subs' });
      function subChip(label, val, filterFn, cls) {
        var chip = U.el('div', { class: 'stage-chip ' + (cls || '') + (val > 0 ? ' clickable' : '') }, [
          U.el('span', { class: 'chip-num', text: U.num(val) }),
          U.el('span', { class: 'chip-label', text: label }),
        ]);
        if (val > 0) {
          chip.addEventListener('click', function () {
            var list = stageRecs.filter(filterFn);
            UI.openDetail(st.label + '　' + label + '（' + list.length + ' 筆）', list);
          });
        }
        return chip;
      }
      subs.appendChild(subChip('已逾期', st.overdue, F.overdue, 'chip-overdue'));
      subs.appendChild(subChip('近期到期', st.soon, F.soon, 'chip-soon'));
      subs.appendChild(subChip('尚未到期', st.safe, function (r) { return r.realDue && r.daysLeft > soon; }, 'chip-safe'));
      if (st.noDue > 0) subs.appendChild(subChip('無到期日', st.noDue, function (r) { return !r.realDue; }, ''));

      // 整卡(標題/總數)點擊 → 該階段全部
      var top = U.el('div', { class: 'stage-top clickable', onclick: function () {
        UI.openDetail(st.label + '（' + stageRecs.length + ' 筆）', stageRecs);
      } }, [head, big, pctEl]);

      card.appendChild(top);
      card.appendChild(subs);
      grid.appendChild(card);
    });

    /* ---- 圖表 ---- */
    destroyCharts();
    renderStagePie(ss, records);
    renderBandChart(ss, records);
  }

  /* 例外治理雷達：覆蓋率、例外破口、到期預警、慢性風險 */
  function renderGovernance(result) {
    var box = document.getElementById('gov-radar');
    if (!box) return;
    box.innerHTML = '';
    var g = result.governance;
    var records = result.records;

    box.appendChild(U.el('div', { class: 'panel-bar' }, [
      U.el('h3', { text: '例外與展延概況' }),
    ]));

    /* 上排：覆蓋率 / 例外破口 / 到期預警 */
    var covPct = Math.round(g.coverage * 1000) / 10;
    var row = U.el('div', { class: 'gov-row' });

    // 覆蓋率（點 → 看被例外覆蓋的實際筆數＝例外管理中）
    var covList = records.filter(function (r) { return r.stage === 'exception'; });
    var covBox = U.el('div', { class: 'gov-box' + (covList.length ? ' clickable' : '') }, [
      U.el('div', { class: 'gov-num', text: covPct + '%' }),
      U.el('div', { class: 'gov-label', text: '例外覆蓋率' }),
      U.el('div', { class: 'gov-sub', text: g.exCount + ' / ' + g.total + ' 筆' }),
    ]);
    if (covList.length) covBox.addEventListener('click', function () {
      UI.openDetail('例外覆蓋（例外管理中 ' + covList.length + ' 筆）', covList);
    });
    row.appendChild(covBox);

    // 例外破口(例外中卻已逾期)
    var breachBox = U.el('div', { class: 'gov-box gov-breach' + (g.expired.length ? ' clickable' : '') }, [
      U.el('div', { class: 'gov-num', text: U.num(g.expired.length) }),
      U.el('div', { class: 'gov-label', text: '例外核准已逾期' }),
    ]);
    if (g.expired.length) breachBox.addEventListener('click', function () {
      UI.openDetail('例外核准已逾期（' + g.expired.length + ' 筆）', g.expired);
    });
    row.appendChild(breachBox);

    // 到期預警(分級 chips)
    var warnBox = U.el('div', { class: 'gov-box' });
    warnBox.appendChild(U.el('div', { class: 'gov-label', text: '例外核准即將到期' }));
    var chips = U.el('div', { class: 'gov-chips' });
    g.expiry.forEach(function (e) {
      var chip = U.el('div', { class: 'gov-chip' + (e.records.length ? ' clickable' : '') }, [
        U.el('span', { class: 'chip-num', text: U.num(e.records.length) }),
        U.el('span', { class: 'chip-label', text: e.days + ' 天內' }),
      ]);
      if (e.records.length) chip.addEventListener('click', function () {
        UI.openDetail('例外 ' + e.days + ' 天內到期（' + e.records.length + ' 筆）', e.records);
      });
      chips.appendChild(chip);
    });
    warnBox.appendChild(chips);
    row.appendChild(warnBox);

    box.appendChild(row);

    /* 慢性風險清單 */
    var chronicWrap = U.el('div', { class: 'gov-chronic' });
    chronicWrap.appendChild(U.el('div', { class: 'gov-chronic-head' }, [
      U.el('h4', { html: '反覆展延／例外（≥ ' + (CFG.chronicThreshold || 2) + ' 次）　<span class="count-pill">' + g.chronic.length + '</span>' }),
    ]));

    if (!g.chronic.length) {
      chronicWrap.appendChild(U.el('p', { class: 'empty-hint', text: '目前無反覆展延/例外的項目。' }));
    } else {
      var cols = ['處置次數', '展延', '例外', '嚴重度', '弱點', '主機', '負責人', '真正到期日', '狀態'];
      var table = U.el('table', { class: 'mini-table' });
      var thead = U.el('thead'); var htr = U.el('tr');
      cols.forEach(function (c) { htr.appendChild(U.el('th', { text: c })); });
      thead.appendChild(htr); table.appendChild(thead);
      var tbody = U.el('tbody');
      g.chronic.forEach(function (r) {
        var st = r.overdue ? ('逾期 ' + r.overdueDays + ' 天')
               : (r.realDue === null ? '無到期日'
               : (r.daysLeft === 0 ? '今日到期' : ('尚餘 ' + r.daysLeft + ' 天')));
        var tr = U.el('tr', { class: r.overdue ? 'row-overdue' : '' });
        tr.appendChild(U.el('td', { class: 'chronic-count', text: U.num(r.actionCount) }));
        tr.appendChild(U.el('td', { text: U.num(r.extCount) }));
        tr.appendChild(U.el('td', { text: U.num(r.excCount) }));
        var sevTd = U.el('td');
        sevTd.appendChild(U.el('span', { class: 'sev-badge sev-' + (r.severity || 'Unknown'), text: r.severity || '-' }));
        tr.appendChild(sevTd);
        tr.appendChild(U.el('td', { class: 'col-name', text: r.name || '-', title: r.name }));
        tr.appendChild(U.el('td', { text: r.host || '-' }));
        tr.appendChild(U.el('td', { text: r.owner }));
        tr.appendChild(U.el('td', { class: 'col-due', text: U.fmtDate(r.realDue) }));
        tr.appendChild(U.el('td', { class: r.overdue ? 'st-overdue' : '', text: st }));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      chronicWrap.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
      chronicWrap.appendChild(U.el('button', { class: 'btn btn-secondary btn-sm', text: '匯出清單 (CSV)',
        onclick: function () { UI.exportCSV(g.chronic, '反覆展延或例外清單'); } }));
    }
    box.appendChild(chronicWrap);
  }

  function renderStagePie(ss, records) {
    var canvas = document.getElementById('chart-stage-pie');
    if (!canvas || typeof Chart === 'undefined') return;
    var labels = [], data = [], colors = [], keys = [];
    ss.stages.forEach(function (st) {
      if (st.total > 0) { labels.push(st.label); data.push(st.total); colors.push(STAGE_COLORS[st.key]); keys.push(st.key); }
    });
    if (!data.length) { labels = ['無資料']; data = [1]; colors = ['#cfd8dc']; keys = [null]; }
    var drill = UI.drillEvents(function (i) {
      var key = keys[i]; if (!key) return;
      var list = (records || []).filter(function (r) { return r.stage === key; });
      if (list.length) UI.openDetail(labels[i] + '（' + list.length + ' 筆）', list);
    });
    charts.pie = new Chart(canvas.getContext('2d'), {
      type: 'doughnut',
      data: { labels: labels, datasets: [{ data: data, backgroundColor: colors, borderWidth: 2, borderColor: '#fff' }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        onClick: drill.onClick, onHover: drill.onHover,
        plugins: { legend: { position: 'right' }, title: { display: true, text: '處置階段占比', font: { size: 14 } } },
      },
    });
  }

  function renderBandChart(ss, records) {
    var canvas = document.getElementById('chart-stage-band');
    if (!canvas || typeof Chart === 'undefined') return;
    var A = global.Analysis;
    var bands = ss.bands;
    var labels = bands.map(function (b) { return BAND_LABELS[b]; });
    var activeStages = ss.stages.filter(function (st) { return st.total > 0; });
    var datasets = activeStages.map(function (st) {
      return {
        label: st.label,
        data: bands.map(function (b) { return ss.bandMatrix[st.key][b]; }),
        backgroundColor: STAGE_COLORS[st.key],
        borderRadius: 4,
      };
    });
    var drill = UI.drillEvents(function (bandIdx, dsIdx) {
      var st = activeStages[dsIdx]; var bandKey = bands[bandIdx];
      if (!st || !bandKey) return;
      var list = (records || []).filter(function (r) { return r.stage === st.key && A.bandOf(r) === bandKey; });
      if (list.length) UI.openDetail(st.label + ' × ' + BAND_LABELS[bandKey] + '（' + list.length + ' 筆）', list);
    });
    charts.band = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        onClick: drill.onClick, onHover: drill.onHover,
        plugins: { legend: { position: 'top' }, title: { display: true, text: '到期時間帶 × 處置階段', font: { size: 14 } } },
        scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
  }

  global.Stats = { render: render, destroyCharts: destroyCharts };
})(window);

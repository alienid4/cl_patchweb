/* ============================================================
 * js/summary.js
 * 主管總覽首頁：一頁彙總全部項目(工作表)狀態。
 * KPI 合計 + 各項目狀態表(可點進細項) + 各項目未結案堆疊圖。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var CFG = global.APP_CONFIG;
  var chart = null;

  function inDept(r, dept) { return !dept || dept === '__all__' || (r.unit || '(未填)') === dept; }

  /* 單一項目(工作表)的彙總(可限定部門) */
  function agg(sheet, dept) {
    var soon = CFG.soonDays || 30;
    var recs = (sheet.records || []).filter(function (r) { return inDept(r, dept); });
    var open = recs.filter(function (r) { return r.closeBucket === 'open'; });
    var closed = recs.filter(function (r) { return r.closeBucket === 'closed'; });
    var overdue = open.filter(function (r) { return r.realDue && r.daysLeft < 0; });
    var soonN = open.filter(function (r) { return r.realDue && r.daysLeft >= 0 && r.daysLeft <= soon; });
    var high = open.filter(function (r) { return r.severity === 'Critical' || r.severity === 'High'; });
    return {
      name: sheet.name, total: recs.length, open: open.length, closed: closed.length,
      overdue: overdue.length, soon: soonN.length, high: high.length,
      rate: recs.length ? closed.length / recs.length : 0,
      openRecs: open, closedRecs: closed, overdueRecs: overdue, soonRecs: soonN, highRecs: high,
    };
  }

  /* 全部項目彙總(供 KPI / 複製摘要；可限定部門) */
  function overall(sheets, dept) {
    var rows = (sheets || []).map(function (s) { return agg(s, dept); });
    var t = { open: 0, closed: 0, overdue: 0, soon: 0, high: 0, total: 0 };
    rows.forEach(function (r) {
      t.open += r.open; t.closed += r.closed; t.overdue += r.overdue;
      t.soon += r.soon; t.high += r.high; t.total += r.total;
    });
    t.rate = t.total ? Math.round(t.closed / t.total * 1000) / 10 : 0;
    return { rows: rows, totals: t };
  }

  /* 跨所有項目，撈出「未結案且已逾期」的弱點（主管稽核：沒修補的弱點；可限定部門） */
  function collectOverdue(sheets, dept) {
    var out = [];
    (sheets || []).forEach(function (s) {
      (s.records || []).forEach(function (r) {
        if (r.closeBucket === 'open' && r.overdue && inDept(r, dept)) out.push(r);
      });
    });
    return out;
  }

  /* 全域逾期清單的前置欄位：讓主管一眼看到是哪個項目、哪個部門 */
  var OVERDUE_EXTRA_COLS = [
    { h: '項目', cls: 'col-name', disp: function (r) { return r.sheet; }, sortVal: function (r) { return (r.sheet || '').toLowerCase(); } },
    { h: '部門', cls: '', disp: function (r) { return r.unit || '(未填)'; }, sortVal: function (r) { return (r.unit || '').toLowerCase(); } },
  ];

  /* 開啟「全部逾期未結」明細（跨項目彙整，可排序 / 匯出 / 另開分頁） */
  function openAllOverdue(recs) {
    if (!global.UI) return;
    global.UI.openDetail('全部逾期未結清單（' + recs.length + ' 筆）', recs, { extraCols: OVERDUE_EXTRA_COLS });
  }

  function render(sheets, onSelect, dept) {
    var box = document.getElementById('summary-view');
    if (!box) return;
    box.innerHTML = '';
    var o = overall(sheets, dept);
    var rows = o.rows, t = o.totals;

    box.appendChild(U.el('div', { class: 'panel-bar' }, [
      U.el('h3', { text: (dept && dept !== '__all__') ? (dept + ' 總覽') : '全部項目總覽' }),
      U.el('span', { class: 'panel-bar-note', text: '共 ' + sheets.length + ' 個項目' }),
    ]));

    /* 逾期警示橫幅（主管稽核）：跨全部項目的「沒修補（逾期未結）」弱點，一鍵攤開 */
    var overdueRecs = collectOverdue(sheets, dept);
    if (overdueRecs.length) {
      var maxOd = overdueRecs.reduce(function (m, r) { return Math.max(m, r.overdueDays || 0); }, 0);
      var byDept = {};
      overdueRecs.forEach(function (r) { var u = r.unit || '(未填)'; byDept[u] = (byDept[u] || 0) + 1; });
      var topDepts = Object.keys(byDept)
        .sort(function (a, b) { return byDept[b] - byDept[a]; }).slice(0, 4)
        .map(function (u) { return u + '（' + byDept[u] + '）'; });
      box.appendChild(U.el('div', { class: 'overdue-alert' }, [
        U.el('span', { class: 'oa-icon', text: '⚠' }),
        U.el('span', { class: 'oa-text', html:
          '全部項目共 <b>' + U.num(overdueRecs.length) + '</b> 筆弱點<b>已逾期尚未結案</b>，最久已逾期 <b>' + U.num(maxOd) + '</b> 天。' +
          (topDepts.length ? '　逾期集中：' + U.esc(topDepts.join('、')) + '。' : '') }),
        U.el('button', { class: 'btn btn-danger btn-sm', text: '查看全部逾期清單',
          onclick: function () { openAllOverdue(overdueRecs); } }),
      ]));
    }

    /* KPI 合計（可點鑽取：跨全部項目彙整該類未結案紀錄） */
    var soon = CFG.soonDays || 30;
    function collect(pred) {
      var out = [];
      (sheets || []).forEach(function (s) {
        (s.records || []).forEach(function (r) {
          if (r.closeBucket === 'open' && inDept(r, dept) && (!pred || pred(r))) out.push(r);
        });
      });
      return out;
    }
    function collectClosed() {
      var out = [];
      (sheets || []).forEach(function (s) {
        (s.records || []).forEach(function (r) {
          if (r.closeBucket === 'closed' && inDept(r, dept)) out.push(r);
        });
      });
      return out;
    }
    var kpis = [
      { label: '未結案', value: t.open, cls: 'm-total', recs: function () { return collect(null); } },
      { label: '已逾期', value: t.overdue, cls: 'm-overdue', recs: function () { return collect(function (r) { return r.overdue; }); } },
      { label: '近期到期', value: t.soon, cls: 'm-warn', recs: function () { return collect(function (r) { return r.realDue && r.daysLeft >= 0 && r.daysLeft <= soon; }); } },
      { label: '高風險未結', value: t.high, cls: 'm-critical', recs: function () { return collect(function (r) { return r.severity === 'Critical' || r.severity === 'High'; }); } },
      { label: '整體結案率', value: t.rate + '%', cls: 'm-info', title: '整體已結案', recs: function () { return collectClosed(); } },
    ];
    var kgrid = U.el('div', { class: 'summary-kpis' });
    kpis.forEach(function (k) {
      var attrs = { class: 'metric-card ' + k.cls + (k.recs ? ' clickable' : '') };
      if (k.recs) attrs.onclick = function () {
        var list = k.recs();
        if (list.length) global.UI.openDetail((k.title || k.label) + '（' + list.length + ' 筆）', list);
      };
      kgrid.appendChild(U.el('div', attrs, [
        U.el('div', { class: 'metric-value', text: (typeof k.value === 'number' ? U.num(k.value) : k.value) }),
        U.el('div', { class: 'metric-label', text: k.label }),
      ]));
    });
    box.appendChild(kgrid);

    /* ---- 子分頁（免下拉）：項目狀態 / 圖表 / 趨勢 / SLA / 紅黑榜 ---- */
    var F = global.Features;
    function on(id) { return !F || F.isOn(id); }
    var subDefs = [
      { key: 'status', label: '項目狀態', fill: function (p) { buildStatusTable(p, rows, t, onSelect); } },
      { key: 'chart',  label: '圖表',     fill: function (p) { buildChartPanel(p, rows, onSelect); } },
    ];
    if (on('panel-trend') && global.History) subDefs.push({ key: 'trend', label: '趨勢', fill: function (p) { global.History.renderTrend(p, sheets); } });
    if (on('panel-sla')) subDefs.push({ key: 'sla', label: 'SLA', fill: function (p) { renderSLA(p, sheets, dept); } });
    if (on('panel-red-list')) subDefs.push({ key: 'rank', label: '紅黑榜', fill: function (p) { renderRankings(p, sheets, dept); } });

    var nav = U.el('div', { class: 'subtabs' });
    var panelMap = {}, filled = {};
    subDefs.forEach(function (def) {
      var btn = U.el('button', { class: 'subtab-btn', text: def.label });
      var panel = U.el('div', { class: 'subpanel', dataset: { sub: def.key } });
      panelMap[def.key] = { def: def, btn: btn, panel: panel };
      btn.addEventListener('click', function () { activateSub(def.key); });
      nav.appendChild(btn);
    });
    box.appendChild(nav);
    subDefs.forEach(function (def) { box.appendChild(panelMap[def.key].panel); });

    function activateSub(key) {
      subDefs.forEach(function (def) {
        var m = panelMap[def.key];
        m.btn.classList.toggle('active', def.key === key);
        m.panel.classList.toggle('active', def.key === key);
      });
      // 首次顯示才建立內容(圖表在可見狀態下建立，避免 Chart.js 4 在 display:none 下變 0×0)
      if (!filled[key]) { panelMap[key].def.fill(panelMap[key].panel); filled[key] = true; }
    }
    activateSub(subDefs[0].key);
  }

  /* 各項目未結案堆疊圖 → 填入 container(顯示時才建立) */
  function buildChartPanel(container, rows, onSelect) {
    container.appendChild(U.el('div', { class: 'chart-card summary-chart-card' }, [
      U.el('div', { class: 'chart-wrap' }, [U.el('canvas', { id: 'summary-chart' })]),
    ]));
    renderChart(rows, onSelect);
  }

  /* 各項目狀態表（可點列進細項、數字格下鑽、欄位標題排序）→ 填入 container */
  function buildStatusTable(container, rows, t, onSelect) {
    rows.forEach(function (r, i) { r._idx = i; });   // 記住原始項目索引(排序後仍能正確進入該項目)
    var statusOf = function (r) {
      return r.overdue > 0 ? { t: '有逾期', c: 'st-red' }
           : r.open > 0 ? { t: '追蹤中', c: 'st-amber' }
           : { t: '已全數結案', c: 'st-green' };
    };
    var cols = [
      { h: '項目', sortVal: function (r) { var m = String(r.name).match(/^\d+/); return m ? parseInt(m[0], 10) : String(r.name).toLowerCase(); },
        cell: function (r) { return U.el('td', { class: 'owner-cell', text: r.name }); } },
      { h: '未結案', num: true, sortVal: function (r) { return r.open; },
        cell: function (r) { return numDrillTd(r.open, r.name + '　未結案', r.openRecs); } },
      { h: '已逾期', num: true, sortVal: function (r) { return r.overdue; },
        cell: function (r) { return numDrillTd(r.overdue, r.name + '　已逾期', r.overdueRecs, r.overdue > 0 ? 'has-overdue' : ''); } },
      { h: '近期到期', num: true, sortVal: function (r) { return r.soon; },
        cell: function (r) { return numDrillTd(r.soon, r.name + '　近期到期', r.soonRecs); } },
      { h: '高風險未結', num: true, sortVal: function (r) { return r.high; },
        cell: function (r) { return numDrillTd(r.high, r.name + '　高風險未結', r.highRecs); } },
      { h: '已結案', num: true, sortVal: function (r) { return r.closed; },
        cell: function (r) { return numDrillTd(r.closed, r.name + '　已結案', r.closedRecs); } },
      { h: '結案率', num: true, sortVal: function (r) { return r.rate; },
        cell: function (r) { return U.el('td', { class: 'num-cell', text: (Math.round(r.rate * 1000) / 10) + '%' }); } },
      { h: '狀態', sortVal: function (r) { return r.overdue > 0 ? 2 : (r.open > 0 ? 1 : 0); },
        cell: function (r) { var st = statusOf(r); var td = U.el('td'); td.appendChild(U.el('span', { class: 'status-pill ' + st.c, text: st.t })); return td; } },
    ];

    var table = U.el('table', { class: 'tracking-table summary-table' });
    var thead = U.el('thead'); var htr = U.el('tr');
    var ths = [];
    cols.forEach(function (c, ci) {
      var th = U.el('th', { class: 'th-sort', dataset: { ci: String(ci) } }, [
        U.el('span', { text: c.h }), U.el('span', { class: 'sort-ind', text: '' }),
      ]);
      ths.push(th); htr.appendChild(th);
    });
    thead.appendChild(htr); table.appendChild(thead);

    var tbody = U.el('tbody'); table.appendChild(tbody);
    var sortState = { ci: null, dir: 1 };   // null = 原始項目順序

    function cmp(a, b, dir) {
      var an = (a === null || a === undefined || a === ''), bn = (b === null || b === undefined || b === '');
      if (an && bn) return 0; if (an) return 1; if (bn) return -1;
      var base = (typeof a === 'number' && typeof b === 'number') ? (a - b) : (String(a) < String(b) ? -1 : (String(a) > String(b) ? 1 : 0));
      return base * dir;
    }
    function renderBody() {
      var arr = rows.slice();
      if (sortState.ci !== null) {
        var col = cols[sortState.ci];
        arr.sort(function (a, b) { return cmp(col.sortVal(a), col.sortVal(b), sortState.dir); });
      }
      tbody.innerHTML = '';
      arr.forEach(function (r) {
        var tr = U.el('tr', { class: 'clickable' + (r.overdue > 0 ? ' row-overdue' : '') });
        tr.addEventListener('click', (function (idx) { return function () { onSelect(idx); }; })(r._idx));
        cols.forEach(function (c) { tr.appendChild(c.cell(r)); });
        tbody.appendChild(tr);
      });
      ths.forEach(function (th, i) {
        var ind = th.querySelector('.sort-ind');
        ind.textContent = (i === sortState.ci) ? (sortState.dir === 1 ? ' ▲' : ' ▼') : '';
        th.classList.toggle('sorted', i === sortState.ci);
      });
    }
    ths.forEach(function (th, i) {
      th.addEventListener('click', function () {
        if (sortState.ci === i) sortState.dir = -sortState.dir;
        else { sortState.ci = i; sortState.dir = cols[i].num ? -1 : 1; }   // 數字欄首點＝大到小
        renderBody();
      });
    });
    renderBody();

    var allR = { open: [], overdue: [], soon: [], high: [], closed: [] };
    rows.forEach(function (r) {
      allR.open = allR.open.concat(r.openRecs); allR.overdue = allR.overdue.concat(r.overdueRecs);
      allR.soon = allR.soon.concat(r.soonRecs); allR.high = allR.high.concat(r.highRecs);
      allR.closed = allR.closed.concat(r.closedRecs);
    });
    var tfoot = U.el('tfoot'); var ftr = U.el('tr', { class: 'total-row' });
    ftr.appendChild(U.el('td', { class: 'owner-cell', text: '合計' }));
    ftr.appendChild(numDrillTd(t.open, '全部項目　未結案', allR.open));
    ftr.appendChild(numDrillTd(t.overdue, '全部項目　已逾期', allR.overdue, t.overdue > 0 ? 'has-overdue' : ''));
    ftr.appendChild(numDrillTd(t.soon, '全部項目　近期到期', allR.soon));
    ftr.appendChild(numDrillTd(t.high, '全部項目　高風險未結', allR.high));
    ftr.appendChild(numDrillTd(t.closed, '全部項目　已結案', allR.closed));
    ftr.appendChild(U.el('td', { class: 'num-cell', text: t.rate + '%' }));
    ftr.appendChild(U.el('td', {}));
    tfoot.appendChild(ftr); table.appendChild(tfoot);
    container.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
  }

  /* 可點的數字格：val>0 → 點開該批實際筆數 */
  function drillTd(val, title, recs, extraCls) {
    var cls = 'num-cell' + (extraCls ? ' ' + extraCls : '');
    if (val > 0) {
      return U.el('td', { class: cls + ' clickable', text: U.num(val),
        onclick: function () { global.UI.openDetail(title + '（' + recs.length + ' 筆）', recs); } });
    }
    return U.el('td', { class: cls, text: U.num(val) });
  }

  /* 同 drillTd，但阻止冒泡(用於「整列可點」的表格內，避免同時觸發整列動作) */
  function numDrillTd(val, title, recs, extraCls) {
    var cls = 'num-cell' + (extraCls ? ' ' + extraCls : '');
    if (val > 0 && recs && recs.length) {
      return U.el('td', { class: cls + ' clickable', text: U.num(val),
        onclick: function (e) { e.stopPropagation(); global.UI.openDetail(title + '（' + recs.length + ' 筆）', recs); } });
    }
    return U.el('td', { class: cls, text: U.num(val) });
  }

  /* SLA 達成率：各嚴重度「未結案中未逾期」的比率（政策天數為目標對照） */
  function slaStats(sheets, dept) {
    var order = ['Critical', 'High', 'Medium', 'Low'];   // 只對有政策的四級
    var pol = CFG.sla || {};
    var acc = {};
    order.forEach(function (k) { acc[k] = { sev: k, days: pol[k], open: 0, overdue: 0, openRecords: [], overdueRecords: [] }; });
    (sheets || []).forEach(function (s) {
      (s.records || []).forEach(function (r) {
        if (!inDept(r, dept)) return;
        var a = acc[r.severity]; if (!a) return;
        if (r.closeBucket !== 'open') return;
        a.open++; a.openRecords.push(r);
        if (r.overdue) { a.overdue++; a.overdueRecords.push(r); }
      });
    });
    return order.map(function (k) { var a = acc[k]; a.rate = a.open ? (a.open - a.overdue) / a.open : null; return a; });
  }

  function renderSLA(box, sheets, dept) {
    var stats = slaStats(sheets, dept);
    box.appendChild(U.el('div', { class: 'panel-bar' }, [U.el('h3', { text: 'SLA 達成率' })]));

    var table = U.el('table', { class: 'tracking-table sla-table' });
    var thead = U.el('thead'), htr = U.el('tr');
    ['嚴重度', '政策(天)', '未結案', '逾期', '達成率'].forEach(function (h) { htr.appendChild(U.el('th', { text: h })); });
    thead.appendChild(htr); table.appendChild(thead);

    var tbody = U.el('tbody');
    stats.forEach(function (a) {
      var tr = U.el('tr', { class: a.overdue > 0 ? 'row-overdue' : '' });
      var sevTd = U.el('td'); sevTd.appendChild(U.el('span', { class: 'sev-badge sev-' + a.sev, text: a.sev }));
      tr.appendChild(sevTd);
      tr.appendChild(U.el('td', { class: 'num-cell', text: (a.days != null ? a.days : '-') }));
      tr.appendChild(drillTd(a.open, a.sev + '　未結案', a.openRecords));
      tr.appendChild(drillTd(a.overdue, a.sev + '　逾期未結', a.overdueRecords, a.overdue > 0 ? 'has-overdue' : ''));
      // 達成率格：長條 + 百分比
      var rateTd = U.el('td', { class: 'sla-rate-cell' });
      if (a.rate === null) {
        rateTd.appendChild(U.el('span', { class: 'sla-na', text: '—' }));
      } else {
        var pct = Math.round(a.rate * 1000) / 10;
        var cls = pct >= 90 ? 'sla-ok' : (pct >= 70 ? 'sla-warn' : 'sla-bad');
        var bar = U.el('div', { class: 'sla-bar' }, [U.el('i', { class: cls })]);
        bar.firstChild.style.width = pct + '%';
        rateTd.appendChild(bar);
        rateTd.appendChild(U.el('span', { class: 'sla-pct ' + cls, text: pct + '%' }));
      }
      tr.appendChild(rateTd);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
  }

  /* 跨全部項目，依某維度(部門/負責人)彙整並排名(逾期多者在前) */
  function rankBy(sheets, dept, keyFn) {
    var map = {};
    (sheets || []).forEach(function (s) {
      (s.records || []).forEach(function (r) {
        if (!inDept(r, dept)) return;
        var k = keyFn(r) || '(未填)';
        var g = map[k] || (map[k] = { name: k, total: 0, open: 0, closed: 0, overdue: 0, high: 0,
          openRecords: [], overdueRecords: [], highRecords: [], closedRecords: [] });
        g.total++;
        if (r.closeBucket === 'open') {
          g.open++; g.openRecords.push(r);
          if (r.overdue) { g.overdue++; g.overdueRecords.push(r); }
          if (r.severity === 'Critical' || r.severity === 'High') { g.high++; g.highRecords.push(r); }
        } else if (r.closeBucket === 'closed') { g.closed++; g.closedRecords.push(r); }
      });
    });
    var list = Object.keys(map).map(function (k) { var g = map[k]; g.rate = g.total ? g.closed / g.total : 0; return g; });
    list.sort(function (a, b) { return (b.overdue - a.overdue) || (b.open - a.open) || (a.rate - b.rate); });
    return list;
  }

  function renderRankings(box, sheets, dept) {
    box.appendChild(U.el('div', { class: 'panel-bar' }, [ U.el('h3', { text: '部門／負責人紅黑榜' }) ]));
    var wrap = U.el('div', { class: 'rank-wrap' });
    wrap.appendChild(rankBlock('部門', rankBy(sheets, dept, function (r) { return r.unit || '(未填)'; })));
    wrap.appendChild(rankBlock('負責人', rankBy(sheets, dept, function (r) { return r.owner || '(未指定)'; })));
    box.appendChild(wrap);
  }

  function rankBlock(nameHeader, list) {
    var block = U.el('div', { class: 'rank-block' });
    block.appendChild(U.el('div', { class: 'rank-block-title', text: nameHeader + '排行' }));
    if (!list.length) { block.appendChild(U.el('p', { class: 'empty-hint', text: '無資料。' })); return block; }

    function drillCell(val, title, recs, extraCls) {
      var cls = 'num-cell' + (extraCls ? ' ' + extraCls : '');
      if (val > 0) {
        return U.el('td', { class: cls + ' clickable', text: U.num(val),
          onclick: function () { global.UI.openDetail(title + '（' + recs.length + ' 筆）', recs); } });
      }
      return U.el('td', { class: cls, text: U.num(val) });
    }
    var cols = [
      { h: nameHeader, sortVal: function (g) { return String(g.name).toLowerCase(); },
        cell: function (g) { return U.el('td', { class: 'owner-cell', text: g.name }); } },
      { h: '未結案', num: true, sortVal: function (g) { return g.open; },
        cell: function (g) { return drillCell(g.open, g.name + '　未結案', g.openRecords); } },
      { h: '已逾期', num: true, sortVal: function (g) { return g.overdue; },
        cell: function (g) { return drillCell(g.overdue, g.name + '　已逾期', g.overdueRecords, g.overdue > 0 ? 'has-overdue' : ''); } },
      { h: '高風險未結', num: true, sortVal: function (g) { return g.high; },
        cell: function (g) { return drillCell(g.high, g.name + '　高風險未結', g.highRecords); } },
      { h: '已結案', num: true, sortVal: function (g) { return g.closed; },
        cell: function (g) { return drillCell(g.closed, g.name + '　已結案', g.closedRecords); } },
      { h: '結案率', num: true, sortVal: function (g) { return g.rate; },
        cell: function (g) { return U.el('td', { class: 'num-cell', text: (Math.round(g.rate * 1000) / 10) + '%' }); } },
    ];
    var rowClass = function (g) { return g.overdue > 0 ? 'row-overdue' : (g.open === 0 ? 'row-clean' : ''); };
    block.appendChild(makeSortableTable(cols, list, 2, -1, rowClass, 5));   // 預設只顯示前 5 名
    return block;
  }

  /* 通用可排序表：cols=[{h,num,sortVal,cell}], initCi/initDir 預設排序欄, rowClass(row)→class
   * limit：預設只顯示前 N 列(依目前排序)，超過時附「展開全部／收合」按鈕。 */
  function makeSortableTable(cols, rows, initCi, initDir, rowClass, limit) {
    var table = U.el('table', { class: 'tracking-table rank-table' });
    var thead = U.el('thead'); var htr = U.el('tr'); var ths = [];
    cols.forEach(function (c) {
      var th = U.el('th', { class: 'th-sort' }, [U.el('span', { text: c.h }), U.el('span', { class: 'sort-ind', text: '' })]);
      ths.push(th); htr.appendChild(th);
    });
    thead.appendChild(htr); table.appendChild(thead);
    var tbody = U.el('tbody'); table.appendChild(tbody);
    var sortState = { ci: (initCi == null ? null : initCi), dir: initDir || 1 };
    var capped = !!(limit && rows.length > limit);
    var expanded = false;
    function cmp(a, b, dir) {
      var an = (a === null || a === undefined || a === ''), bn = (b === null || b === undefined || b === '');
      if (an && bn) return 0; if (an) return 1; if (bn) return -1;
      var base = (typeof a === 'number' && typeof b === 'number') ? (a - b) : (String(a) < String(b) ? -1 : (String(a) > String(b) ? 1 : 0));
      return base * dir;
    }
    function renderBody() {
      var arr = rows.slice();
      if (sortState.ci != null) { var col = cols[sortState.ci]; arr.sort(function (a, b) { return cmp(col.sortVal(a), col.sortVal(b), sortState.dir); }); }
      var show = (capped && !expanded) ? arr.slice(0, limit) : arr;
      tbody.innerHTML = '';
      show.forEach(function (r) {
        var tr = U.el('tr', { class: (rowClass ? rowClass(r) : '') });
        cols.forEach(function (c) { tr.appendChild(c.cell(r)); });
        tbody.appendChild(tr);
      });
      ths.forEach(function (th, i) {
        var ind = th.querySelector('.sort-ind');
        ind.textContent = (i === sortState.ci) ? (sortState.dir === 1 ? ' ▲' : ' ▼') : '';
        th.classList.toggle('sorted', i === sortState.ci);
      });
    }
    ths.forEach(function (th, i) {
      th.addEventListener('click', function () {
        if (sortState.ci === i) sortState.dir = -sortState.dir;
        else { sortState.ci = i; sortState.dir = cols[i].num ? -1 : 1; }
        renderBody();
      });
    });
    renderBody();

    var wrapper = U.el('div', {}, [U.el('div', { class: 'table-scroll' }, [table])]);
    if (capped) {
      var btn = U.el('button', { class: 'btn btn-secondary btn-sm rank-expand' });
      function updateBtn() { btn.textContent = expanded ? '收合' : ('展開全部（' + rows.length + '）'); }
      btn.addEventListener('click', function () { expanded = !expanded; renderBody(); updateBtn(); });
      updateBtn();
      wrapper.appendChild(btn);
    }
    return wrapper;
  }

  function renderChart(rows, onSelect) {
    if (chart) { chart.destroy(); chart = null; }
    var canvas = document.getElementById('summary-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    var labels = rows.map(function (r) { var m = r.name.match(/^\d+/); return m ? m[0] : r.name; });
    var drill = global.UI.drillEvents(function (i) { if (onSelect && rows[i]) onSelect(i); });
    chart = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          { label: '已逾期', data: rows.map(function (r) { return r.overdue; }), backgroundColor: '#b71c1c', borderRadius: 3 },
          { label: '其他未結', data: rows.map(function (r) { return Math.max(0, r.open - r.overdue); }), backgroundColor: '#1976d2', borderRadius: 3 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        onClick: drill.onClick, onHover: drill.onHover,
        plugins: { legend: { position: 'top' }, title: { display: false } },
        scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
  }

  function destroyChart() { if (chart) { chart.destroy(); chart = null; } }

  global.Summary = { render: render, overall: overall, destroyChart: destroyChart, collectOverdue: collectOverdue };
})(window);

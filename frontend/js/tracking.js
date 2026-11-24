/* ============================================================
 * js/tracking.js
 * TAB2 人員追蹤：每位負責人的統計表(數字可點擊 drill-down) +
 * 「產生催辦內容」按鈕。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var UI = global.UI;

  /* 可下鑽的數字格：val>0 → 點開該批實際筆數 */
  function drillTd(val, title, recs, extraCls) {
    var cls = 'num-cell' + (extraCls ? ' ' + extraCls : '');
    if (val > 0 && recs && recs.length) {
      return U.el('td', { class: cls + ' clickable', text: U.num(val),
        onclick: function () { UI.openDetail(title + '（' + recs.length + ' 筆）', recs); } });
    }
    return U.el('td', { class: cls, text: U.num(val) });
  }

  function render(result) {
    var owners = result.owners;
    var container = document.getElementById('tracking-body');
    container.innerHTML = '';

    if (!owners.length) {
      container.appendChild(U.el('p', { class: 'empty-hint', text: '無符合條件的未結案資料。' }));
      return;
    }

    // 工具列：批次催辦
    var needRemind = owners.filter(function (o) {
      var soon = o.records.filter(function (r) { return !r.overdue && global.Analysis.helpers.withinDays(r, (global.APP_CONFIG.soonDays || 30)); }).length;
      return (o.overdue + soon) > 0;
    }).length;
    var toolbar = U.el('div', { class: 'track-toolbar' }, [
      U.el('button', {
        class: 'btn btn-primary btn-sm',
        text: '產生全部催辦（' + needRemind + ' 人）',
        onclick: function () { openBatch(owners); },
      }),
    ]);
    container.appendChild(toolbar);

    var BANDS = global.Analysis.BANDS;
    var bandOf = global.Analysis.bandOf;

    // 合計列資料：未結案 + 各互斥時間帶（含實際紀錄供下鑽）
    var totals = { open: 0, bands: {} };
    var totalRecs = { open: [], bands: {} };
    BANDS.forEach(function (b) { totals.bands[b.key] = 0; totalRecs.bands[b.key] = []; });
    owners.forEach(function (o) {
      totals.open += o.open;
      totalRecs.open = totalRecs.open.concat(o.records);
      BANDS.forEach(function (b) { totals.bands[b.key] += o.bands[b.key]; });
      o.records.forEach(function (r) { var bk = bandOf(r); if (totalRecs.bands[bk]) totalRecs.bands[bk].push(r); });
    });

    var table = U.el('table', { class: 'tracking-table sortable' });

    var headDefs = [{ label: '負責人', key: 'owner' }, { label: (result.closeLabel || '未結案'), key: 'open' }]
      .concat(BANDS.map(function (b) { return { label: b.label, key: 'band:' + b.key }; }))
      .concat([{ label: '催辦', key: null }]);

    function sortVal(o, key) {
      if (key === 'owner') return o.owner;
      if (key === 'open') return o.open;
      if (key.indexOf('band:') === 0) return o.bands[key.slice(5)];
      return 0;
    }
    var sortState = { key: 'band:overdue', dir: -1 }; // 預設逾期多者在前
    var thead = U.el('thead');
    var htr = U.el('tr');
    var ths = [];
    headDefs.forEach(function (h) {
      var th = U.el('th', { class: h.key ? 'th-sort' : '' }, [
        U.el('span', { text: h.label }), U.el('span', { class: 'sort-ind', text: '' }),
      ]);
      if (h.key) {
        th.addEventListener('click', function () {
          if (sortState.key === h.key) sortState.dir = -sortState.dir;
          else { sortState.key = h.key; sortState.dir = (h.key === 'owner' ? 1 : -1); }
          renderRows();
        });
      }
      ths.push({ th: th, def: h });
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    var tbody = U.el('tbody');
    table.appendChild(tbody);

    function renderRows() {
      var arr = owners.slice().sort(function (a, b) {
        var va = sortVal(a, sortState.key), vb = sortVal(b, sortState.key);
        var c = (sortState.key === 'owner') ? String(va).localeCompare(String(vb)) : (va - vb);
        return c * sortState.dir;
      });
      tbody.innerHTML = '';
      arr.forEach(function (o) { tbody.appendChild(ownerRow(o)); });
      ths.forEach(function (x) {
        var ind = x.th.querySelector('.sort-ind');
        var on = x.def.key === sortState.key;
        ind.textContent = on ? (sortState.dir === 1 ? ' ▲' : ' ▼') : '';
        x.th.classList.toggle('sorted', on);
      });
    }
    renderRows();

    // 合計列
    var tfoot = U.el('tfoot');
    var ftr = U.el('tr', { class: 'total-row' });
    ftr.appendChild(U.el('td', { class: 'owner-cell', text: '合計（' + owners.length + ' 人）' }));
    ftr.appendChild(drillTd(totals.open, '全部負責人　未結案', totalRecs.open));
    BANDS.forEach(function (b) {
      ftr.appendChild(drillTd(totals.bands[b.key], '全部負責人　' + b.label, totalRecs.bands[b.key], (b.key === 'overdue' && totals.bands[b.key] > 0) ? 'has-overdue' : ''));
    });
    ftr.appendChild(U.el('td', {}));
    tfoot.appendChild(ftr);
    table.appendChild(tfoot);

    var scroll = U.el('div', { class: 'table-scroll' }, [table]);
    container.appendChild(scroll);

    // 未來六個月「人員 × 月」到期矩陣
    renderMatrix(result, container);
  }

  /* 人員 × 月 到期矩陣（未來六個月），格子可點開明細 */
  function renderMatrix(result, container) {
    var dm = result.dueMatrix;
    if (!dm) return;

    container.appendChild(U.el('div', { class: 'matrix-head' }, [
      U.el('h3', { text: '未來六個月到期（人員 × 月）' }),
      U.el('span', { class: 'panel-bar-note', text: '期間 ' + dm.rangeLabel }),
    ]));

    if (!dm.rows.length) {
      container.appendChild(U.el('p', { class: 'empty-hint', text: '未來六個月無到期項目。' }));
      return;
    }

    var table = U.el('table', { class: 'tracking-table matrix-table' });

    // 表頭：負責人 + 各月 + 合計
    var thead = U.el('thead'); var htr = U.el('tr');
    htr.appendChild(U.el('th', { text: '負責人' }));
    dm.months.forEach(function (mm) { htr.appendChild(U.el('th', { text: mm.label })); });
    htr.appendChild(U.el('th', { text: '合計' }));
    thead.appendChild(htr); table.appendChild(thead);

    // 內容
    var tbody = U.el('tbody');
    dm.rows.forEach(function (row) {
      var tr = U.el('tr');
      tr.appendChild(U.el('td', { class: 'owner-cell', text: row.owner }));
      dm.months.forEach(function (mm) {
        var n = row.counts[mm.key] || 0;
        var td = U.el('td', { class: 'num-cell' + (n > 0 ? ' clickable' : ' zero'), text: n ? U.num(n) : '·' });
        if (n > 0) {
          td.title = row.owner + ' ' + mm.label + ' 到期 ' + n + ' 筆';
          td.addEventListener('click', function () {
            UI.openDetail(row.owner + '　' + mm.label + '到期（' + n + ' 筆）', row.recs[mm.key]);
          });
        }
        tr.appendChild(td);
      });
      var rowRecs = [];
      dm.months.forEach(function (mm) { if (row.recs[mm.key]) rowRecs = rowRecs.concat(row.recs[mm.key]); });
      tr.appendChild(drillTd(row.total, row.owner + '　未來六個月到期', rowRecs, 'total-cell'));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    // 合計列（可下鑽）
    var colRecs = {}; var allRecs = [];
    dm.months.forEach(function (mm) { colRecs[mm.key] = []; });
    dm.rows.forEach(function (row) {
      dm.months.forEach(function (mm) { if (row.recs[mm.key]) colRecs[mm.key] = colRecs[mm.key].concat(row.recs[mm.key]); });
    });
    dm.months.forEach(function (mm) { allRecs = allRecs.concat(colRecs[mm.key]); });
    var tfoot = U.el('tfoot'); var ftr = U.el('tr', { class: 'total-row' });
    ftr.appendChild(U.el('td', { class: 'owner-cell', text: '合計（' + dm.rows.length + ' 人）' }));
    dm.months.forEach(function (mm) { ftr.appendChild(drillTd(dm.totals[mm.key], mm.label + ' 到期', colRecs[mm.key])); });
    ftr.appendChild(drillTd(dm.totals.total, '未來六個月到期', allRecs, 'total-cell'));
    tfoot.appendChild(ftr); table.appendChild(tfoot);

    container.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
  }

  function rowHeader(labels) {
    var tr = U.el('tr');
    labels.forEach(function (l) { tr.appendChild(U.el('th', { text: l })); });
    return tr;
  }

  function ownerRow(o) {
    var BANDS = global.Analysis.BANDS;
    var bandOf = global.Analysis.bandOf;
    var tr = U.el('tr');
    tr.appendChild(U.el('td', { class: 'owner-cell', text: o.owner }));

    // 未結案(總)：點開全部
    var openTd = U.el('td', { class: 'num-cell' + (o.open > 0 ? ' clickable' : ''), text: U.num(o.open) });
    if (o.open > 0) {
      openTd.title = '查看 ' + o.owner + ' 全部未結案';
      openTd.addEventListener('click', function () {
        UI.openDetail(o.owner + '　未結案（' + o.open + ' 筆）', o.records);
      });
    }
    tr.appendChild(openTd);

    // 互斥時間帶：各格點開該帶明細
    BANDS.forEach(function (b) {
      var n = o.bands[b.key];
      var clickable = n > 0;
      var td = U.el('td', {
        class: 'num-cell' + (clickable ? ' clickable' : ' zero') + (b.key === 'overdue' && n > 0 ? ' has-overdue' : ''),
        text: n ? U.num(n) : '·',
      });
      if (clickable) {
        td.title = '查看 ' + o.owner + ' 的「' + b.label + '」明細';
        td.addEventListener('click', function () {
          var list = o.records.filter(function (r) { return bandOf(r) === b.key; });
          UI.openDetail(o.owner + '　' + b.label + '（' + list.length + ' 筆）', list);
        });
      }
      tr.appendChild(td);
    });

    // 催辦按鈕
    var btnTd = U.el('td', { class: 'action-cell' });
    btnTd.appendChild(U.el('button', {
      class: 'btn btn-primary btn-sm',
      text: '產生催辦內容',
      onclick: function () { openReminder(o); },
    }));
    tr.appendChild(btnTd);

    return tr;
  }

  /* 開啟催辦內容 modal(含複製) — 單人 */
  function openReminder(ownerGroup) {
    openMailModal('催辦內容　—　' + ownerGroup.owner, global.Reminder.build(ownerGroup));
  }

  /* 開啟批次催辦 modal */
  function openBatch(owners) {
    var mail = global.Reminder.buildAll(owners);
    openMailModal('批次催辦　—　' + global.APP_CONFIG.filter.department + '（' + mail.included + ' 人）', mail);
  }

  /* 共用：郵件內容 modal(主旨可編、內容可編、可複製/開信) */
  function openMailModal(title, mail) {
    var content = U.el('div', { class: 'reminder-box' });

    var subjRow = U.el('div', { class: 'reminder-field' }, [
      U.el('label', { text: '主旨' }),
      U.el('input', { type: 'text', class: 'reminder-subject', value: mail.subject, readonly: 'readonly' }),
    ]);
    content.appendChild(subjRow);

    var bodyRow = U.el('div', { class: 'reminder-field' }, [U.el('label', { text: '內容' })]);
    var ta = U.el('textarea', { class: 'reminder-body', rows: '18' });
    ta.value = mail.body;
    bodyRow.appendChild(ta);
    content.appendChild(bodyRow);

    var copyAll = U.el('button', {
      class: 'btn btn-primary',
      text: '複製整封（主旨+內容）',
      onclick: function () { UI.copyText('主旨：' + mail.subject + '\n\n' + ta.value); },
    });
    var copyBody = U.el('button', {
      class: 'btn btn-secondary',
      text: '只複製內容',
      onclick: function () { UI.copyText(ta.value); },
    });
    var mailto = U.el('a', {
      class: 'btn btn-secondary',
      href: 'mailto:?subject=' + encodeURIComponent(mail.subject) + '&body=' + encodeURIComponent(ta.value),
      text: '開啟郵件軟體',
    });

    var footer = U.el('div', { class: 'reminder-actions' }, [copyAll, copyBody, mailto]);
    // sticky：催辦內容常在複製到一半，誤點關掉就得重產
    UI.openModal(title, content, { footer: footer, sticky: true });
  }

  global.Tracking = { render: render };
})(window);

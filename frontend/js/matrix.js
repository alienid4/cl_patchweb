/* ============================================================
 * js/matrix.js
 * 交叉分析（排除式漏斗 cross-filter）。
 *   維度：嚴重度、到期時間帶（矩陣）、部門、負責人（facet chips）。
 *   互動：
 *     - 點嚴重度列首 / 到期欄首 → 把「那一個」藏起來（其餘全留），再點復原
 *     - 點格子裡的數字 → 直接看那格的實際筆數（drill-down）
 *     - 部門 / 負責人 chip → 聚焦某一個（再點取消）
 *   把不想看的關掉，剩下的就是最後想看的資料（下方清單可匯出）。
 * 母體 = 目前分頁的 result.records（已套用左側部門 + 結案狀態；預設未結案）。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var UI = global.UI;
  var A = global.Analysis;

  var SEV_ORDER = ['Critical', 'High', 'Medium', 'Low', 'Info', 'Unknown'];

  var DIMS = {
    sev:   function (r) { return r.severity || 'Unknown'; },
    band:  function (r) { return A.bandOf(r); },
    dept:  function (r) { return r.unit || r.department || '(未填)'; },
    owner: function (r) { return r.owner || '(未指定)'; },
  };

  var base = [];
  var title = '交叉分析';
  // hiddenSevs/hiddenBands：被藏起來的嚴重度/時間帶；dept/owner：聚焦值
  var state = { hiddenSevs: [], hiddenBands: [], dept: null, owner: null };

  function newState() { return { hiddenSevs: [], hiddenBands: [], dept: null, owner: null }; }

  function visSev(r)  { return state.hiddenSevs.indexOf(DIMS.sev(r)) < 0; }
  function visBand(r) { return state.hiddenBands.indexOf(DIMS.band(r)) < 0; }
  function okDept(r)  { return state.dept === null || DIMS.dept(r) === state.dept; }
  function okOwner(r) { return state.owner === null || DIMS.owner(r) === state.owner; }

  // 矩陣母體：只套 部門/負責人 聚焦（嚴重度/時間帶的藏顯由 render 決定）
  function matrixRecords() { return base.filter(function (r) { return okDept(r) && okOwner(r); }); }
  // 最終清單：套用全部（藏起來的排除、聚焦的保留）
  function filtered() { return base.filter(function (r) { return visSev(r) && visBand(r) && okDept(r) && okOwner(r); }); }

  function bandLabel(key) {
    var b = A.BANDS.filter(function (x) { return x.key === key; })[0];
    return b ? b.label : key;
  }

  function render(result) {
    var host = document.getElementById('matrix-body');
    if (!host) return;
    base = (result && result.records) || [];
    state = newState();
    draw();
  }

  function draw() {
    var host = document.getElementById('matrix-body');
    host.innerHTML = '';
    if (!base.length) {
      host.appendChild(U.el('p', { class: 'empty-hint', text: '目前母體無資料可交叉。' }));
      return;
    }
    var cond = buildConditionBar();
    if (cond) host.appendChild(cond);
    host.appendChild(buildMatrix());
    host.appendChild(buildFacets());
    host.appendChild(buildResult());
  }

  /* -------- 目前條件列：已隱藏的嚴重度/時間帶 + 聚焦的部門/負責人 -------- */
  function buildConditionBar() {
    var tags = [];
    state.hiddenSevs.forEach(function (s) {
      tags.push({ label: '隱藏 ' + s, undo: function () { pull(state.hiddenSevs, s); } });
    });
    state.hiddenBands.forEach(function (bk) {
      tags.push({ label: '隱藏 ' + bandLabel(bk), undo: function () { pull(state.hiddenBands, bk); } });
    });
    if (state.dept !== null)  tags.push({ label: '部門：' + state.dept,   undo: function () { state.dept = null; } });
    if (state.owner !== null) tags.push({ label: '負責人：' + state.owner, undo: function () { state.owner = null; } });
    if (!tags.length) return null;

    var bar = U.el('div', { class: 'xf-condition' });
    bar.appendChild(U.el('span', { class: 'xf-cond-label', text: '目前條件' }));
    tags.forEach(function (t) {
      bar.appendChild(U.el('button', {
        class: 'xf-cond-tag', html: U.esc(t.label) + ' ✕',
        onclick: function () { t.undo(); draw(); },
      }));
    });
    bar.appendChild(U.el('button', {
      class: 'btn btn-secondary btn-sm', text: '清除全部',
      onclick: function () { state = newState(); draw(); },
    }));
    return bar;
  }

  /* -------- 矩陣：嚴重度 × 到期時間帶 --------
   * 藏起來的嚴重度/時間帶 → 整列/整欄不出現，其餘全留。格子數字可點看實際筆數。 */
  function buildMatrix() {
    var recs = matrixRecords();
    var set = {}; recs.forEach(function (r) { set[DIMS.sev(r)] = 1; });
    var allSevs = SEV_ORDER.filter(function (s) { return set[s]; });
    var visSevs = allSevs.filter(function (s) { return state.hiddenSevs.indexOf(s) < 0; });
    var visBands = A.BANDS.filter(function (b) { return state.hiddenBands.indexOf(b.key) < 0; });

    // 全量統計(所有嚴重度×時間帶；藏的只是不 render)
    var g = {};
    allSevs.forEach(function (s) { g[s] = {}; A.BANDS.forEach(function (b) { g[s][b.key] = 0; }); });
    recs.forEach(function (r) {
      var s = DIMS.sev(r); if (!g[s]) return;
      var bk = DIMS.band(r); if (g[s][bk] !== undefined) g[s][bk]++;
    });

    var wrap = U.el('div', { class: 'xf-matrix-wrap' });
    if (!visSevs.length || !visBands.length) {
      wrap.appendChild(U.el('p', { class: 'empty-hint', text: '已隱藏全部。' }));
      return wrap;
    }

    var showRowTotal = visBands.length > 1;   // 每列右側「合計」欄
    var showColTotal = visSevs.length > 1;    // 底部「合計」列
    function rowTot(s) { var t = 0; visBands.forEach(function (b) { t += g[s][b.key]; }); return t; }
    function colTot(bk) { var t = 0; visSevs.forEach(function (s) { t += g[s][bk]; }); return t; }
    var grand = 0; visSevs.forEach(function (s) { grand += rowTot(s); });

    // 合計格下鑽
    var visBandKeys = visBands.map(function (b) { return b.key; });
    var visSevSet = {}; visSevs.forEach(function (s) { visSevSet[s] = 1; });
    function totalTd(val, recs2, ttl) {
      if (val > 0 && recs2.length) {
        return U.el('td', { class: 'total-cell clickable', text: U.num(val),
          onclick: function () { UI.openDetail(ttl + '（' + recs2.length + ' 筆）', recs2); } });
      }
      return U.el('td', { class: 'total-cell', text: U.num(val) });
    }
    function rowTotRecs(s) { return recs.filter(function (r) { return DIMS.sev(r) === s && visBandKeys.indexOf(DIMS.band(r)) >= 0; }); }
    function colTotRecs(bk) { return recs.filter(function (r) { return DIMS.band(r) === bk && visSevSet[DIMS.sev(r)]; }); }
    function grandRecs() { return recs.filter(function (r) { return visSevSet[DIMS.sev(r)] && visBandKeys.indexOf(DIMS.band(r)) >= 0; }); }

    var table = U.el('table', { class: 'matrix-table xf-matrix' });

    var thead = U.el('thead'), htr = U.el('tr');
    htr.appendChild(U.el('th', { class: 'xf-corner', text: '嚴重度＼到期' }));
    visBands.forEach(function (b) {
      htr.appendChild(U.el('th', {
        class: 'xf-col-head', text: b.label,
        onclick: function () { toggleHide('hiddenBands', b.key); },   // 藏這一欄
      }));
    });
    if (showRowTotal) htr.appendChild(U.el('th', { class: 'xf-col-total', text: '合計' }));
    thead.appendChild(htr); table.appendChild(thead);

    var tbody = U.el('tbody');
    visSevs.forEach(function (s) {
      var tr = U.el('tr');
      tr.appendChild(U.el('th', {
        class: 'xf-row-head',
        onclick: function () { toggleHide('hiddenSevs', s); },        // 藏這一列
      }, [U.el('span', { class: 'sev-badge sev-' + s, text: s })]));
      visBands.forEach(function (b) {
        var n = g[s][b.key];
        var td = U.el('td', { class: 'xf-cell' });
        if (n) {
          td.appendChild(U.el('span', {
            class: 'num-cell clickable' + (b.key === 'overdue' ? ' has-overdue' : ''),
            text: U.num(n),
            onclick: function () { cellDrill(s, b); },               // 看實際筆數
          }));
        } else {
          td.appendChild(U.el('span', { class: 'xf-zero', text: '·' }));
        }
        tr.appendChild(td);
      });
      if (showRowTotal) tr.appendChild(totalTd(rowTot(s), rowTotRecs(s), s + ' 合計'));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    if (showColTotal) {
      var tfoot = U.el('tfoot'), ftr = U.el('tr', { class: 'total-row' });
      ftr.appendChild(U.el('th', { class: 'xf-row-head', text: '合計' }));
      visBands.forEach(function (b) { ftr.appendChild(totalTd(colTot(b.key), colTotRecs(b.key), b.label + ' 合計')); });
      if (showRowTotal) ftr.appendChild(totalTd(grand, grandRecs(), '合計'));
      tfoot.appendChild(ftr); table.appendChild(tfoot);
    }

    wrap.appendChild(table);
    return wrap;
  }

  /* -------- Facet chips：部門 / 負責人（聚焦某一個） -------- */
  var FACET_DEFS = [
    { key: 'dept', name: '部門', pool: function () { return base.filter(function (r) { return visSev(r) && visBand(r) && okOwner(r); }); } },
    { key: 'owner', name: '負責人', pool: function () { return base.filter(function (r) { return visSev(r) && visBand(r) && okDept(r); }); } },
  ];
  function buildFacets() {
    var box = U.el('div', { class: 'xf-facets' });
    FACET_DEFS.forEach(function (def) {
      var counts = {};
      def.pool().forEach(function (r) { var v = DIMS[def.key](r); counts[v] = (counts[v] || 0) + 1; });
      var vals = Object.keys(counts);
      if (vals.length <= 1 && state[def.key] === null) return;
      vals.sort(function (a, b) { return counts[b] - counts[a] || a.localeCompare(b); });

      var row = U.el('div', { class: 'xf-facet' });
      row.appendChild(U.el('span', { class: 'xf-facet-label', text: def.name }));
      vals.forEach(function (v) {
        var on = state[def.key] === v;
        row.appendChild(U.el('button', {
          class: 'search-chip xf-chip' + (on ? ' active' : ''),
          html: U.esc(v) + ' <span class="xf-chip-n">' + counts[v] + '</span>',
          onclick: (function (k, val) { return function () { setFacet(k, val); }; })(def.key, v),
        }));
      });
      box.appendChild(row);
    });
    return box;
  }

  /* -------- 篩選結果清單 -------- */
  function buildResult() {
    var recs = filtered();
    var box = U.el('div', { class: 'xf-result' });
    var head = U.el('div', { class: 'xf-result-head' });
    head.appendChild(U.el('strong', { text: '符合 ' + U.num(recs.length) + ' 筆' }));
    var acts = U.el('div', { class: 'reminder-actions' });
    acts.appendChild(U.el('button', {
      class: 'btn btn-secondary btn-sm', text: '另開新分頁',
      onclick: function () { UI.popOutTable(resultTitle(), recs); },
    }));
    acts.appendChild(U.el('button', {
      class: 'btn btn-secondary btn-sm', text: '匯出此清單 (CSV)',
      onclick: function () { UI.exportCSV(recs, resultTitle()); },
    }));
    head.appendChild(acts);
    box.appendChild(head);
    box.appendChild(UI.buildDetailTable(recs));
    return box;
  }
  function resultTitle() {
    var t = [];
    if (state.dept !== null) t.push(state.dept);
    if (state.owner !== null) t.push(state.owner);
    return title + (t.length ? '（' + t.join(' × ') + '）' : '');
  }

  /* -------- 互動 -------- */
  function pull(arr, v) { var i = arr.indexOf(v); if (i >= 0) arr.splice(i, 1); }
  function toggleHide(setName, val) {
    var arr = state[setName]; var i = arr.indexOf(val);
    if (i >= 0) arr.splice(i, 1); else arr.push(val);
    draw();
  }
  function setFacet(dim, val) { state[dim] = state[dim] === val ? null : val; draw(); }
  function cellDrill(s, b) {
    var list = matrixRecords().filter(function (r) { return DIMS.sev(r) === s && DIMS.band(r) === b.key; });
    if (list.length) UI.openDetail(s + ' × ' + b.label + '（' + list.length + ' 筆）', list);
  }

  global.Matrix = { render: render };
})(window);

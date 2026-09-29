/* ============================================================
 * js/history.js
 * 歷史快照與趨勢（跟上次比）。
 *   每次「匯入檔案」自動記一份輕量快照(不存整份 Excel，只存彙總數字＋未結弱點指紋)。
 *   去重：同檔名同日 → 覆蓋更新(重載/自動還原不重複記)。
 *   趨勢畫面：本次 vs 上次(新增/結案/淨變化，可點看實際筆數) + 未結/逾期趨勢線。
 * 儲存：localStorage『vulnDashboard.history』，只留最近 CAP 期。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var KEY = 'vulnDashboard.history';
  var CAP = 12;
  var trendChart = null;

  /* 型別守門：JSON 合法但型別不對(舊版格式/外部寫入)會讓後續 push 丟例外，
   * 而該例外原本被吞掉或誤報成「解析失敗」 */
  function load() {
    try {
      var v = JSON.parse(localStorage.getItem(KEY));
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }
  /* 配額耗盡原本靜默失敗：歷史悄悄停在舊的一期，「跟上次比」的數字從此都是錯的 */
  function save(arr) {
    try { localStorage.setItem(KEY, JSON.stringify(arr)); return true; }
    catch (e) {
      if (global.UI && global.UI.toast) {
        global.UI.toast('本機儲存空間不足，趨勢快照未能保存（可到「清除暫存資料」釋放）', 'error');
      }
      return false;
    }
  }
  function clear() { try { localStorage.removeItem(KEY); } catch (e) {} }
  function keyOf(r) { return (r.host || '') + '||' + (r.pluginId || ''); }

  /* 一份檔案 → 輕量彙總(全部部門) */
  function summarize(sheets) {
    var open = 0, overdue = 0, closed = 0, high = 0, total = 0, seen = {};
    (sheets || []).forEach(function (s) {
      (s.records || []).forEach(function (r) {
        total++;
        if (r.closeBucket === 'open') {
          open++; seen[keyOf(r)] = 1;
          if (r.overdue) overdue++;
          if (r.severity === 'Critical' || r.severity === 'High') high++;
        } else if (r.closeBucket === 'closed') closed++;
      });
    });
    return {
      open: open, overdue: overdue, closed: closed, high: high, total: total,
      rate: total ? Math.round(closed / total * 1000) / 10 : 0,
      openKeys: Object.keys(seen),
    };
  }

  /* 記一份快照(同檔名同日覆蓋)；dateStr = 'YYYY-MM-DD' */
  function record(sheets, fileName, dateStr) {
    var snap = summarize(sheets);
    snap.fileName = fileName || '';
    snap.date = dateStr;
    snap.at = Date.now();
    var arr = load();
    var idx = -1;
    for (var i = 0; i < arr.length; i++) {
      if (arr[i].fileName === snap.fileName && arr[i].date === snap.date) { idx = i; break; }
    }
    if (idx >= 0) arr[idx] = snap; else arr.push(snap);
    arr.sort(function (a, b) { return a.at - b.at; });
    if (arr.length > CAP) arr = arr.slice(arr.length - CAP);
    save(arr);
    return arr;
  }

  function destroyChart() { if (trendChart) { trendChart.destroy(); trendChart = null; } }

  /* -------- 趨勢畫面 -------- */
  function renderTrend(box, sheets) {
    destroyChart();
    var arr = load();
    box.appendChild(U.el('div', { class: 'panel-bar' }, [U.el('h3', { text: '趨勢（跟上次比）' })]));
    if (!arr.length) {
      box.appendChild(U.el('p', { class: 'empty-hint', text: '尚無歷史資料。' }));
      return;
    }
    var cur = arr[arr.length - 1], prev = arr.length >= 2 ? arr[arr.length - 2] : null;

    var wrap = U.el('div', { class: 'trend-delta' });
    if (prev) {
      var newKeys = cur.openKeys.filter(function (k) { return prev.openKeys.indexOf(k) < 0; });
      var goneKeys = prev.openKeys.filter(function (k) { return cur.openKeys.indexOf(k) < 0; });
      var net = cur.open - prev.open;
      wrap.appendChild(deltaCard('新增未結', U.num(newKeys.length), 'up-bad', newKeys.length ? function () {
        var recs = currentNewRecords(sheets, newKeys);
        if (recs.length) global.UI.openDetail('本次新增未結（' + recs.length + ' 筆）', recs);
      } : null));
      wrap.appendChild(deltaCard('結案／消失', U.num(goneKeys.length), 'down-good', goneKeys.length ? function () {
        openKeyList('本次結案／消失（' + goneKeys.length + ' 筆）', goneKeys);
      } : null));
      wrap.appendChild(deltaCard('未結淨變化', (net > 0 ? '+' : '') + U.num(net), net > 0 ? 'up-bad' : (net < 0 ? 'down-good' : 'flat'), null));
      wrap.appendChild(U.el('div', { class: 'trend-cmp-note', text: '本期 ' + cur.date + '　vs　上期 ' + prev.date }));
    } else {
      wrap.appendChild(U.el('div', { class: 'trend-cmp-note', text: '目前只有 1 期（' + cur.date + '）' }));
    }
    box.appendChild(wrap);

    if (arr.length >= 2) {
      box.appendChild(U.el('div', { class: 'chart-card' }, [
        U.el('div', { class: 'chart-wrap' }, [U.el('canvas', { id: 'trend-chart' })]),
      ]));
      renderChart(arr);
    }
  }

  function deltaCard(label, valText, cls, onclick) {
    var attrs = { class: 'trend-card ' + cls + (onclick ? ' clickable' : '') };
    if (onclick) attrs.onclick = onclick;
    return U.el('div', attrs, [
      U.el('div', { class: 'tc-val', text: valText }),
      U.el('div', { class: 'tc-label', text: label }),
    ]);
  }

  /* 本次新增(未結)的實際紀錄：目前檔案裡 open 且 key 在 newKeys 內 */
  function currentNewRecords(sheets, newKeys) {
    var set = {}; newKeys.forEach(function (k) { set[k] = 1; });
    var out = [];
    (sheets || []).forEach(function (s) {
      (s.records || []).forEach(function (r) {
        if (r.closeBucket === 'open' && set[keyOf(r)]) out.push(r);
      });
    });
    return out;
  }

  /* 已消失(上次未結、本次不在未結)的弱點：僅有指紋(主機+Plugin ID) */
  function openKeyList(title, keys) {
    var table = U.el('table', { class: 'detail-table' });
    var thead = U.el('thead'), htr = U.el('tr');
    ['Host', 'Plugin ID'].forEach(function (h) { htr.appendChild(U.el('th', { text: h })); });
    thead.appendChild(htr); table.appendChild(thead);
    var tbody = U.el('tbody');
    keys.forEach(function (k) {
      var p = k.split('||');
      var tr = U.el('tr');
      tr.appendChild(U.el('td', { class: 'col-host', text: p[0] || '-' }));
      tr.appendChild(U.el('td', { text: p[1] || '-' }));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    global.UI.openModal(title, U.el('div', {}, [
      U.el('p', { class: 'detail-count', text: '共 ' + keys.length + ' 筆' }),
      U.el('div', { class: 'detail-viewport' }, [table]),
    ]));
  }

  function renderChart(arr) {
    destroyChart();
    var canvas = document.getElementById('trend-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    var labels = arr.map(function (s) { return s.date; });
    trendChart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          { label: '未結案', data: arr.map(function (s) { return s.open; }), borderColor: '#009142', backgroundColor: '#009142', tension: 0.25 },
          { label: '已逾期', data: arr.map(function (s) { return s.overdue; }), borderColor: '#c62828', backgroundColor: '#c62828', tension: 0.25 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top' }, title: { display: true, text: '未結案／已逾期 趨勢', font: { size: 14 } } },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
  }

  global.History = { record: record, all: load, clear: clear, renderTrend: renderTrend, destroyChart: destroyChart };
})(window);

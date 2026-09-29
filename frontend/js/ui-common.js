/* ============================================================
 * js/ui-common.js
 * 共用 UI：Modal 彈窗、Drill-down 明細表、Toast 提示、複製。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;

  /* -------- Toast -------- */
  function toast(msg, type) {
    var box = document.getElementById('toast');
    if (!box) return;
    box.textContent = msg;
    box.className = 'toast show ' + (type || 'info');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { box.className = 'toast'; }, 2600);
  }

  /* -------- Modal --------
   * 全站共用同一個 overlay。若在 modal 內再開 modal（例如 Email 設定 → 發信紀錄），
   * 直接覆寫會把原本的表單與勾選狀態整個清掉，關閉後回不去。
   * 因此提供 opts.stack：把目前這層的節點暫存，關閉時還原上一層。
   *
   * opts.sticky：這層「不會被誤觸關掉」——點 overlay 空白處與按 Esc 都無效，
   * 只有右上 ✕ 或畫面上的按鈕能關。用於有使用者輸入、關掉就得重來的流程
   * (Email 設定、催辦內容)。唯讀的明細 modal 不要設，那些好關比較方便。 */
  var modalStack = [];
  var stickyNow = false;

  function openModal(title, contentNode, opts) {
    opts = opts || {};
    var overlay = document.getElementById('modal-overlay');
    var titleEl = document.getElementById('modal-title');
    var bodyEl = document.getElementById('modal-body');
    var footEl = document.getElementById('modal-footer');

    if (opts.stack && overlay.classList.contains('show')) {
      // 用 DocumentFragment 保存節點本身(而非 innerHTML)，才能保留輸入值與事件
      var savedBody = document.createDocumentFragment();
      while (bodyEl.firstChild) savedBody.appendChild(bodyEl.firstChild);
      var savedFoot = document.createDocumentFragment();
      while (footEl.firstChild) savedFoot.appendChild(footEl.firstChild);
      // 連同上一層的 sticky 一起存，收掉這層時要還原回去
      modalStack.push({ title: titleEl.textContent, body: savedBody, foot: savedFoot, sticky: stickyNow });
    }

    stickyNow = !!opts.sticky;

    titleEl.textContent = title;
    bodyEl.innerHTML = '';
    footEl.innerHTML = '';
    bodyEl.appendChild(contentNode);

    if (opts.footer) footEl.appendChild(opts.footer);

    overlay.classList.add('show');
    document.body.classList.add('modal-open');
  }

  function closeModal() {
    var overlay = document.getElementById('modal-overlay');
    // 若有上一層，先還原上一層而不是整個關掉
    if (modalStack.length) {
      var prev = modalStack.pop();
      var titleEl = document.getElementById('modal-title');
      var bodyEl = document.getElementById('modal-body');
      var footEl = document.getElementById('modal-footer');
      titleEl.textContent = prev.title;
      bodyEl.innerHTML = ''; bodyEl.appendChild(prev.body);
      footEl.innerHTML = ''; footEl.appendChild(prev.foot);
      stickyNow = prev.sticky;      // 回到上一層，sticky 也跟著還原
      return;
    }
    overlay.classList.remove('show');
    document.body.classList.remove('modal-open');
    stickyNow = false;
  }

  /* 完全關閉(清空堆疊)：需要一次收掉整組 modal 時用 */
  function closeAllModals() {
    modalStack.length = 0;
    stickyNow = false;
    closeModal();
  }

  /* 目前這層是否禁止誤觸關閉（給 main.js 的 overlay 點擊與 Esc 判斷用） */
  function isModalSticky() { return stickyNow; }

  /* -------- 明細表欄位定義(供 drill-down / 搜尋 / 新分頁共用) --------
   * disp: 顯示字串；sortVal: 排序鍵(數字/時間戳/字串，null 一律排最後)
   */
  var SEV_RANK = { Critical: 5, High: 4, Medium: 3, Low: 2, Info: 1 };
  function ts(d) { return d ? d.getTime() : null; }
  function overdueSortVal(r) {
    if (r.realDue === null) return null;
    return r.daysLeft < 0 ? r.overdueDays : 0;
  }
  var DETAIL_COLUMNS = [
    { h: 'Host', cls: 'col-host', disp: function (r) { return r.host; }, sortVal: function (r) { return (r.host || '').toLowerCase(); } },
    { h: '負責人', cls: '', disp: function (r) { return r.owner; }, sortVal: function (r) { return (r.owner || '').toLowerCase(); } },
    { h: 'Name', cls: 'col-name', disp: function (r) { return r.name; }, sortVal: function (r) { return (r.name || '').toLowerCase(); } },
    { h: 'Risk', cls: '', disp: function (r) { return r.risk; }, sortVal: function (r) { return (r.risk || '').toLowerCase(); } },
    { h: 'Severity', cls: '', sev: true, disp: function (r) { return r.severity; }, sortVal: function (r) { return SEV_RANK[r.severity] || 0; } },
    { h: 'Plugin ID', cls: '', disp: function (r) { return r.pluginId; }, sortVal: function (r) { var n = parseInt(r.pluginId, 10); return isNaN(n) ? (r.pluginId || '').toLowerCase() : n; } },
    { h: '修補期限', cls: '', disp: function (r) { return U.fmtDate(r.fixDeadline); }, sortVal: function (r) { return ts(r.fixDeadline); } },
    { h: '首次展延上限', cls: '', disp: function (r) { return U.fmtDate(r.firstExtension); }, sortVal: function (r) { return ts(r.firstExtension); } },
    { h: '例外核准期限', cls: '', disp: function (r) { return U.fmtDate(r.exceptionApproval); }, sortVal: function (r) { return ts(r.exceptionApproval); } },
    { h: '真正到期日', cls: 'col-due', disp: function (r) { return U.fmtDate(r.realDue); }, sortVal: function (r) { return ts(r.realDue); } },
    { h: '逾期天數', cls: '', overdue: true, disp: function (r) { return r.realDue === null ? '-' : (r.daysLeft < 0 ? String(r.overdueDays) : '0'); }, sortVal: overdueSortVal },
  ];

  function cmp(a, b) {
    // null/undefined 一律排最後
    var an = (a === null || a === undefined || a === ''), bn = (b === null || b === undefined || b === '');
    if (an && bn) return 0;
    if (an) return 1;
    if (bn) return -1;
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return String(a) < String(b) ? -1 : (String(a) > String(b) ? 1 : 0);
  }
  /* 方向感知比較：空值一律墊底(不受升/降序影響) */
  function sortCmp(a, b, dir) {
    var an = (a === null || a === undefined || a === ''), bn = (b === null || b === undefined || b === '');
    if (an && bn) return 0;
    if (an) return 1;   // a 空 → 永遠在後
    if (bn) return -1;  // b 空 → 永遠在後
    var base = (typeof a === 'number' && typeof b === 'number') ? (a - b)
             : (String(a) < String(b) ? -1 : (String(a) > String(b) ? 1 : 0));
    return base * dir;
  }

  /* -------- Drill-down 明細表(可排序、固定高度捲動視窗) --------
   * extraCols: 選填，前置欄位陣列(如「項目」「部門」)，接在固定欄位之前。
   */
  function buildDetailTable(records, extraCols) {
    if (!records.length) {
      return U.el('div', {}, [U.el('p', { class: 'empty-hint', text: '無符合條件的資料。' })]);
    }
    var cols = (extraCols && extraCols.length) ? extraCols.concat(DETAIL_COLUMNS) : DETAIL_COLUMNS;
    var count = U.el('p', { class: 'detail-count', text: '共 ' + records.length + ' 筆' });

    var table = U.el('table', { class: 'detail-table sortable' });
    var thead = U.el('thead');
    var htr = U.el('tr');
    var ths = [];
    cols.forEach(function (c, ci) {
      var th = U.el('th', { class: 'th-sort ' + (c.cls || ''), dataset: { ci: String(ci) } }, [
        U.el('span', { text: c.h }),
        U.el('span', { class: 'sort-ind', text: '' }),
      ]);
      ths.push(th);
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    var tbody = U.el('tbody');
    table.appendChild(tbody);

    // 初始排序：真正到期日 asc(最急在前)。以欄名定位，避免前置欄位造成索引位移。
    var dueCi = 0;
    for (var di = 0; di < cols.length; di++) { if (cols[di].h === '真正到期日') { dueCi = di; break; } }
    var sortState = { ci: dueCi, dir: 1 };

    function renderBody() {
      var col = cols[sortState.ci];
      var arr = records.slice().sort(function (a, b) {
        return sortCmp(col.sortVal(a), col.sortVal(b), sortState.dir);
      });
      tbody.innerHTML = '';
      arr.forEach(function (r) {
        var tr = U.el('tr', { class: r.overdue ? 'row-overdue' : '' });
        cols.forEach(function (c) {
          var val = c.disp(r);
          var td = U.el('td', { class: c.cls || '' });
          if (c.sev) {
            td.appendChild(U.el('span', { class: 'sev-badge sev-' + (r.severity || 'Unknown'), text: val || '-' }));
          } else if (c.overdue && r.overdue) {
            td.appendChild(U.el('span', { class: 'overdue-num', text: val }));
          } else {
            td.textContent = (val === '' || val === null || val === undefined) ? '-' : val;
            if (c.cls === 'col-name' || c.cls === 'col-remark' || c.cls === 'col-host') td.title = td.textContent;
          }
          tr.appendChild(td);
        });
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
        else { sortState.ci = i; sortState.dir = 1; }
        renderBody();
      });
    });
    renderBody();

    var viewport = U.el('div', { class: 'detail-viewport' }, [table]);
    enableDragScroll(viewport);   // 可用滑鼠「抓住拖曳」左右/上下捲動
    return U.el('div', {}, [count, viewport]);
  }

  /* -------- 抓取拖曳捲動：在捲動容器上按住拖曳即可平移(寬表好拉) --------
   * 不攔截表頭排序/按鈕/可點數字；移動超過門檻才視為拖曳。 */
  function enableDragScroll(el) {
    if (!el) return;
    el.classList.add('drag-scroll');
    el.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      if (e.target.closest('th, button, a, input, select, .clickable, .num-cell')) return;
      var startX = e.pageX, startY = e.pageY, sl = el.scrollLeft, st = el.scrollTop, moved = false;
      el.classList.add('dragging');
      function move(ev) {
        var dx = ev.pageX - startX, dy = ev.pageY - startY;
        if (!moved && (Math.abs(dx) > 3 || Math.abs(dy) > 3)) moved = true;
        el.scrollLeft = sl - dx; el.scrollTop = st - dy;
        if (moved) ev.preventDefault();
      }
      function up() {
        el.classList.remove('dragging');
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      }
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  }

  /* 開啟 drill-down modal(含 匯出 / 新分頁開啟)
   * opts.extraCols: 選填，前置欄位(如「項目」「部門」)，明細/CSV/新分頁一致沿用。
   */
  function openDetail(title, records, opts) {
    opts = opts || {};
    var extra = opts.extraCols;
    var content = buildDetailTable(records, extra);
    var footer = U.el('div', { class: 'reminder-actions' }, [
      U.el('button', { class: 'btn btn-secondary', text: '另開新分頁', onclick: function () { popOutTable(title, records, extra); } }),
      U.el('button', { class: 'btn btn-secondary', text: '匯出此清單 (CSV)', onclick: function () { exportCSV(records, title, extra); } }),
    ]);
    openModal(title, content, { footer: footer });
  }

  /* -------- 在新瀏覽器分頁開啟可排序表格(獨立頁面) -------- */
  function popOutTable(title, records, extraCols) {
    var cols = (extraCols && extraCols.length) ? extraCols.concat(DETAIL_COLUMNS) : DETAIL_COLUMNS;
    function esc2(v) { return U.esc(v === null || v === undefined ? '' : v); }
    // 欄位型別掃全欄取第一個非空值判定：原本只看 records[0]，
    // 首筆若沒有到期日(sortVal 回 null)整欄會被當字串排序，導致逾期 100 天排在 9 天之後
    function colType(c) {
      for (var i = 0; i < records.length; i++) {
        var v = c.sortVal(records[i]);
        if (v !== null && v !== undefined) return (typeof v === 'number') ? 'num' : 'str';
      }
      return 'str';
    }
    var thead = cols.map(function (c) { return '<th data-t="' + colType(c) + '">' + esc2(c.h) + '<span class="ind"></span></th>'; }).join('');
    var rowsHtml = records.slice().sort(function (a, b) { return cmp(ts(a.realDue), ts(b.realDue)); }).map(function (r) {
      var tds = cols.map(function (c) {
        var val = c.disp(r);
        var sv = c.sortVal(r);
        var svAttr = (sv === null || sv === undefined) ? '' : (' data-s="' + esc2(sv) + '"');
        var cls = c.sev ? 'sev sev-' + (r.severity || 'Unknown') : '';
        var disp = (val === '' || val === null || val === undefined) ? '-' : val;
        return '<td class="' + cls + '"' + svAttr + '>' + esc2(disp) + '</td>';
      }).join('');
      return '<tr class="' + (r.overdue ? 'od' : '') + '">' + tds + '</tr>';
    }).join('');

    var html = '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">' +
      '<title>' + esc2(title) + '</title><style>' +
      'body{font-family:"Segoe UI","Microsoft JhengHei",sans-serif;margin:0;background:#f4f6fa;color:#1f2937;}' +
      'h1{font-size:16px;padding:12px 16px;margin:0;background:#009142;color:#fff;position:sticky;top:0;z-index:5;}' +
      '.meta{padding:8px 16px;font-size:13px;color:#64748b;}' +
      '.wrap{overflow:auto;max-height:calc(100vh - 90px);margin:0 12px 12px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;}' +
      'table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap;}' +
      'th{position:sticky;top:0;background:#eef2f7;padding:9px 12px;text-align:left;cursor:pointer;border-bottom:2px solid #e2e8f0;user-select:none;}' +
      'th:hover{background:#e2e8f0;}td{padding:8px 12px;border-bottom:1px solid #eef1f5;}' +
      'tr.od{background:#fff5f5;}tr:hover{background:#f8fafd;}' +
      '.sev{color:#fff;font-weight:700;text-align:center;}.sev-Critical{background:#b71c1c;}.sev-High{background:#e64a19;}' +
      '.sev-Medium{background:#f9a825;color:#4a3800;}.sev-Low{background:#43a047;}.sev-Info{background:#607d8b;}.sev-Unknown{background:#90a4ae;}' +
      '.ind{color:#009142;font-weight:700;}</style></head><body>' +
      '<h1>' + esc2(title) + '</h1><div class="meta">共 ' + records.length + ' 筆 · 點欄位標題排序 · 可自由左右捲動</div>' +
      '<div class="wrap"><table><thead><tr>' + thead + '</tr></thead><tbody>' + rowsHtml + '</tbody></table></div>' +
      // 排序規則與畫面內的 sortCmp 一致：空值一律墊底(不論升冪或降冪)，
      // 原本空值取 -Infinity，升冪時會全部擠到最前面，同一份資料兩種呈現
      '<script>(function(){var t=document.querySelector("table");var ths=t.tHead.rows[0].cells;var dir=1,cur=-1;' +
      'function raw(td){var s=td.getAttribute("data-s");return (s===null||s==="")?null:s;}' +
      'function val(s,type){return type==="num"?(parseFloat(String(s).replace(/[^0-9.\\-]/g,""))):String(s).toLowerCase();}' +
      'for(var i=0;i<ths.length;i++){(function(ci){ths[ci].addEventListener("click",function(){var type=ths[ci].getAttribute("data-t");if(cur===ci)dir=-dir;else{cur=ci;dir=1;}' +
      'for(var k=0;k<ths.length;k++){var sp=ths[k].querySelector(".ind");if(sp)sp.textContent=(k===ci)?(dir===1?" \\u25B2":" \\u25BC"):"";}' +
      'var tb=t.tBodies[0];var rows=[].slice.call(tb.rows);rows.sort(function(a,b){' +
      'var ra=raw(a.cells[ci]),rb=raw(b.cells[ci]);' +
      'if(ra===null&&rb===null)return 0;if(ra===null)return 1;if(rb===null)return -1;' +
      'var va=val(ra,type),vb=val(rb,type);' +
      'if(type==="num"){if(isNaN(va)&&isNaN(vb))return 0;if(isNaN(va))return 1;if(isNaN(vb))return -1;}' +
      'return (va<vb?-1:va>vb?1:0)*dir;});' +
      'rows.forEach(function(r){tb.appendChild(r);});});})(i);}})();<\/script></body></html>';

    var w = window.open('', '_blank');
    if (!w) { toast('瀏覽器封鎖了新分頁，請允許彈出視窗', 'error'); return; }
    w.document.open(); w.document.write(html); w.document.close();
  }

  /* -------- 複製到剪貼簿 -------- */
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text)
        .then(function () { toast('已複製到剪貼簿', 'success'); })
        .catch(function () { fallbackCopy(text); });
    }
    fallbackCopy(text);
    return Promise.resolve();
  }
  function fallbackCopy(text) {
    var ta = U.el('textarea', { class: 'copy-fallback' });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('已複製到剪貼簿', 'success'); }
    catch (e) { toast('複製失敗，請手動選取', 'error'); }
    document.body.removeChild(ta);
  }

  /* -------- 匯出 CSV(drill-down 清單) --------
   * extraCols: 選填，前置欄位(如「項目」「部門」)，與畫面明細一致。
   */
  function exportCSV(records, title, extraCols) {
    extraCols = extraCols || [];
    var headers = extraCols.map(function (c) { return c.h; }).concat(
      ['Host', 'Name', 'Risk', 'Severity', 'Plugin ID', '修補期限',
       '首次展延上限', '例外核准期限', '真正到期日', '逾期天數', '負責人']);
    var rows = records.map(function (r) {
      return extraCols.map(function (c) { return c.disp(r); }).concat([
        r.host, r.name, r.risk, r.severity, r.pluginId,
        U.fmtDate(r.fixDeadline), U.fmtDate(r.firstExtension), U.fmtDate(r.exceptionApproval),
        U.fmtDate(r.realDue),
        (r.realDue === null ? '' : (r.daysLeft < 0 ? r.overdueDays : 0)),
        r.owner,
      ]);
    });
    var csv = [headers].concat(rows).map(function (arr) {
      return arr.map(function (v) {
        var s = (v === null || v === undefined) ? '' : String(v);
        if (/[",\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
        return s;
      }).join(',');
    }).join('\r\n');

    // 加 BOM 讓 Excel 正確辨識 UTF-8
    var blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = U.el('a', { href: url, download: (title || '弱點清單').replace(/[\\\/:*?"<>|]/g, '_') + '.csv' });
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('已匯出 CSV', 'success');
  }

  /* -------- 圖表鑽取：綁到 Chart.js options（點圖元素→drill、hover→手指游標） --------
   * resolve(index, datasetIndex) 由呼叫端自行決定動作(通常 openDetail 或切表)。
   */
  function drillEvents(resolve) {
    return {
      onHover: function (evt, els) {
        var t = evt && evt.native && evt.native.target;
        if (t) t.style.cursor = (els && els.length) ? 'pointer' : 'default';
      },
      onClick: function (evt, els) {
        if (!els || !els.length) return;
        resolve(els[0].index, els[0].datasetIndex);
      },
    };
  }

  global.UI = {
    toast: toast,
    openModal: openModal,
    closeModal: closeModal,
    closeAllModals: closeAllModals,
    isModalSticky: isModalSticky,
    buildDetailTable: buildDetailTable,
    openDetail: openDetail,
    popOutTable: popOutTable,
    copyText: copyText,
    exportCSV: exportCSV,
    drillEvents: drillEvents,
    enableDragScroll: enableDragScroll,
  };
})(window);

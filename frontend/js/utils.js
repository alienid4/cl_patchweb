/* ============================================================
 * js/utils.js
 * 共用工具函式：日期解析、天數計算、格式化、DOM 輔助。
 * ============================================================ */
(function (global) {
  'use strict';

  /* 去掉時分秒，只留日期(避免同日因時間差被判為逾期) */
  function stripTime(d) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }

  /* 取「今天」(去時分秒) */
  function today() {
    return stripTime(new Date());
  }

  /* 由年月日建 Date；不合法日期(如 2025/2/31)回 null 而非靜默滾到下個月 */
  function mkDate(y, m, d) {
    var dt = new Date(y, m - 1, d);
    if (dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== d) return null;
    return dt;
  }

  /* --------------------------------------------------------
   * 強韌的日期解析。可吃：
   *   - JS Date 物件(SheetJS cellDates 產生)
   *   - Excel 序號(數字)
   *   - 字串 "2025/4/12" "2025-04-12" "2025.4.12" "2025年4月12日"
   * 解析失敗回傳 null(不丟例外)。
   * -------------------------------------------------------- */
  function parseDate(v) {
    if (v === null || v === undefined || v === '') return null;

    if (v instanceof Date) {
      return isNaN(v.getTime()) ? null : stripTime(v);
    }

    if (typeof v === 'number' && isFinite(v)) {
      // Excel 序號起點 1899/12/30。只取整數日，小數(時分秒)一律捨去：
      // 原本用 UTC 毫秒換算再以本地時區取年月日，UTC+8 下「16:00 之後」會被算成隔天
      var days = Math.floor(v);
      var ms = (days - 25569) * 86400 * 1000;
      var d = new Date(ms);
      if (isNaN(d.getTime())) return null;
      return new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    }

    var s = String(v).trim();
    if (!s) return null;

    // 中文年月日（同樣支援民國年：<1911 視為民國，+1911）
    var cm = s.match(/^(\d{3,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})/);
    if (cm) {
      var cy = +cm[1];
      if (cy < 1911) cy += 1911;
      return mkDate(cy, +cm[2], +cm[3]);
    }

    // yyyy/m/d、yyyy-m-d、yyyy.m.d
    var m = s.match(/^(\d{3,4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})/);
    if (m) {
      var y = +m[1];
      // 支援民國年(<1911 視為民國，+1911)
      if (y < 1911) y += 1911;
      return mkDate(y, +m[2], +m[3]);
    }

    var d2 = new Date(s);
    return isNaN(d2.getTime()) ? null : stripTime(d2);
  }

  /* [CL_WebVuln 加 V2.108] 期限欄專用：格子是「文字」且裡面有一個以上 yyyy/m/d（或 yyyy-m-d）時，
   * 取最早的那一天（保守：寧可早到期也不晚到期）。例：「DMZ 2026/9/27\nintranet 2027/2/27」→ 2026/9/27、
   * 「DMZ 2026/10/27」→ 2026/10/27。Date／Excel 序號／找不到日期的文字 → 照原本 parseDate。
   * 民國年規則同 parseDate（<1911 視為民國）。不合法日期（2025/2/31）略過不採。 */
  // 分隔符前後一致（2026/9/27、2026-9-27、2026.9.27），不吃 2026/9-27 這種混用（GPT 檢核 #3 #4）
  var DATE_SCAN_RE = /(^|[^\d])(\d{3,4})([\/\-.])(\d{1,2})\3(\d{1,2})(?!\d)/g;
  function parseDateEarliest(v) {
    if (typeof v !== 'string') return parseDate(v);
    var s = v, m, best = null, seen = false;
    DATE_SCAN_RE.lastIndex = 0;
    while ((m = DATE_SCAN_RE.exec(s)) !== null) {
      seen = true;
      var y = +m[2];
      if (y < 1911) y += 1911;
      var d = mkDate(y, +m[4], +m[5]);
      if (d && (!best || d.getTime() < best.getTime())) best = d;
    }
    if (best) return best;
    // 有日期樣式但全不合法（如 2025/2/31）→ 當讀不出來（進缺期限待確認），不交給寬鬆解析猜成別天
    return seen ? null : parseDate(v);
  }

  /* [CL_WebVuln 加 V2.108] 備註裡的例外到期日：表沒有「例外核准期限」欄（如 9-Security Header），
   * 備註寫「#25102249243例外申請於2026/6/30修補 / #26062617179 申請例外於2026/12/31修」。
   * 把備註依換行／#／分號切段，只看含「例外」的段落，取裡面「最晚」的日期（最新一次核准的期限）。
   * 只在「已申請例外、但例外核准期限欄空白」時使用。 */
  function remarkExcDate(remark) {
    var segs = String(remark || '').split(/[\n\r#;；]+/), best = null;
    for (var i = 0; i < segs.length; i++) {
      if (segs[i].indexOf('例外') < 0) continue;
      var m; DATE_SCAN_RE.lastIndex = 0;
      while ((m = DATE_SCAN_RE.exec(segs[i])) !== null) {
        var y = +m[2]; if (y < 1911) y += 1911;
        var d = mkDate(y, +m[4], +m[5]);
        if (d && (!best || d.getTime() > best.getTime())) best = d;
      }
    }
    return best;
  }

  /* [CL_WebVuln 加 V2.108] 單號欄判定（與後端 logic._ticket_filled 同規則）：
   * 欄名同時含 word（「例外」或「展延」）與「單號」的欄有填＝已申請。
   * 例：10-外部威脅情資的「例外單號」「弱點修補展延單號」。只填佔位字（-、無、N/A…）不算。 */
  var TICKET_BLANK = { '': 1, '-': 1, '－': 1, '—': 1, '無': 1, 'n/a': 1, 'na': 1, '/': 1, 'none': 1 };
  function ticketApplied(row, word) {
    if (!row || typeof row !== 'object') return false;
    var ks = Object.keys(row);
    for (var i = 0; i < ks.length; i++) {
      var k = String(ks[i] || '');
      if (k.indexOf(word) >= 0 && k.indexOf('單號') >= 0) {
        var v = row[ks[i]];
        var s = (v === null || v === undefined) ? '' : String(v).trim();
        if (!Object.prototype.hasOwnProperty.call(TICKET_BLANK, s.toLowerCase())) return true;   // 不吃到 'constructor' 之類原型屬性
      }
    }
    return false;
  }

  /* 兩日期相差天數 (b - a)，以毫秒換算避免夏令時誤差 */
  function daysBetween(a, b) {
    if (!a || !b) return null;
    return Math.round((b.getTime() - a.getTime()) / 86400000);
  }

  /* 距今天數 (dueDate - today)。負數=已逾期 */
  function daysFromToday(due) {
    if (!due) return null;
    return daysBetween(today(), due);
  }

  /* 日期格式化 yyyy/mm/dd；無值回傳 '-' */
  function fmtDate(d) {
    if (!d) return '-';
    var mm = String(d.getMonth() + 1).padStart(2, '0');
    var dd = String(d.getDate()).padStart(2, '0');
    return d.getFullYear() + '/' + mm + '/' + dd;
  }

  /* 特殊字元一律以碼位建構，避免原始碼中出現肉眼看不見的字面字元
   * （那種寫法只要檔案被重新編碼就可能靜默損壞） */
  var CH = String.fromCharCode;
  var ZERO_WIDTH_RE = new RegExp('[' + CH(0x200B) + '-' + CH(0x200D) + CH(0xFEFF) + ']', 'g'); // ZWSP/ZWNJ/ZWJ/BOM
  var IDEO_SPACE_RE = new RegExp(CH(0x3000), 'g');                                             // 全形空白
  var FULLWIDTH_RE  = new RegExp('[' + CH(0xFF01) + '-' + CH(0xFF5E) + ']', 'g');              // 全形英數符號

  /* 正規化字串：去零寬字元 + 全形空白轉半形 + trim
   * 零寬字元常隨系統匯出夾帶，肉眼看不到卻會讓欄位比對失敗 */
  function normStr(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(ZERO_WIDTH_RE, '').replace(IDEO_SPACE_RE, ' ').trim();
  }

  /* 全形英數/符號 → 半形（U+FF01–FF5E 減 0xFEE0） */
  function toHalfWidth(s) {
    return s.replace(FULLWIDTH_RE, function (ch) {
      return CH(ch.charCodeAt(0) - 0xFEE0);
    });
  }

  /* 正規化用於比對的鍵：去空白、全形轉半形、小寫
   * 「Ｐｌｕｇｉｎ　ＩＤ」這種全形表頭原本比對不到，會讓整張表欄位對不上 */
  function normKey(v) {
    return toHalfWidth(normStr(v)).replace(/\s+/g, '').toLowerCase();
  }

  /* HTML escape，避免 Excel 內容含 <> 破版或 XSS */
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* 千分位 */
  function num(n) {
    if (n === null || n === undefined || isNaN(n)) return '0';
    return Number(n).toLocaleString('en-US');
  }

  /* 簡易 DOM 建立 */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        // null/undefined 一律不設屬性：布林屬性(disabled/checked…)若 setAttribute(k,null)
        // 會被字串化成 "null" 而生效，造成「本該啟用卻停用」
        if (attrs[k] === null || attrs[k] === undefined) return;
        if (k === 'class') node.className = attrs[k];
        else if (k === 'html') node.innerHTML = attrs[k];
        else if (k === 'text') node.textContent = attrs[k];
        else if (k.slice(0, 2) === 'on' && typeof attrs[k] === 'function') {
          node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
        } else if (k === 'dataset') {
          Object.keys(attrs[k]).forEach(function (d) { node.dataset[d] = attrs[k][d]; });
        } else {
          node.setAttribute(k, attrs[k]);
        }
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  // [CL_WebVuln 加] 備註申請紀錄判定(C 折衷，與後端 logic.py 同規則)。
  // 展延/例外的日期要「備註有對應申請紀錄」才算；備註空＝沒申請。
  function _hasAction(s) {
    s = s || '';
    return s.indexOf('申請') >= 0 || s.indexOf('管理') >= 0 || s.indexOf('簽核') >= 0 ||
           s.indexOf('核准') >= 0 || s.toLowerCase().indexOf('iform') >= 0;
  }
  function appliedExc(remark) {
    var s = remark || '';
    return s.indexOf('例外') >= 0 && _hasAction(s) && s.indexOf('排除') < 0 && s.indexOf('解除') < 0;
  }
  function appliedExt(remark) {
    var s = remark || '';
    return s.indexOf('展延') >= 0 && (_hasAction(s) || s.indexOf('已展延') >= 0);
  }

  global.Utils = {
    stripTime: stripTime,
    today: today,
    parseDate: parseDate,
    parseDateEarliest: parseDateEarliest,
    remarkExcDate: remarkExcDate,
    ticketApplied: ticketApplied,
    daysBetween: daysBetween,
    daysFromToday: daysFromToday,
    fmtDate: fmtDate,
    normStr: normStr,
    normKey: normKey,
    esc: esc,
    num: num,
    el: el,
    appliedExc: appliedExc,
    appliedExt: appliedExt,
  };
})(window);

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

  global.Utils = {
    stripTime: stripTime,
    today: today,
    parseDate: parseDate,
    daysBetween: daysBetween,
    daysFromToday: daysFromToday,
    fmtDate: fmtDate,
    normStr: normStr,
    normKey: normKey,
    esc: esc,
    num: num,
    el: el,
  };
})(window);

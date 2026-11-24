/* ============================================================
 * js/features.js
 * 功能開關（模組化 feature toggle）。
 *   layer A：config/features.js 的出廠預設（隨資料夾發布）
 *   layer B：個人覆寫，存 localStorage（vulnDashboard.features）
 * isOn(id) = 個人有覆寫就用覆寫，否則用出廠預設。
 * 「其他功能 → 功能開關」開設定面板；勾選即存、即時重繪。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var KEY = 'vulnDashboard.features';

  function registry() { return global.APP_FEATURES || []; }
  function find(id) { var r = registry(); for (var i = 0; i < r.length; i++) if (r[i].id === id) return r[i]; return null; }
  /* 型別守門：合法 JSON 但不是物件(例如陣列或字串)會讓 hasOwnProperty 判定失準 */
  function overrides() {
    try {
      var v = JSON.parse(localStorage.getItem(KEY));
      return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
    } catch (e) { return {}; }
  }
  function saveOverrides(o) {
    try { localStorage.setItem(KEY, JSON.stringify(o)); return true; }
    catch (e) {
      if (global.UI && global.UI.toast) global.UI.toast('本機儲存空間不足，功能開關未能保存', 'error');
      return false;
    }
  }

  /* 開關判定：個人覆寫優先，否則出廠預設（未登錄的 id 一律視為開） */
  function isOn(id) {
    var o = overrides();
    if (Object.prototype.hasOwnProperty.call(o, id)) return !!o[id];
    var f = find(id);
    return f ? f.default !== false : true;
  }
  function set(id, on) { var o = overrides(); o[id] = !!on; saveOverrides(o); }
  function reset() { try { localStorage.removeItem(KEY); } catch (e) {} }

  /* -------- 設定面板 -------- */
  function openSettings(onChange) {
    var UI = global.UI;
    var footer = U.el('div', { class: 'reminder-actions' }, [
      U.el('button', { class: 'btn btn-secondary', text: '還原預設', onclick: function () {
        reset(); if (onChange) onChange(); openSettings(onChange);   // 重開刷新勾選
      } }),
      U.el('button', { class: 'btn btn-primary', text: '完成', onclick: function () { UI.closeModal(); } }),
    ]);
    UI.openModal('功能開關', buildContent(onChange), { footer: footer });
  }

  function buildContent(onChange) {
    var wrap = U.el('div', { class: 'feature-settings' });
    var order = [], byGroup = {};
    registry().forEach(function (f) {
      if (!byGroup[f.group]) { byGroup[f.group] = []; order.push(f.group); }
      byGroup[f.group].push(f);
    });
    if (!order.length) {
      wrap.appendChild(U.el('p', { class: 'empty-hint', text: '尚無可設定的功能。' }));
      return wrap;
    }
    order.forEach(function (gname) {
      wrap.appendChild(U.el('div', { class: 'feature-group-title', text: gname }));
      byGroup[gname].forEach(function (f) {
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = isOn(f.id);
        cb.addEventListener('change', function () { set(f.id, cb.checked); if (onChange) onChange(); });
        var row = U.el('label', { class: 'feature-row' });
        row.appendChild(cb);
        row.appendChild(U.el('span', { class: 'feature-name', text: f.label }));
        wrap.appendChild(row);
      });
    });
    return wrap;
  }

  global.Features = { isOn: isOn, set: set, reset: reset, all: registry, openSettings: openSettings };
})(window);

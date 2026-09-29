/* ============================================================
 * autoimport.js — 開 app 時自動抓「來源資料夾最新報告」匯入
 *
 * 為什麼要小幫手：純前端網頁（file://）讀不到 \\伺服器\分享 這種 UNC 路徑，
 * 由本機小幫手代讀（GET /latest-report，帶權杖）。來源路徑存本機 localStorage，
 * 不寫死內網資訊（公開 repo）。任何失敗一律安靜退回手動匯入，不擋、不嚇人。
 * ============================================================ */
(function (global) {
  'use strict';
  var U = global.Utils, UI = global.UI;

  var CFG_KEY  = 'vulnDashboard.autoImport';        // { enabled, dir, pattern }
  var LAST_KEY = 'vulnDashboard.autoImport.last';   // 上次自動匯入的「檔名|修改時間」，避免同檔重覆沖畫面
  var AGENT_PORTS = [8899, 8900, 8901, 8902, 8903, 8904];
  // 出廠預設「留空」：不預填任何內網路徑，使用者自行在設定畫面填（存本機）
  var DEFAULTS = { enabled: true, dir: '', pattern: '' };

  function loadCfg() {
    try {
      var c = JSON.parse(localStorage.getItem(CFG_KEY)) || {};
      return { enabled: c.enabled !== false, dir: c.dir || '', pattern: c.pattern || '' };
    } catch (e) { return { enabled: DEFAULTS.enabled, dir: '', pattern: '' }; }
  }
  function saveCfg(c) { try { localStorage.setItem(CFG_KEY, JSON.stringify(c)); return true; } catch (e) { return false; } }
  function token() { try { return ((global.EmailCfg && global.EmailCfg.load().agentToken) || '').trim(); } catch (e) { return ''; } }

  /* 探測小幫手實際在哪個埠（沿用 email 的候選埠與驗證方式） */
  function probe() {
    var i = 0;
    function next() {
      if (i >= AGENT_PORTS.length) return Promise.reject(new Error('小幫手未啟動'));
      var base = 'http://localhost:' + AGENT_PORTS[i++];
      return fetch(base + '/health').then(function (r) { return r.json(); })
        .then(function (j) { if (j && j.ok && j.agent === 'mail-agent') return base; return next(); })
        .catch(function () { return next(); });
    }
    return next();
  }

  function b64ToArrayBuffer(b64) {
    var bin = atob(b64), len = bin.length, bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  /* 開 app 時呼叫。沒設定或小幫手沒開 → 直接不動作（行為同今天，退回手動）。 */
  function run(opts) {
    opts = opts || {};
    var c = loadCfg();
    if (!c.enabled || !c.dir) return;          // 沒開或沒設來源 → 不動作
    var tok = token();
    if (!tok) { if (opts.manual) UI.toast('尚未設定小幫手權杖（Email 設定）', 'error'); return; }

    probe().then(function (base) {
      var url = base + '/latest-report?dir=' + encodeURIComponent(c.dir) +
                '&pattern=' + encodeURIComponent(c.pattern || '');
      return fetch(url, { headers: { 'X-Agent-Token': tok } }).then(function (r) { return r.json(); });
    }).then(function (j) {
      if (!j || !j.ok) {
        if (opts.manual) UI.toast('自動匯入失敗：' + ((j && j.error) || '未知錯誤'), 'error');
        return;                                 // 讀不到 → 安靜略過（手動仍可）
      }
      var tag = j.name + '|' + j.modified, last = '';
      try { last = localStorage.getItem(LAST_KEY) || ''; } catch (e) {}
      if (tag === last && !opts.manual) return; // 跟上次同一個檔 → 不重覆沖掉當下畫面
      if (!(global.App && global.App.importArrayBuffer)) return;
      var ok = global.App.importArrayBuffer(b64ToArrayBuffer(j.contentB64), j.name,
        { source: 'auto', modified: j.modified, dir: c.dir });
      if (ok) { try { localStorage.setItem(LAST_KEY, tag); } catch (e) {} }
    }).catch(function () {
      if (opts.manual) UI.toast('連不到小幫手，請先啟動（自動匯入需要它代讀資料夾）', 'error');
      // 非手動：安靜退回手動匯入
    });
  }

  /* 其他功能 → 自動匯入設定 */
  function openSettings() {
    var c = loadCfg();
    var iEnabled = U.el('input', { type: 'checkbox' }); iEnabled.checked = !!c.enabled;
    var iDir = U.el('input', { type: 'text', class: 'email-input',
      value: c.dir, placeholder: '\\\\伺服器\\分享\\資料夾（本機設定，不上傳）' });
    var iPat = U.el('input', { type: 'text', class: 'email-input',
      value: c.pattern, placeholder: '例：弱點彙總報告_*.xlsx（留空＝任何 .xlsx）' });

    function field(label, node) {
      return U.el('label', { class: 'email-field' }, [
        U.el('span', { class: 'email-field-label', text: label }), node,
      ]);
    }
    var body = U.el('div', { class: 'email-form' }, [
      U.el('div', { class: 'email-field' }, [
        U.el('label', { class: 'email-check' }, [iEnabled,
          U.el('span', { text: '開啟時自動抓最新（關閉＝只手動匯入）' })]),
      ]),
      field('來源資料夾', iDir),
      field('檔名樣式', iPat),
      U.el('p', { class: 'email-field-label',
        text: '「最新」以檔名內 8 碼日期(YYYYMMDD)最大者為準，無日期則用檔案修改時間。設定只存在本機，來源換位置時改這裡即可。需本機小幫手在跑、且此電腦能讀該資料夾。' }),
    ]);

    function collect() { return { enabled: iEnabled.checked, dir: iDir.value.trim(), pattern: iPat.value.trim() }; }
    var save = U.el('button', { class: 'btn btn-primary btn-sm', text: '儲存設定',
      onclick: function () { saveCfg(collect()); UI.toast('自動匯入設定已儲存', 'success'); UI.closeModal(); } });
    var now = U.el('button', { class: 'btn btn-secondary btn-sm', text: '立即抓一次',
      onclick: function () {
        saveCfg(collect());
        try { localStorage.removeItem(LAST_KEY); } catch (e) {}   // 清掉去重記號，強制重抓
        UI.closeModal(); run({ manual: true });
      } });
    // sticky：填了路徑誤點旁邊不該整個關掉（沿用 V1.73 規則）
    UI.openModal('自動匯入設定', body, { footer: U.el('div', { class: 'reminder-actions' }, [save, now]), sticky: true });
  }

  global.AutoImport = { run: run, openSettings: openSettings, loadCfg: loadCfg };
})(window);

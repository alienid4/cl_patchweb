/* ============================================================
 * js/email.js
 * Email 設定（其他功能 → Email 設定）— 個人套個人的 SMTP 設定，存本機。
 * 公司為「免認證內部 relay」，故不收密碼，只存寄件人/收件人/主機等非機密設定。
 * 網頁本身無法直接走 SMTP 寄信（瀏覽器限制）；本模組負責「設定＋產生寄送任務」，
 * 實際寄送由本機 PowerShell 腳本讀取匯出的 mail-task.json 執行。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var UI = global.UI;
  var STORAGE_KEY = 'vulnDashboard.email';

  var DEFAULTS = {
    smtpHost: '',
    smtpPort: 25,
    from: '',
    cc: '',
    ccSelf: true,          // 每封都副本給發信人自己
    fallbackTo: '',        // 查無 email/離職者 → 轉寄給（主管/窗口）
    subjectPrefix: '【弱點修補提醒】',
    scopeOverdue: true,
    scopeSoon: false,
    agentToken: '',        // 小幫手存取權杖（見 agent_token.txt；防其他網頁盜用小幫手發信）
  };

  function loadCfg() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return Object.assign({}, DEFAULTS);
      return Object.assign({}, DEFAULTS, JSON.parse(raw));
    } catch (e) { return Object.assign({}, DEFAULTS); }
  }
  function saveCfg(cfg) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg)); return true; }
    catch (e) { return false; }
  }

  /* 記住上次勾選的催辦人選（跨檔沿用）；null = 從未選過 → 預設全勾 */
  var SEL_KEY = 'vulnDashboard.emailSel';
  function loadSel() {
    try {
      var raw = localStorage.getItem(SEL_KEY);
      if (!raw) return null;
      var v = JSON.parse(raw);
      return Array.isArray(v) ? v : null;      // 型別不對就當作沒選過，不要讓後續操作丟例外
    } catch (e) { return null; }
  }
  function saveSel(names) {
    try { localStorage.setItem(SEL_KEY, JSON.stringify(names || [])); } catch (e) {}
  }

  /* 發信紀錄（存本機，最多留 500 筆） */
  var LOG_KEY = 'vulnDashboard.emailLog';
  function loadLog() {
    try {
      var v = JSON.parse(localStorage.getItem(LOG_KEY));
      return Array.isArray(v) ? v : [];        // 型別不對時回空陣列，否則 concat 會丟例外
    } catch (e) { return []; }
  }
  /* 寫入失敗原本靜默：使用者會看到舊紀錄而誤以為信沒寄出，進而重寄 */
  function appendLog(entries) {
    var log = loadLog().concat(entries || []);
    if (log.length > 500) log = log.slice(log.length - 500);
    try { localStorage.setItem(LOG_KEY, JSON.stringify(log)); return true; }
    catch (e) {
      UI.toast('信已寄出，但發信紀錄未能寫入本機（儲存空間不足）。請改看小幫手的 mail_log.csv', 'error');
      return false;
    }
  }
  function nowStamp() {
    var d = new Date(); function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function statusLabel(mode) { return mode === 'ad' ? '寄出' : mode === 'fallback' ? '轉主管' : mode === 'skip' ? '跳過' : '失敗'; }

  /* 收件人字串 → 陣列（換行/逗號/分號分隔、去空白去重） */
  function parseList(s) {
    var seen = {}, out = [];
    String(s || '').split(/[\n,;，；]+/).forEach(function (x) {
      var t = x.trim();
      if (t && !seen[t]) { seen[t] = 1; out.push(t); }
    });
    return out;
  }

  /* 取目前資料的「要通知」清單：逾期未結（可含近期到期） */
  function scopedRecords(cfg) {
    var st = (global.App && global.App.getState) ? global.App.getState() : null;
    var sheets = (st && st.sheets) ? st.sheets : [];
    var dept = (st && st.myDept) ? st.myDept : '__all__';
    function inDept(r) { return dept === '__all__' || (r.unit || '(未填)') === dept; }
    var out = [];
    if (cfg.scopeOverdue && global.Summary && global.Summary.collectOverdue) {
      out = global.Summary.collectOverdue(sheets, dept).slice();
    }
    if (cfg.scopeSoon) {
      var soon = (global.APP_CONFIG && global.APP_CONFIG.soonDays) || 30;
      sheets.forEach(function (s) {
        (s.records || []).forEach(function (r) {
          if (r.closeBucket === 'open' && r.realDue && r.daysLeft >= 0 && r.daysLeft <= soon && inDept(r)) out.push(r);
        });
      });
    }
    out.sort(function (a, b) { return (b.overdueDays || 0) - (a.overdueDays || 0); });
    return out;
  }

  /* 產生信件主旨與內文（純事實，不加多餘說明） */
  function buildContent(cfg) {
    var recs = scopedRecords(cfg);
    var today = U.fmtDate(U.today());
    var subject = (cfg.subjectPrefix || '') + '弱點待處理 ' + recs.length + ' 筆（' + today + '）';
    var lines = [];
    lines.push('弱點待處理清單（' + today + '）：');
    lines.push('');
    recs.forEach(function (r) {
      var od = (r.realDue && r.daysLeft < 0) ? ('逾期 ' + r.overdueDays + ' 天')
             : (r.realDue ? ('剩 ' + r.daysLeft + ' 天') : '無到期日');
      lines.push('· [' + r.sheet + '] ' + (r.unit || '(未填)') + ' / ' + r.owner +
                 '｜' + r.name + '｜' + r.severity + '｜到期 ' + U.fmtDate(r.realDue) + '（' + od + '）');
    });
    if (!recs.length) lines.push('（目前無符合條件的項目）');
    return { subject: subject, body: lines.join('\n'), count: recs.length };
  }

  /* 依負責人分組，每人一封催辦（B：各負責人各別催辦；email 由腳本查 AD 補） */
  function buildBatch(cfg) {
    var recs = scopedRecords(cfg);
    var byOwner = {};
    recs.forEach(function (r) { var o = r.owner || '(未指定)'; (byOwner[o] = byOwner[o] || []).push(r); });
    var today = U.fmtDate(U.today());
    var owners = Object.keys(byOwner).sort(function (a, b) { return byOwner[b].length - byOwner[a].length; });
    return owners.map(function (o) {
      var list = byOwner[o];
      var subject = (cfg.subjectPrefix || '') + o + ' 弱點待處理 ' + list.length + ' 筆（' + today + '）';
      var lines = ['以下弱點待您處理（' + today + '）：', ''];
      var deptTally = {};
      list.forEach(function (r) {
        var od = (r.realDue && r.daysLeft < 0) ? ('逾期 ' + r.overdueDays + ' 天')
               : (r.realDue ? ('剩 ' + r.daysLeft + ' 天') : '無到期日');
        lines.push('· [' + r.sheet + '] ' + (r.unit || '(未填)') +
                   '｜' + r.name + '｜' + r.severity + '｜到期 ' + U.fmtDate(r.realDue) + '（' + od + '）');
        var u = r.unit || '';
        if (u) deptTally[u] = (deptTally[u] || 0) + 1;   // 該負責人的部門（供小幫手查「部門主管」副本）
      });
      // 跨部門時取最常出現的那個部門
      var dept = Object.keys(deptTally).sort(function (a, b) { return deptTally[b] - deptTally[a]; })[0] || '';
      return { owner: o, count: list.length, dept: dept, subject: subject, body: lines.join('\n') };
    });
  }

  /* 本機小幫手位址（背景常駐、只聽 localhost） */
  /* 小幫手位址：8899 可能被其他程式占用，小幫手會自動往後找埠，
   * 這裡依序探測同一組候選埠，並驗證回應方確實是本服務（避免誤連別的程式）。 */
  var AGENT_PORTS = [8899, 8900, 8901, 8902, 8903, 8904];
  var AGENT = 'http://localhost:' + AGENT_PORTS[0];   // 預設值；探測成功後更新

  function probeAgent() {
    var i = 0;
    function next() {
      if (i >= AGENT_PORTS.length) return Promise.reject(new TypeError('agent not found'));
      var base = 'http://localhost:' + AGENT_PORTS[i++];
      return fetch(base + '/health')
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j && j.ok && j.agent === 'mail-agent') { AGENT = base; return j; }
          return next();                                  // 有回應但不是本服務 → 換下一個
        })
        .catch(function () { return next(); });
    }
    return next();
  }

  /* 組成寄送批次 payload（匯出檔 / 送小幫手共用） */
  function buildPayload(c, selected) {
    var cc = parseList(c.cc);
    if (c.ccSelf && c.from && cc.indexOf(c.from) < 0) cc.push(c.from);   // 副本給自己（寄件人）
    return {
      generatedAt: new Date().toISOString(),
      smtp: { host: c.smtpHost, port: c.smtpPort, auth: false },
      from: c.from,
      cc: cc,
      fallbackTo: parseList(c.fallbackTo),
      subjectPrefix: c.subjectPrefix,
      owners: selected,
    };
  }

  /* 下載 JSON 檔 */
  function downloadJSON(obj, fileName) {
    var blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = U.el('a', { href: url, download: fileName });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  /* ---- 設定表單 ---- */
  function field(labelText, inputNode) {
    return U.el('label', { class: 'email-field' }, [
      U.el('span', { class: 'email-field-label', text: labelText }),
      inputNode,
    ]);
  }

  function buildForm(cfg) {
    var iHost = U.el('input', { type: 'text', class: 'email-input', value: cfg.smtpHost, placeholder: '公司 SMTP relay 主機（本機設定，不上傳）' });
    var iPort = U.el('input', { type: 'number', class: 'email-input email-input-sm', value: String(cfg.smtpPort), min: '1' });
    var iFrom = U.el('input', { type: 'text', class: 'email-input', value: cfg.from, placeholder: '你的寄件信箱' });
    var iCc = U.el('textarea', { class: 'email-input email-area', rows: '2', placeholder: '每封都副本給（選填，如主管）' });
    iCc.value = cfg.cc;
    var iFallback = U.el('input', { type: 'text', class: 'email-input', value: cfg.fallbackTo || '', placeholder: '查無 email/離職者轉寄給（主管/窗口）' });
    var iSubj = U.el('input', { type: 'text', class: 'email-input', value: cfg.subjectPrefix });
    var cOverdue = U.el('input', { type: 'checkbox' }); cOverdue.checked = !!cfg.scopeOverdue;
    var cSoon = U.el('input', { type: 'checkbox' }); cSoon.checked = !!cfg.scopeSoon;
    var cSelf = U.el('input', { type: 'checkbox' }); cSelf.checked = !!cfg.ccSelf;
    var iToken = U.el('input', { type: 'text', class: 'email-input', value: cfg.agentToken || '',
      placeholder: '貼上小幫手資料夾內 agent_token.txt 的內容' });

    var form = U.el('div', { class: 'email-form' }, [
      field('SMTP 主機', iHost),
      field('埠', iPort),
      field('寄件人', iFrom),
      field('副本', iCc),
      U.el('div', { class: 'email-field' }, [
        U.el('label', { class: 'email-check' }, [cSelf, U.el('span', { text: '副本給自己（寄件人）' })]),
      ]),
      field('查無 email 轉寄', iFallback),
      field('主旨前綴', iSubj),
      field('小幫手權杖', iToken),
      U.el('div', { class: 'email-field' }, [
        U.el('span', { class: 'email-field-label', text: '寄送範圍' }),
        U.el('div', { class: 'email-scope' }, [
          U.el('label', { class: 'email-check' }, [cOverdue, U.el('span', { text: '逾期未結' })]),
          U.el('label', { class: 'email-check' }, [cSoon, U.el('span', { text: '近期到期' })]),
        ]),
      ]),
    ]);

    function read() {
      return {
        smtpHost: iHost.value.trim(),
        smtpPort: parseInt(iPort.value, 10) || 25,
        from: iFrom.value.trim(),
        cc: iCc.value,
        ccSelf: cSelf.checked,
        fallbackTo: iFallback.value,
        subjectPrefix: iSubj.value,
        scopeOverdue: cOverdue.checked,
        scopeSoon: cSoon.checked,
        agentToken: iToken.value.trim(),
      };
    }
    return { node: form, read: read };
  }

  function openSettings() {
    var cfg = loadCfg();
    var f = buildForm(cfg);
    var listBox = U.el('div', { class: 'batch-list hidden' });
    var body = U.el('div', {}, [f.node, listBox]);
    var listState = { batch: [], checks: {} };
    var sending = false;                        // 寄送中旗標，擋重複點擊

    function doSave() {
      var c = f.read();
      if (saveCfg(c)) UI.toast('Email 設定已儲存', 'success');
      else UI.toast('儲存失敗（瀏覽器空間不足）', 'error');
    }

    function setAll(v) { Object.keys(listState.checks).forEach(function (k) { listState.checks[k].checked = v; }); }

    /* 列出催辦名單（可勾選）：測試時只勾一個人即可 */
    function doList() {
      var c = f.read();
      listState.batch = buildBatch(c);
      listState.checks = {};
      listBox.innerHTML = '';
      listBox.classList.remove('hidden');
      if (!listState.batch.length) {
        listBox.appendChild(U.el('p', { class: 'empty-hint', text: '目前無符合條件的負責人。' }));
        return;
      }
      listBox.appendChild(U.el('div', { class: 'batch-tools' }, [
        U.el('span', { class: 'batch-tools-label', text: '勾選要催辦的人（' + listState.batch.length + ' 位）' }),
        U.el('button', { class: 'btn btn-secondary btn-sm', text: '全選', onclick: function () { setAll(true); } }),
        U.el('button', { class: 'btn btn-secondary btn-sm', text: '全不選', onclick: function () { setAll(false); } }),
      ]));
      var savedSel = loadSel();   // 有記憶就套用上次選擇；沒有則預設全勾
      listState.batch.forEach(function (b) {
        var cb = U.el('input', { type: 'checkbox' });
        cb.checked = savedSel ? (savedSel.indexOf(b.owner) >= 0) : true;
        listState.checks[b.owner] = cb;
        listBox.appendChild(U.el('label', { class: 'batch-row' }, [
          cb,
          U.el('span', { class: 'batch-owner', text: b.owner }),
          U.el('span', { class: 'batch-count', text: b.count + ' 筆' }),
        ]));
      });
    }

    /* 取目前設定＋勾選的人（驗證＋記住選擇）；回 {c, selected} 或 null */
    function getSelected() {
      var c = f.read(); saveCfg(c);
      if (!c.smtpHost || !c.from) { UI.toast('請先填 SMTP 主機、寄件人', 'error'); return null; }
      if (!listState.batch.length) { UI.toast('請先按「列出催辦名單」', 'error'); return null; }
      var selected = listState.batch.filter(function (b) { var cb = listState.checks[b.owner]; return cb && cb.checked; });
      if (!selected.length) { UI.toast('請至少勾選一位', 'error'); return null; }
      saveSel(selected.map(function (b) { return b.owner; }));   // 記住這次選擇
      return { c: c, selected: selected };
    }

    /* 備用：匯出批次檔（給 send.bat 用；不透過小幫手） */
    function doExportSelected() {
      var g = getSelected(); if (!g) return;
      downloadJSON(buildPayload(g.c, g.selected), 'mail-batch.json');
      UI.toast('已匯出 mail-batch.json（勾選 ' + g.selected.length + ' 位）', 'success');
    }

    /* 測試本機小幫手是否在跑 */
    function doTestAgent() {
      // probeAgent 已驗證回應方確為 mail-agent，並記住實際使用的埠
      probeAgent()
        .then(function (j) {
          var hasTok = !!(loadCfg().agentToken || '').trim();
          var port = AGENT.replace(/^.*:/, '');
          var portNote = (port === String(AGENT_PORTS[0])) ? '' : '（埠 ' + port + '）';
          if (j.needToken && !hasTok) {
            UI.toast('小幫手已連線 ' + (j.version || '') + portNote + '，但尚未設定權杖：請貼上 agent_token.txt 的內容', 'error');
          } else {
            UI.toast('本機小幫手已連線 ✓　' + (j.version || '') + portNote, 'success');
          }
        })
        .catch(function () { UI.toast('連不到小幫手，請先執行 install_agent.bat', 'error'); });
    }

    /* 發信紀錄（本機）：逐筆結果，失敗標紅，可匯出 CSV */
    function doViewLog() {
      var log = loadLog();
      var wrap = U.el('div', {});
      if (!log.length) {
        wrap.appendChild(U.el('p', { class: 'empty-hint', text: '尚無發信紀錄。' }));
      } else {
        var cols = ['時間', '負責人', '收件人', '副本', '狀態', '錯誤'];
        var keys = ['time', 'owner', 'to', 'cc', 'status', 'error'];
        var table = U.el('table', { class: 'tracking-table' });
        var thead = U.el('thead'), htr = U.el('tr');
        cols.forEach(function (h) { htr.appendChild(U.el('th', { text: h })); });
        thead.appendChild(htr); table.appendChild(thead);
        var tbody = U.el('tbody');
        log.slice().reverse().forEach(function (r) {
          var tr = U.el('tr', { class: (r.status === '失敗' ? 'row-overdue' : '') });
          keys.forEach(function (k) { tr.appendChild(U.el('td', { text: r[k] || '' })); });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrap.appendChild(U.el('div', { class: 'table-scroll' }, [table]));
      }
      var footer = U.el('div', { class: 'reminder-actions' }, [
        U.el('button', { class: 'btn btn-secondary', text: '匯出 CSV', disabled: log.length ? null : 'disabled', onclick: function () { exportLog(log); } }),
        U.el('button', { class: 'btn btn-secondary', text: '清空紀錄', disabled: log.length ? null : 'disabled',
          onclick: function () { try { localStorage.removeItem(LOG_KEY); } catch (e) {} UI.toast('已清空發信紀錄', 'success'); UI.closeModal(); /* 只收這一層，回到 Email 設定 */ } }),
      ]);
      // stack:true → 關閉後回到原本的 Email 設定畫面(含已勾選的催辦名單)，而不是整個關掉
      UI.openModal('發信紀錄（' + log.length + ' 筆）', wrap, { footer: footer, stack: true, sticky: true });
    }
    function exportLog(log) {
      var keys = ['time', 'owner', 'to', 'cc', 'status', 'error'];
      var rows = [['時間', '負責人', '收件人', '副本', '狀態', '錯誤']].concat(log.map(function (r) { return keys.map(function (k) { return r[k] || ''; }); }));
      var csv = rows.map(function (arr) {
        return arr.map(function (v) { var s = String(v == null ? '' : v); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }).join(',');
      }).join('\r\n');
      var blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
      var url = URL.createObjectURL(blob);
      var a = U.el('a', { href: url, download: '發信紀錄.csv' });
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(url);
      UI.toast('已匯出 發信紀錄.csv', 'success');
    }

    /* 送小幫手的標頭：帶存取權杖，避免其他網頁盜用小幫手發信 */
    function agentHeaders() {
      var h = { 'Content-Type': 'application/json' };
      var t = (loadCfg().agentToken || '').trim();
      if (t) h['X-Agent-Token'] = t;
      return h;
    }

    /* 寄出：先查 AD 出計畫 → 顯示 → 確認才寄 */
    function doSend() {
      var g = getSelected(); if (!g) return;
      var payload = buildPayload(g.c, g.selected);
      UI.toast('查 AD 解析中…', 'info');
      // 先探測小幫手實際在哪個埠（可能因占用而改用備用埠），再送出
      probeAgent()
        .then(function () {
          return fetch(AGENT + '/plan', { method: 'POST', headers: agentHeaders(), body: JSON.stringify(payload) });
        })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j || !j.ok) { UI.toast((j && j.error) || '小幫手錯誤', 'error'); return; }
          renderPlan(payload, j.owners || []);
        })
        // 區分「真的連不到」與「連到了但回應有問題」，後者叫使用者重跑 install 只會更困惑
        .catch(function (e) {
          if (e && e.name === 'TypeError') UI.toast('連不到小幫手，請先啟動（install_agent.bat）', 'error');
          else UI.toast('小幫手回應異常：' + (e && e.message ? e.message : '未知錯誤'), 'error');
        });
    }

    function renderPlan(payload, plan) {
      listBox.innerHTML = ''; listBox.classList.remove('hidden');
      var willSend = plan.filter(function (p) { return p.mode !== 'skip'; }).length;
      listBox.appendChild(U.el('div', { class: 'batch-tools' }, [
        U.el('span', { class: 'batch-tools-label', text: '寄送計畫（將寄出 ' + willSend + ' 封）' }),
      ]));
      plan.forEach(function (p) {
        // 區分「同名多筆」與「真的查無」：兩者原本都顯示「查無 email」，
        // 使用者去 AD 一查明明有這個人，會誤以為程式壞了
        var why = p.reason === 'ambiguous'
                    ? ('AD 同名多筆，請用 override.json 指定'
                       + (p.candidates && p.candidates.length ? '（' + p.candidates.join('、') + '）' : ''))
                  : p.reason === 'error' ? 'AD 查詢失敗'
                  : '查無 email';
        var label = p.mode === 'ad' ? ('→ ' + p.to)
                  : p.mode === 'fallback' ? ('→ 轉主管 ' + p.to + '（' + why + '）')
                  : why + '，跳過';
        // AD 命中時揭露「主管副本」狀態：有就顯示主管信箱，沒有就明講未填、提示補哪個部門
        if (p.mode === 'ad') {
          if (p.managerCc) { label += '　（副本主管 ' + p.managerCc + '）'; }
          else { label += '　（主管未填' + (p.dept ? '：' + p.dept : '') + '，未副本）'; }
        }
        listBox.appendChild(U.el('div', { class: 'batch-row plan-' + p.mode }, [
          U.el('span', { class: 'batch-owner', text: p.owner }),
          U.el('span', { class: 'batch-count', text: label }),
        ]));
      });
      listBox.appendChild(U.el('div', { class: 'plan-actions' }, [
        U.el('button', { class: 'btn btn-primary btn-sm', text: '確認寄出（' + willSend + '）',
          onclick: function () { doConfirmSend(payload, this); } }),
        U.el('button', { class: 'btn btn-secondary btn-sm', text: '取消',
          onclick: function () { listBox.classList.add('hidden'); } }),
      ]));
    }

    function doConfirmSend(payload, btn) {
      if (sending) return;                       // 防重複點擊：寄送中再按一次會重複寄信
      sending = true;
      if (btn) { btn.disabled = true; btn.textContent = '寄送中…'; }
      function done() {
        sending = false;
        if (btn) { btn.disabled = false; btn.textContent = '確認寄出'; }
      }
      // 逾時保護：AD 不通或 relay 卡住時，不要永遠停在沒有回應的畫面
      var ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      var timer = setTimeout(function () { if (ac) ac.abort(); }, 120000);
      UI.toast('寄送中…（請勿重複按）', 'info');
      fetch(AGENT + '/send', { method: 'POST', headers: agentHeaders(),
                               body: JSON.stringify(payload), signal: ac ? ac.signal : undefined })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j || !j.ok) { UI.toast((j && j.error) || '寄送失敗', 'error'); return; }
          var stamp = nowStamp();
          appendLog((j.details || []).map(function (d) {
            return { time: stamp, owner: d.owner, to: (d.to || ''), cc: (d.cc || ''), status: statusLabel(d.mode), error: (d.error || '') };
          }));
          listBox.innerHTML = ''; listBox.classList.remove('hidden');
          listBox.appendChild(U.el('div', { class: 'batch-tools' + (j.failed > 0 ? ' send-fail' : '') }, [
            U.el('span', { class: 'batch-tools-label',
              text: '完成：寄出 ' + j.sent + '　轉主管 ' + j.fallback + '　跳過 ' + j.skipped + '　失敗 ' + j.failed }),
          ]));
          (j.details || []).forEach(function (d) {
            var info = d.error ? ('失敗：' + d.error) : (d.to ? ('→ ' + d.to) : '');
            listBox.appendChild(U.el('div', { class: 'batch-row plan-' + d.mode }, [
              U.el('span', { class: 'batch-owner', text: d.owner }),
              U.el('span', { class: 'batch-count', text: statusLabel(d.mode) + (info ? '　' + info : '') }),
            ]));
          });
          if (j.logError) {
            UI.toast('信已寄出，但小幫手的稽核檔寫入失敗（' + j.logError + '）。若正用 Excel 開著 mail_log.csv 請先關閉', 'error');
          }
          if (j.failed > 0) UI.toast('⚠ 有 ' + j.failed + ' 封寄送失敗！點「發信紀錄」看細節', 'error');
          else UI.toast('寄出 ' + j.sent + ' 封', 'success');
        })
        .catch(function (e) {
          if (e && e.name === 'AbortError') {
            UI.toast('小幫手逾時未回應（可能仍在寄）。請先看「發信紀錄」確認，勿直接重寄', 'error');
          } else {
            UI.toast('寄送時連不到小幫手', 'error');
          }
        })
        .then(function () { clearTimeout(timer); done(); });
    }

    var useAgent = !global.Features || global.Features.isOn('email-agent');
    var footBtns = [
      U.el('button', { class: 'btn btn-primary', text: '儲存設定', onclick: doSave }),
      U.el('button', { class: 'btn btn-secondary', text: '列出催辦名單', onclick: doList }),
      U.el('button', { class: 'btn btn-secondary', text: '發信紀錄', onclick: doViewLog }),
    ];
    if (useAgent) {
      footBtns.push(U.el('button', { class: 'btn btn-primary', text: '寄出', onclick: doSend }));
      footBtns.push(U.el('button', { class: 'btn btn-secondary', text: '測試小幫手', onclick: doTestAgent }));
      footBtns.push(U.el('button', { class: 'btn btn-secondary', text: '匯出（備用）', onclick: doExportSelected }));
    } else {
      footBtns.push(U.el('button', { class: 'btn btn-primary', text: '匯出寄送檔', onclick: doExportSelected }));
    }
    // sticky：表單填到一半誤點空白處不該整組關掉（要關按右上 ✕）
    UI.openModal('Email 設定', body, { footer: U.el('div', { class: 'reminder-actions' }, footBtns), sticky: true });
  }

  function init() {
    var btn = document.getElementById('email-settings-btn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var d = document.getElementById('more-dropdown');
      if (d) d.classList.add('hidden');
      openSettings();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }

  global.EmailCfg = { load: loadCfg, buildContent: buildContent, buildBatch: buildBatch };
})(window);

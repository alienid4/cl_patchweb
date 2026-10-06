/* ============================================================
 * js/sheets.js  (ms-01 讀取 + ms-02 正規化)
 * 讀取所有「數字-」開頭工作表，依 profile 對應並正規化成統一紀錄。
 * 特例：表8 單位欄拆單位+負責人；表9 風險計數「中*4 低*2」展開成多筆。
 * 不影響現有單表流程（此檔為新增，尚未接 UI）。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var P = global.SHEET_PROFILES;

  /* ---- 欄位解析：先精確、再前綴，皆用 normKey 去空白/大小寫 ---- */
  function resolveField(normHeaders, aliases) {
    var i, h, ak;
    for (i = 0; i < aliases.length; i++) {
      ak = U.normKey(aliases[i]);
      for (h = 0; h < normHeaders.length; h++) if (normHeaders[h].k === ak) return normHeaders[h].raw;
    }
    for (i = 0; i < aliases.length; i++) {
      ak = U.normKey(aliases[i]);
      for (h = 0; h < normHeaders.length; h++) if (normHeaders[h].k.indexOf(ak) === 0) return normHeaders[h].raw;
    }
    return null;
  }

  function resolveProfile(headers) {
    var normHeaders = headers.filter(function (x) { return U.normStr(x) !== ''; })
      .map(function (x) { return { raw: x, k: U.normKey(x) }; });
    var map = {};
    Object.keys(P.fieldAliases).forEach(function (field) {
      map[field] = resolveField(normHeaders, P.fieldAliases[field]);
    });
    return map;
  }

  /* ---- 嚴重度正規化 ---- */
  function mapSeverity(raw) {
    var s = U.normStr(raw); if (!s) return 'Unknown';
    var low = s.toLowerCase();
    var keys = Object.keys(P.severityMap);
    for (var i = 0; i < keys.length; i++) {
      if (low.indexOf(keys[i]) >= 0) return P.severityMap[keys[i]];
    }
    return 'Unknown';
  }

  /* 判斷是否為「計數字串」如「中*4 低*2」「High*3」 */
  var COUNT_RE = /(嚴重|critical|高|high|中|medium|低|low|info)\s*[\*xX×]\s*(\d+)/gi;
  function parseCounts(raw) {
    var s = U.normStr(raw); if (!s) return null;
    COUNT_RE.lastIndex = 0;
    var m, out = [];
    while ((m = COUNT_RE.exec(s)) !== null) {
      out.push({ sev: mapSeverity(m[1]), n: parseInt(m[2], 10) || 0 });
    }
    return out.length ? out : null;
  }

  /* ---- 結案分桶：open / closed / other ----
   * 先判「未結」類（含『結案中』這種帶「結案」二字但其實未完成的），再判「已結」類，
   * 避免子字串誤判。對應不到的落入 other，由上層顯示「未分類狀態」而非靜默丟棄。 */
  var OPEN_WORDS = ['未結', '進行中', '處理中', '追蹤中', '未完成', '未修補', '未修復', '結案中', '修補中', '改善中'];
  var CLOSED_WORDS = ['已結案', '結案', '已修補', '已修復', '已完成', '完成', '已改善', '改善完成', '已解決', '無需修補'];
  function classifyClose(rawStatus, closeDate) {
    var s = U.normStr(rawStatus);
    if (s) {
      for (var i = 0; i < OPEN_WORDS.length; i++) if (s.indexOf(OPEN_WORDS[i]) >= 0) return 'open';
      for (var j = 0; j < CLOSED_WORDS.length; j++) if (s.indexOf(CLOSED_WORDS[j]) >= 0) return 'closed';
      return 'other';
    }
    return closeDate ? 'closed' : 'open';
  }

  /* ---- 取值 ---- */
  function pick(row, header) {
    if (!header) return '';
    var v = row[header];
    return v === undefined ? '' : v;
  }

  /* 表8 特例：單位欄含人名「資訊架構部-林偉竹 (EDR偵測規則)」→ 拆 */
  /* 破折號：半形 - / en dash / em dash / 全形－ 都算（資料實際三種都出現過）
   * 以碼位建構，避免原始碼夾帶難以辨識的字面字元 */
  var DASH_RE = new RegExp('[-' + String.fromCharCode(0x2013) + String.fromCharCode(0x2014) + String.fromCharCode(0xFF0D) + ']');
  function splitUnitOwner(raw) {
    var s = U.normStr(raw);
    var m = s.match(DASH_RE);
    var idx = m ? m.index : -1;
    if (idx < 0) return { unit: s, owner: '' };
    var unit = s.slice(0, idx).trim();
    var owner = s.slice(idx + 1).replace(/[（(].*$/, '').trim(); // 去尾註 (…)
    return { unit: unit, owner: owner };
  }

  /* ---- 一列 → 一或多筆標準紀錄 ---- */
  function buildRecords(row, map, sheetName) {
    var fix = U.parseDate(pick(row, map.fixDeadline));
    var ext = U.parseDate(pick(row, map.firstExtension));
    var exc = U.parseDate(pick(row, map.exceptionApproval));
    var other = U.parseDate(pick(row, map.otherDue));
    // 真正到期日（2026-10-03 志安釐清）：展延/例外的日期『有填』不代表已申請——
    // 要「備註有對應申請紀錄」才算數（備註空＝沒申請）。否則一律用修補期限。
    // 備註格式例：「例外管理(iForm_…)」「首次展延(iForm_…)」。
    var remarkRaw = U.normStr(pick(row, map.remark));
    var realDue = (exc && U.appliedExc(remarkRaw)) ? exc
                : (ext && U.appliedExt(remarkRaw)) ? ext
                : (fix || other || null);
    var daysLeft = U.daysFromToday(realDue);

    var closeDate = U.parseDate(pick(row, map.closeDate));
    var closeRaw = U.normStr(pick(row, map.closeStatus));
    var closeBucket = classifyClose(closeRaw, closeDate);

    // 單位/負責人（表8：無 owner 欄且單位含「-」→ 拆）
    var unit = U.normStr(pick(row, map.unit));
    var owner = map.owner ? U.normStr(pick(row, map.owner)) : '';
    if (!map.owner && DASH_RE.test(unit)) {
      var so = splitUnitOwner(unit); unit = so.unit; owner = so.owner;
    }
    if (!owner) owner = '(未指定)';

    var base = {
      sheet: sheetName,
      // [CL_WebVuln 加] 沒主機/IP 的列(如 10-外部威脅情資)顯示資產名稱；hostRaw 留真實值給存回伺服器(承辦鍵不變)
      host: U.normStr(pick(row, map.host)) || U.normStr(pick(row, map.assetName)),
      hostRaw: U.normStr(pick(row, map.host)),
      assetName: U.normStr(pick(row, map.assetName)),
      name: U.normStr(pick(row, map.name)),
      pluginId: U.normStr(pick(row, map.pluginId)),
      unit: unit,
      owner: owner,
      fixDeadline: fix, firstExtension: ext, exceptionApproval: exc, otherDue: other,
      realDue: realDue, daysLeft: daysLeft,
      // 已結案就不再判逾期（結案的弱點不論到期日都不算已逾期）
      overdue: (closeBucket !== 'closed' && daysLeft !== null && daysLeft < 0),
      overdueDays: (closeBucket !== 'closed' && daysLeft !== null && daysLeft < 0) ? -daysLeft : 0,
      closeStatus: closeRaw, closeBucket: closeBucket, closeDate: closeDate,
      remark: remarkRaw,
      raw: row,  // [CL_WebVuln 加] 整列原始欄位，供原封匯出＋伺服器快照重建；不影響任何渲染
    };

    // 嚴重度：表9 計數字串 → 展開多筆；否則單筆
    var sevRaw = U.normStr(pick(row, map.severity));
    var counts = parseCounts(sevRaw);
    base.risk = sevRaw; // Risk 欄/CSV 顯示原始嚴重度值
    if (counts) {
      var out = [];
      counts.forEach(function (c) {
        for (var i = 0; i < c.n; i++) {
          out.push(Object.assign({}, base, { severity: c.sev, severityRaw: sevRaw, fromCount: true }));
        }
      });
      return out.length ? out : [Object.assign({}, base, { severity: 'Unknown', severityRaw: sevRaw })];
    }
    base.severity = map.severity ? mapSeverity(sevRaw) : 'Unknown';
    base.severityRaw = sevRaw;
    return [base];
  }

  /* ---- 工作表 → {name, headers, rawCount, records, profile} ---- */
  function parseSheet(ws, name) {
    var matrix = XLSX.utils.sheet_to_json(ws, { header: 1, defval: '', blankrows: false });
    if (!matrix.length) return { name: name, headers: [], rawCount: 0, records: [], profile: {} };
    var headers = matrix[0].map(function (h) { return U.normStr(h); });
    var map = resolveProfile(headers);
    var records = [];
    var rawCount = 0;
    var warnings = [];

    // 欄位全對不上 → 多半是「真正表頭不在第一列」(第一列是大標題)，
    // 若不提醒，會產出一整批空白紀錄而使用者毫無所覺
    var KEY_FIELDS = ['severity', 'name', 'host', 'unit', 'owner', 'fixDeadline', 'closeStatus', 'closeDate'];
    var hit = KEY_FIELDS.filter(function (k) { return !!map[k]; });
    if (hit.length === 0) {
      warnings.push('「' + name + '」的欄位全部無法辨識，可能表頭不在第一列（第一列是標題？）或欄位名稱不符，資料將顯示為空白。');
    } else if (hit.length <= 2) {
      warnings.push('「' + name + '」只辨識出 ' + hit.length + ' 個欄位，部分內容可能顯示不完整。');
    }
    // 重複表頭：原本直接用欄名當 key，後者會覆蓋前者，左邊那欄資料整欄遺失。
    // 改為「保留第一欄原名、後續同名改成 欄名#2」——欄位對應吃到的是第一欄
    // （多半才是使用者預期的那欄），重複欄的資料也不會消失。
    var seen = {}, dup = [], keys = [];
    headers.forEach(function (h) {
      if (!h) { keys.push(h); return; }
      if (seen[h]) {
        seen[h]++;
        if (dup.indexOf(h) < 0) dup.push(h);
        keys.push(h + '#' + seen[h]);
      } else {
        seen[h] = 1;
        keys.push(h);
      }
    });
    if (dup.length) warnings.push('「' + name + '」有重複的欄位名稱（' + dup.join('、') + '），以最左邊那欄為準，其餘已改名為「欄名#2」保留。');
    for (var i = 1; i < matrix.length; i++) {
      var arr = matrix[i];
      if (arr.every(function (c) { return U.normStr(c) === ''; })) continue;
      rawCount++;
      var obj = {};
      for (var c = 0; c < keys.length; c++) obj[keys[c]] = arr[c] === undefined ? '' : arr[c];
      buildRecords(obj, map, name).forEach(function (r) { records.push(r); });
    }
    return { name: name, headers: headers, rawCount: rawCount, records: records, profile: map, warnings: warnings };
  }

  /* ---- 主入口：ArrayBuffer → 各數字表結果（依表號排序） ---- */
  function parseWorkbook(buf) {
    return parseWorkbookWb(XLSX.read(buf, { type: 'array', cellDates: true }));
  }
  /* 直接吃「已建好的 workbook 物件」，跳過 ArrayBuffer→XLSX.read 的反序列化。
     伺服器版用這條：server 送原始列 → json_to_sheet 組 wb → 直接進解析，省掉 write+read 兩段（大量列省近 1 秒）。*/
  function parseWorkbookWb(wb) {
    var pat = new RegExp(P.numberedSheetPattern);
    var names = (wb.SheetNames || []).filter(function (n) { return pat.test(n); });
    names.sort(function (a, b) { return leadNum(a) - leadNum(b); });
    return names.map(function (n, i) {
      var res = parseSheet(wb.Sheets[n], n);
      res.index = i;
      return res;
    });
  }
  function leadNum(name) {
    var m = String(name).match(/\d+/);
    return m ? parseInt(m[0], 10) : 9999;
  }

  global.MultiSheet = { parseWorkbook: parseWorkbook, parseWorkbookWb: parseWorkbookWb, resolveProfile: resolveProfile, mapSeverity: mapSeverity, classifyClose: classifyClose };
})(window);

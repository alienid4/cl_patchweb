/* ============================================================
 * js/multi.js  (ms-03)
 * 把 MultiSheet 讀到的各表紀錄，組成看板可用的 result。
 * 預設母體：全部部門 · 未結案(closeBucket=open)。（部門/結案篩選為 ms-04）
 * ============================================================ */
(function (global) {
  'use strict';

  var A = global.Analysis;

  /* 依 profile 有無欄位，判斷該表可出哪些面板 */
  function computeCaps(profile, records) {
    profile = profile || {};
    return {
      severity: !!profile.severity,
      exception: !!profile.exceptionApproval,
      extension: !!profile.firstExtension,
      stagePanel: !!profile.exceptionApproval || !!profile.firstExtension, // 例外/展延統計分頁是否有意義
      owner: (records || []).some(function (r) { return r.owner && r.owner !== '(未指定)'; }),
      host: !!profile.host,
    };
  }

  /* 單張表 → result（供 Dashboard/Tracking/Stats/Search 使用） */
  function buildSheetResult(sheet) {
    var records = sheet.records || [];
    records.forEach(function (r) { if (r.department === undefined) r.department = r.unit || ''; });
    // 預設母體：未結案（全部部門）
    var scoped = records.filter(function (r) { return r.closeBucket === 'open'; });
    var result = A.assembleResult(records, scoped, {
      allCount: records.length,
      scopeLabel: '工作表：' + sheet.name + '　·　全部部門 · 未結案',
    });
    result.caps = sheet.caps;
    return result;
  }

  /* ArrayBuffer → [{ name, index, rawCount, records, profile, caps, result }] */
  function buildAll(buf) {
    var sheets = global.MultiSheet.parseWorkbook(buf);
    return sheets.map(function (s) {
      s.caps = computeCaps(s.profile, s.records);
      s.result = buildSheetResult(s);
      return s;
    });
  }

  global.Multi = { buildAll: buildAll, buildSheetResult: buildSheetResult };
})(window);

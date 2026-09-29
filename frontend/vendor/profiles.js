/* ============================================================
 * config/profiles.js
 * 多工作表欄位對應設定（依 docs/多表欄位對照表_草稿.md 定案）。
 * 純資料；邏輯在 js/sheets.js。
 * ============================================================ */
window.SHEET_PROFILES = {
  /* 只處理「數字-」開頭的工作表（1-…～10-…）；其餘(如 資安弱點追蹤窗口)略過 */
  numberedSheetPattern: '^\\s*\\d+\\s*[-\\u2013\\uFF0D]',

  /* 各概念的候選欄名（依序偏好；比對時先精確、再前綴，皆去空白/大小寫） */
  fieldAliases: {
    unit:       ['負責單位', '部門'],
    owner:      ['負責人', '負責人員'],
    host:       ['Host', '內部Host IP', '標的IP', 'IP', '網址'],
    name:       ['Name', '風險項目', '發現', '標的', 'Audit Name', '外部情資'],
    pluginId:   ['Plugin ID'],
    // 嚴重度：發現嚴重性(E1) 優先，其次英文 Risk Severity，再風險等級，最後單一「風險」
    severity:   ['發現嚴重性', 'Finding Severity', 'Risk Severity', '風險等級', 'Grade', '風險'],
    fixDeadline:       ['修補期限', '改善期限', '預計完成日期', '改善完成日'],
    firstExtension:    ['首次展延上限', '首次展延期限'],
    exceptionApproval: ['例外核准期限'],
    otherDue:          ['預計完成日', '修補完成日'],
    closeStatus:       ['結案狀態', '改善狀況'],
    closeDate:         ['結案日期'],
    remark:            ['備註'],
  },

  /* 嚴重度值正規化：正規化(小寫/去空白)後比對關鍵詞 → 標準等級 */
  severityMap: {
    'critical': 'Critical', 'severe': 'Critical', '嚴重': 'Critical',
    'high': 'High', 'material': 'High', '高': 'High',
    'medium': 'Medium', 'moderate': 'Medium', '中': 'Medium', 'warn': 'Medium',
    'low': 'Low', 'minor': 'Low', 'info': 'Low', '低': 'Low',
  },
};

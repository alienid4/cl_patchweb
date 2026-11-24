/* ============================================================
 * config/features.js
 * 功能開關「出廠預設」(layer A)：隨資料夾發布，給所有人的預設長相。
 * 個人可在「其他功能 → 功能開關」面板覆寫(layer B，存 localStorage)。
 * 加新功能 = 在此加一行；設定面板與畫面渲染都會自動吃這張表。
 *   id     : 對應分頁 data-tab（tab-xxx）或面板容器（panel-xxx）
 *   group  : 設定面板分組標題
 *   label  : 設定面板顯示名稱
 *   default: 出廠是否開啟（省略視為 true）
 * ============================================================ */
window.APP_FEATURES = [
  { id: 'tab-tracking',        group: '分頁',           label: '人員追蹤',              default: true },
  { id: 'tab-matrix',          group: '分頁',           label: '交叉分析',              default: true },
  { id: 'tab-stats',           group: '分頁',           label: '例外／展延統計',        default: true },
  { id: 'tab-search',          group: '分頁',           label: '查詢',                  default: true },

  { id: 'panel-trend',         group: '總覽（首頁）',   label: '趨勢（跟上次比）',      default: true },
  { id: 'panel-red-list',      group: '總覽（首頁）',   label: '部門／負責人紅黑榜',    default: true },
  { id: 'panel-sla',           group: '總覽（首頁）',   label: 'SLA 達成率',            default: true },

  { id: 'panel-sev-repair',    group: '各項目看板面板', label: '各嚴重度結案進度',      default: true },
  { id: 'panel-today-actions', group: '各項目看板面板', label: '優先處理清單',          default: true },
  { id: 'panel-risk-top',      group: '各項目看板面板', label: '風險排序',              default: true },
  { id: 'panel-charts',        group: '各項目看板面板', label: '圖表（嚴重度／到期分布）', default: true },

  { id: 'email-agent',         group: 'Email',          label: '用本機小幫手直接寄（關閉＝只匯出檔給 send.bat）', default: true },
];

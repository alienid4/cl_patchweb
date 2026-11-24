本機函式庫（離線可用）
=====================================

本工具的 SheetJS 與 Chart.js 已「內附」在此資料夾，index.html 直接載入本機檔，
不需要網路、不依賴 CDN，Wi-Fi 斷了也能開。

  xlsx.full.min.js      SheetJS 0.18.5（解析 Excel）
  chart.umd.min.js      Chart.js 4.4.1（畫圖）

若要升級版本：到有網路的電腦下載對應版本、覆蓋同名檔，並更新 index.html 內
各 <script> 的 ?v=xxx 版本查詢字串即可。

  xlsx：https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js
  chart：https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js

檔案僅在本機瀏覽器解析，不會上傳到任何伺服器。

# CL_WebVuln 執行說明（v0.1）

弱點彙總網頁版：FastAPI + SQLite，純唯讀查詢、無登入（內部工具）。

## 本機開發

```bash
cd APP
py -m pip install -r requirements.txt
py -m webvuln.seed                       # 載入天龍八部假資料（demo/驗證用；真實資料走匯入）
py -m uvicorn webvuln.main:app --host 127.0.0.1 --port 3100
# 開瀏覽器 http://127.0.0.1:3100
```

跑測試：`cd APP && py -m pytest -q`（目前 20 passed）

## 匯入資料

- **v0.1**：網頁右上「匯入 JSON」→ 選一個符合匯入契約的 JSON（見下），或用 `webvuln.seed` 種子。
- **契約**：`POST /api/import`，body =
  ```json
  { "source_file": "報告.xlsx", "findings": [
    { "host":"10.30.1.11","owner":"玄慈","severity":"Critical","department":"資訊架構部",
      "sheet_key":"1-系統弱點掃描弱點","plugin_id":"10420","name":"...",
      "remediation_due":"2026-06-01","first_extension_due":null,"exception_due":null,
      "close_status":"未結案","close_date":null,"remark":null }
  ]}
  ```
  日期為 ISO(前端已解析民國年)、severity 已正規化、close_status 已分類。後端算 effective_due/stage。
- **⏳ 待補(S3)**：把單機版 `sheets.js/analysis.js/profiles.js` 接進「上傳 Excel」→ 前端解析成上面 JSON → 呼叫 /api/import。目前先用 JSON/種子。

## 部署到 221（你來做；我 SSH 不到 221）

在 `192.168.1.221` 上：

```bash
# 1) 取得程式（git clone 或 scp APP/ 過去）
# 2) 安裝
cd /opt/webvuln/APP    # 或你的路徑
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# 3) 設 DB 路徑（正式資料落地處；資料夾要可寫）
export WEBVULN_DB_URL="sqlite:////opt/webvuln/data/vuln.db"
# 4) 起服務（測試）
python -m uvicorn webvuln.main:app --host 0.0.0.0 --port 3100
# 內網開 http://192.168.1.221:3100
```

常駐建議用 systemd（見 `ops/webvuln.service.example`）。nginx/TLS 之後要再加。

## 備份（SQLite）

`vuln.db` 是單一檔；備份用 SQLite online backup（比照 WEBITv3 做法），別在寫入中直接複製檔案。

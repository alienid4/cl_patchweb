# CL_WebVuln 執行說明（v0.1）

弱點彙總網頁版：FastAPI + SQLite（內部工具）。查詢免登入；寫入（承辦申請狀態等）預設關閉，
待 W4 登入+audit 上線再由權限控管（開發可設 `WEBVULN_ALLOW_WRITE=1` 開啟）。
> 部署主機 IP／實際路徑屬內網識別，不寫進此公開檔；見本機（非公開）部署筆記。

## 本機開發

```bash
cd APP
py -m pip install -r requirements.txt        # 需 Python 3.10+（union 型別語法）
py -m webvuln.seed                            # 載入示範假資料（真實資料走匯入）
py -m uvicorn webvuln.main:app --host 127.0.0.1 --port 3100
# 開 http://127.0.0.1:3100
```

測試：`cd APP && py -m pytest -q`（目前 20 passed）

## 匯入資料

- 網頁右上「匯入 JSON」，或 `py -m webvuln.seed` 種子。
- 契約：`POST /api/import`，body =
  ```json
  { "source_file": "報告.xlsx", "findings": [
    { "host":"...","owner":"...","severity":"Critical","department":"...",
      "sheet_key":"1-...","plugin_id":"10420","name":"...",
      "remediation_due":"2026-06-01","first_extension_due":null,"exception_due":null,
      "close_status":"未結案","close_date":null,"remark":null } ] }
  ```
  日期為 ISO（前端已解析民國年）、severity 已正規化、close_status 已分類；後端算 effective_due/stage。
- ⏳ 待補（S3）：接單機版 `sheets.js/analysis.js/profiles.js` 做「上傳 Excel → 前端解析 → /api/import」。目前用 JSON/種子。

## 部署（內部 Linux 主機）

以服務帳號執行，SQLite 檔放主機本機、以環境變數指定：

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export WEBVULN_DB_URL="sqlite:////<部署資料目錄>/vuln.db"   # 實際路徑見內網部署筆記
python -m uvicorn webvuln.main:app --host 0.0.0.0 --port 3100
```

常駐用 systemd（見 `ops/patchweb.service.example`）。備份 SQLite 用 online backup，勿於寫入中直接複製檔。

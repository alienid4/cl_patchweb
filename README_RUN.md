# CL_WebVuln 執行說明（V2）

弱點彙總網頁版：FastAPI + SQLite（內部工具）。沿用單機版整套前端（畫面一模一樣），
加上伺服器端新功能（承辦管線／結案統計／缺口示警／原封匯出／登入）。
> 部署主機 IP／實際路徑屬內網識別，不寫進此公開檔；見本機（非公開）部署筆記。

## 一鍵安裝＋設定＋起服務（最省事；在 /tmp 解壓後一行搞定）

在 /tmp 解開 repo 後，直接跑一支：自動搬到 /opt、建 venv、裝相依、設 systemd 服務並**啟動**、開防火牆；
偵測到舊 `data/` 會保留（重裝不洗資料）。需 Python 3.10+、可連網。

```bash
cd cl_patchweb-main          # 解壓出來的目錄
sudo bash deploy/oneclick.sh
```

覆蓋選項：`DEST=/opt/patchweb/src PORT=3100 NO_AUTH=1 OPEN_FIREWALL=1`（環境變數）。

## 手動安裝（分步）

```bash
# 1) 取得程式
git clone <本 repo> && cd cl_patchweb   # 或把 APP/ 內容放到目標目錄後 cd 進去
# 2) 安裝（建 venv＋裝相依；需 Python 3.10+）
bash deploy/install.sh
# 3) 啟動（預設 http://0.0.0.0:3100，測試期免登入）
bash deploy/run.sh
```

- 換埠：`PORT=8080 bash deploy/run.sh`
- 開登入（寫入需登入）：`WEBVULN_NO_AUTH=0 bash deploy/run.sh`，再用 `python -m webvuln.useradmin add <帳號> admin` 建帳號。
- 開機常駐：見 `ops/patchweb.service.example`（systemd）。
- 防火牆：`sudo firewall-cmd --add-port=3100/tcp --permanent && sudo firewall-cmd --reload`
- 首次無資料時會顯示上傳畫面：用「選擇檔案」或拖放弱點彙總報告 Excel（沿用單機版解析），
  上傳同時會存進伺服器，之後所有人開頁都直接看得到（不必重傳）。

### 離線一鍵安裝包（全新 Linux、無網路、連 Python 都沒有）

目標機不能連網、也沒裝 Python 時，用「離線安裝包」：在**有網路的機器**產出單一 tar.gz
（自帶可攜 CPython 3.11 ＋ 全相依離線 wheels），拷到目標機解開即裝，全程不連網。

```bash
# 有網路的機器：產出安裝包（約 60MB，落在 dist/install/，不進版控）
python .project/make_install_pack.py
# 目標機（Linux x86_64）：
tar xzf CL_WebVuln_離線安裝包_*.tar.gz && cd CL_WebVuln_installpack
bash setup.sh      # 解可攜 python＋離線裝相依（不連網、不動系統、不需 root）
bash run.sh        # 啟動 http://0.0.0.0:3100
```
平台鎖 Linux x86_64 / CPython 3.11；要別的平台改 `.project/make_install_pack.py` 的 `TARGET_*`。

## 本機開發

```bash
cd APP
py -m pip install -r requirements.txt        # 需 Python 3.10+（union 型別語法）
py -m webvuln.seed                            # 載入示範假資料（真實資料走匯入）
py -m uvicorn webvuln.main:app --host 127.0.0.1 --port 3100
# 開 http://127.0.0.1:3100
```

測試：`cd APP && py -m pytest -q`（目前 42 passed）

## 帳號 / 登入（W4）

讀取免登入；寫入（推進承辦申請狀態等）需登入，且限 `admin` / `承辦`。密碼用 stdlib pbkdf2（零外部相依）。

```bash
# 建帳號（密碼從 WEBVULN_PWD 取，沒設就隨機產生並印出一次）
WEBVULN_PWD=請改我 py -m webvuln.useradmin add <帳號> admin --name 管理員
py -m webvuln.useradmin add <帳號> 承辦            # 不給 WEBVULN_PWD → 印臨時密碼
py -m webvuln.useradmin list                        # 列出帳號
py -m webvuln.useradmin passwd <帳號>               # 重設密碼
```

登入 API：`POST /api/login {username,password}`（設 httponly cookie）、`POST /api/logout`、`GET /api/me`。
維護時可設 `WEBVULN_DISABLE_WRITE=1` 暫停所有寫入。正式機上 TLS 後設 `WEBVULN_COOKIE_SECURE=1`。
日後轉 AD：設 `WEBVULN_AUTH_BACKEND=ad`（seam 已留於 `security.authenticate`，實作待 W4-B）。

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

#!/usr/bin/env bash
# ============================================================
# 啟動弱點彙總網頁版。先跑過 deploy/install.sh。
#   bash deploy/run.sh                 # 預設 3100、測試期免登入
#   PORT=8080 bash deploy/run.sh       # 換埠
#   WEBVULN_NO_AUTH=0 bash deploy/run.sh   # 開登入（寫入需登入；用 useradmin 建帳號）
# ============================================================
set -e
cd "$(dirname "$0")/.."   # 進到 APP/
if [ ! -d .venv ]; then echo "尚未安裝，請先： bash deploy/install.sh"; exit 1; fi
# shellcheck disable=SC1091
. .venv/bin/activate

mkdir -p data
export WEBVULN_DB_URL="${WEBVULN_DB_URL:-sqlite:///$(pwd)/data/vuln.db}"
export WEBVULN_NO_AUTH="${WEBVULN_NO_AUTH:-1}"     # 測試期免登入；正式要密碼設 0
PORT="${PORT:-3100}"

echo "============================================================"
echo " 啟動 http://0.0.0.0:${PORT}"
echo " DB   ${WEBVULN_DB_URL}"
echo " 免登入模式 WEBVULN_NO_AUTH=${WEBVULN_NO_AUTH}（1=免登入可寫入，0=寫入需登入）"
echo " 停止：Ctrl+C"
echo "============================================================"
exec python -m uvicorn webvuln.main:app --host 0.0.0.0 --port "${PORT}"

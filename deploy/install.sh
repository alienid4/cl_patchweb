#!/usr/bin/env bash
# ============================================================
# 一鍵安裝：在「全新 Linux」把弱點彙總網頁版(V2)裝起來
#   用法：  bash deploy/install.sh
#   需求：  Python 3.10+（含 venv）。CentOS/RHEL 系可先: sudo dnf install -y python3
#   裝完：  bash deploy/run.sh   啟動（預設 http://0.0.0.0:3100，測試期免登入）
# 只用標準庫＋requirements.txt 內套件，無需 root（除非要開防火牆/systemd）。
# ============================================================
set -e
cd "$(dirname "$0")/.."   # 進到 APP/
PY="${PYTHON:-python3}"

echo "==> [1/4] 檢查 Python（需 3.10+）"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 $PY。請先安裝 Python 3.10+（例：sudo dnf install -y python3 或 sudo apt install -y python3 python3-venv）"; exit 1
fi
"$PY" --version
"$PY" - <<'PYEOF'
import sys
assert sys.version_info >= (3, 10), "需要 Python 3.10 以上（union 型別語法）"
print("Python 版本 OK")
PYEOF

echo "==> [2/4] 建立虛擬環境 .venv"
"$PY" -m venv .venv || { echo "建 venv 失敗；Debian/Ubuntu 請先: sudo apt install -y python3-venv"; exit 1; }
# shellcheck disable=SC1091
. .venv/bin/activate

echo "==> [3/4] 安裝相依套件"
# 離線模式：若同層有 offline_wheels/（或 wheels/）內含 .whl，就不連網、直接用這些輪子裝
WHEELS=""
for d in offline_wheels wheels ../offline_wheels; do
  if [ -d "$d" ] && ls "$d"/*.whl >/dev/null 2>&1; then WHEELS="$d"; break; fi
done
if [ -n "$WHEELS" ]; then
  echo "   離線模式：用 $WHEELS 的輪子安裝（不連網，共 $(ls "$WHEELS"/*.whl | wc -l) 個）"
  pip install --no-index --find-links "$WHEELS" -r requirements.txt
else
  pip install --upgrade pip >/dev/null 2>&1 || true
  pip install -r requirements.txt
fi

echo "==> [4/4] 建立資料目錄"
mkdir -p data

cat <<'DONE'

============================================================
 安裝完成 ✓
 啟動：   bash deploy/run.sh
          （預設 http://0.0.0.0:3100，測試期免登入 WEBVULN_NO_AUTH=1）
 換埠：   PORT=8080 bash deploy/run.sh
 要開登入：WEBVULN_NO_AUTH=0 bash deploy/run.sh   （再用 useradmin 建帳號）
 開機常駐：見 ops/patchweb.service.example（systemd）
 防火牆：  sudo firewall-cmd --add-port=3100/tcp --permanent && sudo firewall-cmd --reload
============================================================
DONE

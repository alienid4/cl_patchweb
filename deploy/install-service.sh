#!/usr/bin/env bash
# 把本安裝裝成 systemd 服務（開機自動起、掛了自動重啟、背景常駐）。
# 在「安裝目錄」下執行（離線包解開的 CL_WebVuln_installpack，或 git clone 的 repo 根）：
#   sudo bash deploy/install-service.sh            # 預設服務名 webvuln、埠 3100、免登入
#   PORT=8080 NO_AUTH=0 SVC=webvuln2 sudo bash deploy/install-service.sh
set -e
cd "$(dirname "$0")/.."          # 到安裝根
ROOT="$(pwd)"
SVC="${SVC:-webvuln}"
PORT="${PORT:-3100}"
NO_AUTH="${NO_AUTH:-1}"
U="${SUDO_USER:-$(whoami)}"

# 找 venv 的 python（離線包=安裝根/.venv；也支援 app 同層）
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/Scripts/python"
[ -x "$PY" ] || { echo "找不到 .venv，請先 bash setup.sh 或 deploy/install.sh"; exit 1; }
# 找 app 目錄（離線包=app/；git clone=根目錄有 webvuln/）
APPDIR="$ROOT/app"; [ -d "$APPDIR/webvuln" ] || APPDIR="$ROOT"
[ -d "$APPDIR/webvuln" ] || { echo "找不到 webvuln/，路徑不對"; exit 1; }

UNIT="/etc/systemd/system/${SVC}.service"
echo "寫入 $UNIT （User=$U, WorkingDirectory=$APPDIR, PORT=$PORT, NO_AUTH=$NO_AUTH）"
tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=CL_WebVuln 弱點彙總網頁版
After=network.target

[Service]
Type=simple
User=$U
WorkingDirectory=$APPDIR
Environment=WEBVULN_DB_URL=sqlite:///$ROOT/data/vuln.db
Environment=WEBVULN_NO_AUTH=$NO_AUTH
ExecStart=$PY -m uvicorn webvuln.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$ROOT/data"
systemctl daemon-reload
systemctl enable --now "$SVC"
sleep 2
systemctl --no-pager status "$SVC" | head -6 || true
echo
echo "完成。管理： systemctl restart|stop $SVC ； journalctl -u $SVC -n 50 --no-pager"
echo "若要用前景測試，先 systemctl stop $SVC 再 bash run.sh（避免搶埠）。"

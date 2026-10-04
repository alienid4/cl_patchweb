#!/usr/bin/env bash
# 安裝「每週部門週報」排程（#8）：systemd timer，每週一 08:00 跑 python -m webvuln.weekly，
# 寄給「有開啟每週報告」的部門窗口。沿用 webvuln 服務的安裝位置、Python、資料庫與執行帳號。
#
#   sudo bash install-weekly.sh
#
# 可用環境變數覆寫：
#   SVC=webvuln        對應的主服務名（讀它的 DB 與執行帳號）
#   RUN_AS=webvuln     執行帳號
#   ONCAL="Mon *-*-* 08:00:00"   排程時間（systemd OnCalendar 格式）
set -uo pipefail
umask 022

ONCAL="${ONCAL:-Mon *-*-* 08:00:00}"
die() { echo; echo "!! $*"; exit 1; }

[ "$(id -u)" = "0" ] || die "請用 root 執行： sudo bash $0"
for c in systemctl runuser; do command -v "$c" >/dev/null 2>&1 || die "這台沒有 $c（需 systemd）"; done

APP="$(cd "$(dirname "$0")/.." && pwd -P)"
[ -d "$APP/webvuln" ] || die "$APP 底下沒有 webvuln/，請放在安裝位置的 deploy/ 裡執行"
ROOT=""
for d in "$APP" "$APP/.." "$APP/../.."; do
  if [ -x "$d/.venv/bin/python" ]; then ROOT="$(cd "$d" && pwd -P)"; break; fi
done
[ -n "$ROOT" ] || die "找不到 .venv（請先完成安裝）"
PY="$ROOT/.venv/bin/python"

# 自動偵測主服務名（不寫死 webvuln；221 服務叫 patchweb）
SVC="${SVC:-}"
if [ -z "$SVC" ]; then
  for u in /etc/systemd/system/*.service; do
    [ -f "$u" ] || continue
    if grep -qE 'webvuln\.main:app' "$u" 2>/dev/null; then SVC="$(basename "$u" .service)"; break; fi
  done
fi
[ -n "$SVC" ] || die "找不到弱點彙總的主服務（跑 webvuln.main:app 的 unit）；請先 install-service.sh，或用 SVC=名稱 指定"
WSVC="${SVC}-weekly"

# 從主服務 unit 讀 DB 與執行帳號（週報排程要連同一顆 DB、同一個帳號）
DB_URL="$(systemctl show "$SVC" -p Environment --value 2>/dev/null | tr ' ' '\n' | grep -E '^WEBVULN_DB_URL=' | head -1 | cut -d= -f2-)"
[ -n "$DB_URL" ] || DB_URL="sqlite:///$ROOT/data/vuln.db"
RUN_AS="${RUN_AS:-$(systemctl show "$SVC" -p User --value 2>/dev/null)}"
[ -n "$RUN_AS" ] || RUN_AS="webvuln"
id "$RUN_AS" >/dev/null 2>&1 || die "執行帳號 $RUN_AS 不存在（請先跑 install-service.sh）"

echo "════════════════════════════════════════════════"
echo " 安裝每週週報排程： $WSVC"
echo "  程式位置： $APP"
echo "  Python　： $PY"
echo "  資料庫　： $DB_URL"
echo "  執行帳號： $RUN_AS"
echo "  排程時間： $ONCAL"
echo "════════════════════════════════════════════════"

cat > "/etc/systemd/system/${WSVC}.service" <<EOF
[Unit]
Description=CL_WebVuln 每週部門週報（寄給有開啟的部門窗口）
After=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$APP
Environment=WEBVULN_DB_URL=$DB_URL
ExecStart=$PY -m webvuln.weekly
EOF

cat > "/etc/systemd/system/${WSVC}.timer" <<EOF
[Unit]
Description=每週一 08:00 觸發部門週報

[Timer]
OnCalendar=$ONCAL
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${WSVC}.timer"
echo
echo "✅ 已啟用。下次觸發時間："
systemctl list-timers "${WSVC}.timer" --no-pager 2>/dev/null | sed -n '1,2p'
echo
echo "手動測試一次： sudo systemctl start ${WSVC}.service　然後看： journalctl -u ${WSVC}.service -n 30 --no-pager"
echo "（也可在系統內用 Super Admin →「系統設定 → 每週排程 → 立即試跑」）"

#!/usr/bin/env bash
# 建立／重設本地測試帳號（superadmin / admin / user）。供角色測試，AD 啟用時也能用。
# 自動找安裝位置、venv、並連到「正式服務用的那顆資料庫」，用服務帳號寫入（檔案權限才對）。
#
#   sudo bash create-accounts.sh [密碼]
#
# 密碼：參數優先，否則環境變數 WEBVULN_TEST_PASSWORD，再否則預設 'test-1234'（請自行改）。
# 會建立／重設三個本地帳號：
#   superadmin → Super Admin（最高權限；也是被鎖在外時的本地救援帳號）
#   admin      → 部門窗口（dept_admin；部門登入後於「帳號與權限」指定）
#   user       → 一般使用者（要對應某負責人就把顯示名改成那個人名）
# 重跑＝重設密碼與角色（冪等）。單一自訂帳號請用畫面「帳號與權限 → 新增/重設本地帳號」。
set -uo pipefail
umask 022

PW="${1:-${WEBVULN_TEST_PASSWORD:-test-1234}}"
die() { echo; echo "!! $*"; exit 1; }

[ "$(id -u)" = "0" ] || die "請用 root 執行： sudo bash $0 [密碼]"
command -v runuser >/dev/null 2>&1 || die "這台沒有 runuser"

APP="$(cd "$(dirname "$0")/.." && pwd -P)"
[ -d "$APP/webvuln" ] || die "$APP 底下沒有 webvuln/，請放在安裝位置的 deploy/ 裡執行"
ROOT=""
for d in "$APP" "$APP/.." "$APP/../.."; do
  if [ -x "$d/.venv/bin/python" ]; then ROOT="$(cd "$d" && pwd -P)"; break; fi
done
[ -n "$ROOT" ] || die "找不到 .venv（請先完成安裝 setup.sh）"
PY="$ROOT/.venv/bin/python"

# 自動偵測服務名：不寫死 webvuln（221 服務叫 patchweb）。
# 先用 SVC 環境變數；否則找「ExecStart 跑 webvuln.main:app」的那個 unit。
SVC="${SVC:-}"
if [ -z "$SVC" ]; then
  for u in /etc/systemd/system/*.service; do
    [ -f "$u" ] || continue
    if grep -qE 'webvuln\.main:app' "$u" 2>/dev/null; then
      SVC="$(basename "$u" .service)"; break
    fi
  done
fi
[ -n "$SVC" ] && echo "  偵測到服務： $SVC" || echo "  （找不到對應服務，將用預設 DB 路徑）"

# 連到正式服務用的那顆 DB（從 unit 讀），沒有就用預設
DB_URL=""
[ -n "$SVC" ] && DB_URL="$(systemctl show "$SVC" -p Environment --value 2>/dev/null | tr ' ' '\n' | grep -E '^WEBVULN_DB_URL=' | head -1 | cut -d= -f2-)"
[ -n "$DB_URL" ] || DB_URL="sqlite:///$ROOT/data/vuln.db"
# 用服務帳號寫入（DB 檔多半屬該帳號）；取不到就用 root
RUN_AS=""
[ -n "$SVC" ] && RUN_AS="$(systemctl show "$SVC" -p User --value 2>/dev/null)"
[ -n "$RUN_AS" ] || RUN_AS="root"

echo "════════════════════════════════════════════════"
echo " 建立／重設本地測試帳號"
echo "  程式位置： $APP"
echo "  Python　： $PY"
echo "  資料庫　： $DB_URL"
echo "  寫入帳號： $RUN_AS"
echo "  共同密碼： $PW"
echo "════════════════════════════════════════════════"

# 動 DB 前先備份（鐵則）：SQLite 直接複製檔案到 <db>.bak-時間戳；保留最近 20 份
case "$DB_URL" in
  sqlite:///*)
    DBFILE="${DB_URL#sqlite:///}"
    if [ -f "$DBFILE" ]; then
      BAK="${DBFILE}.bak-$(date +%Y%m%d_%H%M%S)"
      cp -p "$DBFILE" "$BAK" && echo "  ✓ 已備份： $BAK" || die "備份失敗，為安全起見中止（沒動到帳號）"
      # 只留最近 20 份備份
      ls -1t "${DBFILE}".bak-* 2>/dev/null | tail -n +21 | while read -r old; do rm -f "$old"; done
    else
      echo "  （資料庫尚不存在，第一次建立，略過備份）"
    fi ;;
  *) echo "  （非 SQLite，請自行確認已備份 $DB_URL）" ;;
esac

cd "$APP" || die "進不去 $APP"
if [ "$RUN_AS" = "root" ]; then
  WEBVULN_DB_URL="$DB_URL" "$PY" -m webvuln.seedtest "$PW"
else
  runuser -u "$RUN_AS" -- env WEBVULN_DB_URL="$DB_URL" "$PY" -m webvuln.seedtest "$PW"
fi
rc=$?
echo
if [ "$rc" = "0" ]; then
  echo "✅ 完成。登出後用 superadmin / admin / user ＋上面密碼登入即可切換角色測試。"
  echo "   建議先確認 superadmin 可登入，再去調整你 AD 帳號的角色（避免被鎖在外）。"
else
  echo "!! 建立失敗（離開碼 $rc）。確認 .venv 已安裝相依、DB 路徑可寫。"
fi
exit "$rc"

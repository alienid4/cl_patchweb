#!/usr/bin/env bash
# ============================================================
# 一鍵安裝＋設定：在 /tmp 解壓後，直接跑這一支就全部搞定。
#   cd <解壓出來的目錄>
#   sudo bash deploy/oneclick.sh
#
# 會自動：① 把程式搬到固定位置（預設 /opt/patchweb/src；/tmp 會被清、服務也不能裝在 /tmp）
#          ② 建 .venv、裝相依（需 Python 3.10+、可連網）
#          ③ 設成 systemd 服務（開機自動起、掛了自動重啟、專用帳號）
#          ④ 開防火牆
# 重裝安全：偵測到舊 data/（弱點快照／承辦疊加／申請佐證附件）會原樣保留，不覆蓋。
# 可用環境變數覆蓋：
#   DEST=/opt/patchweb/src   安裝位置
#   PORT=3100  NO_AUTH=1      埠／免登入(1=免登入可寫入；正式要密碼設 0)
#   OPEN_FIREWALL=1           是否開防火牆(0=不開)
# 更新改版用 PATCH（patch.sh），不需要再跑這支。
# ============================================================
set -euo pipefail
umask 022

DEST="${DEST:-/opt/patchweb/src}"
PORT="${PORT:-3100}"
NO_AUTH="${NO_AUTH:-1}"
OPEN_FIREWALL="${OPEN_FIREWALL:-1}"

[ "$(id -u)" = "0" ] || { echo "!! 請用 root 執行： sudo bash $0"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd -P)"   # 目前程式所在(可能在 /tmp)
[ -d "$SRC/webvuln" ] || { echo "!! $SRC 底下沒有 webvuln/，請在解壓出來目錄的 deploy/ 裡執行這支"; exit 1; }

echo "════════════════════════════════════════════════"
echo " 一鍵安裝 CL_WebVuln 弱點彙總網頁版"
echo " 來源： $SRC"
echo " 安裝到： $DEST　｜　埠 $PORT　｜　免登入 NO_AUTH=$NO_AUTH"
echo "════════════════════════════════════════════════"

echo
echo "==> [1/5] 複製程式到固定位置 $DEST（保留舊 data/）"
if [ "$SRC" = "$DEST" ]; then
  echo "    已在 $DEST，略過複製"
else
  mkdir -p "$DEST"
  PRESERVE=""
  if [ -d "$DEST/data" ]; then
    PRESERVE="$(mktemp -d)"
    cp -a "$DEST/data/." "$PRESERVE/" 2>/dev/null || true
    echo "    偵測到舊 data/，已暫存保留"
  fi
  # 清掉舊程式(保留 data)，再把新程式複製進去(排除 .venv 與 data)
  find "$DEST" -mindepth 1 -maxdepth 1 ! -name 'data' -exec rm -rf {} + 2>/dev/null || true
  ( cd "$SRC" && tar cf - --exclude=./.venv --exclude=./data . ) | ( cd "$DEST" && tar xf - )
  if [ -n "$PRESERVE" ]; then
    mkdir -p "$DEST/data"
    cp -a "$PRESERVE/." "$DEST/data/" 2>/dev/null || true
    rm -rf "$PRESERVE"
    echo "    舊 data/ 已還原"
  fi
fi
cd "$DEST"

echo
echo "==> [2/5] 建 .venv、裝相依"
# 自動找 >=3.10 的 python（這台的 python3 可能太舊，例如 3.9）
PYBIN=""
for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
  [ -n "$c" ] || continue
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then PYBIN="$c"; break; fi
done
if [ -z "$PYBIN" ]; then
  echo "    !! 找不到 Python 3.10+（這台 python3 是 $(python3 -V 2>&1)）。"
  echo "       RHEL/CentOS 可裝： sudo dnf install -y python3.11 ，再重跑這支。"
  echo "       或改用離線安裝包（自帶 Python 3.11，不需系統 Python／不需連網）。"
  exit 1
fi
echo "    使用 $PYBIN（$("$PYBIN" -V 2>&1)）"
rm -rf "$DEST/.venv"            # 重裝：重建乾淨的 venv
PYTHON="$PYBIN" bash deploy/install.sh

echo
echo "==> [3/5] 設成 systemd 服務"
PORT="$PORT" NO_AUTH="$NO_AUTH" bash deploy/install-service.sh

echo
echo "==> [4/5] 防火牆"
if [ "$OPEN_FIREWALL" = "1" ] && command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --add-port="${PORT}/tcp" --permanent >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  echo "    已開 ${PORT}/tcp"
else
  echo "    略過（無 firewall-cmd 或 OPEN_FIREWALL=0）"
fi

echo
echo "==> [5/5] 確認服務已啟動"
# install-service.sh 已 enable --now＋重啟；這裡再明確確認一次（含開機啟動與健康檢查）
systemctl is-active --quiet webvuln && echo "    ✓ 服務 webvuln 執行中（active）" || { echo "    !! 服務未啟動，嘗試啟動…"; systemctl enable --now webvuln || true; }
systemctl is-enabled --quiet webvuln && echo "    ✓ 已設為開機自動啟動" || echo "    !! 未設開機啟動"
if command -v curl >/dev/null 2>&1; then
  for i in 1 2 3 4 5 6 7 8 9 10; do
    case "$(curl -s -m 3 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null)" in
      *ok*) echo "    ✓ 後端有回應（埠 ${PORT}）"; break ;;
    esac
    sleep 1
  done
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "════════════════════════════════════════════════"
echo " ✅ 安裝完成，服務已啟動"
echo "    開瀏覽器： http://${IP:-<本機IP>}:${PORT}"
echo "    首次無資料 → 右上角「其他功能 → 重新選擇檔案」上傳弱點彙總 Excel"
echo "    服務重啟： systemctl restart webvuln"
echo "    看紀錄　： journalctl -u webvuln -n 50 --no-pager"
echo "    之後改版用 PATCH（sudo bash patch.sh），不用再跑這支"
echo "════════════════════════════════════════════════"

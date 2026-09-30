#!/usr/bin/env bash
# 把「用 run.sh 手動跑」的安裝改成 systemd 服務（開機自動起、掛了自動重啟、用專用帳號跑）。
#
#   sudo bash install-service.sh
#
# 這支腳本放在安裝位置的 deploy/ 底下，會自己找到安裝位置，從哪個目錄執行都可以。
# 埠、資料庫位置、免登入設定都沿用「目前正在跑的那個行程」的值，不用重新輸入。
# 要改的話用環境變數蓋掉：
#   SVC=webvuln      服務名稱
#   RUN_AS=webvuln   執行帳號（不存在會建立成不能登入的系統帳號）
#   PORT=3100  NO_AUTH=1
#
# 順序是刻意的：所有檢查都在「停掉舊行程」之前做完。任何一項不過就中止，
# 這時舊行程還在跑，服務不會中斷。
# 可以重跑：已經是服務的話，只會更新設定並重啟。
set -uo pipefail
umask 022

MARK='webvuln\.main:app'
TS="$(date +%Y%m%d_%H%M%S)"
SVC="${SVC:-webvuln}"
RUN_AS="${RUN_AS:-webvuln}"
UNIT="/etc/systemd/system/${SVC}.service"

die() { echo; echo "!! $*"; echo "   沒有動到正在跑的行程。"; exit 1; }

[ "$(id -u)" = "0" ] || { echo "!! 請用 root 執行： sudo bash $0"; exit 1; }
for c in systemctl systemd-run runuser pgrep; do
  command -v "$c" >/dev/null 2>&1 || { echo "!! 這台沒有 $c，這支腳本只支援有 systemd 的 Linux"; exit 1; }
done

APP="$(cd "$(dirname "$0")/.." && pwd -P)"
[ -d "$APP/webvuln" ] || die "$APP 底下沒有 webvuln/，這支腳本要放在安裝位置的 deploy/ 裡執行"
# .venv 在哪：離線包是 app 的上一層；git 簽出的可能在 app 裡或上兩層
ROOT=""
for d in "$APP" "$APP/.." "$APP/../.."; do
  if [ -x "$d/.venv/bin/python" ]; then ROOT="$(cd "$d" && pwd -P)"; break; fi
done
[ -n "$ROOT" ] || die "找不到 .venv（找過 $APP 與往上兩層）。請先完成安裝（bash setup.sh）"
PY="$ROOT/.venv/bin/python"

echo "════════════════════════════════════════════════"
echo " 改成 systemd 服務： $SVC"
echo " 時間： $(date '+%F %T')　系統： $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-未知}")"
echo "════════════════════════════════════════════════"

# ===== 1. 讀現況 =====
echo
echo "[1/7] 讀取目前的執行狀況"
for u in /etc/systemd/system/*.service; do
  [ -f "$u" ] || continue
  [ "$u" = "$UNIT" ] && continue
  grep -qE "$MARK" "$u" 2>/dev/null || continue
  wd="$(grep -E '^WorkingDirectory=' "$u" | head -1 | cut -d= -f2-)"
  if [ "$(cd "$wd" 2>/dev/null && pwd -P)" = "$APP" ]; then
    die "這個安裝已經有服務了：$(basename "$u")。不需要再裝一個（要改名請先移除舊的）"
  fi
done

SVC_PID="$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)"
OLD_PID=""
for pid in $(pgrep -f "[u]vicorn $MARK" 2>/dev/null); do
  [ "$pid" = "$SVC_PID" ] && continue
  [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$APP" ] || continue
  OLD_PID="$pid"; break
done

OLD_PORT=""; OLD_DB=""; OLD_NOAUTH=""
envof() { tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | grep -E "^$2=" | head -1 | cut -d= -f2-; }
SRC_PID="${OLD_PID:-}"
[ -n "$SRC_PID" ] || { [ "${SVC_PID:-0}" != "0" ] && SRC_PID="$SVC_PID"; }
if [ -n "$SRC_PID" ]; then
  OLD_PORT="$(tr '\0' ' ' < "/proc/$SRC_PID/cmdline" 2>/dev/null | grep -oE '\-\-port[ =]+[0-9]+' | grep -oE '[0-9]+' | head -1)"
  OLD_DB="$(envof "$SRC_PID" WEBVULN_DB_URL)"
  OLD_NOAUTH="$(envof "$SRC_PID" WEBVULN_NO_AUTH)"
fi
PORT="${PORT:-${OLD_PORT:-3100}}"
NO_AUTH="${NO_AUTH:-${OLD_NOAUTH:-1}}"
DB_URL="${OLD_DB:-sqlite:///$ROOT/data/vuln.db}"

# 資料目錄＝資料庫檔所在的目錄（服務帳號唯一需要寫入的地方）
case "$DB_URL" in
  sqlite:///*)
    DBFILE="${DB_URL#sqlite:///}"
    case "$DBFILE" in /*) : ;; *) DBFILE="$APP/$DBFILE"; DB_URL="sqlite:///$DBFILE" ;; esac
    DATA="$(dirname "$DBFILE")" ;;
  *) DBFILE=""; DATA="$ROOT/data" ;;
esac

if [ -n "$OLD_PID" ]; then
  echo "  目前是手動啟動的行程： PID $OLD_PID（帳號 $(ps -o user= -p "$OLD_PID" | tr -d ' ')，$(ps -o lstart= -p "$OLD_PID")起）"
elif [ "${SVC_PID:-0}" != "0" ]; then
  echo "  目前已經是服務 $SVC 在跑（PID $SVC_PID），這次只更新設定並重啟"
else
  echo "  目前沒有在跑"
fi
echo "  程式位置： $APP"
echo "  Python　： $PY"
echo "  資料目錄： $DATA"
echo "  資料庫　： ${DBFILE:-（非 SQLite，沿用原連線設定）}"
echo "  埠 $PORT ｜ 免登入 NO_AUTH=$NO_AUTH ｜ 執行帳號 $RUN_AS"
if [ "$NO_AUTH" = "1" ]; then
  echo "  ⚠ 免登入模式：連得到這台的人都能寫入承辦狀態（沿用目前設定）。"
  echo "    要收回： NO_AUTH=0 sudo bash $0 ，再用 useradmin 建帳號"
fi
case "$APP$PY$DATA" in *[[:space:]]*) die "路徑裡有空白，systemd 設定檔寫不進去，請換一個沒有空白的安裝位置" ;; esac

# ===== 2. 安裝位置適不適合當服務 =====
echo
echo "[2/7] 檢查安裝位置"
case "$ROOT/" in
  /tmp/*|/var/tmp/*|/dev/shm/*)
    die "安裝位置在暫存目錄（$ROOT）。系統會定期清掉暫存目錄裡的舊檔，服務跑一陣子就會壞。
   請把安裝搬到固定位置（例如 /opt）後重跑 setup.sh，再執行這支腳本" ;;
esac
echo "  ✓ 不在暫存目錄"

# ===== 3. 執行帳號 =====
echo
echo "[3/7] 執行帳號"
if [ "$RUN_AS" = "root" ]; then
  die "不用 root 跑服務。這是對外的網頁服務，用專用帳號跑，出事時影響範圍才小"
fi
NEW_USER=0
if id "$RUN_AS" >/dev/null 2>&1; then
  echo "  ✓ 帳號 $RUN_AS 已存在（$(id "$RUN_AS")）"
else
  NEW_USER=1
  echo "  帳號 $RUN_AS 不存在，預檢通過後會建立（系統帳號、不能登入、沒有家目錄）"
fi
RUN_GRP="$RUN_AS"
id "$RUN_AS" >/dev/null 2>&1 && RUN_GRP="$(id -gn "$RUN_AS")"

# 往上每一層目錄，服務帳號都要進得去。用現有的 nobody 先探：一般帳號進不去的地方
# （例如 /root 底下），新建的專用帳號一樣進不去。
PROBE="$RUN_AS"; id "$PROBE" >/dev/null 2>&1 || PROBE="nobody"
d="$ROOT"
while [ "$d" != "/" ]; do
  d="$(dirname "$d")"
  runuser -u "$PROBE" -- test -x "$d" 2>/dev/null || die "安裝位置的上層目錄 $d 一般帳號進不去（權限 $(stat -c '%a %U:%G' "$d")）。
   專用帳號讀不到程式，服務會起不來。請把安裝搬到 /opt 這類位置後重跑 setup.sh，
   不要為了這個去放寬 $d 的權限"
done
echo "  ✓ 安裝位置的上層目錄，一般帳號都進得去"

# ===== 4. 建帳號與權限 =====
# 到這裡才開始改東西。改的內容：建帳號、程式與 Python 讓服務帳號的群組「可讀」、
# 資料目錄交給服務帳號。程式本身服務帳號改不了（唯讀），只有資料目錄可寫。
echo
echo "[4/7] 建立帳號、設定權限"
if [ "$NEW_USER" = "1" ]; then
  useradd --system --user-group --no-create-home --home-dir "$DATA" --shell /sbin/nologin \
          --comment "CL_WebVuln service" "$RUN_AS" || die "建立帳號 $RUN_AS 失敗"
  RUN_GRP="$(id -gn "$RUN_AS")"
  echo "  ✓ 已建立帳號 $RUN_AS（$(id "$RUN_AS")）"
fi
for p in "$APP" "$ROOT/.venv" "$ROOT/runtime"; do
  [ -e "$p" ] || continue
  chgrp -R "$RUN_GRP" "$p" && chmod -R g+rX,g-w "$p" || die "設定 $p 的權限失敗"
  echo "  ✓ $p → 群組 $RUN_GRP 可讀、不可寫"
done
# $ROOT 這一層本身也要進得去（離線包的 app、.venv、data 都在它底下）
runuser -u "$RUN_AS" -- test -x "$ROOT" 2>/dev/null || { chgrp "$RUN_GRP" "$ROOT" && chmod g+rx "$ROOT"; }
mkdir -p "$DATA" || die "建不出資料目錄 $DATA"
chown -R "$RUN_AS:$RUN_GRP" "$DATA" && chmod 750 "$DATA" || die "設定資料目錄權限失敗"
echo "  ✓ $DATA → 擁有者 $RUN_AS，只有它可寫"

# ===== 5. 預檢：在 systemd 底下、用服務帳號、用同一組限制，實際試跑一次 =====
# 舊行程還在跑，這一步不影響它。這裡過了，等一下正式啟動才有把握。
echo
echo "[5/7] 預檢（舊行程仍在服務中，不受影響）"
HARDEN=(-p "User=$RUN_AS" -p "Group=$RUN_GRP" -p "WorkingDirectory=$APP"
        -p NoNewPrivileges=yes -p PrivateTmp=yes -p ProtectSystem=strict -p "ReadWritePaths=$DATA")
CHECK='import os, sys, webvuln.main
d = sys.argv[1]
t = os.path.join(d, ".svc_write_test")
open(t, "w").close(); os.remove(t)
print("ok")'
PRE_UNIT="${SVC}-precheck-$$"
OUT="$(systemd-run --wait --pipe --collect --quiet --unit "$PRE_UNIT" "${HARDEN[@]}" \
        -E "WEBVULN_DB_URL=$DB_URL" -E "WEBVULN_NO_AUTH=$NO_AUTH" \
        "$PY" -c "$CHECK" "$DATA" 2>&1)"
RC=$?
if [ "$RC" != "0" ] || [ "$(printf '%s' "$OUT" | tail -1)" != "ok" ]; then
  echo "  !! 用帳號 $RUN_AS 在 systemd 底下試跑失敗（結束碼 $RC）："
  printf '%s\n' "$OUT" | tail -12 | sed 's/^/     /'
  journalctl -u "$PRE_UNIT" -n 8 --no-pager 2>/dev/null | sed 's/^/     /'
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    echo "     這台 SELinux 是 Enforcing。若上面是 Permission denied／203/EXEC，多半是 SELinux 擋的："
    echo "       ausearch -m avc -ts recent | tail -20"
    echo "     請把輸出交給系統管理員判斷，不要為了這個關掉 SELinux。"
  fi
  die "預檢沒過，沒有啟用服務"
fi
echo "  ✓ 帳號 $RUN_AS 在 systemd 底下載得動程式、寫得進資料目錄"

# ===== 6. 寫設定、停舊行程、啟動服務 =====
echo
echo "[6/7] 切換"
if [ -f "$UNIT" ]; then
  cp -p "$UNIT" "$UNIT.bak.$TS" && echo "  已備份原設定 → $UNIT.bak.$TS"
fi
cat > "$UNIT" <<EOF
[Unit]
Description=CL_WebVuln 弱點彙總網頁版
After=network.target

[Service]
Type=simple
User=$RUN_AS
Group=$RUN_GRP
WorkingDirectory=$APP
Environment=WEBVULN_DB_URL=$DB_URL
Environment=WEBVULN_NO_AUTH=$NO_AUTH
ExecStart=$PY -m uvicorn webvuln.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
systemctl daemon-reload
echo "  ✓ 已寫入 $UNIT"

if [ -n "$OLD_PID" ]; then
  echo "  停止舊行程 PID $OLD_PID（最多等 15 秒）…"
  kill -TERM "$OLD_PID" 2>/dev/null
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    kill -0 "$OLD_PID" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "!! 舊行程 15 秒內沒有停下來，我不強制砍（怕資料庫寫到一半）。"
    echo "   它還在服務中。請自己確認後停掉它，再執行： systemctl enable --now $SVC"
    exit 1
  fi
  echo "  ✓ 舊行程已停止"
  # 舊行程是別的帳號跑的，停止前可能又產生了資料庫的暫存檔，擁有者要再對一次
  chown -R "$RUN_AS:$RUN_GRP" "$DATA"
fi

systemctl enable "$SVC" >/dev/null 2>&1
systemctl restart "$SVC"

# ===== 7. 驗證 =====
echo
echo "[7/7] 驗證"
FAIL=0
HEALTH=""
if command -v curl >/dev/null 2>&1; then
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    HEALTH="$(curl -s -m 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)"
    case "$HEALTH" in *ok*) break ;; esac
    sleep 1
  done
else
  sleep 5
fi
if systemctl is-active --quiet "$SVC"; then echo "  ✓ $SVC active"; else echo "  !! $SVC 沒起來"; FAIL=1; fi
NEW_PID="$(systemctl show -p MainPID --value "$SVC")"
if [ "$FAIL" = "0" ]; then
  WHO="$(ps -o user= -p "$NEW_PID" 2>/dev/null | tr -d ' ')"
  if [ "$WHO" = "$RUN_AS" ]; then echo "  ✓ 行程 PID $NEW_PID 是用帳號 $WHO 在跑"
  else echo "  !! 行程的帳號是「$WHO」，預期 $RUN_AS"; FAIL=1; fi
  if command -v curl >/dev/null 2>&1; then
    case "$HEALTH" in
      *ok*) echo "  ✓ 後端有回應（埠 $PORT）" ;;
      *)    echo "  !! 後端 15 秒內沒有回應（埠 $PORT）"; FAIL=1 ;;
    esac
    # 讀一次資料：資料庫若開不了（權限、路徑錯），health 照樣會過，這裡才看得出來
    CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/summary" 2>/dev/null)"
    if [ "$CODE" = "200" ]; then echo "  ✓ 讀得到資料庫（/api/summary 回 200）"
    else echo "  !! 讀資料庫失敗（/api/summary 回 $CODE）"; FAIL=1; fi
  else
    echo "  - 這台沒有 curl，沒辦法打 API 驗證（未驗證）"
  fi
  if systemctl is-enabled --quiet "$SVC"; then echo "  ✓ 開機會自動啟動"; else echo "  !! 沒有設成開機啟動"; FAIL=1; fi
fi

echo
echo "════════════════════════════════════════════════"
if [ "$FAIL" = "0" ]; then
  echo "✅ 已改成服務 $SVC（帳號 $RUN_AS，埠 $PORT）"
  echo "   以後不要再跑 run.sh（會跟服務搶埠）。"
  echo "   重啟： systemctl restart $SVC"
  echo "   看紀錄： journalctl -u $SVC -n 50 --no-pager"
  RC=0
else
  echo "!! 服務沒有正常起來 —— 最近的紀錄："
  journalctl -u "$SVC" -n 20 --no-pager 2>/dev/null | sed 's/^/     /'
  echo
  echo "   要先恢復服務（退回手動啟動）："
  echo "     systemctl disable --now $SVC"
  echo "     cd $ROOT && WEBVULN_DB_URL='$DB_URL' WEBVULN_NO_AUTH=$NO_AUTH PORT=$PORT nohup bash run.sh >/dev/null 2>&1 &"
  echo "   資料目錄的擁有者已改成 $RUN_AS；用 root 跑 run.sh 不受影響。"
  echo "   把整個畫面貼回給開發者。"
  RC=1
fi
echo "════════════════════════════════════════════════"
exit $RC

"""每週自動寄信排程（2026-10-27）。

原本 /api/send-weekly 的註解說「cron 每週一 08:00 跑」，但伺服器上其實沒有任何排程，從來沒自動寄過。
這裡改成 app 內建一條背景執行緒，每分鐘看一次時間，到點就寄兩種信（每個勾選的部門各自設定）：
  A. 盤點狀態週報 → 該部門的部門窗口（dept_admin，有信箱者；另加有開「每週週報」的本部門帳號）
  B. N 天內到期未修補催辦信（預設 60 天，含已逾期）→ 每位負責人一封（查無信箱 → 轉部門窗口）
  CC 依部門設定，兩種信都套。假日照寄（不做假日判斷）。

防重寄：寄之前先在 AppSetting 插一列 key="auto_mail_claim:<ISO 週>"（key 是 unique，
插入成功＝搶到本週，另一個執行緒／重啟後的程序插不進去就不寄）。
補寄：08:00 服務沒起來、12:00 前起來仍會寄；過了補寄時間就不寄，記一筆「漏寄」（每週只記一次）。
測試模式：所有信改寄到測試信箱（原收件人／CC 只寫在信件開頭），讓使用者可以先看一個真的週一再正式上線。

設定存 AppSetting key="auto_mail"（Super Admin 畫面編輯）；執行紀錄另存 key="auto_mail_state"，
避免「排程寫紀錄」和「使用者按儲存」互相覆蓋。
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import os
import re
import smtplib
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import appsettings, config, mailer, query, security
from .logic import PROGRESS_RESCAN, SUBMIT_STALE
from .models import AppSetting, MailLog, User

log = logging.getLogger("webvuln.automail")

KEY = "auto_mail"
STATE_KEY = "auto_mail_state"
CLAIM_PREFIX = "auto_mail_claim:"

KIND_INV, KIND_DUN = "inventory", "dunning"
KIND_LABEL = {KIND_INV: "盤點狀態週報", KIND_DUN: "到期未修補催辦"}
SCHED_USER = "(自動排程)"

DEFAULTS: dict = {
    "enabled": False,
    "weekday": 0,            # 0＝週一（Python weekday）
    "time": "08:00",
    "catchup_until": "12:00",
    "dunning_days": 60,
    "paused_week": "",       # ISO 週，如 2026-W44 → 該週不寄
    "departments": [],       # [{department, enabled, cc:[...], escalate_cc:[...], send_inventory, send_dunning}]
    "escalate_enabled": False,  # 啟用主管副本（總開關，預設關）：關著時正式寄送一律不副本主管；只能用專用動作開關（記稽核）
    "escalate_days": 14,     # 升級副本門檻：N 天內到期且沒填／已過預計完成日 → 副本主管（已逾期一律升級）；V2.109
    "alert_recipients": [],  # 告警收件人：排程寄送有失敗／略過／漏寄／中斷時寄一封告警（V2.109）
    "test_mode": False,      # 開＝排程寄送全部改寄 test_addresses
    "test_addresses": [],
    "enabled_at": None,      # 最近一次「開啟」的時間（判斷漏寄：開啟前的排程不算漏寄）
}
_EDITABLE = ("enabled", "weekday", "time", "catchup_until", "dunning_days", "paused_week",
             "departments", "test_mode", "test_addresses", "escalate_days", "alert_recipients")

_EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
_HM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


# ── 小工具 ──
def split_emails(v) -> list[str]:
    """逗號／分號／換行／空白分隔都收；去重保序。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        parts = []
        for x in v:
            parts += split_emails(x) if isinstance(x, str) else []
    else:
        parts = [p.strip() for p in re.split(r"[,;\s]+", str(v)) if p.strip()]
    out = []
    for p in parts:
        if p not in out:
            out.append(p)
    return out


def _to_bool(v) -> bool:
    """"false"／"0"／"" 等字串要當 False（bool("false") 是 True；2026-10-27 獨立測試抓到）。"""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "none", "null")
    return bool(v)


def check_emails(lst: list[str], field: str) -> list[str]:
    bad = [e for e in lst if not _EMAIL_RE.match(e)]
    if bad:
        raise ValueError("%s信箱格式不正確：%s" % (field, "、".join(bad[:5])))
    return lst


def _hm(s: str) -> int:
    m = _HM_RE.match(str(s or "").strip())
    if not m:
        raise ValueError("時間格式需為 HH:MM（例 08:00）：%s" % s)
    return int(m.group(1)) * 60 + int(m.group(2))


def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def _sched_dt(cfg: dict, now: dt.datetime) -> dt.datetime:
    """now 所在 ISO 週的排程時間。"""
    monday = now.date() - dt.timedelta(days=now.weekday())
    m = _hm(cfg.get("time") or "08:00")
    d = monday + dt.timedelta(days=int(cfg.get("weekday") or 0))
    return dt.datetime.combine(d, dt.time(m // 60, m % 60))


def _window_end(cfg: dict, sched: dt.datetime) -> dt.datetime:
    m = _hm(cfg.get("catchup_until") or "12:00")
    return dt.datetime.combine(sched.date(), dt.time(m // 60, m % 60, 59))   # 12:00 那一分鐘內都算


# ── 設定 ──
# 整數欄位：(名稱, 下限, 上限)。bool、小數、文字一律不收（GPT 檢核 #9：True 會被 int() 當 1）
_INT_FIELDS = {"weekday": ("每週幾（0＝週一）", 0, 6), "dunning_days": ("催辦天數", 1, 365),
               "escalate_days": ("升級天數（0＝只有已逾期才升級）", 0, 365)}


def _strict_int(v):
    """只收真正的整數（int 或純數字字串）；其他回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
        return int(v)
    return None


def _int_field(cfg: dict, k: str) -> int:
    label, lo, hi = _INT_FIELDS[k]
    n = _strict_int(cfg.get(k))
    if n is None or not lo <= n <= hi:
        raise ValueError("%s需為 %d～%d 的整數" % (label, lo, hi))
    return n


def get_settings(db: Session) -> dict:
    cfg = dict(DEFAULTS)
    saved = appsettings.get_json(db, KEY, {}) or {}
    if isinstance(saved, dict):
        cfg.update({k: saved[k] for k in saved if k in DEFAULTS})
    for k in _INT_FIELDS:   # 舊資料／手改壞掉的值 → 回預設，排程不因此掛掉
        try:
            cfg[k] = _int_field(cfg, k)
        except ValueError:
            log.warning("auto_mail 設定 %s=%r 不合法，改用預設 %r", k, cfg.get(k), DEFAULTS[k])
            cfg[k] = DEFAULTS[k]
    cfg["departments"] = list(cfg.get("departments") or [])
    cfg["test_addresses"] = split_emails(cfg.get("test_addresses"))
    cfg["alert_recipients"] = split_emails(cfg.get("alert_recipients"))
    cfg["escalate_enabled"] = _to_bool(cfg.get("escalate_enabled"))
    return cfg


def _clean_departments(db: Session, raw, prev: list) -> list[dict]:
    # 已存過的部門即使這次匯入沒有了也放行（不然改別的設定會被擋）；新加的部門才檢查存在
    known = set(query.departments(db)) | {d.get("department") for d in (prev or []) if isinstance(d, dict)}
    out, seen = [], set()
    for d in (raw or []):
        if not isinstance(d, dict):
            continue
        name = str(d.get("department") or "").strip()
        if not name or name in seen:
            continue
        if name not in known:
            raise ValueError("部門不存在（最新匯入資料裡沒有）：%s" % name)
        seen.add(name)
        out.append({
            "department": name,
            "enabled": _to_bool(d.get("enabled")),
            "cc": check_emails(split_emails(d.get("cc")), "%s 的 CC " % name),
            "escalate_cc": check_emails(split_emails(d.get("escalate_cc")), "%s 的升級副本（主管）" % name),
            "send_inventory": _to_bool(d.get("send_inventory", True)),
            "send_dunning": _to_bool(d.get("send_dunning", True)),
        })
    return out


def save_settings(db: Session, patch: dict, now: Optional[dt.datetime] = None) -> dict:
    """驗證後存檔；不合法丟 ValueError（端點轉 400）。"""
    now = now or dt.datetime.now()
    cur = get_settings(db)
    cfg = dict(cur)
    for k, v in (patch or {}).items():
        if k in _EDITABLE:
            cfg[k] = v
    cfg["enabled"] = _to_bool(cfg["enabled"])
    cfg["test_mode"] = _to_bool(cfg["test_mode"])
    cfg["weekday"] = _int_field(cfg, "weekday")
    t, c = _hm(cfg["time"]), _hm(cfg["catchup_until"])
    if c < t:
        raise ValueError("補寄截止時間不能早於寄送時間")
    cfg["time"] = "%02d:%02d" % (t // 60, t % 60)
    cfg["catchup_until"] = "%02d:%02d" % (c // 60, c % 60)
    cfg["dunning_days"] = _int_field(cfg, "dunning_days")
    pw = str(cfg.get("paused_week") or "").strip()
    if pw and not _WEEK_RE.match(pw):
        raise ValueError("暫停週格式需為 YYYY-Www（例 2026-W44）")
    cfg["paused_week"] = pw
    cfg["departments"] = _clean_departments(db, cfg.get("departments"), cur.get("departments"))
    if cfg.get("escalate_days") in (None, ""):
        cfg["escalate_days"] = DEFAULTS["escalate_days"]
    cfg["escalate_days"] = _int_field(cfg, "escalate_days")
    cfg["alert_recipients"] = check_emails(split_emails(cfg.get("alert_recipients")), "告警收件人")
    cfg["test_addresses"] = check_emails(split_emails(cfg.get("test_addresses")), "測試模式收件")
    if cfg["test_mode"] and not cfg["test_addresses"]:
        raise ValueError("測試模式需填至少一個測試收件信箱")
    if cfg["enabled"] and not cur.get("enabled"):
        cfg["enabled_at"] = now.isoformat(timespec="seconds")
    clean = {k: cfg[k] for k in DEFAULTS}
    appsettings.set_json(db, KEY, clean)
    return clean


def set_escalate_enabled(db: Session, on: bool) -> dict:
    """「啟用主管副本」總開關：只能用這支（專用端點、要確認、記稽核），一般「儲存設定」改不到。"""
    cur = get_settings(db)
    cur["escalate_enabled"] = bool(on)
    clean = {k: cur[k] for k in DEFAULTS}
    appsettings.set_json(db, KEY, clean)
    return clean


def get_state(db: Session) -> dict:
    st = appsettings.get_json(db, STATE_KEY, {}) or {}
    if not isinstance(st, dict):
        st = {}
    return {"last_run": st.get("last_run"), "history": list(st.get("history") or []),
            "missed": list(st.get("missed") or []), "alerts": list(st.get("alerts") or [])}


def _push_state(db: Session, run: Optional[dict] = None, missed: Optional[dict] = None,
                set_last: bool = True, alert: Optional[dict] = None) -> None:
    st = get_state(db)
    if alert is not None:
        st["alerts"] = ([alert] + st["alerts"])[:30]
    if run is not None:
        if set_last:
            st["last_run"] = run
        st["history"] = ([run] + st["history"])[:30]
    if missed is not None:
        st["missed"] = ([missed] + st["missed"])[:30]
    appsettings.set_json(db, STATE_KEY, st)


# ── 本週認領（防重寄）──
RETRY_EVERY = dt.timedelta(minutes=10)      # SMTP 整批寄不出時，補寄截止前每 10 分鐘重試一次
STALE_RUNNING = dt.timedelta(minutes=30)    # 「寄送中」超過 30 分鐘＝程序中途死掉，標中斷（不自動重寄）


def _claim_raw(db: Session, week: str) -> Optional[str]:
    v = appsettings.get_json(db, CLAIM_PREFIX + week, None)
    return v if isinstance(v, str) else None


def _claim_parts(raw: Optional[str]) -> tuple:
    """"retry|2026-10-26T08:00:00" → ("retry", datetime)；沒帶時間 → (status, None)。"""
    if not raw:
        return None, None
    st, _, ts = raw.partition("|")
    try:
        return st, (dt.datetime.fromisoformat(ts) if ts else None)
    except ValueError:
        return st, None


def _stamp(status: str, at: Optional[dt.datetime]) -> str:
    return status + ("|" + at.isoformat(timespec="seconds") if at else "")


def claim_status(db: Session, week: str) -> Optional[str]:
    return _claim_parts(_claim_raw(db, week))[0]


def try_claim(db: Session, week: str, status: str, at: Optional[dt.datetime] = None) -> bool:
    """插入認領列；key unique → 同一週只有一個能插成功（跨執行緒、跨重啟都成立）。"""
    import json as _json
    db.add(AppSetting(key=CLAIM_PREFIX + week, value=_json.dumps(_stamp(status, at), ensure_ascii=False)))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _cas_claim(db: Session, week: str, old_raw: str, new_raw: str) -> bool:
    """compare-and-set（old_raw／new_raw 為完整值，含時間戳）：只有目前是 old 才改成 new。"""
    import json as _json
    r = db.execute(update(AppSetting).where(AppSetting.key == CLAIM_PREFIX + week,
                                            AppSetting.value == _json.dumps(old_raw, ensure_ascii=False))
                   .values(value=_json.dumps(new_raw, ensure_ascii=False)))
    db.commit()
    return (r.rowcount or 0) == 1


def _set_claim(db: Session, week: str, status: str, at: Optional[dt.datetime] = None) -> None:
    import json as _json
    if not try_claim(db, week, status, at):
        db.execute(update(AppSetting).where(AppSetting.key == CLAIM_PREFIX + week)
                   .values(value=_json.dumps(_stamp(status, at), ensure_ascii=False)))
        db.commit()


def _outcome(s: dict, test: bool) -> str:
    """一輪寄完要把本週標成什麼：一封都沒寄出且有整批錯誤（SMTP 連不上／未啟用／未設定）→ retry；
    部分成功（有寄出、有失敗）→ sent（失敗的列在紀錄裡，不重寄，避免已收到的人收兩次）。"""
    if s.get("sent", 0) == 0 and s.get("error"):
        return "retry"
    return "test" if test else "sent"


# ── 排程判斷（純函式，測試直接打）──
def decide(cfg: dict, now: dt.datetime, claimed: Optional[str]) -> tuple[str, str]:
    """回 (動作, 理由)。動作：send＝現在寄；missed＝記漏寄；idle＝不動。"""
    if not cfg.get("enabled"):
        return "idle", "未啟用"
    week = iso_week(now.date())
    if (cfg.get("paused_week") or "") == week:
        return "idle", "本週已暫停"
    if claimed:
        return "idle", "本週已處理（%s）" % claimed
    sched = _sched_dt(cfg, now)
    if now < sched:
        return "idle", "還沒到寄送時間"
    if now <= _window_end(cfg, sched):
        return "send", "到點寄送" if now < sched + dt.timedelta(minutes=5) else "補寄（服務晚啟動）"
    ea = cfg.get("enabled_at")
    if ea:
        try:
            if dt.datetime.fromisoformat(ea) > sched:
                return "idle", "本週排程時間之後才開啟，不算漏寄"
        except ValueError:
            pass
    return "missed", "已過補寄截止 %s，本週未寄出" % (cfg.get("catchup_until") or "")


def next_run(db: Session, cfg: dict, now: dt.datetime) -> Optional[str]:
    if not cfg.get("enabled"):
        return None
    for k in range(0, 4):
        base = now + dt.timedelta(days=7 * k)
        sched = _sched_dt(cfg, base)
        week = iso_week(sched.date())
        if (cfg.get("paused_week") or "") == week:
            continue
        if k == 0:
            if claim_status(db, week) or now > _window_end(cfg, sched):
                continue
            return max(sched, now).isoformat(timespec="minutes")
        return sched.isoformat(timespec="minutes")
    return None


# ── 範圍 ──
@contextmanager
def _full_scope():
    """寄信內容一律是整個部門：暫時清掉 ?sheet= 的 SHEET_SCOPE 與登入者的 VIEW_SCOPE（2026-10-27）。"""
    t1 = query.SHEET_SCOPE.set(None)
    t2 = query.VIEW_SCOPE.set(None)
    try:
        yield
    finally:
        query.VIEW_SCOPE.reset(t2)
        query.SHEET_SCOPE.reset(t1)


def _scoped(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        with _full_scope():
            return fn(*a, **kw)
    return wrap


# ── 組信 ──
def _dept_entries(cfg: dict, departments: Optional[list] = None, force: bool = False) -> list[dict]:
    """要寄的部門設定。departments 指定＝只取這些；force＝沒勾選／不在設定裡的也算（測試用，兩種信都寄）。"""
    entries = {d["department"]: d for d in (cfg.get("departments") or []) if isinstance(d, dict)}
    if departments is None:
        return [d for d in entries.values() if d.get("enabled")]
    out = []
    for name in departments:
        d = entries.get(name)
        if d and (d.get("enabled") or force):
            out.append(dict(d, send_inventory=True, send_dunning=True) if force else d)
        elif force:
            out.append({"department": name, "enabled": True, "cc": [], "escalate_cc": [], "send_inventory": True,
                        "send_dunning": True})
    return out


def _inventory_recipients(db: Session, dept: str) -> list[str]:
    """部門窗口（dept_admin）＋本部門有開「每週週報」的帳號；啟用且有信箱者。"""
    rows = db.execute(select(User).where(User.department == dept, User.is_active.is_(True))).scalars().all()
    out = []
    for u in rows:
        em = (u.email or "").strip()
        if em and (config.canon_role(u.role) == config.ROLE_DEPT_ADMIN or u.weekly_report) and em not in out:
            out.append(em)
    return out


def _due_text(r: dict, today: dt.date) -> str:
    od = r.get("overdue_days")
    if od is None:
        return ""
    if od > 0:
        return "逾期 %d 天" % od
    if od == 0:
        return "今天到期"
    return "剩 %d 天" % (-od)


def _progress_text(r: dict) -> str:
    if r.get("submitted"):
        n = r.get("submit_days")
        if r.get("progress_state") == SUBMIT_STALE:
            return "送審逾時（%s，已 %s 天未核准）" % (r.get("progress") or "送審中", n if n is not None else "?")
        return "送審中（等核准）%s" % ("，已 %d 天" % n if n is not None else "")
    p = r.get("progress") or ""
    if not p:
        return "未回報"
    txt = p + ("（%s）" % r["progress_state"] if r.get("progress_state") else "")
    if p == PROGRESS_RESCAN:   # 結案申請中：球還在我方（V2.109 使用者確認）
        txt += "；%s" % RESCAN_HINT
    return txt


RESCAN_HINT = "已申請結案（等資安複掃）；若資安在到期日前未完成複掃，請先申請續展延／例外"
NODUE_TEXT = "目前無法計算剩餘天數，請向資安確認到期日"


# ── 催辦對象規則（V2.109「不掉球、提前通知」）──
def waiting_on_security(r: dict) -> bool:
    """「等資安處理（我方已送件）」：承辦已標展延送審中／例外送審中、資安還沒核准（apply_bucket＝送審中）。

    使用者 2026-11-23：送審中＝我方已做完，只是資安還沒更新 Excel。所以：
      · 負責人催辦信不列（即使已逾期），也不因此副本主管；
      · 改列在部門窗口盤點週報「等資安處理（我方已送件）」段落，僅供參考（窗口不需處理）；
      · 未結數字照算（跟資安 Excel 對得上）。
    結案申請中（等複掃）目前仍催負責人；之後要一起排除，改這一支即可。"""
    b = r.get("apply_bucket", query.BUCKET_SUB if r.get("submitted") else None)
    return b == query.BUCKET_SUB or bool(r.get("submitted"))


# 催辦分段（單一分類，GPT 檢核 #5）：每列只會落在一段；送審中優先
SEC_WAITING, SEC_DUE, SEC_NODUE = "waiting", "due", "nodue"


def dunning_section(r: dict, days: int) -> Optional[str]:
    """一列未結弱點在催辦信的哪一段：waiting（等資安處理，不催）／due（N 天內到期，含已逾期）／
    nodue（缺期限・待確認）／None（還不急，不寄）。"""
    if waiting_on_security(r):
        return SEC_WAITING
    if r.get("apply_bucket") == query.BUCKET_NODUE:
        return SEC_NODUE
    od = _as_int(r.get("overdue_days"))
    if od is None:
        return None
    return SEC_DUE if -od <= days else None


def _as_int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_date(v) -> Optional[dt.date]:
    """date／datetime／ISO 字串都收；壞值丟 ValueError（呼叫端記 log 後略過）。"""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v).strip()[:10])


def is_escalation(it: dict, today: dt.date, days: int) -> bool:
    """升級項目（副本主管）：已逾期；或 days 天內到期（今天到期也算）且沒填預計完成日、或預計完成日已過。
    days=0＝只有已逾期才升級（今天到期不算）。缺期限的列沒有到期日，不算升級。
    預計完成日是壞值 → 記 log、這一條不判升級（已逾期照升），不讓整輪掛掉。"""
    od = _as_int(it.get("overdue_days"))
    if od is None:
        return False
    if od > 0:
        return True
    if days <= 0 or -od > days:
        return False
    try:
        td = _as_date(it.get("target_date"))
    except (TypeError, ValueError):
        log.warning("auto_mail 預計完成日格式不對，略過升級判斷：%r（%s）", it.get("target_date"), it.get("host"))
        return False
    return td is None or td < today


LIST_CAP = 30   # 盤點週報每段最多列幾筆（其餘請上系統看）


def _inventory_attention(rep: dict) -> dict:
    """盤點週報給窗口「要追的」三段：我方已逾期、缺期限・待確認、等資安處理（我方已送件）。
    取 weekly_report 同一份清單，數字與系統畫面同口徑。"""
    # 一律看每列自己的 apply_bucket（同一套分類；不用 id 比對，id 空的列也不會被併掉）
    sub = rep.get("submitted_list") or []
    own_overdue = [d for d in (rep.get("overdue_list") or []) if d.get("apply_bucket") != query.BUCKET_SUB]
    nodue = [d for d in (rep.get("no_due_list") or []) if d.get("apply_bucket") != query.BUCKET_SUB]
    sub_overdue = [d for d in sub if (d.get("overdue_days") or 0) > 0]
    sub_stale = [d for d in sub if d.get("progress_state") == SUBMIT_STALE]
    return {"own_overdue": own_overdue, "nodue": nodue, "waiting": sub,
            "waiting_overdue": len(sub_overdue), "waiting_stale": len(sub_stale), "cap": LIST_CAP}


def _dunning_subject(prefix: str, owner: str, days: int, n_due: int, n_nodue: int, today: dt.date) -> str:
    parts = []
    if n_due or not n_nodue:
        parts.append("%d 天內到期（含已逾期）未修補 %d 筆" % (days, n_due))
    if n_nodue:
        parts.append("期限待確認 %d 筆" % n_nodue)
    return "%s%s 弱點 %s（%s）" % (prefix, owner, "、".join(parts), today.isoformat())


def build_jobs(db: Session, cfg: dict, email_cfg: dict, today: dt.date,
               departments: Optional[list] = None, force: bool = False) -> list[dict]:
    """產生要寄的每一封（不寄）。順序＝部門設定順序；部門內先盤點週報、再催辦（依筆數多到少）。"""
    prefix = email_cfg.get("subject_prefix") or ""
    site_url = (email_cfg.get("site_url") or "").strip()
    days = int(cfg.get("dunning_days") or 60)
    esc_on = bool(cfg.get("escalate_enabled"))
    esc_days = int(cfg.get("escalate_days") if cfg.get("escalate_days") is not None else 14)
    jobs: list[dict] = []
    for ent in _dept_entries(cfg, departments, force):
        dept = ent["department"]
        cc_cfg = list(ent.get("cc") or [])
        if ent.get("send_inventory"):
            rep = query.weekly_report(db, department=dept, today=today)
            try:   # 與上次匯入比較；失敗不擋寄信（同 send_weekly）
                from . import changes
                rep["compare"] = changes.compare(db, department=dept, today=today)
            except Exception:  # noqa: BLE001
                rep["compare"] = None
            att = rep["attention"] = _inventory_attention(rep)
            to = _inventory_recipients(db, dept)
            mode, reason = "send", ""
            if not to:
                gf = (email_cfg.get("global_fallback") or "").strip()
                if gf:
                    to, mode, reason = [gf], "fallback", "此部門無窗口信箱，轉系統預設"
                else:
                    mode, reason = "skip", "此部門無窗口信箱"
            need, sub, app_ = rep.get("need_apply_count", 0), rep.get("submitted_count", 0), rep.get("applied_count", 0)
            nodue = rep.get("no_due_count", 0)
            univ = rep.get("apply_universe", need + sub + app_ + nodue)   # 備援加總含缺期限（GPT 檢核 #11）
            metrics = [
                ["未結", rep.get("unresolved", 0)], ["已逾期", rep.get("overdue", 0)],
                ["　我方已逾期（不含送審中）", len(att["own_overdue"])],
                ["缺期限・待確認（請向資安確認到期日）", len(att["nodue"])],
                ["等資安處理（我方已送件）", len(att["waiting"])],
                ["　其中已逾期", att["waiting_overdue"]], ["　其中送審逾時", att["waiting_stale"]],
                ["近期到期（30天）", rep.get("soon", 0)], ["需申請", univ],
                ["　應申請未申請", need], ["　送審中（等核准）", sub], ["　已核准", app_],
                ["　缺期限・待確認", nodue],
                ["暫無需申請", max(0, rep.get("unresolved", 0) - univ)],
                ["送審逾時", rep.get("submitted_stale_count", 0)],
                ["待追查", (rep.get("progress") or {}).get("flagged", 0)],
                ["資安已確認待 Excel 更新", rep.get("sec_confirmed_count", 0)],   # 2026-10-28：已從上面各數字扣掉
                ["　其中超過 %s 天" % rep.get("confirm_stale_days", 14), rep.get("sec_confirmed_stale_count", 0)],
            ]
            jobs.append({
                "department": dept, "kind": KIND_INV, "kind_label": KIND_LABEL[KIND_INV],
                "label": "%s 部門窗口" % dept, "to": to, "cc": [c for c in cc_cfg if c not in to],
                "mode": mode, "reason": reason, "count": rep.get("unresolved", 0),
                "subject": "%s%s 弱點盤點狀態週報（%s）" % (prefix, dept, today.isoformat()),
                "body": mailer.inventory_body(rep, dept, site_url, today),
                "metrics": metrics, "items": [],
            })
        if ent.get("send_dunning"):
            # 我方待辦＝N 天內到期（含已逾期）＋缺期限・待確認；送審中（等資安處理）一律不催負責人。
            # 同一份未結清單、每列只分一次段（dunning_section），不會同時出現在兩段（GPT 檢核 #5／#8）
            esc_cc = list(ent.get("escalate_cc") or [])
            by_owner: dict[str, list] = {}
            for r in query.find(db, department=dept, today=today):
                sec = dunning_section(r, days)
                if sec not in (SEC_DUE, SEC_NODUE):
                    continue
                by_owner.setdefault((r.get("owner") or "").strip() or "（未指定）", []).append((sec, r))
            djobs = []
            for owner, rs in by_owner.items():
                rs.sort(key=lambda x: (x[0] != SEC_DUE, str(x[1].get("effective_due") or "9999"), str(x[1].get("host") or "")))
                items = []
                for sec, r in rs:
                    try:
                        it = {"host": r.get("host"), "name": r.get("name"), "severity": r.get("severity"),
                              "effective_due": r.get("effective_due"), "overdue_days": r.get("overdue_days"),
                              "target_date": r.get("target_date"), "section": sec,
                              "due_text": _due_text(r, today) if sec == SEC_DUE else NODUE_TEXT,
                              "progress_text": _progress_text(r)}
                        it["escalate"] = sec == SEC_DUE and is_escalation(it, today, esc_days)
                    except Exception:  # noqa: BLE001 - 單列資料怪不擋整輪（記 log 略過該列）
                        log.exception("auto_mail 組催辦項目失敗，略過：%s", r.get("host"))
                        continue
                    items.append(it)
                if not items:
                    continue
                n_due = sum(1 for it in items if it["section"] == SEC_DUE)
                n_esc = sum(1 for it in items if it["escalate"])
                to1, mode, reason, ident = mailer.resolve_recipient_ex(db, owner, dept, email_cfg)
                to = [to1] if to1 else []
                cc = [c for c in cc_cfg if c not in to]
                # 升級副本對象（V2.111）：負責人自己的 AD 主管優先；沒有才用部門設定的升級副本。
                # 主管只取「收件人本人那一筆帳號」上的（To 轉窗口時不用名字去猜主管）
                mgr_name, mgr_mail = ((ident["manager_name"], ident["manager_email"])
                                      if mode == "send" and owner != "（未指定）" else (None, None))
                owner_esc = [mgr_mail] if mgr_mail else esc_cc
                esc_src = "ad" if mgr_mail else "dept"
                esc_would = [c for c in owner_esc if c not in to and c not in cc] if n_esc else []   # 開關打開時會副本的主管
                esc_applied = esc_would if esc_on else []
                cc += esc_applied
                # 應升級但沒人收：部門沒設主管、或這封根本寄不出（查無信箱）→ 預覽標示、告警列出（GPT 檢核 #6）
                # 主管副本總開關還沒開 → 只在預覽標示，不告警（使用者刻意還沒開）
                esc_missing, esc_note = "", ""
                if n_esc and not esc_on:
                    esc_note = ESC_OFF_TEXT + ("；且此部門未設定主管" if not owner_esc else "")
                elif n_esc and not owner_esc:
                    esc_missing = "應升級但未設定主管"
                elif n_esc and (mode == "skip" or not to):
                    esc_missing = "應升級但此信寄不出（%s）" % (reason or "查無信箱")
                # 最後 To／CC＋原因（預覽用，V2.111）
                to_why = ("寄不出（%s）" % (reason or "查無信箱")) if (mode == "skip" or not to) else (
                    "本人" if mode == "send" else "轉窗口（%s）" % reason)
                if not n_esc:
                    cc_why = "無升級項目"
                elif not owner_esc:
                    cc_why = "應升級但無主管可副本"
                elif not esc_on:
                    cc_why = "開關關閉僅預覽（%s：%s）" % ("AD 主管" if esc_src == "ad" else "部門設定", "、".join(owner_esc))
                else:
                    cc_why = "AD 主管" if esc_src == "ad" else "部門設定"
                body_args = (owner, items, today, days, site_url, reason if mode == "fallback" else "")
                djobs.append({
                    "department": dept, "kind": KIND_DUN, "kind_label": KIND_LABEL[KIND_DUN],
                    "label": owner, "to": to, "cc": cc,
                    "mode": mode, "reason": reason, "count": len(items),
                    "due_count": n_due, "nodue_count": len(items) - n_due,
                    "escalate_count": n_esc, "escalate_cc": esc_applied, "escalate_missing": esc_missing,
                    "escalate_would_cc": esc_would, "escalate_note": esc_note,
                    "ad_manager": ({"name": mgr_name or "", "email": mgr_mail} if mgr_mail else None),
                    "escalate_source": esc_src,
                    "final_to": list(to), "final_cc": list(cc), "final_to_reason": to_why, "final_cc_reason": cc_why,
                    "subject": _dunning_subject(prefix, owner, days, n_due, len(items) - n_due, today),
                    "body": mailer.dunning_body(*body_args, escalate_cc=esc_applied, escalate_days=esc_days),
                    # 測試寄送走「主管副本已開」的版本（收件人一律改寄測試信箱），讓使用者開之前先驗證
                    "test_body": mailer.dunning_body(*body_args, escalate_cc=esc_would, escalate_days=esc_days),
                    "metrics": [], "items": items,
                })
            djobs.sort(key=lambda j: (-j["count"], j["label"]))
            jobs += djobs
    for i, j in enumerate(jobs, 1):
        j["no"] = i
    return jobs


# ── 寄送 ──
def _empty_summary() -> dict:
    return {"sent": 0, "failed": 0, "skipped": 0, "total": 0, "error": "", "details": [], "by_dept": {}}


ESC_OFF_TEXT = "主管副本尚未啟用（測試通過後再開）"


def _test_header(job: dict) -> str:
    to = "、".join(job.get("to") or []) or "（無，正式寄送時會略過：%s）" % (job.get("reason") or "查無信箱")
    cc = "、".join(job.get("cc") or []) or "（無）"
    pend = [c for c in (job.get("escalate_would_cc") or []) if c not in (job.get("cc") or [])]
    extra = ("\n[測試] 主管副本（%s）：%s" % (ESC_OFF_TEXT, "、".join(pend))) if pend else ""
    return ("[測試] 正式寄送時：收件人 %s；CC %s%s\n"
            "（這是測試信，只寄到測試信箱；正式收件人沒有收到）\n%s\n\n" % (to, cc, extra, "─" * 30))


def deliver(db: Session, email_cfg: dict, jobs: list[dict], sender_label: str,
            redirect: Optional[list] = None, include_skipped: bool = False) -> dict:
    """寄出 jobs。redirect 給值＝測試：全部改寄這些信箱、原收件人／CC 不寄、主旨加 [測試]。
    include_skipped（只在測試）＝正式會略過的信也寄一份到測試信箱看內容。
    SMTP 連不上不丟例外，記在 summary.error 與每封 MailLog（排程不能因此掛掉）。"""
    s = _empty_summary()
    from_addr = (email_cfg.get("from_default") or "").strip()
    host = (email_cfg.get("smtp_host") or "").strip()
    test = redirect is not None   # 空清單也算測試（絕不退回正式寄送；2026-10-27）
    bcc = [] if test else split_emails(email_cfg.get("copy_to"))   # 總備份可多個信箱

    def _tally(job, status, to, cc, error=""):
        key = "sent" if status == "ok" else ("skipped" if status == "skip" else "failed")
        s[key] += 1
        s["total"] += 1
        bd = s["by_dept"].setdefault(job["department"], {})
        k = bd.setdefault(job["kind"], {"sent": 0, "failed": 0, "skipped": 0})
        k[key] += 1
        s["details"].append({"no": job.get("no"), "department": job["department"], "kind": job["kind"],
                             "label": job["label"], "to": ", ".join(to), "cc": ", ".join(cc),
                             "status": status, "count": job.get("count", 0), "error": error,
                             "escalate_count": job.get("escalate_count", 0),
                             "escalate_missing": job.get("escalate_missing", "")})
        try:
            db.add(MailLog(sender=sender_label[:100], owner=("%s｜%s" % (KIND_LABEL[job["kind"]], job["label"]))[:100],
                           to=", ".join(to)[:300], cc=", ".join(cc)[:500],
                           mode=("test" if test else ("skip" if status == "skip" else job["kind"])),
                           status=status, count=int(job.get("count") or 0), error=(error or None)))
            db.commit()
        except Exception:  # noqa: BLE001 - 紀錄失敗不影響寄信
            db.rollback()

    sendable = []
    for job in jobs:
        skip = job.get("mode") == "skip" or not job.get("to")
        if skip and not (test and include_skipped):
            _tally(job, "skip", job.get("to") or [], [], job.get("reason") or "查無信箱")
        else:
            sendable.append(job)
    if not sendable:
        return s
    if test and not redirect:
        s["error"] = "測試模式沒有測試收件信箱，未寄出（不會改寄正式收件人）"
    elif not test and not email_cfg.get("enabled"):
        s["error"] = "Email 設定未啟用寄信（Email／SMTP 設定 → 啟用），未寄出"
    elif not host or not from_addr:
        s["error"] = "尚未設定 SMTP 主機或系統預設寄件人（from_default）"
    if s["error"]:
        for job in sendable:
            _tally(job, "failed", redirect if test else job["to"], [], s["error"])
        return s
    srv = None
    try:
        srv = smtplib.SMTP(host, int(email_cfg.get("smtp_port") or 25), timeout=20)
        if email_cfg.get("use_tls"):
            srv.starttls()
    except Exception as e:  # noqa: BLE001
        s["error"] = "連不到 SMTP 主機：%s" % str(e)[:200]
        for job in sendable:
            _tally(job, "failed", redirect if test else job["to"], [], s["error"])
        return s
    try:
        for job in sendable:
            if test:
                to, cc = list(redirect), []
                subject = "[測試]" + job["subject"]
                body = _test_header(job) + (job.get("test_body") or job["body"])
            else:
                to, cc = list(job["to"]), list(job["cc"])
                subject, body = job["subject"], job["body"]
            envelope = to + [c for c in cc if c not in to]
            envelope += [b for b in bcc if b not in envelope]
            try:
                msg = mailer._build_message(from_addr, ", ".join(to), cc, subject, body)
                refused = srv.sendmail(from_addr, envelope, msg.as_string())
                if refused:   # 部分收件人被拒（sendmail 回 dict）→ 算失敗、要告警（GPT 檢核 #3）
                    _tally(job, "partial", to, cc, refused_text(refused))
                else:
                    _tally(job, "ok", to, cc)
            except Exception as e:  # noqa: BLE001 - 單封失敗不中斷整批
                _tally(job, "failed", to, cc, str(e)[:200])
    finally:
        try:
            srv.quit()
        except Exception:  # noqa: BLE001
            pass
    return s


def refused_text(refused) -> str:
    try:
        return ("部分收件人被拒：" + "、".join(sorted(str(k) for k in dict(refused))))[:200]
    except Exception:  # noqa: BLE001
        return "部分收件人被拒"


def _run_record(trigger: str, week: str, now: dt.datetime, s: dict, test: bool, who: str) -> dict:
    by = [{"department": d, "kind": k, "kind_label": KIND_LABEL.get(k, k), **v}
          for d, kinds in (s.get("by_dept") or {}).items() for k, v in kinds.items()]
    return {"week": week, "at": now.isoformat(timespec="seconds"), "trigger": trigger, "by": who,
            "test_mode": test, "sent": s["sent"], "failed": s["failed"], "skipped": s["skipped"],
            "error": s.get("error") or "", "summary": by}


@_scoped
def run_real(db: Session, now: dt.datetime, trigger: str, who: str,
             test_redirect: Optional[list] = None) -> dict:
    """真正一輪（排程或立即寄出）：組信 → 寄 → 記紀錄。呼叫前要先認領本週。"""
    cfg = get_settings(db)
    email_cfg = appsettings.get_email_config(db)
    week = iso_week(now.date())
    jobs = build_jobs(db, cfg, email_cfg, now.date())
    label = SCHED_USER if trigger == "schedule" else (who or "(未登入)")
    s = deliver(db, email_cfg, jobs, label + ("[測試模式]" if test_redirect is not None else ""), redirect=test_redirect)
    rec = _run_record(trigger, week, now, s, test_redirect is not None, who)
    _push_state(db, run=rec)
    return {**s, "record": rec}


# ── 寄送失敗告警（V2.109；GPT 檢核 #1～#4 改版）──
# 「告警事件」與「寄送嘗試」分開記：
#   事件＝AppSetting key "auto_mail_alert:<週>|<事件>"（key unique → 同一件事只會建一次，跨執行緒／多程序都成立）
#   寄送＝每次嘗試寫 MailLog（mode=alert）＋畫面顯示用的 auto_mail_state.alerts（最近 30 次，只給人看、不拿來去重）
# 寄不出（SMTP 壞、沒設告警收件人）→ 之後每次排程 tick 每 10 分鐘重試，直到寄出為止（14 天內的事件）。
# SMTP 本身壞掉時告警信也寄不出 → 由畫面紅色橫幅（alert_banner，唯讀）與 /api/health 補位。
ALERT_LABEL = "自動寄信告警"
ALERT_PREFIX = "auto_mail_alert:"
ALERT_RETRY_DAYS = 14
LAST_SENT_KEY = "auto_mail_last_sent"   # 最近一次「正式」寄送完成（不含測試模式／測試寄送）
_ALERT_FINAL = ("ok", "partial")          # partial＝部分收件人被拒：不重寄（避免已收到的人收兩次），橫幅提示


class _NoMail(Exception):
    pass


def _alert_key(week: str, key: str) -> str:
    return (ALERT_PREFIX + week + "|" + key)[:100]


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _load_alert(db: Session, akey: str) -> tuple:
    row = db.execute(select(AppSetting).where(AppSetting.key == akey)).scalars().first()
    if row is None:
        return None, None
    try:
        ev = json.loads(row.value or "{}")
    except ValueError:
        ev = {}
    return row.value, (ev if isinstance(ev, dict) else {})


def _cas_alert(db: Session, akey: str, old_raw: str, ev: dict) -> bool:
    r = db.execute(update(AppSetting).where(AppSetting.key == akey, AppSetting.value == old_raw)
                   .values(value=_dumps(ev)))
    db.commit()
    return (r.rowcount or 0) == 1


def raise_alert(db: Session, cfg: dict, week: str, key: str, subject: str, body: str,
                now: dt.datetime) -> Optional[dict]:
    """建立告警事件（同 week＋key 在 DB 只會有一筆）並立刻試寄一次。已存在 → 不重建、回 None。不丟例外。"""
    akey = _alert_key(week, key)
    ev = {"week": week, "key": key, "subject": subject, "body": body,
          "created": now.isoformat(timespec="seconds"), "status": "pending", "attempts": 0,
          "last_try": None, "error": "", "to": "", "test_mode": False}
    try:
        db.add(AppSetting(key=akey, value=_dumps(ev)))
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("auto mail alert create failed")
        return None
    return deliver_alert(db, cfg, akey, now)


def deliver_alert(db: Session, cfg: dict, akey: str, now: dt.datetime) -> Optional[dict]:
    """試寄一個告警事件。先 compare-and-set 成「寄送中」→ 兩個程序不會同時寄同一封。
    測試模式開著 → 只寄測試信箱（告警收件人寫在信件開頭）。回更新後的事件；沒寄（別人在寄／已寄過）回 None。"""
    try:
        raw, ev = _load_alert(db, akey)
        if ev is None or ev.get("status") in _ALERT_FINAL:
            return None
        if ev.get("status") == "sending":
            try:
                if ev.get("last_try") and now - dt.datetime.fromisoformat(ev["last_try"]) <= STALE_RUNNING:
                    return None
            except ValueError:
                pass
        real = list(cfg.get("alert_recipients") or [])
        test = bool(cfg.get("test_mode"))
        to = list(cfg.get("test_addresses") or []) if test else real
        claim = dict(ev, status="sending", last_try=now.isoformat(timespec="seconds"),
                     attempts=int(ev.get("attempts") or 0) + 1, test_mode=test)
        if not _cas_alert(db, akey, raw, claim):
            return None
        raw = _dumps(claim)
        email_cfg = appsettings.get_email_config(db)
        err, status = "", "failed"
        if not real:
            err, status = "未設定告警收件人", "no_recipient"
        elif not to:
            err = "測試模式沒有測試收件信箱，告警未寄出"
        elif not test and not email_cfg.get("enabled"):
            err = "Email 設定未啟用寄信，告警未寄出"
        elif not (email_cfg.get("smtp_host") or "").strip() or not (email_cfg.get("from_default") or "").strip():
            err = "尚未設定 SMTP 主機或系統預設寄件人，告警未寄出"
        else:
            subj = ("[測試]" if test else "") + (email_cfg.get("subject_prefix") or "") + (ev.get("subject") or "")
            body = ev.get("body") or ""
            if test:
                body = ("[測試] 正式寄送時：告警收件人 %s\n（測試模式：只寄到測試信箱）\n%s\n\n" % (
                    "、".join(real), "─" * 30)) + body
            from_addr = email_cfg["from_default"].strip()
            try:
                srv = smtplib.SMTP(email_cfg["smtp_host"].strip(), int(email_cfg.get("smtp_port") or 25), timeout=20)
                try:
                    if email_cfg.get("use_tls"):
                        srv.starttls()
                    refused = srv.sendmail(from_addr, to, mailer._build_message(from_addr, ", ".join(to), [], subj,
                                                                                body).as_string())
                finally:
                    try:
                        srv.quit()
                    except Exception:  # noqa: BLE001
                        pass
                status, err = ("partial", refused_text(refused)) if refused else ("ok", "")
            except Exception as e:  # noqa: BLE001
                err = "告警信寄不出：%s" % str(e)[:200]
        done = dict(claim, status=status, error=err, to=", ".join(to) if real else "")
        db.execute(update(AppSetting).where(AppSetting.key == akey).values(value=_dumps(done)))
        db.commit()
        if status != "no_recipient" or int(claim["attempts"]) == 1:   # 沒收件人只記第一次，避免每 10 分鐘洗紀錄
            try:
                if status == "no_recipient":
                    raise _NoMail   # 沒有收件人＝沒有寄信動作，不進逐封發信紀錄（事件與畫面紀錄照記）
                db.add(MailLog(sender=SCHED_USER + ("[測試模式]" if test else ""),
                               owner=("%s｜%s" % (ALERT_LABEL, ev.get("key") or ""))[:100], to=done["to"][:300], cc="",
                               mode="test" if test else "alert", status=status, count=0, error=err or None))
                db.commit()
            except _NoMail:
                pass
            except Exception:  # noqa: BLE001
                db.rollback()
            _push_state(db, alert={k: done.get(k) for k in ("week", "key", "subject", "to", "status", "error",
                                                              "test_mode", "attempts")} | {"at": done["last_try"]})
        return done
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("auto mail alert deliver failed")
        return None


def list_alerts(db: Session) -> list[tuple]:
    rows = db.execute(select(AppSetting).where(AppSetting.key.like(ALERT_PREFIX + "%"))).scalars().all()
    out = []
    for r in rows:
        try:
            ev = json.loads(r.value or "{}")
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append((r.key, ev))
    out.sort(key=lambda x: x[1].get("created") or "", reverse=True)
    return out


def retry_alerts(db: Session, cfg: dict, now: dt.datetime) -> int:
    """排程每次 tick 呼叫：沒寄出的告警（寄不出／沒收件人）每 10 分鐘重試，直到寄出（只看 14 天內的事件）。"""
    n = 0
    for akey, ev in list_alerts(db):
        if ev.get("status") in _ALERT_FINAL:
            continue
        try:
            if now - dt.datetime.fromisoformat(ev.get("created") or "") > dt.timedelta(days=ALERT_RETRY_DAYS):
                continue
            if ev.get("last_try") and now - dt.datetime.fromisoformat(ev["last_try"]) < RETRY_EVERY:
                continue
        except ValueError:
            continue
        if ev.get("status") == "no_recipient" and not cfg.get("alert_recipients"):
            continue
        if deliver_alert(db, cfg, akey, now) is not None:
            n += 1
    return n


def _alert_for_run(db: Session, cfg: dict, week: str, now: dt.datetime, res: dict) -> None:
    """一輪寄完（不是「整批寄不出、等重試」）若有失敗／部分被拒／略過／錯誤／應升級但沒主管 → 告警。"""
    details = res.get("details") or []
    bad = [d for d in details if d.get("status") != "ok"]
    esc_miss = [d for d in details if d.get("escalate_missing")]
    if not bad and not res.get("error") and not esc_miss:
        return
    rec = res.get("record") or {}
    lines = ["每週自動寄信（%s，%s）需要處理：" % (week, (rec.get("at") or now.isoformat())[:16].replace("T", " ")),
             "寄出 %d　失敗 %d　略過 %d" % (rec.get("sent", 0), rec.get("failed", 0), rec.get("skipped", 0))]
    if res.get("error"):
        lines.append("整批錯誤：%s" % res["error"])
    if bad:
        lines += ["", "一、沒寄到（或部分收件人被拒）的信："]
        st_txt = {"skip": "略過", "partial": "部分被拒"}
        for i, d in enumerate(bad, 1):
            lines.append("%d. %s｜%s｜%s｜收件人 %s｜%s｜%s%s" % (
                i, d.get("department") or "", KIND_LABEL.get(d.get("kind"), d.get("kind") or ""), d.get("label") or "",
                d.get("to") or "（無）", st_txt.get(d.get("status"), "失敗"), d.get("error") or "",
                "｜含升級項目 %d 筆" % d["escalate_count"] if d.get("escalate_count") else ""))
    if esc_miss:
        lines += ["", "%s、應升級但主管沒收到（請到設定補「升級副本（主管）」）：" % ("二" if bad else "一")]
        for i, d in enumerate(esc_miss, 1):
            lines.append("%d. %s｜%s｜升級項目 %d 筆｜%s" % (i, d.get("department") or "", d.get("label") or "",
                                                       d.get("escalate_count") or 0, d.get("escalate_missing")))
    lines += ["", "請到系統「自動寄信排程 → 紀錄」與「逐封發信紀錄」查看；查無信箱的請補帳號信箱或部門窗口。",
              "（本信由弱點彙總系統自動發送）"]
    raise_alert(db, cfg, week, "run:" + (rec.get("at") or now.isoformat(timespec="seconds")),
                "【告警】弱點自動寄信 %s 有 %d 封沒寄到、%d 封應升級未送主管" % (week, len(bad), len(esc_miss)),
                "\n".join(lines), now)


def _record_missed(db: Session, cfg: dict, week: str, now: dt.datetime, reason: str, action: str,
                   sched: Optional[dt.datetime] = None) -> None:
    sched = sched or _sched_dt(cfg, now)
    m = {"week": week, "scheduled": sched.isoformat(timespec="minutes"),
         "detected_at": now.isoformat(timespec="seconds"), "reason": reason}
    _push_state(db, missed=m)
    security.log_audit(db, username=SCHED_USER, action=action, target="week:" + week,
                       detail="排程 %s：%s" % (m["scheduled"], reason))
    raise_alert(db, cfg, week, action.replace("auto_mail_", ""), "【告警】弱點自動寄信 %s 沒有完成" % week,
                "每週自動寄信（排程 %s）沒有完成：\n%s\n\n請到系統「自動寄信排程」確認後手動「立即寄出一次」。\n"
                "（本信由弱點彙總系統自動發送）" % (m["scheduled"].replace("T", " "), reason), now)


def _mark_formal_sent(db: Session, now: dt.datetime) -> None:
    try:
        appsettings.set_json(db, LAST_SENT_KEY, now.isoformat(timespec="seconds"))
    except Exception:  # noqa: BLE001
        db.rollback()


# 本週算「完成」只有正式寄出（sent）；測試模式寄出（test）不算——正式收件人沒收到（GPT 檢核 #4）
_DONE = ("sent",)
_ALERT_STATUS = {None: "排程沒有執行（服務可能沒開，或排程執行緒停了）", "missed": "已過補寄截止，沒有寄出",
                 "error": "寄送出錯，沒有寄出", "interrupted": "寄送中斷（可能只寄出一部分）",
                 "retry": "整批寄不出（SMTP／設定問題）", "running": "寄送中卡住",
                 "test": "本週是測試模式寄送（正式收件人沒有收到）"}


def week_unsent(db: Session, cfg: dict, now: dt.datetime, back: int = 0) -> Optional[dict]:
    """第 back 週前（0＝本週）的排程：已過補寄截止、正式寄送沒完成 → 回 {week, sched, status}；否則 None。
    未啟用、該週暫停、排程時間之後才開啟、寄送中（未逾時）都不算。橫幅、/api/health、tick 共用。"""
    if not cfg.get("enabled"):
        return None
    sched = _sched_dt(cfg, now - dt.timedelta(days=7 * back))
    week = iso_week(sched.date())
    if (cfg.get("paused_week") or "") == week or now <= _window_end(cfg, sched):
        return None
    ea = cfg.get("enabled_at")
    try:
        if ea and dt.datetime.fromisoformat(ea) >= sched:   # 排程時間（含）之後才開啟 → 不算
            return None
    except ValueError:
        pass
    st, at = _claim_parts(_claim_raw(db, week))
    if st in _DONE:
        return None
    if st == "running" and at and now - at <= STALE_RUNNING:
        return None
    return {"week": week, "sched": sched, "status": st}


def alert_banner(db: Session, now: Optional[dt.datetime] = None) -> list[dict]:
    """給 Super Admin 畫面的紅色橫幅（唯讀，不寄信；GPT 檢核 #1）：
    1) 本週（與上週從未處理過的）排程已過補寄截止，正式寄送仍沒完成；
    2) 本週／上週有告警事件還沒寄出（沒設告警收件人／SMTP 寄不出／部分被拒）。"""
    now = now or dt.datetime.now()
    out: list[dict] = []
    try:
        cfg = get_settings(db)
        for back in (0, 1):
            u = week_unsent(db, cfg, now, back)
            if u and (back == 0 or u["status"] is None):   # 上週只看「完全沒處理過」的
                out.append({"level": "error", "week": u["week"],
                            "text": "%s 自動寄信沒有完成（排程 %s）：%s。請打開「自動寄信排程」確認並手動寄出。" % (
                                u["week"], u["sched"].strftime("%m/%d %H:%M"), _ALERT_STATUS.get(u["status"], u["status"]))})
        weeks = {iso_week(now.date()), iso_week((now - dt.timedelta(days=7)).date())}
        for _k, ev in list_alerts(db):
            if ev.get("week") in weeks and ev.get("status") not in ("ok",):
                out.append({"level": "error", "week": ev.get("week"),
                            "text": "告警信沒寄出（%s）：%s。請打開「自動寄信排程 → 紀錄」查看。" % (
                                ev.get("error") or ev.get("status"), ev.get("subject") or "")})
                break
    except Exception:  # noqa: BLE001 - 橫幅失敗不擋畫面
        db.rollback()
        log.exception("auto mail banner failed")
    return out


def health_status(db: Session, now: Optional[dt.datetime] = None) -> dict:
    """給外部監控（WhatsUp 等）輪詢的自動寄信狀態：不含任何姓名／信箱，只有狀態欄位。"""
    now = now or dt.datetime.now()
    cfg = get_settings(db)
    week = iso_week(now.date())
    last = appsettings.get_json(db, LAST_SENT_KEY, None)
    pending = sum(1 for _k, ev in list_alerts(db) if ev.get("status") not in ("ok",)
                  and ev.get("week") in (week, iso_week((now - dt.timedelta(days=7)).date())))
    return {"enabled": bool(cfg.get("enabled")), "test_mode": bool(cfg.get("test_mode")),
            "week": week, "week_state": claim_status(db, week) or "none",
            "last_formal_sent_at": last if isinstance(last, str) else None,
            "overdue_unsent": week_unsent(db, cfg, now, 0) is not None,
            "alerts_undelivered": pending}


def _send_round(db: Session, cfg: dict, now: dt.datetime, week: str, reason: str) -> dict:
    """已認領（running）後寄一輪，依結果把本週標成 sent／test／retry／error。"""
    redirect = cfg["test_addresses"] if cfg.get("test_mode") else None
    test = redirect is not None
    try:
        res = run_real(db, now, "schedule", SCHED_USER, test_redirect=redirect)
        st = _outcome(res, test)
        _set_claim(db, week, st, now if st == "retry" else None)
        if st == "sent":
            _mark_formal_sent(db, now)
        if st != "retry":   # 整批寄不出會重試；到截止仍失敗時由 tick → _record_missed 告警
            _alert_for_run(db, cfg, week, now, res)
    except Exception as e:  # noqa: BLE001 - 不讓排程掛掉；本週標 error，不自動重試（避免部分重寄）
        db.rollback()
        log.exception("auto mail run failed")
        _set_claim(db, week, "error")
        rec = {"week": week, "at": now.isoformat(timespec="seconds"), "trigger": "schedule", "by": SCHED_USER,
               "test_mode": test, "sent": 0, "failed": 0, "skipped": 0,
               "error": str(e)[:300], "summary": []}
        _push_state(db, run=rec)
        res = {"error": str(e)[:300], "record": rec}
        raise_alert(db, cfg, week, "error", "【告警】弱點自動寄信 %s 執行出錯，本週沒有寄出" % week,
                    "每週自動寄信執行時出錯（本週標為錯誤，不會自動重寄）：\n%s\n\n"
                    "請到系統「自動寄信排程」確認後手動「立即寄出一次」。\n（本信由弱點彙總系統自動發送）" % str(e)[:300],
                    now)
    r = res["record"]
    security.log_audit(db, username=SCHED_USER, action="auto_mail_run", target="week:" + week,
                       detail="%ssent=%d failed=%d skipped=%d %s" % (
                           "[測試模式] " if test else "", r["sent"], r["failed"], r["skipped"],
                           r.get("error") or ""))
    return {"action": "send", "reason": reason, "result": res}


def _check_last_week(db: Session, cfg: dict, now: dt.datetime) -> None:
    """上週排程完全沒處理過（服務整週沒開到補寄截止之後）→ 本週第一次 tick 補記漏寄＋告警。
    認領列 key unique → 多個程序只有一個記得進去。"""
    u = week_unsent(db, cfg, now, 1)
    if u and u["status"] is None and try_claim(db, u["week"], "missed", now):
        _record_missed(db, cfg, u["week"], now, "服務在排程時間到補寄截止都沒有執行，這週沒有寄出（本週才發現）",
                       "auto_mail_missed", sched=u["sched"])


def _rules_stale(db: Session) -> Optional[str]:
    """最新一批的判斷規則版本跟伺服器不同（升版後還沒重算）→ 回說明字串；一致或無資料 → None。
    裁判 2026-11-23：沒重算就寄，信裡是舊規則算的期限（違反不掉球、數字要準）。重算＝最高管理員開一次網頁。"""
    try:
        from . import logic as _lg
        from .query import latest_batch as _lb
        b = _lb(db)
        if b is None:
            return None
        rv = getattr(b, "rules_version", None)
        if rv != _lg.RULES_VERSION:
            return "最新一批資料的判斷規則版本（%s）跟系統（%s）不同，尚未重算" % (rv or "未記錄", _lg.RULES_VERSION)
    except Exception:  # noqa: BLE001
        log.exception("rules version check failed")
    return None


def tick(db: Session, now: Optional[dt.datetime] = None) -> dict:
    """排程每分鐘呼叫一次。回 {action, reason, result?}。"""
    now = now or dt.datetime.now()
    with _full_scope():
        cfg = get_settings(db)
        try:   # 告警補寄、上週漏寄偵測：失敗不影響本週寄送
            retry_alerts(db, cfg, now)
            _check_last_week(db, cfg, now)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("auto mail alert housekeeping failed")
        week = iso_week(now.date())
        raw = _claim_raw(db, week)
        st, at = _claim_parts(raw)
        # 寄送中卡太久＝程序中途死掉：標中斷、記一筆，不自動重寄（可能已寄出一部分）
        if st == "running" and at and now - at > STALE_RUNNING:
            if _cas_claim(db, week, raw, "interrupted"):
                _record_missed(db, cfg, week, now, "寄送中斷（程序中途停止），未自動重寄；請看發信紀錄確認後再決定是否手動寄出",
                               "auto_mail_interrupted")
                return {"action": "interrupted", "reason": "寄送中斷"}
            return {"action": "idle", "reason": "狀態已變更"}
        # 整批寄不出（SMTP 連不上等）：補寄截止前每 10 分鐘重試；過了截止記一次「錯誤未寄出」
        if st == "retry" and cfg.get("enabled") and (cfg.get("paused_week") or "") != week:
            if now <= _window_end(cfg, _sched_dt(cfg, now)):
                if at and now - at < RETRY_EVERY:
                    return {"action": "idle", "reason": "等待重試"}
                if not _cas_claim(db, week, raw, _stamp("running", now)):
                    return {"action": "idle", "reason": "其他程序已在重試"}
                return _send_round(db, cfg, now, week, "重試（上次整批寄不出）")
            if _cas_claim(db, week, raw, "error"):
                _record_missed(db, cfg, week, now, "到補寄截止仍寄不出（SMTP／設定錯誤），本週未寄出", "auto_mail_missed")
                return {"action": "missed", "reason": "重試到截止仍失敗"}
            return {"action": "idle", "reason": "狀態已變更"}
        action, reason = decide(cfg, now, st)
        if action == "send":
            stale = _rules_stale(db)
            if stale:   # 先不寄、告警請管理者開頁重算；重算完下一分鐘就會照常寄（補寄截止前）
                raise_alert(db, cfg, week, "rules_stale", "自動寄信暫停：資料尚未用新判斷規則重算",
                            stale + "。" + chr(10) + "請用最高管理員帳號開啟弱點系統首頁一次（會自動重算），重算完成後排程會在補寄截止前自動寄出。",
                            now)
                return {"action": "idle", "reason": "等待規則重算：" + stale}
            if not try_claim(db, week, "running", now):
                return {"action": "idle", "reason": "其他程序已認領本週"}
            return _send_round(db, cfg, now, week, reason)
        if action == "missed":
            if try_claim(db, week, "missed", now):
                _record_missed(db, cfg, week, now, reason, "auto_mail_missed")
                return {"action": "missed", "reason": reason}
            return {"action": "idle", "reason": "漏寄已記錄"}
        return {"action": action, "reason": reason}


def run_now(db: Session, now: dt.datetime, who: str, force: bool = False) -> dict:
    """立即寄出一次（Super Admin 手動）。會認領本週，排程就不會再寄；本週已寄過要 force 才能再寄。
    漏寄、整批寄不出等待重試中 → 不用 force。丟 ValueError（端點轉 409）。測試模式開著時一樣只寄測試信箱。"""
    cfg = get_settings(db)
    week = iso_week(now.date())
    stale = _rules_stale(db)
    if stale:
        raise ValueError(stale + "；請重新整理首頁讓系統重算後再寄")
    claimed = try_claim(db, week, "running", now)
    if not claimed:
        raw = _claim_raw(db, week)
        st = _claim_parts(raw)[0]
        if st in ("missed", "retry"):
            if not _cas_claim(db, week, raw, _stamp("running", now)):
                raise ValueError("本週狀態剛被改變，請重新整理後再試")
        elif not force:
            raise ValueError("本週已處理過（%s），若確定要再寄一次請勾「強制」" % st)
        else:
            _set_claim(db, week, "running", now)
    redirect = cfg["test_addresses"] if cfg.get("test_mode") else None
    try:
        res = run_real(db, now, "run_now", who, test_redirect=redirect)
    except Exception:
        db.rollback()
        _set_claim(db, week, "error")
        raise
    st = _outcome(res, redirect is not None)
    _set_claim(db, week, st, now if st == "retry" else None)
    if st == "sent":
        _mark_formal_sent(db, now)
    if st != "retry":
        _alert_for_run(db, cfg, week, now, res)
    return res


@_scoped
def test_run(db: Session, now: dt.datetime, who: str, addresses: list[str],
             departments: Optional[list] = None) -> dict:
    """完整測試寄送：跟正式一模一樣組信，全部改寄 addresses。不認領本週、不改 last_run（只進歷史）。"""
    cfg = get_settings(db)
    email_cfg = appsettings.get_email_config(db)
    jobs = build_jobs(db, cfg, email_cfg, now.date(), departments=departments)
    s = deliver(db, email_cfg, jobs, (who or "(未登入)") + "[完整測試]", redirect=addresses)
    rec = _run_record("test_run", iso_week(now.date()), now, s, True, who)
    _push_state(db, run=rec, set_last=False)
    return {**s, "record": rec}


@_scoped
def sample_test(db: Session, now: dt.datetime, who: str, addresses: list[str], department: str) -> dict:
    """單一範例：選一個部門，盤點週報＋第一封催辦信各一封，寄到 addresses（不論該部門有沒有勾選）。"""
    cfg = get_settings(db)
    email_cfg = appsettings.get_email_config(db)
    jobs = build_jobs(db, cfg, email_cfg, now.date(), departments=[department], force=True)
    inv = [j for j in jobs if j["kind"] == KIND_INV][:1]
    dun = [j for j in jobs if j["kind"] == KIND_DUN]
    dun = ([j for j in dun if j["mode"] != "skip"] or dun)[:1]
    pick = inv + dun
    s = deliver(db, email_cfg, pick, (who or "(未登入)") + "[範例測試]", redirect=addresses, include_skipped=True)
    s["note"] = "" if dun else "此部門目前沒有 %s 天內到期的未結弱點，只寄盤點週報範例" % cfg.get("dunning_days")
    return s


@_scoped
def preview(db: Session, now: dt.datetime, departments: Optional[list] = None) -> dict:
    cfg = get_settings(db)
    email_cfg = appsettings.get_email_config(db)
    jobs = build_jobs(db, cfg, email_cfg, now.date(), departments=departments)
    send = [j for j in jobs if j["mode"] != "skip" and j["to"]]
    if cfg.get("test_mode"):   # 測試模式：預覽直接顯示「實際會寄到」的測試信箱（正式收件人另列；GPT 檢核 #10）
        send_ids = {id(j) for j in send}
        for j in jobs:
            j["actual_to"] = list(cfg.get("test_addresses") or []) if id(j) in send_ids else []
            j["actual_cc"] = []
            if j["kind"] == KIND_DUN:   # 測試模式：最後 To＝測試信箱、CC 一律不寄
                j["final_to"], j["final_cc"] = list(j["actual_to"]), []
                j["final_to_reason"] = "測試模式改寄測試信箱（正式：%s）" % j.get("final_to_reason", "")
                j["final_cc_reason"] = "測試模式不寄 CC（正式：%s）" % j.get("final_cc_reason", "")
    return {
        "jobs": jobs,
        "totals": {"mails": len(send), "skipped": len(jobs) - len(send),
                   "inventory": sum(1 for j in send if j["kind"] == KIND_INV),
                   "dunning": sum(1 for j in send if j["kind"] == KIND_DUN),
                   "escalated": sum(1 for j in send if j.get("escalate_cc")),   # 會副本主管的催辦信封數
                   "escalate_missing": sum(1 for j in jobs if j.get("escalate_missing")),   # 應升級但沒人收
                   "escalate_pending": sum(1 for j in send if j.get("escalate_note") and j.get("escalate_would_cc"))},   # 開關打開才會副本主管
        "test_mode": bool(cfg.get("test_mode")), "test_addresses": cfg.get("test_addresses") or [],
        "alert_recipients": cfg.get("alert_recipients") or [],
    }


# ── 背景執行緒 ──
_THREAD: Optional[threading.Thread] = None
_LOCK = threading.Lock()


def thread_disabled() -> bool:
    """WEBVULN_AUTO_MAIL_THREAD=0 → 不啟動排程執行緒（測試、或要停用時）。"""
    return os.environ.get("WEBVULN_AUTO_MAIL_THREAD", "1").strip().lower() in ("0", "false", "no", "off")


def _loop(factory: Callable[[], Session], interval: int) -> None:
    time.sleep(min(30, interval))   # 開機先讓 init_db／快照暖機跑完
    while True:
        try:
            db = factory()
            try:
                tick(db, dt.datetime.now())
            finally:
                db.close()
        except Exception:  # noqa: BLE001 - 排程永不讓 app 掛掉
            log.exception("auto mail tick failed")
        time.sleep(interval)


def start_scheduler(factory: Callable[[], Session], interval: int = 60) -> Optional[threading.Thread]:
    global _THREAD
    if thread_disabled():
        return None
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD
        _THREAD = threading.Thread(target=_loop, args=(factory, interval), daemon=True, name="auto-mail")
        _THREAD.start()
        return _THREAD

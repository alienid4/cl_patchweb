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
from .logic import SUBMIT_STALE
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
    "departments": [],       # [{department, enabled, cc:[...], send_inventory, send_dunning}]
    "test_mode": False,      # 開＝排程寄送全部改寄 test_addresses
    "test_addresses": [],
    "enabled_at": None,      # 最近一次「開啟」的時間（判斷漏寄：開啟前的排程不算漏寄）
}
_EDITABLE = ("enabled", "weekday", "time", "catchup_until", "dunning_days", "paused_week",
             "departments", "test_mode", "test_addresses")

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
def get_settings(db: Session) -> dict:
    cfg = dict(DEFAULTS)
    saved = appsettings.get_json(db, KEY, {}) or {}
    if isinstance(saved, dict):
        cfg.update({k: saved[k] for k in saved if k in DEFAULTS})
    cfg["departments"] = list(cfg.get("departments") or [])
    cfg["test_addresses"] = split_emails(cfg.get("test_addresses"))
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
    try:
        cfg["weekday"] = int(cfg["weekday"])
    except (TypeError, ValueError):
        raise ValueError("每週幾需為 0～6（0＝週一）")
    if not 0 <= cfg["weekday"] <= 6:
        raise ValueError("每週幾需為 0～6（0＝週一）")
    t, c = _hm(cfg["time"]), _hm(cfg["catchup_until"])
    if c < t:
        raise ValueError("補寄截止時間不能早於寄送時間")
    cfg["time"] = "%02d:%02d" % (t // 60, t % 60)
    cfg["catchup_until"] = "%02d:%02d" % (c // 60, c % 60)
    try:
        cfg["dunning_days"] = int(cfg["dunning_days"])
    except (TypeError, ValueError):
        raise ValueError("催辦天數需為整數")
    if not 1 <= cfg["dunning_days"] <= 365:
        raise ValueError("催辦天數需介於 1～365")
    pw = str(cfg.get("paused_week") or "").strip()
    if pw and not _WEEK_RE.match(pw):
        raise ValueError("暫停週格式需為 YYYY-Www（例 2026-W44）")
    cfg["paused_week"] = pw
    cfg["departments"] = _clean_departments(db, cfg.get("departments"), cur.get("departments"))
    cfg["test_addresses"] = check_emails(split_emails(cfg.get("test_addresses")), "測試模式收件")
    if cfg["test_mode"] and not cfg["test_addresses"]:
        raise ValueError("測試模式需填至少一個測試收件信箱")
    if cfg["enabled"] and not cur.get("enabled"):
        cfg["enabled_at"] = now.isoformat(timespec="seconds")
    clean = {k: cfg[k] for k in DEFAULTS}
    appsettings.set_json(db, KEY, clean)
    return clean


def get_state(db: Session) -> dict:
    st = appsettings.get_json(db, STATE_KEY, {}) or {}
    if not isinstance(st, dict):
        st = {}
    return {"last_run": st.get("last_run"), "history": list(st.get("history") or []),
            "missed": list(st.get("missed") or [])}


def _push_state(db: Session, run: Optional[dict] = None, missed: Optional[dict] = None,
                set_last: bool = True) -> None:
    st = get_state(db)
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
            out.append({"department": name, "enabled": True, "cc": [], "send_inventory": True,
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
    return p + ("（%s）" % r["progress_state"] if r.get("progress_state") else "")


def build_jobs(db: Session, cfg: dict, email_cfg: dict, today: dt.date,
               departments: Optional[list] = None, force: bool = False) -> list[dict]:
    """產生要寄的每一封（不寄）。順序＝部門設定順序；部門內先盤點週報、再催辦（依筆數多到少）。"""
    prefix = email_cfg.get("subject_prefix") or ""
    site_url = (email_cfg.get("site_url") or "").strip()
    days = int(cfg.get("dunning_days") or 60)
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
            to = _inventory_recipients(db, dept)
            mode, reason = "send", ""
            if not to:
                gf = (email_cfg.get("global_fallback") or "").strip()
                if gf:
                    to, mode, reason = [gf], "fallback", "此部門無窗口信箱，轉系統預設"
                else:
                    mode, reason = "skip", "此部門無窗口信箱"
            need, sub, app_ = rep.get("need_apply_count", 0), rep.get("submitted_count", 0), rep.get("applied_count", 0)
            metrics = [
                ["未結", rep.get("unresolved", 0)], ["已逾期", rep.get("overdue", 0)],
                ["近期到期（30天）", rep.get("soon", 0)], ["需申請", rep.get("apply_universe", need + sub + app_)],
                ["　應申請未申請", need], ["　送審中（等核准）", sub], ["　已核准", app_],
                ["暫無需申請", max(0, rep.get("unresolved", 0) - rep.get("apply_universe", need + sub + app_))],
                ["送審逾時", rep.get("submitted_stale_count", 0)],
                ["待追查", (rep.get("progress") or {}).get("flagged", 0)],
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
            rows = query.find(db, department=dept, due_max=days, today=today)
            by_owner: dict[str, list] = {}
            for r in rows:
                by_owner.setdefault((r.get("owner") or "").strip() or "（未指定）", []).append(r)
            djobs = []
            for owner, rs in by_owner.items():
                rs.sort(key=lambda r: (r.get("effective_due") or "9999", r.get("host") or ""))
                items = [{"host": r.get("host"), "name": r.get("name"), "severity": r.get("severity"),
                          "effective_due": r.get("effective_due"), "overdue_days": r.get("overdue_days"),
                          "due_text": _due_text(r, today), "progress_text": _progress_text(r)} for r in rs]
                to1, mode, reason = mailer._resolve_recipient(db, owner, dept, email_cfg)
                to = [to1] if to1 else []
                djobs.append({
                    "department": dept, "kind": KIND_DUN, "kind_label": KIND_LABEL[KIND_DUN],
                    "label": owner, "to": to, "cc": [c for c in cc_cfg if c not in to],
                    "mode": mode, "reason": reason, "count": len(items),
                    "subject": "%s%s 弱點 %d 天內到期未修補 %d 筆（%s）" % (prefix, owner, days, len(items), today.isoformat()),
                    "body": mailer.dunning_body(owner, items, today, days, site_url,
                                                reason if mode == "fallback" else ""),
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


def _test_header(job: dict) -> str:
    to = "、".join(job.get("to") or []) or "（無，正式寄送時會略過：%s）" % (job.get("reason") or "查無信箱")
    cc = "、".join(job.get("cc") or []) or "（無）"
    return ("[測試] 正式寄送時：收件人 %s；CC %s\n"
            "（這是測試信，只寄到測試信箱；正式收件人沒有收到）\n%s\n\n" % (to, cc, "─" * 30))


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
                             "status": status, "count": job.get("count", 0), "error": error})
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
                body = _test_header(job) + job["body"]
            else:
                to, cc = list(job["to"]), list(job["cc"])
                subject, body = job["subject"], job["body"]
            envelope = to + [c for c in cc if c not in to]
            envelope += [b for b in bcc if b not in envelope]
            try:
                msg = mailer._build_message(from_addr, ", ".join(to), cc, subject, body)
                srv.sendmail(from_addr, envelope, msg.as_string())
                _tally(job, "ok", to, cc)
            except Exception as e:  # noqa: BLE001 - 單封失敗不中斷整批
                _tally(job, "failed", to, cc, str(e)[:200])
    finally:
        try:
            srv.quit()
        except Exception:  # noqa: BLE001
            pass
    return s


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


def _record_missed(db: Session, cfg: dict, week: str, now: dt.datetime, reason: str, action: str) -> None:
    sched = _sched_dt(cfg, now)
    m = {"week": week, "scheduled": sched.isoformat(timespec="minutes"),
         "detected_at": now.isoformat(timespec="seconds"), "reason": reason}
    _push_state(db, missed=m)
    security.log_audit(db, username=SCHED_USER, action=action, target="week:" + week,
                       detail="排程 %s：%s" % (m["scheduled"], reason))


def _send_round(db: Session, cfg: dict, now: dt.datetime, week: str, reason: str) -> dict:
    """已認領（running）後寄一輪，依結果把本週標成 sent／test／retry／error。"""
    redirect = cfg["test_addresses"] if cfg.get("test_mode") else None
    test = redirect is not None
    try:
        res = run_real(db, now, "schedule", SCHED_USER, test_redirect=redirect)
        st = _outcome(res, test)
        _set_claim(db, week, st, now if st == "retry" else None)
    except Exception as e:  # noqa: BLE001 - 不讓排程掛掉；本週標 error，不自動重試（避免部分重寄）
        db.rollback()
        log.exception("auto mail run failed")
        _set_claim(db, week, "error")
        rec = {"week": week, "at": now.isoformat(timespec="seconds"), "trigger": "schedule", "by": SCHED_USER,
               "test_mode": test, "sent": 0, "failed": 0, "skipped": 0,
               "error": str(e)[:300], "summary": []}
        _push_state(db, run=rec)
        res = {"error": str(e)[:300], "record": rec}
    r = res["record"]
    security.log_audit(db, username=SCHED_USER, action="auto_mail_run", target="week:" + week,
                       detail="%ssent=%d failed=%d skipped=%d %s" % (
                           "[測試模式] " if test else "", r["sent"], r["failed"], r["skipped"],
                           r.get("error") or ""))
    return {"action": "send", "reason": reason, "result": res}


def tick(db: Session, now: Optional[dt.datetime] = None) -> dict:
    """排程每分鐘呼叫一次。回 {action, reason, result?}。"""
    now = now or dt.datetime.now()
    with _full_scope():
        cfg = get_settings(db)
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
    return {
        "jobs": jobs,
        "totals": {"mails": len(send), "skipped": len(jobs) - len(send),
                   "inventory": sum(1 for j in send if j["kind"] == KIND_INV),
                   "dunning": sum(1 for j in send if j["kind"] == KIND_DUN)},
        "test_mode": bool(cfg.get("test_mode")), "test_addresses": cfg.get("test_addresses") or [],
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

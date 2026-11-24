"""伺服器端寄信（一鍵發送催辦）。直連公司免認證 SMTP relay（smtplib，stdlib 零相依）。

一鍵發送＝把「逾期（可含近期到期）未結」弱點依負責人分組，每位負責人一封催辦信：
  寄件者＝操作者本人 email（登入時由 AD mail 屬性存入 User；無則用系統預設寄件人）
  收件者＝該負責人 email（同樣來自其 AD mail）
  查無該負責人 email → 轉寄給其部門窗口(dept_admin)；窗口也無 → 系統預設轉寄；再無 → 跳過
  副本＝操作者本人（cc_self）

dept_admin 操作時只處理自己部門的負責人（範圍由端點限定，見 main）。對 Excel 全程唯讀。
設定來自 appsettings.get_email_config()（DB，Super Admin 於畫面編輯、免重部署）。
"""
from __future__ import annotations

import datetime as dt
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, names, query
from .logic import CLOSE_OPEN, SUBMIT_STALE, overdue_days
from .query import display_host
from .models import Finding, MailLog, User


# ── 收件人解析 ──
def resolve_owner(db: Session, owner: Optional[str]) -> dict:
    """負責人 → 唯一的收件身分（V2.111；To 和主管 CC 都從這一筆來，不各查各的）。

    以 names.owner_key 比對啟用帳號的顯示名（「陳小美 AMY」＝「陳小美」）。
    回 {status: ok/none/conflict, user, email, manager_name, manager_email, reason}：
      · 有人工指定（ad_bound_by）的帳號 → 只用它
      · 有身分衝突標記 → conflict（不直寄，轉窗口）
      · 同名多個帳號、信箱不同 → conflict（不猜）
    """
    out = {"status": "none", "user": None, "email": None, "manager_name": None, "manager_email": None, "reason": ""}
    key = names.owner_key(owner)
    if not key:
        return out
    from . import directory
    # 名錄裡同名在職的人超過一位 → 不知道是哪一位，不直寄（裁判 R1；就算只有一位有帳號也一樣）
    n_dir = len(directory.persons_by_name(db, key)) if directory.active(db) else 0
    if n_dir > 1:
        out.update(status="conflict", reason="名錄同名 %d 人，轉部門窗口" % n_dir)
        return out
    us = [u for u in db.execute(select(User).where(User.is_active.is_(True))).scalars().all()
          if names.owner_key(u.display_name) == key]
    # 帳號有綁員編、名錄顯示已停用 → 排除（所有 To／CC 一致）
    gone = [directory.disabled_reason(db, u) for u in us]
    excluded = [w for w in gone if w]
    us = [u for u, w in zip(us, gone) if not w]
    if not us and excluded:
        out.update(status="excluded", reason="%s，轉部門窗口" % excluded[0])
        return out
    bound = [u for u in us if u.ad_bound_by and not u.ad_conflict]
    if bound:
        us = bound
    elif any(u.ad_conflict for u in us):
        c = next(u for u in us if u.ad_conflict)
        out.update(status="conflict", reason="AD 身分衝突待確認（%s），轉部門窗口" % c.ad_conflict)
        return out
    withmail = [u for u in us if (u.email or "").strip()]
    if not withmail:
        return out
    mails = {u.email.strip().lower() for u in withmail}
    if len(mails) > 1:
        out.update(status="conflict", reason="同名帳號 %d 個、信箱不同（待確認），轉部門窗口" % len(withmail))
        return out
    u = next((x for x in withmail if (x.manager_email or "").strip()), withmail[0])
    mgr = (u.manager_email or "").strip() or None
    if mgr and directory.email_disabled(db, mgr):   # AD 主管在名錄已停用 → 不副本
        mgr = None
        out["manager_note"] = "AD 主管名錄顯示已停用，不副本"
    out.update(status="ok", user=u, email=u.email.strip(),
               manager_name=((u.manager_name or "").strip() or None) if mgr else None,
               manager_email=mgr)
    return out


def _user_email(db: Session, display_name: Optional[str]) -> Optional[str]:
    """以負責人顯示名找收件信箱（見 resolve_owner）。"""
    return resolve_owner(db, display_name)["email"]


def _dept_admin_email(db: Session, department: Optional[str]) -> Optional[str]:
    """該部門窗口(dept_admin)的信箱；多位取第一個有信箱者。"""
    dept = (department or "").strip()
    if not dept:
        return None
    rows = db.execute(
        select(User).where(User.department == dept, User.is_active.is_(True))
    ).scalars().all()
    for u in rows:
        if config.canon_role(u.role) == config.ROLE_DEPT_ADMIN and (u.email or "").strip():
            return u.email.strip()
    return None


def resolve_recipients(db: Session, owner: str, dept: Optional[str], cfg: dict, windows: Optional[list] = None) -> dict:
    """負責人 → 收件人（V2.113）。回 {to:[...], mode, reason, rule, ident, notes}。
    順序：①負責人名＝群組名 → 群組信箱 ②負責人本人（名錄／帳號）③設定窗口（名錄的人）
          ④角色窗口（部門窗口角色帳號，舊）⑤系統預設 ⑥寄不出。rule＝哪一條給的收件人（預覽顯示用）。
    windows＝呼叫端的部門窗口設定快照（一輪寄送用同一份）；None＝自己讀設定。"""
    from . import directory
    g = directory.group_by_name(db, owner)
    if g is not None and (g.email or "").strip():
        return {"to": [g.email.strip()], "mode": "send", "reason": "", "rule": "群組",
                "ident": {"status": "group", "user": None, "email": g.email.strip(), "manager_name": None,
                          "manager_email": None, "reason": ""}, "notes": []}
    ident = resolve_owner(db, owner)
    if ident["status"] == "ok":
        return {"to": [ident["email"]], "mode": "send", "reason": "", "rule": "本人", "ident": ident,
                "notes": [ident["manager_note"]] if ident.get("manager_note") else []}
    special = ident["status"] in ("conflict", "excluded")
    why = (ident["reason"] or "").replace("，轉部門窗口", "") if special else "查無負責人信箱"
    wins, rule, notes = directory.dept_windows(db, dept, windows)
    if wins:
        return {"to": wins, "mode": "fallback", "reason": "%s，轉部門窗口" % why, "rule": rule, "ident": ident, "notes": notes}
    gf = (cfg.get("global_fallback") or "").strip()
    if gf:
        return {"to": [gf], "mode": "fallback", "reason": ("%s；部門無窗口信箱，轉系統預設" % why) if special
                else "查無負責人與窗口信箱，轉系統預設", "rule": "系統預設", "ident": ident, "notes": notes}
    return {"to": [], "mode": "skip", "reason": why if special else "查無信箱", "rule": "寄不出",
            "ident": ident, "notes": notes}


def resolve_recipient_ex(db: Session, owner: str, dept: Optional[str], cfg: dict) -> tuple:
    """回 (to, mode, reason, ident)。to＝第一個收件人（相容舊呼叫）；多位窗口請用 resolve_recipients。"""
    r = resolve_recipients(db, owner, dept, cfg)
    return (r["to"][0] if r["to"] else None), r["mode"], r["reason"], r["ident"]


def _resolve_recipient(db: Session, owner: str, dept: Optional[str], cfg: dict) -> tuple:
    """回 (to, mode, reason)。mode: send（負責人本人）/ fallback（轉窗口或系統預設）/ skip。"""
    return resolve_recipient_ex(db, owner, dept, cfg)[:3]


# ── 清單 ──
def _in_scope(f: Finding, cfg: dict, today: dt.date) -> bool:
    od = overdue_days(f.effective_due, today)   # 正值＝逾期；None＝無到期日
    if cfg.get("include_overdue") and od is not None and od > 0:
        return True
    if cfg.get("include_soon") and f.effective_due:
        d = (f.effective_due - today).days
        if 0 <= d <= int(cfg.get("soon_days") or 30):
            return True
    return False


def _common_dept(items: list[Finding]) -> Optional[str]:
    """該負責人弱點最常出現的部門（跨部門時取眾數），供轉寄窗口。"""
    tally: dict[str, int] = {}
    for f in items:
        d = (f.department or "").strip()
        if d:
            tally[d] = tally.get(d, 0) + 1
    if not tally:
        return None
    return max(tally.items(), key=lambda kv: kv[1])[0]


def _item(f: Finding, today: dt.date) -> dict:
    od = overdue_days(f.effective_due, today)
    if f.effective_due is None:
        due_text = "無到期日"
    elif od is not None and od > 0:
        due_text = "逾期 %d 天" % od
    else:
        due_text = "剩 %d 天" % (-od) if od is not None else ""
    return {
        "host": display_host(f), "name": f.name, "severity": f.severity,
        "sheet_key": f.sheet_key,
        "effective_due": f.effective_due.isoformat() if f.effective_due else None,
        "due_text": due_text,
    }


def build_plan(db: Session, cfg: dict, department: Optional[str] = None,
               today: Optional[dt.date] = None) -> list[dict]:
    """依負責人分組的寄送計畫（不寄信）。department 給值＝只算該部門（dept_admin 範圍）。"""
    today = today or dt.date.today()
    fs = query._open_findings(db, department)   # 系統口徑未結：資安已確認結案的不催（2026-10-28）
    scoped = [f for f in fs if _in_scope(f, cfg, today)]
    by_owner: dict[str, list] = {}
    for f in scoped:
        o = (f.owner or "").strip() or "（未指定）"
        by_owner.setdefault(o, []).append(f)
    plan = []
    for owner, items in by_owner.items():
        items.sort(key=lambda f: (f.effective_due or dt.date.max))
        dept = _common_dept(items)
        to, mode, reason = _resolve_recipient(db, owner, dept, cfg)
        plan.append({
            "owner": owner, "department": dept, "count": len(items),
            "to": to, "mode": mode, "reason": reason,
            "items": [_item(f, today) for f in items],
        })
    plan.sort(key=lambda p: -p["count"])
    return plan


# ── 信件內容 ──
def _subject(cfg: dict, owner: str, count: int, today: dt.date) -> str:
    return "%s%s 弱點待處理 %d 筆（%s）" % (
        cfg.get("subject_prefix") or "", owner, count, today.isoformat())


def _body(owner: str, items: list[dict], today: dt.date, note: str = "") -> str:
    lines = ["%s 您好：" % owner, "", "以下弱點待處理（%s）：" % today.isoformat(), ""]
    for it in items:
        lines.append("· [%s] %s｜%s｜到期 %s（%s）" % (
            it.get("sheet_key") or "", it.get("name") or "", it.get("severity") or "",
            it.get("effective_due") or "—", it.get("due_text") or ""))
    lines += ["", "請儘速處理或依程序申請展延／例外。", "（本信由弱點彙總系統自動發送）"]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def sender_from(cfg: dict, sender) -> Optional[str]:
    """操作者本人信箱；無則用系統預設寄件人。兩者皆無回 None（無法寄）。"""
    em = (getattr(sender, "email", None) or "").strip()
    if em:
        return em
    return (cfg.get("from_default") or "").strip() or None


# ── 寄送 ──
def _build_message(from_addr: str, to: str, cc: list, subject: str, body: str):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("弱點彙總系統", "utf-8")), from_addr))
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=True)
    return msg


def send_plan(db: Session, cfg: dict, sender, selected_owners: Optional[list] = None,
              department: Optional[str] = None, today: Optional[dt.date] = None) -> dict:
    """實際寄送。回摘要 {sent, fallback, skipped, failed, details[]}。

    丟 ValueError 給呼叫端轉 400：未啟用／未設 SMTP／寄件者無信箱。
    """
    if not cfg.get("enabled"):
        raise ValueError("尚未啟用一鍵發送（請最高管理員於 Email／SMTP 設定開啟）")
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("尚未設定 SMTP 主機（請最高管理員於 Email／SMTP 設定填寫）")
    from_addr = sender_from(cfg, sender)
    if not from_addr:
        raise ValueError("寄件者無信箱，且未設定系統預設寄件人（from_default）")

    today = today or dt.date.today()
    plan = build_plan(db, cfg, department=department, today=today)
    if selected_owners is not None:
        want = set(selected_owners)
        plan = [p for p in plan if p["owner"] in want]

    cc_base = []
    if cfg.get("cc_self") and from_addr:
        cc_base = [from_addr]
    bcc = (cfg.get("copy_to") or "").strip()   # 系統總備份信箱：每封密件備份一份

    sender_name = getattr(sender, "username", None) or "(未登入)"
    summary = {"sent": 0, "fallback": 0, "skipped": 0, "failed": 0, "details": []}
    port = int(cfg.get("smtp_port") or 25)
    srv = None

    def _log(owner, to, cc, mode, status, count, error=""):
        try:
            db.add(MailLog(sender=sender_name, owner=owner, to=to or "",
                           cc=", ".join(cc) if cc else "", mode=mode, status=status,
                           count=count, error=error or None))
            db.commit()
        except Exception:  # noqa: BLE001 - 紀錄失敗不影響寄信結果
            db.rollback()

    try:
        srv = smtplib.SMTP(host, port, timeout=20)
        if cfg.get("use_tls"):
            srv.starttls()
        for p in plan:
            owner, mode, to = p["owner"], p["mode"], p["to"]
            if mode == "skip" or not to:
                summary["skipped"] += 1
                summary["details"].append({"owner": owner, "mode": "skip", "to": "",
                                           "error": p.get("reason") or "查無信箱"})
                _log(owner, "", [], "skip", "skip", p["count"], p.get("reason") or "查無信箱")
                continue
            cc = [c for c in cc_base if c and c != to]
            note = p.get("reason") if mode == "fallback" else ""
            subject = _subject(cfg, owner, p["count"], today)
            body = _body(owner, p["items"], today, note)
            envelope = [to] + cc + ([bcc] if bcc and bcc not in ([to] + cc) else [])
            try:
                msg = _build_message(from_addr, to, cc, subject, body)
                srv.sendmail(from_addr, envelope, msg.as_string())
                summary["sent" if mode == "send" else "fallback"] += 1
                summary["details"].append({"owner": owner, "mode": mode, "to": to,
                                           "cc": ", ".join(cc), "error": ""})
                _log(owner, to, cc, mode, "ok", p["count"])
            except Exception as e:  # noqa: BLE001 - 單封失敗不中斷整批
                summary["failed"] += 1
                summary["details"].append({"owner": owner, "mode": mode, "to": to,
                                           "error": str(e)[:200]})
                _log(owner, to, cc, mode, "failed", p["count"], str(e)[:200])
    except (smtplib.SMTPException, OSError) as e:
        raise ValueError("連不到 SMTP 主機：%s" % (str(e)[:200]))
    finally:
        if srv is not None:
            try:
                srv.quit()
            except Exception:  # noqa: BLE001
                pass
    return summary


def build_sample(db: Session, cfg: dict, owner: Optional[str] = None,
                 department: Optional[str] = None, today: Optional[dt.date] = None) -> dict:
    """產一封範例信（寄前預覽內容用）。owner 給值取該人；否則取計畫中第一位。"""
    today = today or dt.date.today()
    plan = build_plan(db, cfg, department=department, today=today)
    if not plan:
        return {"ok": False, "message": "目前無待催辦項目"}
    p = next((x for x in plan if x["owner"] == owner), None) if owner else plan[0]
    if p is None:
        p = plan[0]
    note = p.get("reason") if p["mode"] == "fallback" else ""
    return {
        "ok": True, "owner": p["owner"], "to": p["to"], "mode": p["mode"],
        "subject": _subject(cfg, p["owner"], p["count"], today),
        "body": _body(p["owner"], p["items"], today, note),
    }


# ── #8 每週部門週報（排程寄給有開啟的部門窗口 dept_admin）──
def _compare_lines(cp: dict) -> list[str]:
    """「與上次匯入比較」一句話＋重點變化（週報與自動盤點週報共用；2026-10-27 抽出）。"""
    lines = ["與上次匯入比較（%s → 本次）：%s" % ((cp.get("prev") or {}).get("asof", "")[:16].replace("T", " "),
                                            cp.get("summary", ""))]
    for m in cp.get("metrics") or []:
        if m["key"] in ("unresolved", "overdue", "need_apply", "submitted", "applied", "todo", "subext"):
            lines.append("· %s：%s → %d（%s）" % (m["label"].strip(), "—" if m["prev"] is None else m["prev"], m["cur"],
                                               "—" if m["delta"] is None else "%+d" % m["delta"]))
    return lines


STALE_DATA_DAYS = 14   # 資料超過幾天沒更新，盤點週報加一行警告（2026-10-27）


def _att_row(i: int, d: dict, extra: str = "") -> str:
    return "  %d. %s｜%s｜%s｜%s%s" % (i, d.get("owner") or "（未指定）", d.get("host") or "", d.get("name") or "",
                                    d.get("severity") or "", extra)


def _cap_lines(rows: list, cap: int, fmt) -> list[str]:
    out = [fmt(i, d) for i, d in enumerate(rows[:cap], 1)]
    if len(rows) > cap:
        out.append("  …另 %d 筆，請上系統查看完整清單" % (len(rows) - cap))
    return out


def _attention_lines(att: dict) -> list[str]:
    """盤點週報「要窗口追的」（V2.109）：我方待辦（已逾期、缺期限）與等資安處理（我方已送件）分開算。"""
    cap = int(att.get("cap") or 30)
    own, nodue, wait = att.get("own_overdue") or [], att.get("nodue") or [], att.get("waiting") or []
    lines = ["一、本週要追的（請窗口處理）",
             "【我方待辦】",
             "· 已逾期（不含送審中）：%d 筆" % len(own)]
    lines += _cap_lines(own, cap, lambda i, d: _att_row(i, d, "｜到期 %s（逾期 %s 天）｜進度：%s" % (
        d.get("effective_due") or "—", d.get("overdue_days"), d.get("progress") or "未回報")))
    lines.append("· 缺期限・待確認：%d 筆（目前無法計算剩餘天數，請負責人向資安確認到期日）" % len(nodue))
    lines += _cap_lines(nodue, cap, lambda i, d: _att_row(i, d, "｜進度：%s" % (d.get("progress") or "未回報")))
    lines += ["【等資安處理（我方已送件）】（僅供參考）",
              "· 送審中：%d 筆（其中已逾期 %d 筆、送審逾時 %d 筆）：我方已送件，等資安更新 Excel；不需處理。" % (
                  len(wait), att.get("waiting_overdue", 0), att.get("waiting_stale", 0))]

    def _w(i, d):
        flags = []
        if (d.get("overdue_days") or 0) > 0:
            flags.append("已逾期 %d 天" % d["overdue_days"])
        if d.get("progress_state") == SUBMIT_STALE:
            flags.append("送審逾時")
        return _att_row(i, d, "｜%s｜已送審 %s 天｜到期 %s%s" % (
            d.get("progress") or "送審中", d.get("submit_days") if d.get("submit_days") is not None else "?",
            d.get("effective_due") or "—", ("｜⚠ " + "、".join(flags)) if flags else ""))
    lines += _cap_lines(wait, cap, _w)
    return lines


def inventory_body(rep: dict, dept: str, site_url: str, today: dt.date) -> str:
    """自動排程「盤點狀態」週報內文（寄部門窗口；2026-10-27）。數字全部取 query.weekly_report 同一套口徑。"""
    fr = rep.get("freshness") or {}
    imp = (fr.get("imported_at") or "")[:10]
    ago = fr.get("days_ago")
    pg = rep.get("progress") or {}
    need, sub, app_ = rep.get("need_apply_count", 0), rep.get("submitted_count", 0), rep.get("applied_count", 0)
    nodue = rep.get("no_due_count", 0)   # 缺期限・待確認（V2.108）
    universe = rep.get("apply_universe", need + sub + app_ + nodue)
    lines = ["%s 弱點盤點狀態週報（%s）" % (dept, today.isoformat()), ""]
    lines.append("資料日期：%s" % ("%s 匯入（%s 天前）" % (imp, ago) if imp else "尚無匯入資料"))
    if ago is not None and ago > STALE_DATA_DAYS:
        lines.append("⚠ 資料已超過 %d 天未更新，以下數字可能不是最新，請確認是否已匯入最新彙總表。" % STALE_DATA_DAYS)
    att = rep.get("attention")
    if att is not None:
        lines += [""] + _attention_lines(att)
    lines += [
        "",
        "%s、盤點數字（未結案，含送審中，與資安 Excel 同口徑）" % ("二" if att is not None else "一"),
        "· 未結：%d" % rep.get("unresolved", 0),
        "· 已逾期：%d（其中高風險 %d）" % (rep.get("overdue", 0), rep.get("high_risk_overdue", 0)),
        "· 近期到期（30 天內）：%d" % rep.get("soon", 0),
        "· 需申請：%d ＝ 應申請未申請 %d ＋ 送審中（等核准）%d ＋ 已核准 %d ＋ 缺期限・待確認 %d" % (
            universe, need, sub, app_, nodue),
        "· 暫無需申請：%d" % max(0, rep.get("unresolved", 0) - universe),
        "· 送審逾時（送審超過 %s 天仍未核准）：%d" % (rep.get("submit_stale_days", ""), rep.get("submitted_stale_count", 0)),
        "· 待追查：%d" % pg.get("flagged", 0),
        # 資安已確認結案（2026-10-28）：上面的數字都已扣掉；這行說明 Excel 跟系統差在哪
        "· 資安已確認待 Excel 更新 %d 筆（其中超過 %s 天 %d 筆）" % (
            rep.get("sec_confirmed_count", 0), rep.get("confirm_stale_days", 14), rep.get("sec_confirmed_stale_count", 0)),
        "",
        "%s、與上次匯入比較" % ("三" if att is not None else "二"),
    ]
    cp = rep.get("compare") or {}
    if cp.get("has_prev"):
        lines += _compare_lines(cp)
    else:
        lines.append("（%s）" % (cp.get("reason") or "無上一次匯入可比較"))
    if site_url:
        lines += ["", "系統網址（可看明細、列印週報）：%s" % site_url.rstrip("/")]
    lines += ["", "（本信由弱點彙總系統每週自動發送）"]
    return "\n".join(lines)


def dunning_body(owner: str, items: list[dict], today: dt.date, days: int,
                 site_url: str = "", note: str = "", escalate_cc: Optional[list] = None,
                 escalate_days: int = 14) -> str:
    """自動排程催辦信內文（寄負責人；2026-10-27，V2.109 分段）。
    items 已排序：section="due"（N 天內到期，含已逾期）在前、"nodue"（期限待確認）在後；
    escalate=True 的列標 ★（升級項目，副本主管）。送審中（等資安處理）的列不會在 items 裡。"""
    due = [it for it in items if it.get("section", "due") == "due"]
    nodue = [it for it in items if it.get("section") == "nodue"]
    n_esc = sum(1 for it in due if it.get("escalate"))
    lines = ["%s 您好：" % owner, "",
             "以下是您負責、尚未修補的弱點，共 %d 筆（%s）：" % (len(items), today.isoformat())]
    if n_esc:
        lines.append("※ 其中 %d 筆標 ★ 為升級項目（已逾期，或 %d 天內到期但未填預計完成日／預計完成日已過）%s。" % (
            n_esc, escalate_days, "，本信已副本主管：" + "、".join(escalate_cc) if escalate_cc else ""))
    lines.append("")
    sec = 0
    if due:
        sec += 1
        lines.append("%s、%d 天內到期（含已逾期），共 %d 筆（依到期日排序）：" % ("一二三"[sec - 1], days, len(due)))
        for i, it in enumerate(due, 1):
            lines.append("%d. %s%s｜%s｜%s｜到期 %s（%s）｜預計完成 %s｜處理進度：%s" % (
                i, "★ " if it.get("escalate") else "", it.get("host") or "", it.get("name") or "",
                it.get("severity") or "", it.get("effective_due") or "—", it.get("due_text") or "",
                it.get("target_date") or "未填", it.get("progress_text") or "未回報"))
        lines.append("")
    if nodue:
        sec += 1
        lines.append("%s、期限待確認（請向資安確認到期日），共 %d 筆：" % ("一二三"[sec - 1], len(nodue)))
        for i, it in enumerate(nodue, 1):
            lines.append("%d. %s｜%s｜%s｜%s｜處理進度：%s" % (
                i, it.get("host") or "", it.get("name") or "", it.get("severity") or "",
                it.get("due_text") or "目前無法計算剩餘天數，請向資安確認到期日", it.get("progress_text") or "未回報"))
        lines.append("（彙總表上沒有可用的到期日，目前無法計算剩餘天數；請向資安確認到期日，避免不知不覺逾期。）")
        lines.append("")
    lines += ["請儘速修補；無法如期完成請依程序申請展延／例外，並在系統填預計完成日。",
              "已送展延／例外審核（送審中）的項目我方已送件、等資安更新，不列在本信。"]
    if site_url:
        lines.append("系統網址：%s" % site_url.rstrip("/"))
    lines.append("（本信由弱點彙總系統每週自動發送）")
    if note:
        lines += ["", note]
    return "\n".join(lines)


def _weekly_body(rep: dict, dept: str, site_url: str) -> str:
    ch = rep.get("change") or {}
    tg = rep.get("target") or {}
    lines = [
        "%s 部門弱點週報（%s）" % (dept, rep.get("today", "")),
        "",
        "未結案：%d　逾期：%d　高風險且逾期：%d" % (
            rep.get("unresolved", 0), rep.get("overdue", 0), rep.get("high_risk_overdue", 0)),
        "應申請未申請：%d　送審中（等核准）：%d　已核准展延／例外：%d" % (
            rep.get("need_apply_count", 0), rep.get("submitted_count", 0), rep.get("applied_count", 0)),
        "預計完成未回報：%d　已逾自訂完成日：%d" % (
            tg.get("no_target", 0), tg.get("target_overdue", 0)),
    ]
    if ch.get("has_prev"):
        lines.append("本週變化：新增 %d、解決 %d（淨 %+d）" % (
            ch.get("new", 0), ch.get("resolved", 0), ch.get("delta", 0)))
    cp = rep.get("compare") or {}
    if cp.get("has_prev"):
        lines += [""] + _compare_lines(cp)
    lines += ["", "最需要處理（落後清單前幾筆）："]
    for d in (rep.get("overdue_list") or [])[:8]:
        lines.append("· %s｜%s｜%s｜逾期 %s 天" % (
            d.get("owner") or "", d.get("host") or "", d.get("severity") or "",
            d.get("overdue_days")))
    if not (rep.get("overdue_list")):
        lines.append("（目前無逾期項目）")
    if site_url:
        lines += ["", "完整週報（可列印／存 PDF）：%s" % site_url.rstrip("/")]
    lines += ["", "（本信由弱點彙總系統每週一自動發送；如不需要可於系統內關閉）"]
    return "\n".join(lines)


def send_weekly(db: Session, cfg: dict, today: Optional[dt.date] = None,
                only_usernames: Optional[list] = None) -> dict:
    """排程寄部門週報給『有開啟 weekly_report 且有信箱』的部門窗口。

    自動寄送無操作者 → 寄件人用 from_default。only_usernames 給值＝只寄這些人（測試用）。
    回摘要 {sent, skipped, failed, details[]}。設定未啟用/未設 SMTP/無 from_default → ValueError。
    """
    if not cfg.get("enabled"):
        raise ValueError("尚未啟用寄信（Email／SMTP 設定）")
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("尚未設定 SMTP 主機")
    from_addr = (cfg.get("from_default") or "").strip()
    if not from_addr:
        raise ValueError("每週排程為自動寄送，需先設定『系統預設寄件人 from_default』")

    today = today or dt.date.today()
    site_url = (cfg.get("site_url") or "").strip()
    bcc = (cfg.get("copy_to") or "").strip()
    prefix = cfg.get("subject_prefix") or ""

    admins = db.execute(
        select(User).where(User.weekly_report.is_(True), User.is_active.is_(True))
    ).scalars().all()
    if only_usernames is not None:
        want = set(only_usernames)
        admins = [u for u in admins if u.username in want]

    summary = {"sent": 0, "skipped": 0, "failed": 0, "details": []}
    port = int(cfg.get("smtp_port") or 25)
    srv = None

    def _log(owner, to, mode, status, error=""):
        try:
            db.add(MailLog(sender="(每週排程)", owner=owner, to=to or "", cc="",
                           mode=mode, status=status, count=0, error=error or None))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()

    try:
        srv = smtplib.SMTP(host, port, timeout=20)
        if cfg.get("use_tls"):
            srv.starttls()
        for u in admins:
            email = (u.email or "").strip()
            dept = (u.department or "").strip()
            label = u.display_name or u.username
            if not email or not dept:
                summary["skipped"] += 1
                summary["details"].append({"owner": label, "to": email,
                                           "error": "無信箱" if not email else "未設部門"})
                _log(label, email, "weekly", "skip", "無信箱" if not email else "未設部門")
                continue
            try:
                rep = query.weekly_report(db, department=dept, today=today)
                try:   # 與上次匯入比較：主管看數字（失敗就不放，不擋寄信）
                    from . import changes
                    rep["compare"] = changes.compare(db, department=dept, today=today)
                except Exception:  # noqa: BLE001
                    rep["compare"] = None
                subject = "%s%s 部門週報（%s）" % (prefix, dept, today.isoformat())
                body = _weekly_body(rep, dept, site_url)
                envelope = [email] + ([bcc] if bcc and bcc != email else [])
                msg = _build_message(from_addr, email, [], subject, body)
                srv.sendmail(from_addr, envelope, msg.as_string())
                summary["sent"] += 1
                summary["details"].append({"owner": label, "to": email, "dept": dept, "error": ""})
                _log(label, email, "weekly", "ok")
            except Exception as e:  # noqa: BLE001
                summary["failed"] += 1
                summary["details"].append({"owner": label, "to": email, "error": str(e)[:200]})
                _log(label, email, "weekly", "failed", str(e)[:200])
    except (smtplib.SMTPException, OSError) as e:
        raise ValueError("連不到 SMTP 主機：%s" % (str(e)[:200]))
    finally:
        if srv is not None:
            try:
                srv.quit()
            except Exception:  # noqa: BLE001
                pass
    return summary


def send_test(cfg: dict, sender, to: Optional[str] = None) -> dict:
    """寄一封測試信給自己（或指定 to）。回 {ok, message}。不需 enabled，但需 SMTP 設定。"""
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        return {"ok": False, "message": "尚未設定 SMTP 主機"}
    from_addr = sender_from(cfg, sender)
    if not from_addr:
        return {"ok": False, "message": "寄件者無信箱，且未設定系統預設寄件人"}
    dest = (to or "").strip() or from_addr
    today = dt.date.today()
    subject = "%s測試信（%s）" % (cfg.get("subject_prefix") or "", today.isoformat())
    body = "這是一封測試信，用來確認伺服器可連上公司 SMTP relay 並寄出。\n\n寄件者：%s\n收件者：%s\n（本信由弱點彙總系統發送）" % (from_addr, dest)
    port = int(cfg.get("smtp_port") or 25)
    try:
        srv = smtplib.SMTP(host, port, timeout=20)
        if cfg.get("use_tls"):
            srv.starttls()
        msg = _build_message(from_addr, dest, [], subject, body)
        srv.sendmail(from_addr, [dest], msg.as_string())
        try:
            srv.quit()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "message": "已寄出測試信至 %s" % dest}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "寄送失敗：%s" % (str(e)[:200])}

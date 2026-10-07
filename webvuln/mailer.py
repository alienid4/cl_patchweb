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

from . import config, query
from .logic import CLOSE_OPEN, overdue_days
from .query import display_host
from .models import Finding, MailLog, User


# ── 收件人解析 ──
def _user_email(db: Session, display_name: Optional[str]) -> Optional[str]:
    """以負責人顯示名找啟用帳號的信箱（登入時由 AD mail 存入）。"""
    name = (display_name or "").strip()
    if not name:
        return None
    u = db.execute(
        select(User).where(User.display_name == name, User.is_active.is_(True))
    ).scalars().first()
    return (u.email or "").strip() or None if u else None


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


def _resolve_recipient(db: Session, owner: str, dept: Optional[str], cfg: dict) -> tuple:
    """回 (to, mode, reason)。mode: send（負責人本人）/ fallback（轉窗口或系統預設）/ skip。"""
    em = _user_email(db, owner)
    if em:
        return em, "send", ""
    da = _dept_admin_email(db, dept)
    if da:
        return da, "fallback", "查無負責人信箱，轉部門窗口"
    gf = (cfg.get("global_fallback") or "").strip()
    if gf:
        return gf, "fallback", "查無負責人與窗口信箱，轉系統預設"
    return None, "skip", "查無信箱"


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
    fs = [f for f in query._latest_findings(db, department) if f.close_status == CLOSE_OPEN]
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
def _weekly_body(rep: dict, dept: str, site_url: str) -> str:
    ch = rep.get("change") or {}
    tg = rep.get("target") or {}
    lines = [
        "%s 部門弱點週報（%s）" % (dept, rep.get("today", "")),
        "",
        "未結案：%d　逾期：%d　高風險且逾期：%d" % (
            rep.get("unresolved", 0), rep.get("overdue", 0), rep.get("high_risk_overdue", 0)),
        "應申請未申請：%d　已核准展延／例外：%d" % (
            rep.get("need_apply_count", 0), rep.get("applied_count", 0)),
        "預計完成未回報：%d　已逾自訂完成日：%d" % (
            tg.get("no_target", 0), tg.get("target_overdue", 0)),
    ]
    if ch.get("has_prev"):
        lines.append("本週變化：新增 %d、解決 %d（淨 %+d）" % (
            ch.get("new", 0), ch.get("resolved", 0), ch.get("delta", 0)))
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

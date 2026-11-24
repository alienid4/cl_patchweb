"""匯入 AD 名單（V2.111）：讓從沒登入過的負責人也收得到每週催辦信（不掉球）。

核心原則：寄錯人比沒寄到更糟。名字只拿來「找候選人」，綁定一律以員編為準；不確定就不寄本人、轉部門窗口。

來源：Super Admin 上傳 AD 匯出檔（CSV／xlsx），瀏覽器只送 7 欄：
  employeeID, SamAccountName, DisplayName, mail, department, manager, Enabled
電話／分機不送、不存（後端也拒收其他欄位）。

流程：先預覽（不寫入）→ 管理者看過再「確認套用」（以 token 綁定同一份名單＋同一個資料狀態，過期就拒絕）。

規則：
  · 中文名用 names.norm_name；同名一律以「檔案裡所有列」算（含停用、沒員編、沒信箱、資料錯誤的列）
  · 負責人名在檔案裡剛好 1 列、且該列啟用＋有員編＋有效信箱 → 候選人
      - 帳號已綁別的員編 → 身分衝突（不覆蓋，該名字催辦先轉窗口）
      - 第一次自動對上，但 AD 部門跟該負責人未結弱點的部門對不上（相等或互相包含）→ 待確認「部門不符」
      - 其餘 → 綁定（沒帳號就建只收信帳號 mail:<名>，不能登入）
  · 同名多列 → 同名待確認；管理者指定（記誰、何時、哪個員編）後才綁
  · 信箱：只覆蓋／清除 AD 名單帶入的（email_source=ad_import）；手填與來源不明的舊信箱一律不動
  · 清信箱：只有該員編「在檔案裡且 Enabled=FALSE」才清；名單缺席、在職但沒信箱 → 只回報
  · 主管：manager CN 中文名在全檔案剛好 1 列（排除本人）且啟用＋有效信箱 → 主管；否則不設並記原因
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import appsettings, config, mailer, names
from .logic import CLOSE_DONE
from .models import Finding, User
from .query import latest_batch

HIST_KEY = "ad_import_history"
KEEP = 10
FIELDS = ("employeeID", "SamAccountName", "DisplayName", "mail", "department", "manager", "Enabled")
REQUIRED = ("employeeID", "DisplayName", "mail", "Enabled")
SRC_IMPORT = "ad_import"
SRC_MANUAL = "manual"
_MAIL_RE = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
_CN_RE = re.compile(r"(?:^|,)\s*CN=((?:\\.|[^,\\])*)", re.IGNORECASE)

ST_OK, ST_DISABLED, ST_NOEMP, ST_NOMAIL, ST_NONAME, ST_ERROR = "ok", "disabled", "no_employee_id", "no_mail", "no_name", "error"
ST_LABEL = {ST_OK: "可用", ST_DISABLED: "AD 已停用", ST_NOEMP: "沒有員編（多半是服務帳號）", ST_NOMAIL: "在職但沒有信箱",
            ST_NONAME: "顯示名開頭不是中文名", ST_ERROR: "資料錯誤"}


class ImportError_(ValueError):
    pass


def chinese_name(display_name: Any) -> str:
    return names.norm_name(display_name)


def manager_cn(dn: Any) -> str:
    """manager DN 的 CN 值（'CN=趙主管,OU=…' → 趙主管；處理 \\, 跳脫）。"""
    m = _CN_RE.search("" if dn is None else str(dn))
    if not m:
        return ""
    return re.sub(r"\\(.)", r"\1", m.group(1)).strip()


def parse_enabled(v: Any) -> Optional[bool]:
    """Enabled 只認 TRUE／FALSE（不分大小寫）；其他值回 None（列錯誤）。"""
    if isinstance(v, bool):
        return v
    t = ("" if v is None else str(v)).strip().lower()
    return {"true": True, "false": False}.get(t)


def valid_mail(m: str) -> bool:
    return bool(_MAIL_RE.match(m)) and m.count("@") == 1


def _txt(v: Any) -> str:
    return names.collapse(v) if isinstance(v, str) else ""


def build_index(rows: list) -> dict:
    """逐列驗證、分類。回 {all, by_name(全部列), by_emp(非錯誤列), login_map, errors, counts}。"""
    recs: list[dict] = []
    for i, raw in enumerate(rows or []):
        g = (lambda k: raw.get(k)) if isinstance(raw, dict) else (lambda k: getattr(raw, k, None))
        row_no = g("row_no") or (i + 1)
        errs = []
        emp_raw = g("employeeID")
        if emp_raw is not None and not isinstance(emp_raw, str):
            errs.append("員編格式需為文字")
            emp = ""
        else:
            emp = (emp_raw or "").strip()
        en = parse_enabled(g("Enabled"))
        if en is None:
            errs.append("啟用欄需為 TRUE／FALSE")
        mail = (g("mail") or "").strip() if isinstance(g("mail"), str) else ""
        if mail and not valid_mail(mail):
            errs.append("信箱格式不正確")
        dn = g("DisplayName") if isinstance(g("DisplayName"), str) else ""
        p = {"row_no": row_no, "employee_id": emp, "sam": _txt(g("SamAccountName")), "display_name": names.collapse(dn),
             "name": names.norm_name(dn), "mail": mail, "department": _txt(g("department")),
             "manager_cn": names.norm_name(manager_cn(g("manager"))) or manager_cn(g("manager")),
             "enabled": bool(en), "errors": errs}
        recs.append(p)
    # 員編重複：內容完全相同 → 算一列；內容不同 → 全部列為錯誤（不取第一列）
    by_emp_rows: dict[str, list] = {}
    for p in recs:
        if p["employee_id"] and not p["errors"]:
            by_emp_rows.setdefault(p["employee_id"], []).append(p)
    dup_same = 0
    drop = set()
    for e, lst in by_emp_rows.items():
        if len(lst) < 2:
            continue
        sig = {(x["name"], x["mail"].lower(), x["enabled"], x["department"], x["manager_cn"], x["display_name"]) for x in lst}
        if len(sig) == 1:
            dup_same += len(lst) - 1
            drop.update(id(x) for x in lst[1:])
        else:
            for x in lst:
                x["errors"].append("員編重複且內容不同")
    recs = [p for p in recs if id(p) not in drop]
    for p in recs:
        if p["errors"]:
            p["status"] = ST_ERROR
        elif not p["enabled"]:
            p["status"] = ST_DISABLED
        elif not p["employee_id"]:
            p["status"] = ST_NOEMP
        elif not p["mail"]:
            p["status"] = ST_NOMAIL
        elif not p["name"]:
            p["status"] = ST_NONAME
        else:
            p["status"] = ST_OK
    by_name: dict[str, list] = {}
    for p in recs:
        if p["name"]:
            by_name.setdefault(p["name"], []).append(p)
    by_emp = {p["employee_id"]: p for p in recs if p["employee_id"] and p["status"] != ST_ERROR}
    login_map: dict[str, dict] = {}
    for p in recs:
        if p["status"] == ST_ERROR:
            continue
        for k in (p["employee_id"], p["sam"]):
            if k:
                login_map[k.lower()] = p
    counts = {k: 0 for k in ST_LABEL}
    for p in recs:
        counts[p["status"]] += 1
    counts["duplicate_same"] = dup_same
    return {"all": recs, "by_name": by_name, "by_emp": by_emp, "login_map": login_map, "counts": counts,
            "errors": [{"row_no": p["row_no"], "employee_id": p["employee_id"], "display_name": p["display_name"],
                        "error": "、".join(p["errors"])} for p in recs if p["errors"]]}


def resolve_manager(idx: dict, p: dict) -> dict:
    cn = p.get("manager_cn") or ""
    out = {"manager_cn": cn, "name": "", "email": "", "status": ""}
    if not cn:
        out["status"] = "AD 沒填主管"
        return out
    rows = [r for r in idx["by_name"].get(names.owner_key(cn), []) if r is not p and
            not (r["employee_id"] and r["employee_id"] == p.get("employee_id"))]
    if not rows:
        out["status"] = "名單裡查無主管"
    elif len(rows) > 1:
        out["status"] = "主管同名 %d 列，不指定" % len(rows)
    elif rows[0]["status"] != ST_OK:
        out["status"] = "主管%s" % ST_LABEL[rows[0]["status"]]
    else:
        out.update(name=rows[0]["name"], email=rows[0]["mail"], status="已對上")
    return out


def _cand(idx: dict, p: dict) -> dict:
    mg = resolve_manager(idx, p)
    return {"employee_id": p["employee_id"], "display_name": p["display_name"], "department": p["department"],
            "mail": p["mail"], "status": p["status"], "status_label": ST_LABEL[p["status"]],
            "pickable": p["status"] == ST_OK, "row_no": p["row_no"],
            "manager_name": mg["name"], "manager_email": mg["email"], "manager_status": mg["status"]}


def _users_by_key(db: Session, key: str) -> list[User]:
    return [u for u in db.execute(select(User)).scalars().all() if names.owner_key(u.display_name) == key]


def _owner_depts(db: Session) -> dict:
    """最新快照：負責人比對鍵 → 未結弱點的部門集合（沒有未結就用全部）。"""
    b = latest_batch(db)
    if not b:
        return {}
    out: dict[str, dict] = {}
    for owner, dept, cs in db.execute(select(Finding.owner, Finding.department, Finding.close_status)
                                      .where(Finding.batch_id == b.id)).all():
        k = names.owner_key(owner)
        if not k:
            continue
        d = out.setdefault(k, {"open": set(), "all": set()})
        dd = names.collapse(dept).replace(" ", "")
        if dd:
            d["all"].add(dd)
            if cs != CLOSE_DONE:
                d["open"].add(dd)
    return {k: (v["open"] or v["all"]) for k, v in out.items()}


def dept_plausible(ad_dept: str, depts: set) -> bool:
    a = names.collapse(ad_dept).replace(" ", "")
    if not depts:
        return True   # 沒有部門可比對：不擋
    if not a:
        return False
    return any(a == d or a in d or d in a for d in depts)


def _keep_email(u: User) -> bool:
    """手填、或來源不明的舊信箱（AD 登入帶入／舊版補的）→ 視同手填，不覆蓋、不清。"""
    return bool((u.email or "").strip()) and u.email_source != SRC_IMPORT


def _apply(db: Session, key: str, cand: dict, now: dt.datetime, user: Optional[User] = None,
           bound_by: Optional[str] = None) -> tuple[User, str]:
    u = user
    action = ""
    if u is None:
        uname = "mail:" + key
        u = db.execute(select(User).where(User.username == uname)).scalars().first()
        if u is None:
            u = User(username=uname, password_hash=None, display_name=key, role=config.ROLE_USER, is_active=True)
            db.add(u)
            action = "新建只收信帳號"
    mail = cand["mail"]
    if _keep_email(u):
        if u.email.strip().lower() != mail.lower():
            action = action or "保留現有信箱（非 AD 名單帶入；AD 是 %s）" % mail
        else:
            action = action or "不變"
    elif (u.email or "").strip().lower() != mail.lower():
        action = action or ("更新信箱（原 %s）" % u.email if u.email else "補上信箱")
        u.email = mail
        u.email_source = SRC_IMPORT
    else:
        action = action or "不變"
        u.email_source = SRC_IMPORT
    if (u.manager_email or "") != (cand.get("manager_email") or "") and action == "不變":
        action = "主管變更"
    u.email_note = None
    u.ad_conflict = None
    u.employee_id = cand["employee_id"]
    u.ad_department = cand.get("department") or None
    u.manager_name = cand.get("manager_name") or None
    u.manager_email = cand.get("manager_email") or None
    u.email_checked_at = now
    if bound_by:
        u.ad_bound_by = bound_by[:100]
        u.ad_bound_at = now
    return u, action


def _row(name: str, cand: dict, action: str, via: str) -> dict:
    return {"name": name, "employee_id": cand["employee_id"], "mail": cand["mail"], "department": cand.get("department") or "",
            "manager_name": cand.get("manager_name") or "", "manager_email": cand.get("manager_email") or "",
            "manager_status": cand.get("manager_status") or "", "action": action, "via": via}


def rows_hash(rows: list) -> str:
    data = [{k: (r.get(k) if isinstance(r, dict) else getattr(r, k, None)) for k in FIELDS} for r in rows or []]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def state_version(db: Session, owners: list) -> str:
    us = sorted((u.id, u.username, u.display_name, u.email, u.email_source, u.employee_id, u.manager_email,
                 u.ad_conflict, u.ad_bound_by, bool(u.is_active)) for u in db.execute(select(User)).scalars().all())
    return hashlib.sha256(json.dumps([us, sorted(owners)], ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _owners(db: Session) -> list[tuple[str, str]]:
    """最新快照的負責人：(比對鍵, 原名)。"""
    from . import query
    out, seen = [], set()
    for o in query.owners(db):
        k = names.owner_key(o)
        if k and k not in seen:
            seen.add(k)
            out.append((k, o))
    return out


def _recipient_view(db: Session, key: str) -> dict:
    ident = mailer.resolve_owner(db, key)
    if ident["status"] == "ok":
        return {"to": ident["email"], "cc": ident["manager_email"] or "", "why": "本人",
                "cc_why": "AD 主管（升級時）" if ident["manager_email"] else "部門設定（升級時）"}
    return {"to": "", "cc": "", "why": ident["reason"] or "轉部門窗口", "cc_why": "部門設定（升級時；To 轉窗口時不用 AD 主管）"}


def token_for(db: Session, rows: list) -> tuple[str, str]:
    rh = rows_hash(rows)
    sv = state_version(db, [k for k, _ in _owners(db)])
    return rh, hashlib.sha256((rh + "|" + sv).encode()).hexdigest()


def run_import(db: Session, rows: list, file_name: Optional[str], who: Optional[str], apply: bool = False,
               token: Optional[str] = None, sheet: Optional[str] = None, now: Optional[dt.datetime] = None) -> dict:
    """apply=False：預覽（算完整套變更後 rollback，不寫入）；apply=True：token 必須等於預覽那次的 token。"""
    now = now or dt.datetime.now()
    idx = build_index(rows)
    if not idx["counts"][ST_OK]:
        raise ImportError_("名單裡沒有可用的列（啟用＋員編＋有效信箱＋中文名），未匯入")
    rh, tok = token_for(db, rows)
    if apply and token != tok:
        raise ImportError_("STALE")
    owners = _owners(db)
    before = {k: _recipient_view(db, k) for k, _ in owners}
    depts = _owner_depts(db)
    rep: dict = {"at": now.isoformat(timespec="seconds"), "file": (file_name or "")[:200], "sheet": (sheet or "")[:100],
                 "by": who or "", "rows_hash": rh, "rows": len(rows or []), "counts": idx["counts"], "owners": len(owners),
                 "matched": [], "ambiguous": [], "conflicts": [], "notfound": [], "absent": [], "nomail": [],
                 "departed": [], "refreshed": [], "managers": [], "errors": idx["errors"][:2000],
                 "recipient_changes": [], "confirmations": [], "applied": bool(apply)}
    handled: set[int] = set()
    for u in db.execute(select(User)).scalars().all():   # 衝突每次重算
        u.ad_conflict = None
    db.flush()
    for key, raw in owners:
        rws = idx["by_name"].get(key, [])
        us = _users_by_key(db, key)
        bound = [u for u in us if u.ad_bound_by and u.employee_id]
        if bound:   # 人工指定過：只認那個員編
            b = bound[0]
            p = idx["by_emp"].get(b.employee_id)
            if p is not None and p["status"] == ST_OK:
                cv = _cand(idx, p)
                _, action = _apply(db, key, cv, now, user=b)
                handled.add(b.id)
                rep["matched"].append(_row(key, cv, action, "人工指定（%s）" % b.ad_bound_by))
            continue   # 停用／缺席由下面的帳號迴圈處理
        if len(rws) > 1:
            rep["ambiguous"].append({"name": key, "reason": "同名 %d 列" % len(rws), "candidates": [_cand(idx, p) for p in rws]})
            continue
        if not rws:
            rep["notfound"].append({"name": key})
            continue
        p = rws[0]
        if p["status"] != ST_OK:
            ent = {"name": key, "employee_id": p["employee_id"], "status": ST_LABEL[p["status"]]}
            if p["status"] == ST_NOMAIL:
                rep["nomail"].append(dict(ent, display_name=p["display_name"]))
            else:
                rep["notfound"].append(dict(ent, reason=ST_LABEL[p["status"]]))
            continue
        cv = _cand(idx, p)
        same = [u for u in us if u.employee_id == p["employee_id"]]
        other = [u for u in us if u.employee_id and u.employee_id != p["employee_id"]]
        if other:   # 帳號已綁別的員編 → 不覆蓋、不直寄
            for u in us:
                u.ad_conflict = "帳號已綁員編 %s，AD 唯一同名者是 %s" % (other[0].employee_id, p["employee_id"])
            cands = [cv] + [_cand(idx, idx["by_emp"][u.employee_id]) for u in other if u.employee_id in idx["by_emp"]]
            rep["conflicts"].append({"name": key, "reason": "帳號已綁員編 %s ≠ AD %s" % (other[0].employee_id, p["employee_id"]),
                                     "accounts": [{"username": u.username, "employee_id": u.employee_id, "email": u.email or ""}
                                                  for u in us], "candidates": cands})
            continue
        if same:
            target = same[0]
        else:
            free = [u for u in us if not u.employee_id]
            if len(free) > 1:
                for u in free:
                    u.ad_conflict = "同名帳號 %d 個" % len(free)
                rep["conflicts"].append({"name": key, "reason": "系統裡同名帳號 %d 個" % len(free),
                                         "accounts": [{"username": u.username, "employee_id": "", "email": u.email or ""} for u in free],
                                         "candidates": [cv]})
                continue
            if not dept_plausible(p["department"], depts.get(key, set())):
                rep["ambiguous"].append({"name": key, "reason": "部門不符（AD：%s；弱點：%s）" % (
                    p["department"] or "空白", "、".join(sorted(depts.get(key, set())))), "candidates": [cv]})
                continue
            target = free[0] if free else None
        u, action = _apply(db, key, cv, now, user=target)
        db.flush()
        handled.add(u.id)
        rep["matched"].append(_row(key, cv, action, "唯一" if not same else "員編相同"))
    # 其他帳號：以員編／登入帳號（身分）對，不用名字
    for u in db.execute(select(User)).scalars().all():
        if u.id in handled or u.ad_conflict:
            continue
        emp = (u.employee_id or "").strip()
        p = idx["by_emp"].get(emp) if emp else idx["login_map"].get((u.username or "").strip().lower())
        if p is None:
            if emp and emp not in {e["employee_id"] for e in idx["errors"]}:
                rep["absent"].append({"username": u.username, "name": u.display_name or "", "employee_id": emp,
                                      "email": u.email or "", "note": "名單裡查無此員編（只回報，不清信箱）"})
            continue
        if p["status"] == ST_OK:
            cv = _cand(idx, p)
            before_mail, before_mgr = u.email, u.manager_email
            _, action = _apply(db, u.display_name or "", cv, now, user=u)
            if before_mail != u.email or before_mgr != u.manager_email or action not in ("不變",):
                rep["refreshed"].append({"username": u.username, "name": u.display_name or "",
                                         "employee_id": p["employee_id"], "mail": p["mail"], "action": action})
        elif p["status"] == ST_DISABLED:
            if u.email_source == SRC_IMPORT and (u.email or "").strip():
                old = u.email
                u.email = None
                u.email_source = None
                u.manager_name = u.manager_email = None
                u.email_note = "AD 已停用（%s 匯入）" % now.date().isoformat()
                u.email_checked_at = now
                rep["departed"].append({"username": u.username, "name": u.display_name or "", "employee_id": p["employee_id"],
                                        "old_email": old, "reason": "AD 已停用", "cleared": True})
            else:
                rep["departed"].append({"username": u.username, "name": u.display_name or "", "employee_id": p["employee_id"],
                                        "old_email": u.email or "", "cleared": False,
                                        "reason": "AD 已停用（信箱非 AD 名單帶入，未清，請人工確認）" if u.email else "AD 已停用"})
        elif p["status"] == ST_NOMAIL:
            rep["nomail"].append({"name": u.display_name or "", "username": u.username, "employee_id": p["employee_id"],
                                  "display_name": p["display_name"], "status": "在職但沒有信箱（只回報，不清信箱）"})
    db.flush()
    for ent in rep["notfound"] + rep["ambiguous"] + rep["nomail"]:
        if "name" in ent and ent.get("username") is None:
            rv = _recipient_view(db, ent["name"])
            ent["current_email"] = rv["to"]
            ent["note"] = "寄 " + rv["to"] if rv["to"] else "催辦信轉部門窗口"
    for key, raw in owners:
        after = _recipient_view(db, key)
        b = before[key]
        if (b["to"], b["cc"]) != (after["to"], after["cc"]):
            rep["recipient_changes"].append({"name": key, "before_to": b["to"] or "（轉部門窗口）", "before_cc": b["cc"],
                                             "after_to": after["to"] or "（轉部門窗口）", "after_cc": after["cc"],
                                             "to_why": after["why"], "cc_why": after["cc_why"]})
    rep["managers"] = [{"name": m["name"], "employee_id": m["employee_id"], "manager_name": m["manager_name"],
                        "manager_email": m["manager_email"], "status": m["manager_status"]} for m in rep["matched"]]
    rep["token"] = tok
    if not apply:
        db.rollback()
        return rep
    hist = appsettings.get_json(db, HIST_KEY, []) or []
    rep["batch_id"] = (hist[-1].get("batch_id", 0) if hist else 0) + 1
    rep.pop("token", None)
    db.commit()
    appsettings.set_json(db, HIST_KEY, (hist + [rep])[-KEEP:])
    return rep


def history(db: Session) -> list:
    return appsettings.get_json(db, HIST_KEY, []) or []


def last_report(db: Session) -> Optional[dict]:
    h = history(db)
    return h[-1] if h else None


def confirm_pick(db: Session, name: str, employee_id: str, batch_id: Optional[int], who: Optional[str],
                 now: Optional[dt.datetime] = None) -> dict:
    """同名待確認／部門不符／身分衝突：管理者指定「這個負責人＝這位員編」。只能指定最新一次匯入報告裡、可用的候選人。"""
    now = now or dt.datetime.now()
    hist = history(db)
    if not hist:
        raise ImportError_("尚未匯入 AD 名單")
    rep = hist[-1]
    if batch_id is not None and batch_id != rep.get("batch_id"):
        raise ImportError_("報告已不是最新一次匯入，請重開結果再指定")
    key = names.owner_key(name)
    ent, lst = None, None
    for ln in ("ambiguous", "conflicts"):
        ent = next((a for a in rep.get(ln) or [] if a.get("name") == key), None)
        if ent:
            lst = ln
            break
    if ent is None:
        raise ImportError_("「%s」不在待確認清單" % name)
    emp = (employee_id or "").strip()
    cand = next((c for c in ent.get("candidates") or [] if c.get("employee_id") == emp), None)
    if cand is None:
        raise ImportError_("員編 %s 不是「%s」的候選人" % (employee_id, name))
    if not cand.get("pickable", True) or not cand.get("mail"):
        raise ImportError_("員編 %s 在 AD 是「%s」，不能指定" % (emp, cand.get("status_label") or "不可用"))
    us = _users_by_key(db, key)
    target = next((u for u in us if u.employee_id == emp), None)
    if target is None:
        target = db.execute(select(User).where(User.username == "mail:" + key)).scalars().first()
        if target is not None and target.employee_id and target.employee_id != emp and _keep_email(target):
            raise ImportError_("只收信帳號 %s 已綁員編 %s 且信箱為手填，請先到「帳號與權限」處理" % (target.username, target.employee_id))
    u, action = _apply(db, key, cand, now, user=target, bound_by=who or "(未登入)")
    for x in us:   # 已人工指定：同名其他帳號的衝突標記解除（寄信只認指定的這筆）
        if x is not u:
            x.ad_conflict = None
    db.commit()
    rep[lst] = [a for a in rep[lst] if a is not ent]
    row = _row(key, cand, action, "人工指定（%s）" % (who or ""))
    rep["matched"].append(row)
    rep.setdefault("managers", []).append({"name": key, "employee_id": emp, "manager_name": row["manager_name"],
                                           "manager_email": row["manager_email"], "status": row["manager_status"]})
    rep.setdefault("confirmations", []).append({"name": key, "employee_id": emp, "by": who or "", "at": now.isoformat(timespec="seconds")})
    hist[-1] = rep
    appsettings.set_json(db, HIST_KEY, hist)
    return {"report": rep, "user": u.username, "action": action}


def summary_text(rep: dict) -> str:
    c = rep.get("counts") or {}
    return ("batch=%s file=%s sheet=%s rows=%d hash=%s owners=%d matched=%d ambiguous=%d conflicts=%d notfound=%d "
            "absent=%d nomail=%d departed=%d refreshed=%d errors=%d ok=%d disabled=%d" % (
                rep.get("batch_id"), rep.get("file") or "", rep.get("sheet") or "", rep.get("rows", 0),
                (rep.get("rows_hash") or "")[:12], rep.get("owners", 0), len(rep.get("matched") or []),
                len(rep.get("ambiguous") or []), len(rep.get("conflicts") or []), len(rep.get("notfound") or []),
                len(rep.get("absent") or []), len(rep.get("nomail") or []), len(rep.get("departed") or []),
                len(rep.get("refreshed") or []), len(rep.get("errors") or []), c.get(ST_OK, 0), c.get(ST_DISABLED, 0)))

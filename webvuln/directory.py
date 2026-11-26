"""員工名錄（V2.113）：負責人只能從 AD 名單挑，不再自由打字（打錯字＝沒信箱＝掉球）。

  · person：每次「AD 名單匯入・確認套用」時，從檔案裡所有「有員編＋中文名」的列刷新（含停用者，enabled=False）
            不存電話／分機；這次檔案沒有的人 → 名單完整才標停用（不刪）
  · group：管理者在「帳號與權限 › 群組信箱」維護（DBA、主機組…）；負責人名＝群組名 → 寄群組信箱
  · 部門窗口（收件人）：自動寄信的部門設定裡挑名錄的人（存員編＋信箱快照，每次匯入刷新信箱）；
            跟帳號角色無關，不會改任何人的角色或看得到的範圍
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config, deptmap, names
from .models import DirectoryEntry, User

KIND_PERSON = "person"
KIND_GROUP = "group"
SEARCH_LIMIT = 20
OWNER_REQUIRED = "負責人須從名錄選擇"
DEPT_REQUIRED = "部門須從清單選擇"


def person_count(db: Session) -> int:
    return db.execute(select(func.count(DirectoryEntry.id)).where(DirectoryEntry.kind == KIND_PERSON)).scalar() or 0


def active(db: Session) -> bool:
    """名錄啟用（負責人須從名錄選擇、過舊提醒）＝至少套用過一次 AD 名單匯入（有 ad_import 來源的人）。
    只有一個人 AD 登入、或只有手動新增，不算啟用（檢核 #1）。"""
    return (db.execute(select(func.count(DirectoryEntry.id)).where(
        DirectoryEntry.kind == KIND_PERSON, DirectoryEntry.source == "ad_import")).scalar() or 0) > 0


NOT_FOUND_HINT = "名錄查無此人。新進同仁：請他用 AD 登入一次，或請最高管理員在員工名錄手動新增"
NOT_FOUND_HINT_NO_AD = "名錄查無此人。新進同仁：請最高管理員在員工名錄手動新增"
SRC_IMPORT, SRC_LOGIN, SRC_MANUAL = "ad_import", "ad_login", "manual"
DIS_AD, DIS_ABSENT, DIS_MANUAL = "ad_disabled", "absent", "manual"   # 停用原因：AD 名單說停用／名單缺席／管理者手動
FAIL_KEY = "directory_login_failures"
log = logging.getLogger("webvuln.directory")


def last_import_at(db: Session) -> Optional[dt.datetime]:
    from .adimport import history
    for r in reversed(history(db)):
        if r.get("applied") and r.get("at"):
            try:
                return dt.datetime.fromisoformat(r["at"])
            except ValueError:
                continue
    return None


def _ad_login_on(db: Session) -> bool:
    from . import appsettings
    try:
        return bool(appsettings.get_ad_config(db).get("enabled"))
    except Exception:  # noqa: BLE001
        return False


def status(db: Session, now: Optional[dt.datetime] = None, is_super: bool = False) -> dict:
    """名錄狀態：人數、上次匯入 AD 名單時間、是否過舊（門檻在系統設定，預設 35 天）。從沒匯入過 → 不提醒、顯示「尚未匯入」。"""
    from . import appsettings
    now = now or dt.datetime.now()
    g = db.execute(select(func.count(DirectoryEntry.id)).where(DirectoryEntry.kind == KIND_GROUP)).scalar() or 0
    n = person_count(db)
    act = active(db)
    last = last_import_at(db)
    limit = int(appsettings.get_general_config(db).get("directory_stale_days") or 35)
    days = (now - last).days if last else None
    ad_on = _ad_login_on(db)
    stale = ""
    if act and last and days is not None and days > limit:
        stale = "名錄已 %d 天未更新，%s" % (days, "新進同仁可請其 AD 登入一次或由管理員手動新增" if ad_on
                                           else "新進同仁請由管理員手動新增")
        if is_super:
            stale += "；請重新匯入 AD 名單"
    fails = appsettings.get_json(db, FAIL_KEY, {}) or {}
    return {"persons": n, "groups": g, "active": act,
            "warning": "" if act else "尚未匯入 AD 名單，負責人無法驗證",
            "last_import_at": last.isoformat(timespec="seconds") if last else None,
            "last_import_label": last.isoformat(timespec="seconds").replace("T", " ") if last else "尚未匯入",
            "days_since_import": days, "stale_days_limit": limit, "stale_warning": stale,
            "not_found_hint": NOT_FOUND_HINT if ad_on else NOT_FOUND_HINT_NO_AD,
            "login_write_failures": int(fails.get("count") or 0) if is_super else None,
            "login_write_last_error": (fails.get("last_error") or "") if is_super else None}


# ── 匯入時刷新 ──
def absent_candidates(db: Session, in_file: set, file_time: Optional[dt.datetime]) -> tuple[list, list]:
    """這次檔案沒有的在職人員 → (停用候選, 豁免名單)。
    手動新增／AD 登入加入的人：建立或最近登入時間晚於檔案時間，而且只缺席一次 → 豁免；
    連續第 2 次缺席、或比檔案還舊 → 跟一般人一樣是停用候選（算進上限）。"""
    cands, exempt = [], []
    for e in db.execute(select(DirectoryEntry).where(DirectoryEntry.kind == KIND_PERSON,
                                                     DirectoryEntry.enabled.is_(True))).scalars().all():
        if e.employee_id in in_file:
            continue
        if (e.source or SRC_IMPORT) != SRC_IMPORT:
            fresh_at = max([t for t in (e.created_at, e.last_seen) if t] or [dt.datetime.min])
            fresh = file_time is None or fresh_at > file_time
            if fresh and int(e.absent_count or 0) < 1:
                exempt.append(e)
                continue
        cands.append(e)
    return cands, exempt


def refresh_from_import(db: Session, idx: dict, known: Optional[list], blocked: Optional[set],
                        now: dt.datetime, allow_disable: bool, file_time: Optional[dt.datetime] = None,
                        confirm_identity: bool = False, who: Optional[str] = None) -> dict:
    """從 AD 名單（build_index 的結果）刷新 person。不 commit（跟著匯入交易；稽核也一起）。"""
    from .adimport import resolve_manager
    from .models import AuditLog
    res = {"added": 0, "updated": 0, "disabled": 0, "disable_skipped": False, "conflicts": [],
           "manual_confirmed": [], "manual_absent": [], "login_absent": [], "identity_conflicts": [],
           "identity_needs_confirm": False}
    conflict_emps = sorted({p["employee_id"] for p in idx["all"]
                            if p.get("employee_id") and "員編重複且內容不同" in (p.get("errors") or [])})
    res["conflicts"] = conflict_emps
    have = {e.employee_id: e for e in db.execute(select(DirectoryEntry).where(DirectoryEntry.kind == KIND_PERSON)).scalars().all()}
    seen = set()
    for p in idx["all"]:
        if p.get("status") == "error" or not p.get("employee_id") or not p.get("name"):
            continue
        emp = p["employee_id"]
        if emp in seen:
            continue
        seen.add(emp)
        mapped, _ = deptmap.map_ad_department(p.get("department"), known, blocked)
        mg = resolve_manager(idx, p)
        en = bool(p.get("enabled"))
        vals = {"chinese_name": p["name"], "display_name": p.get("display_name") or "", "ad_department": p.get("department") or None,
                "department": mapped, "email": p.get("mail") or None, "enabled": en,
                "manager_name": mg["name"] or None, "manager_email": mg["email"] or None,
                "disabled_reason": None if en else DIS_AD, "absent_count": 0}
        e = have.get(emp)
        if e is None:
            db.add(DirectoryEntry(kind=KIND_PERSON, employee_id=emp, source=SRC_IMPORT, updated_at=now, created_at=now, **vals))
            res["added"] += 1
            continue
        if e.source != SRC_IMPORT:   # 手動新增／AD 登入加入的人 → AD 名單確認
            if e.chinese_name != p["name"]:   # 名字不同＝可能不是同一人 → 要管理者確認（檢核 #8）
                res["identity_conflicts"].append({"employee_id": emp, "old_name": e.chinese_name, "new_name": p["name"],
                                                  "source": e.source})
                if not confirm_identity:
                    res["identity_needs_confirm"] = True
                    continue
            if e.source == SRC_MANUAL:
                res["manual_confirmed"].append({"name": p["name"], "employee_id": emp, "old_name": e.chinese_name})
            db.add(AuditLog(username=(who or "(系統)")[:100], action="directory_source_convert", target=("person:%s" % emp)[:200],
                            detail="source: %s → ad_import；name: %s → %s" % (e.source, e.chinese_name, p["name"])))
            e.source = SRC_IMPORT
            e.updated_at = now
        if any(getattr(e, k) != v for k, v in vals.items()):
            for k, v in vals.items():
                setattr(e, k, v)
            e.updated_at = now
            res["updated"] += 1
    in_file = {p["employee_id"] for p in idx["all"] if p.get("employee_id")}
    cands, exempt = absent_candidates(db, in_file | set(conflict_emps), file_time)
    res["manual_absent"] = [{"name": e.chinese_name, "employee_id": e.employee_id} for e in exempt if e.source == SRC_MANUAL]
    res["login_absent"] = [{"name": e.chinese_name, "employee_id": e.employee_id} for e in exempt if e.source == SRC_LOGIN]
    for e in exempt:
        e.absent_count = int(e.absent_count or 0) + 1
    if cands and not allow_disable:
        res["disable_skipped"] = True
    else:
        for e in cands:
            e.enabled = False
            e.disabled_reason = DIS_ABSENT
            e.absent_count = int(e.absent_count or 0) + 1
            e.updated_at = now
            res["disabled"] += 1
    db.flush()
    return res


# ── AD 登入／手動新增 ──
def record_login_failure(db: Session, err: str) -> None:
    from . import appsettings
    try:
        cur = appsettings.get_json(db, FAIL_KEY, {}) or {}
        appsettings.set_json(db, FAIL_KEY, {"count": int(cur.get("count") or 0) + 1, "last_error": err[:300],
                                            "last_at": dt.datetime.now().isoformat(timespec="seconds")})
    except Exception:  # noqa: BLE001
        db.rollback()


def sync_login(db: Session, attrs: dict, login: str, ip: Optional[str], now: Optional[dt.datetime] = None) -> str:
    """AD 登入成功後寫名錄（在 api_login 裡呼叫，有 IP 可記稽核）。回動作說明（測試／稽核用）。
    條件：AD 恰好找到一筆、而且讀得到員編屬性；否則不寫（記 warning）。"""
    from . import security
    now = now or dt.datetime.now()
    if int(attrs.get("_matches") or 0) != 1:
        log.warning("AD 登入 %s：AD 查到 %s 筆，不寫名錄", login, attrs.get("_matches"))
        return "AD 查到的不是剛好一筆，不寫名錄"
    emp = names.collapse(attrs.get("employee_id"))
    if not emp:
        log.warning("AD 登入 %s：讀不到員編屬性，不寫名錄", login)
        return "讀不到員編屬性，不寫名錄"
    name_attr = attrs.get("name")
    if not names.norm_name(name_attr):
        return "顯示名不是中文名，不寫名錄"
    name = names.owner_key(name_attr)
    from .adimport import valid_mail
    em = names.collapse(attrs.get("email"))
    em = em if em and valid_mail(em) else None
    ad_dept = deptmap.clean(attrs.get("department")) or None
    known = deptmap.known_departments(db)
    others = [x for (x,) in db.execute(select(DirectoryEntry.ad_department).where(
        DirectoryEntry.ad_department.isnot(None)).distinct()).all()]
    coll = deptmap.collisions(others + ([ad_dept] if ad_dept else []), known)
    blocked = {deptmap.norm_ad_dept(r) for lst in coll.values() for r in lst}
    mapped, _ = deptmap.map_ad_department(ad_dept, known, blocked)   # 撞名／多於一個 → 不對應（同匯入規則，檢核 #10）
    e = person_by_emp(db, emp)
    before = None if e is None else {"name": e.chinese_name, "enabled": e.enabled, "source": e.source, "email": e.email,
                                     "department": e.department, "disabled_reason": e.disabled_reason}
    action = ""
    if e is None:
        same = len(persons_by_name(db, name))
        e = DirectoryEntry(kind=KIND_PERSON, employee_id=emp, chinese_name=name, display_name=names.collapse(name_attr),
                           ad_department=ad_dept, department=mapped, email=em, enabled=True,
                           source=SRC_LOGIN, updated_at=now, created_at=now, last_seen=now)
        db.add(e)
        action = "AD 登入加入名錄" + ("（既有 %d 位同名在職，催辦會轉部門窗口）" % same if same else "")
    else:
        e.last_seen = now
        if e.source == SRC_MANUAL and e.chinese_name != name:   # 手動新增的人名字不同 → 不合併，記衝突（檢核 #3）
            e.login_conflict = "AD 登入名字「%s」≠ 名錄「%s」" % (name, e.chinese_name)
            action = "手動新增與 AD 登入名字不同，未合併"
        else:
            if not e.enabled:
                if e.disabled_reason == DIS_AD:   # AD 名單說停用、現在能登入 → 比名單新 → 恢復（檢核 #9）
                    e.enabled, e.disabled_reason = True, None
                    action = "AD 登入成功，恢復在職"
                else:   # 名單缺席或管理者手動停用 → 不自動恢復，列給管理者
                    e.login_conflict = "已停用（%s）但 AD 登入成功，請確認" % {DIS_ABSENT: "名單缺席", DIS_MANUAL: "管理者停用"}.get(
                        e.disabled_reason, e.disabled_reason or "原因不明")
                    action = "已停用但 AD 登入成功，未自動恢復"
            if e.source == SRC_IMPORT:   # 名單為準：只補空白欄
                if not e.email and em:
                    e.email = em
                if not e.ad_department and ad_dept:
                    e.ad_department, e.department = ad_dept, mapped
            elif e.source == SRC_LOGIN:
                e.chinese_name, e.display_name = name, names.collapse(name_attr)
                e.ad_department, e.department = ad_dept or e.ad_department, mapped if ad_dept else e.department
                e.email = em or e.email
                e.updated_at = now
            elif e.source == SRC_MANUAL and e.chinese_name == name:
                e.email = e.email or em
                e.updated_at = now
            action = action or "更新登入時間"
    db.flush()
    after = {"name": e.chinese_name, "enabled": e.enabled, "source": e.source, "email": e.email,
             "department": e.department, "disabled_reason": e.disabled_reason}
    if before is None or before != after or action not in ("更新登入時間",):
        security.log_audit(db, username=login, action="directory_login_sync", target="person:%s" % emp,
                           detail="%s；before=%s after=%s" % (action, before, after), ip=ip)
    else:
        db.commit()
    return action


_EMP_RE = __import__("re").compile(r"^[A-Za-z0-9]{1,50}$")
_ASCII_NAME = __import__("re").compile(r"^[A-Za-z][A-Za-z .'\-]{0,99}$")


def manual_name_key(name) -> str:
    """手動新增的姓名：中文姓名（不可夾英文，避免默默截斷）；或純英文名（照原樣、只完全相同才算）。"""
    raw = names.collapse(name)
    if not raw:
        raise ValueError("姓名必填")
    cjk = names.norm_name(raw)
    if cjk:
        if cjk != raw.replace(" ", ""):
            raise ValueError("姓名請只填中文姓名（系統會存成「%s」），請修正後再存" % cjk)
        return cjk
    if _ASCII_NAME.match(raw):
        return raw
    raise ValueError("姓名請填中文姓名，或純英文名")


def save_manual_person(db: Session, pid: Optional[int], name: str, emp: str, department: Optional[str],
                       email: str, enabled: bool, now: dt.datetime, confirm_same_name: bool = False) -> DirectoryEntry:
    """最高管理員手動新增／編輯名錄人員（source=manual）。不 commit（呼叫端加稽核一起存）。
    AD 匯入或 AD 登入來的列不能在這裡改（以 AD 為準）；員編建立後不能改（停用後另新增）。"""
    from .adimport import valid_mail
    key = manual_name_key(name)
    emp = (emp or "").strip()
    if not _EMP_RE.match(emp):
        raise ValueError("員編只能是英數字（1～50 碼）")
    em = names.collapse(email)
    if not em or not valid_mail(em):
        raise ValueError("信箱格式不正確（一個信箱）")
    domain_err = check_email_domain(db, em)
    if domain_err:
        raise ValueError(domain_err)
    dep = names.collapse(department) or None
    if not dep or dep not in departments(db):
        raise ValueError(DEPT_REQUIRED)
    if group_by_name(db, key, enabled_only=False) is not None:
        raise ValueError("姓名跟群組信箱同名：%s" % key)
    other = person_by_emp(db, emp)
    if pid:
        e = db.get(DirectoryEntry, pid)
        if e is None or e.kind != KIND_PERSON:
            raise ValueError("名錄人員不存在")
        if e.source != SRC_MANUAL:
            raise ValueError("這筆來自 AD（名單匯入或登入），以 AD 為準，不能手動修改")
        if emp != e.employee_id:
            raise ValueError("員編建立後不能改；請把這筆停用，再新增一筆")
    else:
        if other is not None:
            raise ValueError("員編已存在")
        e = None
    same = [p for p in persons_by_name(db, key) if e is None or p.id != e.id]
    if same and enabled and not confirm_same_name:
        raise SameNameError("既有 %d 位同名，存檔後催辦會轉部門窗口" % len(same))
    if e is None:
        e = DirectoryEntry(kind=KIND_PERSON, source=SRC_MANUAL, created_at=now)
        db.add(e)
        e.employee_id = emp
    e.chinese_name, e.display_name = key, key
    e.department, e.email, e.enabled, e.updated_at = dep, em, bool(enabled), now
    e.disabled_reason = None if enabled else DIS_MANUAL
    db.flush()
    return e


class SameNameError(ValueError):
    pass


def allowed_domains(db: Session) -> Optional[list]:
    """允許的信箱網域（系統設定）。沒設定過 → 用名錄（AD 匯入）裡現有信箱的網域；設成空清單＝不限制。"""
    from . import appsettings
    v = appsettings.get_general_config(db).get("allowed_email_domains")
    if v is None:
        doms = sorted({(x or "").split("@")[-1].lower() for (x,) in db.execute(select(DirectoryEntry.email).where(
            DirectoryEntry.source == SRC_IMPORT, DirectoryEntry.email.isnot(None))).all() if x and "@" in x})
        return doms or None
    return [d.lower() for d in v] or None


def check_email_domain(db: Session, em: str) -> Optional[str]:
    doms = allowed_domains(db)
    if not doms:
        return None
    d = em.split("@")[-1].lower()
    return None if d in doms else "信箱網域須為：%s（系統設定可調整）" % "、".join(doms)


# ── 查詢 ──
def disabled_reason(db: Session, u) -> Optional[str]:
    """帳號有綁員編、名錄說這個人已停用 → 回原因（所有 To／CC 都要排除）；沒綁員編或名錄沒這人 → None。"""
    emp = (getattr(u, "employee_id", None) or "").strip()
    if not emp:
        return None
    p = person_by_emp(db, emp)
    if p is not None and not p.enabled:
        return "%s 名錄顯示已停用" % (getattr(u, "display_name", None) or getattr(u, "username", ""))
    return None


def email_disabled(db: Session, em: Optional[str]) -> bool:
    """這個信箱在名錄只屬於已停用的人（例如 AD 主管離職）→ True。"""
    em = (em or "").strip().lower()
    if not em:
        return False
    rows = [e for e in db.execute(select(DirectoryEntry).where(DirectoryEntry.kind == KIND_PERSON)).scalars().all()
            if (e.email or "").strip().lower() == em]
    return bool(rows) and not any(e.enabled for e in rows)


def group_key(name) -> str:
    return names.owner_key(name)



def person_by_emp(db: Session, emp) -> Optional[DirectoryEntry]:
    emp = (emp or "").strip() if isinstance(emp, str) else ""
    if not emp:
        return None
    return db.execute(select(DirectoryEntry).where(DirectoryEntry.kind == KIND_PERSON,
                                                   DirectoryEntry.employee_id == emp)).scalars().first()


def persons_by_name(db: Session, name, enabled_only: bool = True) -> list[DirectoryEntry]:
    key = names.owner_key(name)
    if not key:
        return []
    q = select(DirectoryEntry).where(DirectoryEntry.kind == KIND_PERSON, DirectoryEntry.chinese_name == key)
    if enabled_only:
        q = q.where(DirectoryEntry.enabled.is_(True))
    return list(db.execute(q).scalars().all())


def group_by_name(db: Session, name, enabled_only: bool = True) -> Optional[DirectoryEntry]:
    key = group_key(name)
    if not key:
        return None
    q = select(DirectoryEntry).where(DirectoryEntry.kind == KIND_GROUP, DirectoryEntry.group_key == key)
    g = db.execute(q).scalars().first()
    if g is None or (enabled_only and not g.enabled):
        return None
    return g


def _mask(emp: Optional[str], full: bool) -> str:
    """員編遮罩：非最高管理員只看末 4 碼；4 碼以下整個不顯示。"""
    emp = emp or ""
    if full:
        return emp
    return "" if len(emp) <= 4 else "*" * (len(emp) - 4) + emp[-4:]


def search(db: Session, q: str, full: bool) -> list[dict]:
    """名錄搜尋：只回啟用的人＋啟用的群組；最多 20 筆。排序：完全相同 → 開頭相同 → 包含；同分依名字。"""
    t = names.collapse(q)
    key = names.owner_key(q) or t
    if len(t) < 2:   # 至少 2 個字
        return []
    out = []
    rows = db.execute(select(DirectoryEntry).where(DirectoryEntry.enabled.is_(True))).scalars().all()
    for e in rows:
        nm = e.chinese_name or ""
        hay_dep = (e.department or "") + " " + (e.ad_department or "")
        if nm == key:
            rank = 0
        elif nm.startswith(key):
            rank = 1
        elif key in nm:
            rank = 2
        elif full and e.employee_id and t in e.employee_id:
            rank = 3
        elif len(t) >= 2 and t in hay_dep:
            rank = 4
        else:
            continue
        out.append((rank, e.kind != KIND_GROUP, nm, e.employee_id or "", e))
    out.sort(key=lambda x: x[:4])
    res = []
    for rank, _, nm, _, e in out[:SEARCH_LIMIT]:
        # pick_id＝名錄列的不透明 id（不外露員編）；不給信箱、主管
        res.append({"kind": e.kind, "name": nm, "department": e.department or e.ad_department or "",
                    "employee_id": _mask(e.employee_id, full) if e.kind == KIND_PERSON else "",
                    "pick_id": "d:%d" % e.id})
    return res


def by_pick(db: Session, pick_id: Optional[str]) -> Optional[DirectoryEntry]:
    t = (pick_id or "").strip()
    if not t.startswith("d:") or not t[2:].isdigit():
        return None
    return db.get(DirectoryEntry, int(t[2:]))


def listing(db: Session) -> list[dict]:
    rows = db.execute(select(DirectoryEntry).order_by(DirectoryEntry.kind, DirectoryEntry.chinese_name)).scalars().all()
    same_count: dict = {}
    for e in rows:
        if e.kind == KIND_PERSON and e.enabled:
            same_count[e.chinese_name] = same_count.get(e.chinese_name, 0) + 1
    return [{"id": e.id, "kind": e.kind, "employee_id": e.employee_id, "name": e.chinese_name, "display_name": e.display_name,
             "ad_department": e.ad_department, "department": e.department, "email": e.email, "enabled": bool(e.enabled),
             "manager_name": e.manager_name, "manager_email": e.manager_email, "source": e.source,
             "last_seen": e.last_seen.isoformat(timespec="seconds") if e.last_seen else None,
             "disabled_reason": e.disabled_reason, "login_conflict": e.login_conflict,
             "same_name": (same_count.get(e.chinese_name, 1) - 1) if e.kind == KIND_PERSON and e.enabled else 0,
             "updated_at": e.updated_at.isoformat(timespec="seconds") if e.updated_at else None} for e in rows]


def departments(db: Session) -> list[str]:
    """編輯視窗的部門清單：已知部門（弱點＋自動寄信設定）＋名錄裡對應到的部門＋群組部門。"""
    out = set(deptmap.known_departments(db) or [])
    for (d,) in db.execute(select(DirectoryEntry.department).where(DirectoryEntry.department.isnot(None)).distinct()).all():
        if d and d.strip():
            out.add(d)
    return sorted(out)


# ── 群組 ──
def save_group(db: Session, gid: Optional[int], name: str, email: str, department: Optional[str],
               enabled: bool, now: dt.datetime) -> DirectoryEntry:
    from .adimport import valid_mail
    nm = names.collapse(name)
    em = names.collapse(email)
    if not nm:
        raise ValueError("群組名稱必填")
    if not em or not valid_mail(em):
        raise ValueError("群組信箱格式不正確")
    derr = check_email_domain(db, em)
    if derr:
        raise ValueError(derr)
    dep = names.collapse(department) or None
    if dep and dep not in departments(db):
        raise ValueError(DEPT_REQUIRED)
    gk = group_key(nm)
    other = group_by_name(db, nm, enabled_only=False)
    if other is not None and other.id != gid:
        raise ValueError("群組名稱重複：%s" % nm)
    if persons_by_name(db, nm, enabled_only=False):
        raise ValueError("群組名稱跟名錄裡的人同名：%s（請改用群組專用名稱）" % nm)
    if gid:
        g = db.get(DirectoryEntry, gid)
        if g is None or g.kind != KIND_GROUP:
            raise ValueError("群組不存在")
    else:
        g = DirectoryEntry(kind=KIND_GROUP, source="manual")
        db.add(g)
    g.chinese_name, g.display_name, g.email, g.department, g.enabled, g.updated_at = nm, nm, em, dep, bool(enabled), now
    g.group_key = gk
    db.flush()   # 不 commit：呼叫端加稽核後一起 commit（同一個交易；group_key 唯一限制在這裡就會擋下）
    return g


# ── 負責人／部門驗證與帳號連結 ──
def check_owner(db: Session, new_owner: Optional[str], current: Optional[str], pick_id: Optional[str]) -> dict:
    """回 {ok, error, kind, person}。空字串（回到 Excel 值）與不變一律放行；名錄未啟用也放行（舊行為）。"""
    out = {"ok": True, "error": "", "kind": "", "person": None}
    v = names.collapse(new_owner)
    if not v or v == names.collapse(current) or not active(db):
        return out
    picked = by_pick(db, pick_id) if pick_id else None
    g = group_by_name(db, v)
    if g is not None and (picked is None or picked.id == g.id):   # 群組優先（群組名不會跟名錄的人同名）
        out["kind"] = KIND_GROUP
        return out
    ps = [p for p in persons_by_name(db, v) if p.chinese_name == v]   # 要跟名錄名字完全相同（「陳小美X」不算）
    if pick_id:
        ps = [p for p in ps if picked is not None and p.id == picked.id]
    if not ps:
        return {"ok": False, "error": OWNER_REQUIRED, "kind": "", "person": None}
    if len(ps) > 1:
        return {"ok": False, "error": "同名多人，請從名錄挑選（依部門／員編）", "kind": "", "person": None}
    out.update(kind=KIND_PERSON, person=ps[0])
    return out


def check_department(db: Session, new_dept: Optional[str], current: Optional[str]) -> Optional[str]:
    v = names.collapse(new_dept)
    if not v or v == names.collapse(current) or not active(db):
        return None
    return None if v in {names.collapse(d) for d in departments(db)} else DEPT_REQUIRED


def link_account(db: Session, p: DirectoryEntry, who: Optional[str], now: Optional[dt.datetime] = None) -> str:
    """挑了名錄的人當負責人：有綁這個員編的帳號就好；這個名字完全沒有帳號 → 建只收信帳號（比照 AD 名單匯入）。
    已有同名、但沒綁或綁別的員編的帳號 → 不動（避免綁錯人），回提示。"""
    now = now or dt.datetime.now()
    key = p.chinese_name
    us = [u for u in db.execute(select(User)).scalars().all() if names.owner_key(u.display_name) == key]
    if any(u.employee_id == p.employee_id for u in us):
        return "已有帳號"
    if us:
        return "已有同名帳號（未綁此員編），寄信以帳號為準"
    if not p.email:
        return "名錄沒有信箱，未建帳號"
    u = User(username="mail:" + key, password_hash=None, display_name=key, role=config.ROLE_USER, is_active=True,
             email=p.email, email_source="ad_import", employee_id=p.employee_id, manager_name=p.manager_name,
             manager_email=p.manager_email, email_checked_at=now)
    deptmap.apply_department(u, p.ad_department, deptmap.known_departments(db), db=db, trigger="owner_pick", who=who)
    db.add(u)
    db.flush()   # 不 commit：跟疊加層、稽核同一個交易
    return "已建只收信帳號"


# ── 部門窗口（收件人）──
def dept_windows(db: Session, dept: Optional[str], windows: Optional[list] = None) -> tuple[list, str, list]:
    """(信箱清單, 依據, 略過說明)。順序：①設定的部門窗口（名錄在職、有信箱；信箱寄送當下從名錄取）
    ②部門窗口角色帳號（舊；有綁員編且名錄顯示停用的排除）。windows＝呼叫端那一份設定快照（None＝自己讀）。"""
    dept = (dept or "").strip()
    if not dept:
        return [], "", []
    notes = []
    if windows is None:
        from . import automail
        try:
            ent = next((d for d in automail.get_settings(db).get("departments") or []
                        if isinstance(d, dict) and d.get("department") == dept), None)
        except Exception:  # noqa: BLE001
            ent = None
        windows = (ent or {}).get("windows") or []
    emails = []
    for w in windows or []:
        p = person_by_emp(db, w.get("employee_id"))
        label = (w.get("name") or (p.chinese_name if p else "") or "（未命名）")
        if p is None:
            notes.append("%s 名錄查無，略過" % label)
        elif not p.enabled:
            notes.append("%s AD 已停用，略過" % label)
        elif not (p.email or "").strip():
            notes.append("%s 沒有信箱，略過" % label)
        elif p.email not in emails:
            emails.append(p.email)
    if emails:
        return emails, "設定窗口", notes
    rows = db.execute(select(User).where(User.department == dept, User.is_active.is_(True))).scalars().all()
    for u in rows:
        if config.canon_role(u.role) == config.ROLE_DEPT_ADMIN and (u.email or "").strip() and u.email.strip() not in emails:
            why = disabled_reason(db, u)
            if why:
                notes.append(why + "，略過")
                continue
            emails.append(u.email.strip())
    if emails:
        return emails, "角色窗口", notes
    return [], "", notes


def clean_windows(db: Session, raw, prev: Optional[list] = None) -> list[dict]:
    """部門設定存檔：只存員編＋姓名（信箱寄送時才從名錄取）。
    已存在的窗口就算後來停用也保留（標示，不擋存檔）；新加的才驗證（名錄在職、有信箱）。
    raw 每筆：{employee_id}（既有）或 {dir_id}（新挑的名錄列）。"""
    prev_emps = {(w.get("employee_id") or "") for w in (prev or []) if isinstance(w, dict)}
    out, seen = [], set()
    for w in raw or []:
        if not isinstance(w, dict):
            continue
        p = None
        if w.get("dir_id") is not None:
            p = by_pick(db, "d:%s" % w.get("dir_id"))
            if p is None or p.kind != KIND_PERSON:
                raise ValueError("部門窗口須從名錄選擇")
            emp = p.employee_id
        else:
            emp = str(w.get("employee_id") or "").strip()
            p = person_by_emp(db, emp)
        if not emp or emp in seen:
            continue
        if emp not in prev_emps:   # 新加的
            if p is None or not p.enabled:
                raise ValueError("部門窗口須從名錄選擇（不在名錄或已停用）")
            if not (p.email or "").strip():
                raise ValueError("部門窗口 %s 沒有信箱" % p.chinese_name)
            derr = check_email_domain(db, p.email.strip())
            if derr:
                raise ValueError("部門窗口 %s：%s" % (p.chinese_name, derr))
        seen.add(emp)
        out.append({"employee_id": emp, "name": (p.chinese_name if p else (w.get("name") or ""))})
    return out


def windows_view(db: Session, windows: Optional[list]) -> list[dict]:
    """設定畫面顯示：姓名、信箱與狀態都從名錄現取。"""
    out = []
    for w in windows or []:
        p = person_by_emp(db, w.get("employee_id"))
        out.append({"employee_id": w.get("employee_id"), "name": (p.chinese_name if p else w.get("name") or ""),
                    "email": (p.email if p else "") or "", "enabled": bool(p and p.enabled),
                    "status": "名錄查無" if p is None else ("在職" if p.enabled else "AD 已停用")})
    return out

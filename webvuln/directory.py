"""員工名錄（V2.113）：負責人只能從 AD 名單挑，不再自由打字（打錯字＝沒信箱＝掉球）。

  · person：每次「AD 名單匯入・確認套用」時，從檔案裡所有「有員編＋中文名」的列刷新（含停用者，enabled=False）
            不存電話／分機；這次檔案沒有的人 → 名單完整才標停用（不刪）
  · group：管理者在「帳號與權限 › 群組信箱」維護（DBA、主機組…）；負責人名＝群組名 → 寄群組信箱
  · 部門窗口（收件人）：自動寄信的部門設定裡挑名錄的人（存員編＋信箱快照，每次匯入刷新信箱）；
            跟帳號角色無關，不會改任何人的角色或看得到的範圍
"""
from __future__ import annotations

import datetime as dt
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
    """名錄有人（匯入過 AD 名單）才啟用「負責人須從名錄選擇」；還沒匯入時維持舊的自由輸入。"""
    return person_count(db) > 0


def status(db: Session) -> dict:
    g = db.execute(select(func.count(DirectoryEntry.id)).where(DirectoryEntry.kind == KIND_GROUP)).scalar() or 0
    n = person_count(db)
    return {"persons": n, "groups": g, "active": n > 0,
            "warning": "" if n else "尚未匯入 AD 名單，負責人無法驗證"}


# ── 匯入時刷新 ──
def refresh_from_import(db: Session, idx: dict, known: Optional[list], blocked: Optional[set],
                        now: dt.datetime, allow_disable: bool) -> dict:
    """從 AD 名單（build_index 的結果）刷新 person。不 commit（跟著匯入交易）。"""
    from .adimport import resolve_manager
    res = {"added": 0, "updated": 0, "disabled": 0, "disable_skipped": False, "conflicts": []}
    # 員編重複且內容不同（build_index 標成錯誤）→ 這些人這次不刷新、也不因「缺席」被標停用，列衝突（不取第一列）
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
        vals = {"chinese_name": p["name"], "display_name": p.get("display_name") or "", "ad_department": p.get("department") or None,
                "department": mapped, "email": p.get("mail") or None, "enabled": bool(p.get("enabled")),
                "manager_name": mg["name"] or None, "manager_email": mg["email"] or None}
        e = have.get(emp)
        if e is None:
            db.add(DirectoryEntry(kind=KIND_PERSON, employee_id=emp, source="ad_import", updated_at=now, **vals))
            res["added"] += 1
        elif any(getattr(e, k) != v for k, v in vals.items()):
            for k, v in vals.items():
                setattr(e, k, v)
            e.updated_at = now
            res["updated"] += 1
    absent = [e for k, e in have.items() if k not in seen and k not in conflict_emps and e.enabled]
    if absent and not allow_disable:
        res["disable_skipped"] = True
    else:
        for e in absent:
            e.enabled = False
            e.updated_at = now
            res["disabled"] += 1
    db.flush()
    return res


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
    return [{"id": e.id, "kind": e.kind, "employee_id": e.employee_id, "name": e.chinese_name, "display_name": e.display_name,
             "ad_department": e.ad_department, "department": e.department, "email": e.email, "enabled": bool(e.enabled),
             "manager_name": e.manager_name, "manager_email": e.manager_email, "source": e.source,
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

"""AD 部門 → Excel 部門短名自動對應（V2.112）。

AD 的 department 長這樣：「甲處資訊架構部(1234567)」；資安 Excel（Finding.department）用「資訊架構部」。
部門窗口、看得到的範圍都要求部門名完全相同，所以 AD 帶進來的長名永遠對不上。

規則（map_detail／map_ad_department）：
  · AD 端正規化：去頭尾空白、只拿掉「結尾一組、內容全是數字」的括號代碼（半形 (123) 或全形（１２３），括號要成對），
    其他括號說明（如「（海外）」）保留；空白全部拿掉
  · Excel 端（候選）只拿掉空白，不去括號
  · 候選＝最新一批弱點的部門＋自動寄信設定的部門；讀不到 → 不對應（「候選清單讀取失敗」）
  · AD 端 == 候選，或以候選結尾 → 符合；取最長；同長度有兩個不同的 → 多於一個（不對應）
寫入規則（apply_department）：
  · 原始 AD 值一律存 ad_department
  · 部門窗口／最高管理員：部門是權限範圍，自動流程一律不改，只回「建議部門」
  · 其他帳號：department 空白或上次也是自動對應才改；手填（manual）或來源不明不動
  · 每一次自動改部門都記一筆稽核（觸發來源、原始 AD 值、舊→新部門、舊→新來源）
  · 不會自動給「部門窗口」角色
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config

SRC_AUTO = "ad_auto"
SRC_MANUAL = "manual"
READ_FAIL = "候選清單讀取失敗"
_TRAIL_CODE = re.compile(r"\s*(?:\([0-9０-９]+\)|（[0-9０-９]+）)\s*$")
_WS = re.compile(r"[\s　﻿\u200b\u200c\u200d\u2060]+")
# 結尾相符時，前面剩下的部分要是空的、或以組織單位字結尾（擋掉「資安營運部」→「營運部」）
ORG_UNIT_SUFFIX = ("處", "室", "中心", "部", "組", "分公司")


def clean(s) -> str:
    """共用清理（登入、AD 名單、寫入部門都用）：去 BOM／零寬字元、空白收成一個、去頭尾。"""
    return _WS.sub(" ", "" if s is None else str(s)).strip()


def norm_excel_dept(s) -> str:
    return _WS.sub("", "" if s is None else str(s))


def norm_ad_dept(s) -> str:
    t = _WS.sub(" ", "" if s is None else str(s)).strip()
    t = _TRAIL_CODE.sub("", t, count=1)
    return _WS.sub("", t)


norm_dept = norm_ad_dept   # 相容舊名


def looks_like_ad_long_name(s) -> bool:
    t = ("" if s is None else str(s)).strip()
    return bool(t) and bool(_TRAIL_CODE.search(t))


def _suffix_ok(n: str, dn: str) -> bool:
    if n == dn:
        return True
    if not n.endswith(dn):
        return False
    prefix = n[:-len(dn)]
    return prefix.endswith(ORG_UNIT_SUFFIX)


def map_detail(ad_dept, known: Optional[Iterable[str]], blocked: Optional[set] = None) -> dict:
    """回 {raw, normalized, mapped, reason, candidates}。known=None＝候選讀取失敗。
    mapped 是候選原字串（＝Finding.department 存的那個字串，不是正規化後的）。
    blocked＝這次匯入裡「多個不同 AD 部門對到同一個短名」的 AD 端正規化名 → 不對應。"""
    n = norm_ad_dept(ad_dept)
    out = {"raw": clean(ad_dept), "normalized": n, "mapped": None, "reason": "", "candidates": []}
    if known is None:
        out["reason"] = READ_FAIL
        return out
    if not n:
        out["reason"] = "AD 部門空白"
        return out
    cands: dict[str, str] = {}   # 原字串 → 正規化；同長度兩個不同原字串＝多於一個
    for d in known:
        if d is None or not str(d).strip():
            continue
        orig = str(d)
        dn = norm_excel_dept(orig)
        if dn and _suffix_ok(n, dn):
            cands[orig] = dn
    if not cands:
        out["reason"] = "對不到"
        return out
    longest = max(len(v) for v in cands.values())
    top = sorted(k for k, v in cands.items() if len(v) == longest)
    out["candidates"] = sorted(cands, key=lambda k: (-len(cands[k]), k))
    if len(top) > 1:
        out["reason"] = "多於一個"
        return out
    if blocked and n in blocked:
        out["reason"] = "多個 AD 部門對到同一個短名"
        return out
    out["mapped"], out["reason"] = top[0], ("相同" if cands[top[0]] == n else "結尾相符")
    return out


def map_ad_department(ad_dept, known: Optional[Iterable[str]], blocked: Optional[set] = None) -> tuple[Optional[str], str]:
    d = map_detail(ad_dept, known, blocked)
    return d["mapped"], d["reason"]


def collisions(raw_depts: Iterable[str], known: Optional[Iterable[str]]) -> dict:
    """同一批 AD 部門裡，兩個以上不同的 AD 部門對到同一個短名 → {短名: [AD 部門…]}。"""
    by: dict[str, dict] = {}
    for r in raw_depts:
        n = norm_ad_dept(r)
        m, _ = map_ad_department(r, known)
        if m:
            by.setdefault(m, {}).setdefault(n, clean(r))
    return {m: sorted(v.values()) for m, v in by.items() if len(v) > 1}


def candidate_departments(db: Session) -> tuple[Optional[list], str]:
    """(候選清單, 錯誤)。任一來源讀取失敗 → (None, 原因)，呼叫端不做部分對應。"""
    from .models import Finding
    from .query import latest_batch
    out = set()
    try:
        b = latest_batch(db)
        if b:
            for (d,) in db.execute(select(Finding.department).where(Finding.batch_id == b.id).distinct()).all():
                if d and str(d).strip():
                    out.add(str(d))   # 原字串照存：對應結果要跟 Finding.department 完全相同
    except Exception as e:  # noqa: BLE001
        return None, "%s（弱點部門：%s）" % (READ_FAIL, str(e)[:100])
    try:
        from . import automail
        for ent in automail.get_settings(db).get("departments") or []:
            nm = ent.get("department") or ""
            if nm.strip():
                out.add(nm)
    except Exception as e:  # noqa: BLE001
        return None, "%s（自動寄信設定：%s）" % (READ_FAIL, str(e)[:100])
    return sorted(out), ""


def known_departments(db: Session) -> Optional[list]:
    return candidate_departments(db)[0]


def is_privileged(u) -> bool:
    """部門窗口／最高管理員：部門是權限範圍，自動流程不改。"""
    return config.canon_role(u.role or "") in (config.ROLE_DEPT_ADMIN, config.ROLE_SUPER)


def _audit(db: Optional[Session], u, trigger: str, raw: str, old: Optional[str], new: Optional[str],
           old_src: Optional[str], new_src: Optional[str], who: Optional[str]) -> None:
    """不 commit：跟著呼叫端的交易一起進（預覽 rollback 就一起不算）。"""
    if db is None:
        return
    from .models import AuditLog
    db.add(AuditLog(username=(who or "(系統)")[:100], action="dept_auto_change", target=("user:%s" % u.username)[:200],
                    detail="trigger=%s raw=%s dept: %s → %s source: %s → %s" % (
                        trigger, raw, old or "（空）", new or "（空）", old_src or "（空）", new_src or "（空）")))


def apply_department(u, ad_dept, known: Optional[Iterable[str]], db: Optional[Session] = None,
                     trigger: str = "", who: Optional[str] = None, blocked: Optional[set] = None) -> dict:
    """存原始 AD 部門；依規則決定要不要改 department。回 {changed, cleared, mapped, reason, suggestion}。"""
    raw = clean(ad_dept)
    prev_raw = u.ad_department
    u.ad_department = raw or None
    mapped, reason = map_ad_department(raw, known, blocked)
    out = {"changed": False, "cleared": False, "mapped": mapped, "reason": reason, "suggestion": None}
    if is_privileged(u):
        if mapped and mapped != u.department:
            out["suggestion"] = mapped   # 只建議，不寫
        return out
    if (u.department or "").strip() and u.department_source != SRC_AUTO:
        return out   # 手填或來源不明 → 不動
    if not mapped:
        # 上次是自動對應、AD 部門換了、新的對不到／不明確 → 清掉（留原始值），免得卡在舊部門；讀取失敗不清
        if (u.department_source == SRC_AUTO and (u.department or "").strip() and not reason.startswith(READ_FAIL)
                and norm_ad_dept(prev_raw) != norm_ad_dept(raw)):
            old = u.department
            u.department = None
            u.department_source = None
            out["cleared"] = True
            _audit(db, u, trigger, raw, old, None, SRC_AUTO, None, who)
        return out
    if mapped != u.department or u.department_source != SRC_AUTO:
        old, old_src = u.department, u.department_source
        u.department = mapped
        u.department_source = SRC_AUTO
        if old != mapped:
            out["changed"] = True
            _audit(db, u, trigger, raw, old, mapped, old_src, SRC_AUTO, who)
    return out


def cleanup_legacy(db: Session) -> dict:
    """一次性清理（V2.112，開機時跑，冪等）：舊版 AD 登入把 AD 長名直接寫進 department（來源不明）。
    條件：來源不明、非部門窗口／最高管理員、部門＝原始 AD 部門或結尾有數字代碼括號；
    原始 AD 部門有值但跟舊部門不同 → 衝突，不改。候選清單讀不到或是空的 → 整批不做。
    資料修改＋逐筆稽核＋總結稽核在同一個 commit。回 {changed, conflicts, skipped_reason}。"""
    from .models import AuditLog, User
    res = {"changed": 0, "conflicts": [], "skipped_reason": ""}
    rows = [u for u in db.execute(select(User).where(User.department_source.is_(None))).scalars().all()
            if (u.department or "").strip()]
    todo = []
    for u in rows:
        dep = u.department.strip()
        raw = (u.ad_department or "").strip()
        if not (dep == raw or looks_like_ad_long_name(dep)):
            continue
        if is_privileged(u):
            continue
        if raw and norm_ad_dept(raw) != norm_ad_dept(dep):
            res["conflicts"].append(u.username)
            continue
        todo.append(u)
    if not todo:
        return res
    known, err = candidate_departments(db)
    if known is None or not known:
        res["skipped_reason"] = err or "候選部門清單是空的"
        return res
    for u in todo:
        dep = u.department.strip()
        mapped, _ = map_ad_department(dep, known)
        if not mapped:
            continue
        old_src = u.department_source
        u.ad_department = u.ad_department or dep
        u.department = mapped
        u.department_source = SRC_AUTO
        _audit(db, u, "cleanup", dep, dep, mapped, old_src, SRC_AUTO, None)
        res["changed"] += 1
    if res["changed"] or res["conflicts"]:
        db.add(AuditLog(username="(系統)", action="dept_legacy_cleanup",
                        detail="舊 AD 長名部門自動對應成 Excel 短名 %d 筆；原始值不一致未改 %d 筆%s" % (
                            res["changed"], len(res["conflicts"]),
                            ("（" + "、".join(res["conflicts"][:20]) + "）") if res["conflicts"] else "")))
        db.commit()
    return res

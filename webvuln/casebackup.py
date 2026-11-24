"""承辦狀態備份／還原（處理進度、預計完成日、追蹤備註、改過的負責人／部門）。

用途（使用者 2026-10-05）：把一台機器上承辦標過的狀態匯出成檔案，在另一台（或同一台出事後）匯回，
方便兩台對照分析。只帶「有人動過」的記錄；不帶 Excel 內容、不帶附件實體檔。

還原的安全設計：
  - 先預覽（對得上幾筆、會新增幾筆、跟現有資料衝突幾筆、對不到幾筆），確認才寫
  - 衝突預設「保留現有」，要勾才覆蓋
  - 兩台的識別碼版本（舊鍵 / v2）不同就拒絕：鍵的格式不同，對了也是錯的
  - 寫入前先備份 SQLite 檔；整段一個交易
"""
from __future__ import annotations

import datetime as dt
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import rowkey
from .logic import CLOSE_OPEN, PROGRESS_SEC_CONFIRMED, PROGRESS_VALUES
from .models import Case, Finding
from .query import latest_batch, vuln_key

FORMAT = "webvuln-case-backup"
# confirm_*：資安已確認結案的證據（2026-10-28），跟處理進度一起搬；衝突規則同其他欄
FIELDS = ("status", "track_note", "note", "target_date", "owner_override", "department_override",
          "confirm_by", "confirm_date", "confirm_basis")
_TEXT = ("track_note", "note", "owner_override", "department_override", "confirm_by", "confirm_basis")


def _scheme(session: Session) -> str:
    return "v2" if rowkey.migrated(session) else "v1"


def _val(c: Case, k: str):
    v = getattr(c, k)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v if v not in ("",) else None


def export(session: Session) -> dict:
    b = latest_batch(session)
    items = []
    for c in session.execute(select(Case).order_by(Case.id)).scalars().all():
        if not rowkey.has_manual(c):
            continue
        items.append({
            "vuln_key": c.vuln_key, "sheet_key": c.sheet_key, "plugin_id": c.plugin_id, "host": c.host,
            "owner": c.owner, "department": c.department,
            **{k: _val(c, k) for k in FIELDS},
            "status_changed_at": _val(c, "status_changed_at"), "updated_at": _val(c, "updated_at"),
        })
    return {"format": FORMAT, "version": 1, "key_scheme": _scheme(session),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "source_file": b.source_file if b else None,
            "imported_at": b.imported_at.isoformat() if (b and b.imported_at) else None,
            "count": len(items), "items": items}


def _norm_item(it: dict) -> dict:
    out = {k: it.get(k) for k in FIELDS}
    if out["status"] not in PROGRESS_VALUES:
        out["status"] = ""                       # 舊預設「未申請」之類不搬
    td = out.get("target_date")
    out["target_date"] = dt.date.fromisoformat(td[:10]) if td else None
    cd = out.get("confirm_date")
    out["confirm_date"] = dt.date.fromisoformat(cd[:10]) if cd else None
    for k in _TEXT:
        v = out.get(k)
        out[k] = v if (v is not None and str(v).strip()) else None
    if out["status"] != PROGRESS_SEC_CONFIRMED:   # 證據只跟著資安已確認結案走（舊備份檔沒有這三欄＝None）
        out["confirm_by"] = out["confirm_date"] = out["confirm_basis"] = None
    return out


def _same(c: Case, new: dict) -> bool:
    cur = {k: getattr(c, k) for k in FIELDS}
    if cur["status"] not in PROGRESS_VALUES:
        cur["status"] = ""
    for k in _TEXT:
        if not (cur[k] or "").strip():
            cur[k] = None
    if cur["status"] != PROGRESS_SEC_CONFIRMED:
        cur["confirm_by"] = cur["confirm_date"] = cur["confirm_basis"] = None
    return all(cur[k] == new[k] for k in new)   # 只比要寫的欄（略過的狀態欄不算）


def restore(session: Session, data: dict, overwrite: bool = False, apply: bool = False,
            db_path: Optional[str] = None) -> dict:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("不是承辦狀態備份檔")
    here = _scheme(session)
    if data.get("key_scheme") != here:
        raise ValueError(f"兩台的識別碼版本不同（備份檔 {data.get('key_scheme')}，這台 {here}），"
                         "請兩台都升到同一版再做")
    b = latest_batch(session)
    live_keys = set()
    live_close: dict = {}
    if b:
        for f in session.execute(select(Finding).where(Finding.batch_id == b.id).order_by(Finding.id)).scalars().all():
            live_keys.add("|".join(vuln_key(f)))
            live_close.setdefault("|".join(vuln_key(f)), f.close_status)
    cases = {c.vuln_key: c for c in session.execute(select(Case)).scalars().all()}

    rep = {"total": 0, "new": 0, "same": 0, "conflict": 0, "overwritten": 0, "skipped_conflict": 0,
           "unmatched": 0, "unmatched_samples": [], "conflict_samples": [],
           "backup_source_file": data.get("source_file"), "here_source_file": b.source_file if b else None,
           "applied": False, "db_backup": None, "warnings": []}
    plan = []
    for it in data.get("items") or []:
        rep["total"] += 1
        k = it.get("vuln_key")
        new = _norm_item(it)
        c = cases.get(k)
        desc = f"{(it.get('sheet_key') or '')[:12]} {it.get('host') or ''} {it.get('plugin_id') or ''}".strip()
        # 資安已確認結案要有完整證據、且這台的這筆 Excel 仍未結案，才照搬（2026-10-28）；
        # 不合格就略過「處理進度＋證據」這幾欄（其他欄照常），預覽列出原因
        if new["status"] == PROGRESS_SEC_CONFIRMED:
            why = None
            if not (new["confirm_by"] and new["confirm_date"] and new["confirm_basis"]):
                why = "缺證據"
            elif new["confirm_date"] > dt.date.today():
                why = "確認日期晚於今天"
            elif k in live_close and live_close[k] != CLOSE_OPEN:
                why = "這台 Excel 已不是未結案"
            if why:
                rep["warnings"].append(f"{desc}：資安已確認結案{why}，處理進度不還原")
                for fk in ("status", "confirm_by", "confirm_date", "confirm_basis"):
                    new.pop(fk)
        if c is None and k not in live_keys:
            rep["unmatched"] += 1
            if len(rep["unmatched_samples"]) < 20:
                rep["unmatched_samples"].append(desc)
            continue
        if c is not None and rowkey.has_manual(c):
            if _same(c, new):
                rep["same"] += 1
                continue
            rep["conflict"] += 1
            if len(rep["conflict_samples"]) < 20:
                rep["conflict_samples"].append(desc)
            if not overwrite:
                rep["skipped_conflict"] += 1
                continue
            rep["overwritten"] += 1
        else:
            rep["new"] += 1
        plan.append((k, c, it, new))

    if not apply:
        return rep

    if db_path and Path(db_path).exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = f"{db_path}.bak-before-case-restore-{ts}"
        shutil.copy2(db_path, dst)
        for suf in ("-wal", "-shm"):
            if Path(db_path + suf).exists():
                shutil.copy2(db_path + suf, dst + suf)
        rep["db_backup"] = dst
    now = datetime.now()
    for k, c, it, new in plan:
        sca = it.get("status_changed_at")
        if c is None:
            c = Case(vuln_key=k, sheet_key=it.get("sheet_key"), plugin_id=it.get("plugin_id"), host=it.get("host"),
                     department=it.get("department"), owner=it.get("owner"), status="", is_orphan=False)
            session.add(c)
        for f, v in new.items():
            setattr(c, f, v if f != "status" else (v or ""))
        # 沿用來源機的推進時間（可疑聲稱判定要比對它跟匯入時間）；沒有才用現在
        c.status_changed_at = datetime.fromisoformat(sca) if sca else now
    session.commit()
    # 改過的負責人／部門要同時套到目前這批弱點資料，畫面與統計才會照新的算
    # （2026-10-05 漏了這步：還原後 221 的資訊架構部仍算進已改到別部門的 6 筆）
    if b:
        from . import cases as _cases
        rep["overrides_applied"] = _cases.apply_owner_overrides(session, b)
    rep["applied"] = True
    return rep

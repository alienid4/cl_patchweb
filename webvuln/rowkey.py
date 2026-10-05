"""每一列的穩定識別碼（row_key），承辦疊加層／附件／趨勢都靠它對到「同一列」。

為什麼要有（2026-10-05 用 9/22 真實報告實測）：
舊的鍵是「工作表＋Plugin＋主機」，同一台主機同一個 Plugin 開在不同埠就撞在一起
（2-IoT 有 567 列共用鍵），8-BAS 與 10-外部威脅情資根本沒有 Plugin 和主機（全表共用一個鍵）。
結果是標一列「處理中」，同鍵的其他列一起變。

規則：
  1. 依工作表編號，取「能唯一認出一列、又不會隨處理進度變動」的欄（見 SHEET_KEY_COLS）。
     到期日、結案狀態、備註、負責人這類會變的欄一律不用，否則重匯後就對不上。
  2. 沒設定的工作表（或那幾欄全空）退回 Plugin＋主機。
  3. 組合後仍相同的列，依在 Excel 裡的先後加 #2、#3…（使用者 2026-10-05 同意）。
  4. 「中*4 低*2」計數列展開出的多筆是同一列，共用同一個鍵、不加序號。
鍵以 "v2|" 開頭，跟舊格式（sheet|plugin|host）一眼分得出來。
"""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

PREFIX = "v2|"
MIGRATION_FLAG = "rowkey_v2_migrated"

# 工作表編號 → 認一列用的欄（欄名比對：去空白、全形轉半形、不分大小寫；可寫欄名開頭）
SHEET_KEY_COLS: dict[str, list[str]] = {
    "1": ["Plugin ID", "Host", "Port", "Protocol", "年度"],
    "2": ["Plugin ID", "Host", "Port", "Protocol", "年度"],
    "3": ["案件ID", "識別指標", "內部Host IP", "資產名稱", "Destination Port", "年度"],
    "4": ["Name", "Host", "資產名稱", "Type", "年度"],
    "5": ["Name", "Host", "資產名稱", "年度"],
    "6": ["風險項目", "標的", "標的IP"],
    "7": ["風險項目", "標的", "標的IP"],
    "8": ["Audit Name", "年度"],
    "9": ["網址", "IP"],
    "10": ["外部情資", "資產名稱"],
}


def _norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = unicodedata.normalize("NFKC", str(v))
    return re.sub(r"\s+", "", s).lower()


def _sheet_no(sheet_key: Optional[str]) -> Optional[str]:
    m = re.match(r"^\s*(\d+)\s*[-–－]", sheet_key or "")
    return m.group(1) if m else None


def _col_value(raw: dict, want: str) -> str:
    w = _norm(want)
    keys = list(raw.keys())
    for k in keys:                       # 先精確
        if _norm(k) == w:
            return _norm(raw[k])
    for k in keys:                       # 再前綴（如「案件ID\n(Rolledup…)」）
        if _norm(k).startswith(w):
            return _norm(raw[k])
    return ""


def base_key(sheet_key, plugin_id, host, raw: Optional[dict]) -> str:
    cols = SHEET_KEY_COLS.get(_sheet_no(sheet_key) or "")
    parts: list[str] = []
    if cols and raw:
        parts = [_col_value(raw, c) for c in cols]
    if not any(parts):                   # 沒設定或那幾欄全空 → 退回 Plugin＋主機
        parts = [_norm(plugin_id), _norm(host)]
    return PREFIX + (sheet_key or "") + "|" + "|".join(parts)


def assign(findings: Iterable) -> None:
    """依匯入順序（finding.id 或 list 順序）替同一批的 finding 填 row_key。"""
    seen: dict[str, int] = defaultdict(int)
    prev_raw = None
    prev_key = None
    prev_sheet = None
    for f in findings:
        raw = f.raw or {}
        # 計數列展開：連續且原始列完全相同 → 同一列，沿用上一個鍵
        if prev_key is not None and raw and raw == prev_raw and f.sheet_key == prev_sheet:
            f.row_key = prev_key
            continue
        b = base_key(f.sheet_key, f.plugin_id, f.host, raw)
        seen[b] += 1
        k = b if seen[b] == 1 else f"{b}#{seen[b]}"
        f.row_key = k
        prev_raw, prev_key, prev_sheet = raw, k, f.sheet_key


def has_manual(c) -> bool:
    """這筆承辦記錄有沒有人動過（處理進度、備註、預計完成日、改過負責人/部門）。舊預設狀態「未申請」不算。"""
    from .logic import PROGRESS_VALUES
    return bool((c.status in PROGRESS_VALUES) or (c.track_note or "").strip() or (c.note or "").strip()
                or c.target_date or (c.owner_override or "").strip() or (c.department_override or "").strip())


def legacy_key(f) -> str:
    """舊格式鍵（sheet|plugin|host），轉換時用來找舊的承辦記錄。"""
    host = (f.host or "").strip().lower()
    return "|".join(((f.sheet_key or ""), (f.plugin_id or ""), host))


def migrate(session, db_path: Optional[str] = None, log=print) -> Optional[dict]:
    """一次性轉換：替所有既有 finding 補 row_key，承辦記錄與附件改掛新鍵。

    - 原本只對到 1 列：直接改鍵
    - 原本一筆蓋住多列：複製到每一列（使用者 2026-10-05 選 A）
    - 對不到最新快照的（孤兒）：維持舊鍵不動
    已轉過就直接回 None（冪等）。整段一個交易；動手前先備份 SQLite 檔。
    """
    from sqlalchemy import select

    from .models import AppSetting, Attachment, Case, Finding, ImportBatch

    if session.execute(select(AppSetting).where(AppSetting.key == MIGRATION_FLAG)).scalar_one_or_none():
        return None

    backup = None
    if db_path and Path(db_path).exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for suf in ("", "-wal", "-shm"):
            src = Path(db_path + suf)
            if src.exists():
                dst = Path(f"{db_path}{suf}.bak-before-rowkey-{ts}")
                shutil.copy2(src, dst)
                if suf == "":
                    backup = str(dst)

    # 1) 每一批都補 row_key（趨勢／本期新結案要比對前後兩批，舊批也要有）
    batches = session.execute(select(ImportBatch).order_by(ImportBatch.id)).scalars().all()
    for b in batches:
        fs = session.execute(select(Finding).where(Finding.batch_id == b.id).order_by(Finding.id)).scalars().all()
        assign(fs)

    latest = next((b for b in reversed(batches) if b.is_latest), None)
    fan: dict[str, list[str]] = defaultdict(list)       # 舊鍵 → 最新快照裡對到的新鍵（依序、去重）
    if latest:
        for f in session.execute(select(Finding).where(Finding.batch_id == latest.id).order_by(Finding.id)).scalars().all():
            lk = legacy_key(f)
            if f.row_key not in fan[lk]:
                fan[lk].append(f.row_key)

    report = {"one_to_one": 0, "copied": [], "orphan_kept": 0, "attachments_moved": 0, "attachments_copied": 0,
              "backup": backup, "at": datetime.now().isoformat(timespec="seconds")}
    COPY_FIELDS = ("sheet_key", "plugin_id", "host", "department", "owner", "owner_override",
                   "department_override", "track_note", "target_date", "status", "note",
                   "last_seen_batch_id", "is_orphan", "source_closed", "created_at", "status_changed_at")

    # 2) 承辦記錄
    for c in session.execute(select(Case)).scalars().all():
        if c.vuln_key.startswith(PREFIX):
            continue
        targets = fan.get(c.vuln_key) or []
        if not targets:
            report["orphan_kept"] += 1
            continue
        first, rest = targets[0], targets[1:]
        for k in rest:
            session.add(Case(vuln_key=k, **{a: getattr(c, a) for a in COPY_FIELDS}))
        if rest:
            report["copied"].append({"old_key": c.vuln_key, "rows": len(targets), "manual": has_manual(c),
                                     "status": c.status or "", "has_note": bool((c.track_note or "").strip()),
                                     "target_date": c.target_date.isoformat() if c.target_date else None})
        else:
            report["one_to_one"] += 1
        c.vuln_key = first

    # 3) 附件（同一份實體檔，多掛幾筆 metadata；刪到最後一個參照才刪檔，原機制不變）
    A_FIELDS = ("sheet_key", "plugin_id", "host", "kind", "orig_name", "stored_name", "sha256",
                "size", "content_type", "uploaded_by", "uploaded_at")
    for a in session.execute(select(Attachment)).scalars().all():
        if a.vuln_key.startswith(PREFIX):
            continue
        targets = fan.get(a.vuln_key) or []
        if not targets:
            continue
        for k in targets[1:]:
            session.add(Attachment(vuln_key=k, **{x: getattr(a, x) for x in A_FIELDS}))
            report["attachments_copied"] += 1
        a.vuln_key = targets[0]
        report["attachments_moved"] += 1

    session.add(AppSetting(key=MIGRATION_FLAG, value=json.dumps(report, ensure_ascii=False)))
    session.commit()
    report["copied_manual"] = sum(1 for x in report["copied"] if x["manual"])
    log(f"[rowkey] 轉換完成：1:1 轉移 {report['one_to_one']} 筆、一筆複製到多列 {len(report['copied'])} 筆"
        f"（其中有人工資料 {report['copied_manual']} 筆）、"
        f"對不到最新快照維持原樣 {report['orphan_kept']} 筆；附件改掛 {report['attachments_moved']}、"
        f"複製 {report['attachments_copied']}；備份 {backup}")
    return report


def migrated(session) -> bool:
    from sqlalchemy import select

    from .models import AppSetting
    return session.execute(select(AppSetting).where(AppSetting.key == MIGRATION_FLAG)).scalar_one_or_none() is not None

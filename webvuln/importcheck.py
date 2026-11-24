"""匯入檢核報告＋規則版本就地重算（V2.110，2026-11-23）。

起因（同一天三件真實事故）：
  (a) 資安改了某張表的格式（單號欄、期限寫成「DMZ 2026/9/27\\nintranet 2027/2/27」文字），系統默默判錯；
  (b) 升級新規則後，最新一批存的 effective_due／stage 還是舊規則算的，要有人重匯才會變（週報逾期 2、畫面 1）；
  (c) 右上角顯示舊檔名，有人可能拿舊 Excel 蓋掉新資料。
這裡負責：
  1. build_report：匯入時產出檢核報告（不擋匯入），存在 ImportBatch.import_report。
     - 前端才知道的（日期格讀不到、已申請卻找不到日期、解析警告）由前端 sheets.js checkImport 帶來；
     - 後端算的：缺期限、工作表少了／變 0 筆、欄位不見、上一批未結這批整筆消失、內容跟上一批一模一樣。
  2. older_conflict：檔名的資料日期（VYYYYMMDD）比目前最新一批舊 → API 回 409，要使用者明確確認才覆蓋。
  3. recompute：規則版本不同時，用前端（同一套解析）重算的日期就地更新最新一批，不新增批次。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import logic
from .models import Finding, ImportBatch, SheetColumns

REPORT_VERSION = 1
LIST_CAP = 3000          # 每份清單最多存幾筆（整張表不見時「消失的項目」可能上千筆；計數照實）
_SHEET_NO = re.compile(r"^\s*\d+\s*[-–－]")   # 同 profiles.js numberedSheetPattern
_DATA_DATE = re.compile(r"[Vv](\d{4})(\d{2})(\d{2})(?!\d)")


def data_date(name: Optional[str]) -> Optional[dt.date]:
    """檔名裡的資料日期：「…V20261116…」→ 2026-11-16；沒有或不合法 → None（不猜）。"""
    if not name:
        return None
    best = None
    for m in _DATA_DATE.finditer(str(name)):
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        best = d if best is None or d > best else best
    return best


def _latest(session: Session) -> Optional[ImportBatch]:
    return session.execute(select(ImportBatch).where(ImportBatch.is_latest.is_(True))
                           .order_by(ImportBatch.id.desc())).scalars().first()


def older_conflict(session: Session, source_file: Optional[str]) -> Optional[dict]:
    """新檔的資料日期比目前最新一批舊 → 回衝突內容；否則 None（任一邊讀不出日期就不擋）。"""
    cur = _latest(session)
    if cur is None:
        return None
    nd, od = data_date(source_file), data_date(cur.source_file)
    if nd is None or od is None or nd >= od:
        return None
    return {"new_file": source_file, "new_date": nd.isoformat(),
            "latest_file": cur.source_file, "latest_date": od.isoformat(),
            "latest_batch_id": cur.id,
            "latest_imported_at": cur.imported_at.isoformat(timespec="minutes") if cur.imported_at else None}


def missing_sheets_conflict(session: Session, findings, sheet_columns: Optional[dict]) -> Optional[list]:
    """上一批（目前最新）有資料的編號工作表，這次整張不見或 0 筆 → 回清單；否則 None。
    跟舊檔防呆一樣要使用者明確確認（GPT 檢核：整張表漏貼是最大的掉球風險）。"""
    cur = _latest(session)
    if cur is None:
        return None
    from sqlalchemy import func
    prev_cnt = dict(session.execute(select(Finding.sheet_key, func.count(Finding.id))
                                    .where(Finding.batch_id == cur.id).group_by(Finding.sheet_key)).all())
    new_cnt = Counter(getattr(f, "sheet_key", None) for f in findings)
    cols = set((sheet_columns or {}).keys())
    out = [{"sheet": k, "prev_rows": n, "now_rows": 0,
            "status": "這次 0 筆" if k in cols else "整張不見（可能改名）"}
           for k, n in sorted(prev_cnt.items(), key=lambda x: x[0] or "")
           if _numbered(k) and n > 0 and new_cnt.get(k, 0) == 0]
    return out or None


class LatestChanged(Exception):
    """使用者確認時看到的「目前最新一批」已經不是最新（有人剛匯入／刪批）→ 確認作廢，要重來。"""


def content_sig(findings) -> str:
    """整批內容指紋（工作表＋原始列，依序）：跟上一批一樣＝重匯同一份檔（只提醒，不擋）。"""
    h = hashlib.sha1()
    for f in findings:
        h.update(json.dumps([f.sheet_key, f.raw], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _key(f: Finding) -> str:
    from .query import vuln_key
    return "|".join(vuln_key(f))


def _host(f: Finding) -> Optional[str]:
    from .query import display_host
    return display_host(f)


def _cap(rows: list) -> tuple[list, bool]:
    return (rows[:LIST_CAP], len(rows) > LIST_CAP)


def _numbered(k: Optional[str]) -> bool:
    return bool(k and _SHEET_NO.match(k))


def build_report(session: Session, batch: ImportBatch, prev: Optional[ImportBatch],
                 row_no: dict, sheet_columns: dict, client_checks: Optional[dict],
                 rules_version: Optional[str], older: Optional[dict] = None,
                 sheets_missing_confirmed: Optional[list] = None) -> dict:
    """匯入檢核報告。row_no＝{finding id: Excel 列號}（前端帶來的，沒有就 None）。"""
    cc = client_checks or {}
    new_fs = session.execute(select(Finding).where(Finding.batch_id == batch.id)
                             .order_by(Finding.id)).scalars().all()
    row_no = row_no or {}

    # ── 前端檢核（原樣收下，只截長度）──
    def _clean(lst):
        out = []
        for x in (lst or []):
            if isinstance(x, dict):
                out.append({k: (str(v)[:300] if isinstance(v, str) else v) for k, v in x.items()})
        return out
    bad_dates = _clean(cc.get("bad_dates"))
    apply_no_date = _clean(cc.get("apply_no_date"))
    parse_warnings = _clean(cc.get("parse_warnings"))
    bad_by = defaultdict(list)
    for b in bad_dates:
        bad_by[(b.get("sheet"), b.get("row"))].append(f"{b.get('column') or b.get('field')}：{b.get('raw')}")

    # ── 缺期限：未結、沒有可用到期日 ──
    no_due = []
    for f in new_fs:
        if f.close_status == logic.CLOSE_OPEN and f.effective_due is None:
            rn = row_no.get(f.id)
            no_due.append({"sheet": f.sheet_key, "row": rn, "host": _host(f), "name": f.name,
                           "owner": f.owner, "department": f.department,
                           "raw": "；".join(bad_by.get((f.sheet_key, rn), [])) or "（期限欄空白）"})

    # ── 工作表／欄位 ──
    new_cnt = Counter(f.sheet_key for f in new_fs)
    new_cols = {k: [c for c in (v or []) if str(c or "").strip()] for k, v in (sheet_columns or {}).items()}
    sheets_missing, columns_gone, columns_new, sheets_new = [], [], [], []
    disappeared, dis_by = [], Counter()
    same_content = False
    sig = content_sig(new_fs)
    if prev is not None:
        prev_fs = session.execute(select(Finding).where(Finding.batch_id == prev.id)).scalars().all()
        prev_cnt = Counter(f.sheet_key for f in prev_fs)
        prev_cols = {sc.sheet_key: [c for c in (sc.columns or []) if str(c or "").strip()]
                     for sc in session.execute(select(SheetColumns).where(SheetColumns.batch_id == prev.id)).scalars()}
        for k in sorted(set(prev_cnt) | set(prev_cols), key=lambda s: (s or "")):
            if not _numbered(k) or prev_cnt.get(k, 0) == 0:
                continue
            if new_cnt.get(k, 0) == 0:
                sheets_missing.append({"sheet": k, "prev_rows": prev_cnt.get(k, 0), "now_rows": 0,
                                       "status": "這次 0 筆" if k in new_cols else "整張不見（可能改名）"})
        for k in new_cols:
            if _numbered(k) and k not in prev_cols and prev_cnt.get(k, 0) == 0:
                sheets_new.append({"sheet": k, "now_rows": new_cnt.get(k, 0)})
        for k, cols in new_cols.items():
            if k not in prev_cols:
                continue
            gone = [c for c in prev_cols[k] if c not in cols]
            added = [c for c in cols if c not in prev_cols[k]]
            if gone:
                columns_gone.append({"sheet": k, "columns": gone})
            if added:
                columns_new.append({"sheet": k, "columns": added})
        # 上一批未結、這批整筆消失（不是變成已結案，是根本不在這批）
        new_keys = {_key(f) for f in new_fs}
        for f in prev_fs:
            if f.close_status == logic.CLOSE_OPEN and _key(f) not in new_keys:
                dis_by[f.sheet_key] += 1
                disappeared.append({"sheet": f.sheet_key, "host": _host(f), "name": f.name, "owner": f.owner,
                                    "department": f.department,
                                    "effective_due": f.effective_due.isoformat() if f.effective_due else None,
                                    "stage": f.stage})
        prev_rep = prev.import_report if isinstance(prev.import_report, dict) else None
        prev_sig = (prev_rep or {}).get("content_sig") or content_sig(prev_fs)
        same_content = (prev_sig == sig)

    lists = {}
    trunc = {}
    for name, rows in (("bad_dates", bad_dates), ("no_due", no_due), ("apply_no_date", apply_no_date),
                       ("disappeared", disappeared), ("parse_warnings", parse_warnings)):
        lists[name], trunc[name] = _cap(rows)
    counts = {
        "bad_dates": len(bad_dates), "no_due": len(no_due), "apply_no_date": len(apply_no_date),
        "sheets_missing": len(sheets_missing), "columns_gone": sum(len(x["columns"]) for x in columns_gone),
        "disappeared": len(disappeared), "parse_warnings": len(parse_warnings),
    }
    return {
        "version": REPORT_VERSION,
        "batch_id": batch.id, "source_file": batch.source_file,
        "data_date": (data_date(batch.source_file).isoformat() if data_date(batch.source_file) else None),
        "prev_batch_id": prev.id if prev else None, "prev_source_file": prev.source_file if prev else None,
        "prev_data_date": (data_date(prev.source_file).isoformat() if (prev and data_date(prev.source_file)) else None),
        "rules_version": rules_version, "server_rules_version": logic.RULES_VERSION,
        "client_checked": client_checks is not None,
        "older_file": older,                      # 有值＝用舊檔覆蓋、使用者已確認
        "sheets_missing_confirmed": bool(sheets_missing_confirmed),   # 工作表不見、使用者已確認
        "content_sig": sig, "same_content_as_prev": same_content,
        "row_count": len(new_fs),
        "counts": counts,
        "issues": sum(counts.values()) + (1 if older else 0),
        "sheets_missing": sheets_missing, "sheets_new": sheets_new,
        "columns_gone": columns_gone, "columns_new": columns_new,
        "disappeared_by_sheet": dict(dis_by),
        **lists,
        "truncated": {k: v for k, v in trunc.items() if v},
    }


# ── 規則版本就地重算 ──
def _js_dedup(cols: list) -> list:
    """同前端 sheets.js parseSheet：重複欄名第二個起改成「欄名#2」。"""
    seen, out = {}, []
    for c in cols:
        if not c:
            out.append(c)
            continue
        if c in seen:
            seen[c] += 1
            out.append(f"{c}#{seen[c]}")
        else:
            seen[c] = 1
            out.append(c)
    return out


FID_COL = "__fid"


def recompute_source(session: Session) -> dict:
    """最新一批每一筆 finding 的原始列（不收合、不套負責人改寫）＋ finding id，給前端用同一套解析重算日期。"""
    b = _latest(session)
    if b is None:
        return {"batch_id": None, "sheets": []}
    scs = {sc.sheet_key: sc.columns for sc in session.execute(
        select(SheetColumns).where(SheetColumns.batch_id == b.id)).scalars()}
    order, by = [], {}
    for f in session.execute(select(Finding).where(Finding.batch_id == b.id).order_by(Finding.id)).scalars():
        k = f.sheet_key or "未分類"
        if k not in by:
            by[k] = []
            order.append(k)
        row = dict(f.raw or {})
        row[FID_COL] = f.id
        by[k].append(row)
    sheets = []
    for k in order:
        cols = [c for c in _js_dedup(list(scs.get(k) or [])) if c]
        have = set(cols)
        for r in by[k]:
            for c in r:
                if c not in have and c != FID_COL:
                    cols.append(c)
                    have.add(c)
        cols.append(FID_COL)
        sheets.append({"name": k, "columns": cols, "rows": by[k]})
    return {"batch_id": b.id, "rules_version": logic.RULES_VERSION, "batch_rules_version": b.rules_version,
            "sheets": sheets}


_FIELDS = ("remediation_due", "first_extension_due", "exception_due", "effective_due", "stage", "close_status")


class RecomputeInvalid(ValueError):
    """前端送來的重算清單跟最新一批對不起來（缺、多、重複、空）→ 400，帶各項計數。"""
    def __init__(self, msg: str, counts: dict):
        super().__init__(msg)
        self.counts = counts


def recompute(session: Session, batch_id: int, client_rules: str, items: list,
              username: Optional[str] = None, ip: Optional[str] = None) -> dict:
    """用前端重算的日期就地更新最新一批（不新增批次）。
    - 已是目前規則版本 → already（什麼都不做）；批次不是最新 → LookupError（409）。
    - items 必須剛好涵蓋這批全部 finding id（不缺、不多、不重複、不可空）→ 否則 RecomputeInvalid（400）。
    - 條件式 UPDATE（id＋is_latest＋版本不同）搶「標成新版本」，搶不到再分辨是別人做完了還是換批了。
    - finding 更新＋版本戳記＋稽核紀錄同一個交易、一次 commit。"""
    from .models import AuditLog
    if client_rules != logic.RULES_VERSION:
        raise ValueError(f"瀏覽器的規則版本（{client_rules}）跟伺服器（{logic.RULES_VERSION}）不同，請按 Ctrl+F5 重新整理後再開")
    cur = _latest(session)
    if cur is None or cur.id != batch_id:
        raise LookupError("資料已換批（有人剛匯入或刪批），請重新整理")
    old_rv = cur.rules_version
    if old_rv == logic.RULES_VERSION:
        return {"already": True, "batch_id": cur.id, "rules_version": old_rv, "updated": 0}
    fs = session.execute(select(Finding).where(Finding.batch_id == cur.id)).scalars().all()
    want = {f.id for f in fs}
    got_ids = [it.id for it in items]
    dup = [k for k, n in Counter(got_ids).items() if n > 1]
    got = set(got_ids)
    counts = {"expected": len(want), "got": len(got_ids), "missing": len(want - got),
              "extra": len(got - want), "duplicate": len(dup)}
    if not items or counts["missing"] or counts["extra"] or dup:
        raise RecomputeInvalid("重算清單跟目前資料對不起來（缺／多／重複／空），沒有寫入任何東西", counts)
    res = session.execute(update(ImportBatch).where(
        ImportBatch.id == cur.id, ImportBatch.is_latest.is_(True),
        (ImportBatch.rules_version.is_(None)) | (ImportBatch.rules_version != logic.RULES_VERSION)
    ).values(rules_version=logic.RULES_VERSION))
    if res.rowcount == 0:
        session.rollback()
        now = session.get(ImportBatch, batch_id)
        if now is None or not now.is_latest:
            raise LookupError("資料已換批（有人剛匯入或刪批），請重新整理")
        return {"already": True, "batch_id": cur.id, "rules_version": now.rules_version, "updated": 0}
    by_id = {it.id: it for it in items}
    changed = Counter()
    samples = []
    n_upd = 0
    close_changed = False
    for f in fs:
        it = by_id[f.id]
        remark = it.remark if it.remark is not None else f.remark
        rem = logic.parse_iso_date(it.remediation_due)
        fext = logic.parse_iso_date(it.first_extension_due)
        exc = logic.parse_iso_date(it.exception_due)
        new = {"remediation_due": rem, "first_extension_due": fext, "exception_due": exc,
               "effective_due": logic.compute_effective_due(exc, fext, rem, remark, f.raw),
               "stage": logic.compute_stage(exc, fext, rem, remark, f.raw),
               "close_status": logic.classify_close(it.close_status)}
        diff = [k for k in _FIELDS if getattr(f, k) != new[k]]
        if not diff:
            continue
        n_upd += 1
        for k in diff:
            changed[k] += 1
        if len(samples) < 50:
            samples.append({"id": f.id, "sheet": f.sheet_key, "host": _host(f), "name": f.name,
                            **{k: [str(getattr(f, k)) if getattr(f, k) is not None else None,
                                   str(new[k]) if new[k] is not None else None] for k in diff}})
        if "close_status" in diff:
            close_changed = True
        for k in diff:
            setattr(f, k, new[k])
    entry = {"at": dt.datetime.now().isoformat(timespec="seconds"), "from": old_rv, "to": logic.RULES_VERSION,
             "updated": n_upd, "fields": dict(changed)}
    rep = dict(cur.import_report) if isinstance(cur.import_report, dict) else {}
    rep["recompute"] = (list(rep.get("recompute") or []) + [entry])[-20:]
    cur.import_report = rep
    session.add(AuditLog(username=username, action="recompute_rules", target=f"batch:{cur.id}", ip=ip,
                         detail=f"規則 {old_rv or '(未記錄)'} → {logic.RULES_VERSION}；更新 {n_upd}/{len(fs)} 筆 {dict(changed)}"))
    session.commit()   # finding＋版本戳記＋稽核：同一筆交易
    if close_changed:   # 結案狀態變了 → 承辦疊加層的 source_closed 跟著對（衍生資料，另外重對）
        from . import cases
        cases.reconcile(session, cur)
        cases.apply_owner_overrides(session, cur)
        session.commit()
    return {"already": False, "batch_id": cur.id, "from": old_rv, "rules_version": logic.RULES_VERSION,
            "updated": n_upd, "fields": dict(changed), "missing": 0, "total": len(fs), "samples": samples}

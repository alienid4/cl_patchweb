"""FastAPI 進入點。查詢免登入；寫入需登入+稽核（W4，本地帳號，決策 writable-with-overlay）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

import datetime as dt
import json

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ad, appsettings, attachments, cases, config, export, importer, mailer, query, security
from .db import SessionLocal, init_db
from .models import AuditLog, Finding, MailLog, User, UserSession
from .schemas import ImportIn, ImportResult

_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    import threading
    threading.Thread(target=query.warm_snapshot_cache, daemon=True).start()   # 開機先算好開頁快照
    yield


app = FastAPI(title="CL_WebVuln 弱點彙總（網頁版）", version="0.1.0", lifespan=lifespan)
# 回應壓縮：開頁快照 ~5MB 的長文字(Description/Plugin Output)壓縮後約剩兩成，下載最花時間的就是它
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def _sheet_scope(request: Request, call_next):
    """?sheet=<工作表名> → 這個請求的所有統計只算那張表（見 query.SHEET_SCOPE）。"""
    tok = query.SHEET_SCOPE.set(request.query_params.get("sheet") or None)
    try:
        return await call_next(request)
    finally:
        query.SHEET_SCOPE.reset(tok)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """讀 cookie 取登入者；未登入回 None（讀取端點不強制）。"""
    token = request.cookies.get(config.SESSION_COOKIE)
    return security.get_session_user(db, token)


def require_login(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    return user


class _AnonUser:
    """免登入模式用的匿名身分（＝Super Admin）；audit 會記成『(未登入)』。"""
    username = "(未登入)"
    role = config.ROLE_SUPER
    display_name = "(未登入)"
    department = None
    email = None


def _open_mode(db: Session) -> bool:
    """是否『免登入』：env NO_AUTH 開、且『未啟用 AD 登入』。
    啟用 AD 登入(設定畫面) → 一律需登入（DB 開關為主，覆蓋 env 的過渡設定）。
    WEBVULN_FORCE_OPEN=1 為緊急救援，強制免登入、覆蓋一切(含 AD)。"""
    if config.FORCE_OPEN:
        return True
    if not config.NO_AUTH:
        return False
    try:
        return not appsettings.get_ad_config(db).get("enabled")
    except Exception:  # noqa: BLE001
        return True


def require_view(user: User | None = Depends(current_user), db: Session = Depends(get_db)) -> User:
    """看／下載附件：需登入；免登入模式比照內部開放。"""
    if _open_mode(db):
        return user or _AnonUser()
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    return user


def require_write_role(user: User | None = Depends(current_user), db: Session = Depends(get_db)) -> User:
    """可寫入端點：需登入或免登入模式。能改哪些由各端點的「範圍檢查」決定。"""
    if config.DISABLE_WRITE:
        raise HTTPException(status_code=503, detail="寫入維護中（暫停）")
    if _open_mode(db):
        return user or _AnonUser()
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    return user


def require_super(user: User | None = Depends(current_user), db: Session = Depends(get_db)) -> User:
    """系統級動作(匯入、清除、AD 設定)：僅 Super Admin（免登入過渡期比照）。"""
    if config.DISABLE_WRITE:
        raise HTTPException(status_code=503, detail="寫入維護中（暫停）")
    if _open_mode(db):
        return user or _AnonUser()
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    if config.canon_role(user.role) != config.ROLE_SUPER:
        raise HTTPException(status_code=403, detail="需最高權限（Super Admin）")
    return user


def _scope_ok(user, finding, db) -> bool:
    """此使用者可否改這筆弱點：super 全部；dept_admin 限同部門；user 限自己(owner＝display_name)。"""
    if _open_mode(db):
        return True
    role = config.canon_role(getattr(user, "role", ""))
    if role == config.ROLE_SUPER:
        return True
    if role == config.ROLE_DEPT_ADMIN:
        return bool(getattr(user, "department", None)) and finding.department == user.department
    return bool(getattr(user, "display_name", None)) and (finding.owner or "") == user.display_name


def _require_scope(user, finding, db):
    if not _scope_ok(user, finding, db):
        raise HTTPException(status_code=403, detail="權限不足：只能處理你負責（或你部門）的弱點")


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── 登入 ──
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user, reason = security.authenticate_detail(db, body.username, body.password)
    if user is None:
        security.log_audit(db, username=body.username, action="login_failed",
                           detail=reason, ip=_client_ip(request))
        raise HTTPException(status_code=401, detail=reason or "帳號或密碼錯誤")
    token = security.create_session(db, user)
    response.set_cookie(
        config.SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=config.COOKIE_SECURE, max_age=security.session_ttl_hours(db) * 3600,
    )
    security.log_audit(db, username=user.username, action="login", ip=_client_ip(request))
    return {"username": user.username, "display_name": user.display_name, "role": user.role}


@app.post("/api/logout")
def api_logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(config.SESSION_COOKIE)
    user = security.get_session_user(db, token)
    security.revoke_session(db, token)
    response.delete_cookie(config.SESSION_COOKIE)
    if user:
        security.log_audit(db, username=user.username, action="logout", ip=_client_ip(request))
    return {"ok": True}


def _online_count(db: Session) -> int:
    """目前登入人數＝有未過期 session 的不重複使用者數。"""
    try:
        return db.execute(select(func.count(func.distinct(UserSession.user_id)))
                          .where(UserSession.expires_at > dt.datetime.now())).scalar() or 0
    except Exception:  # noqa: BLE001
        return 0


@app.get("/api/me")
def api_me(user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    # open_write：伺服器目前是否免登入即可寫入（WEBVULN_NO_AUTH）→ 前端據此顯示操作鈕
    online = _online_count(db)
    open_mode = _open_mode(db)
    if user is None:
        return {"authenticated": False, "open_write": open_mode, "is_super": open_mode, "online": online}
    role = config.canon_role(user.role)
    return {"authenticated": True, "username": user.username,
            "display_name": user.display_name, "role": role,
            "department": user.department, "open_write": open_mode,
            "is_super": open_mode or role == config.ROLE_SUPER, "online": online,
            "weekly_report": bool(getattr(user, "weekly_report", False))}


@app.post("/api/import", response_model=ImportResult)
def import_data(payload: ImportIn, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_super)):
    """收前端解析好的一批 finding → 存成新快照，舊快照退位。
    匯入＝覆蓋最新快照，屬寫入：需登入（承辦/管理員），免登入模式才放行；動作留稽核。"""
    batch = importer.create_batch(db, payload)
    import threading
    threading.Thread(target=query.warm_snapshot_cache, daemon=True).start()   # 換了新批次 → 先算好開頁快照
    security.log_audit(db, username=user.username, action="import",
                       target=f"batch:{batch.id}",
                       detail=f"{payload.source_file or ''} {batch.row_count}筆",
                       ip=_client_ip(request))
    return ImportResult(batch_id=batch.id, row_count=batch.row_count, is_latest=batch.is_latest)


@app.get("/api/sheets")
def api_sheets(db: Session = Depends(get_db)):
    """最新快照的工作表清單＋各表未結筆數（週報『選工作表』用）。"""
    return query.sheets(db)


@app.get("/api/departments")
def api_departments(db: Session = Depends(get_db)):
    return query.departments(db)


@app.get("/api/owners")
def api_owners(db: Session = Depends(get_db)):
    """既有負責人清單，供編輯視窗可搜尋下拉(負責人可新增、部門不可)。"""
    return query.owners(db)


@app.get("/api/reconcile")
def api_reconcile(department: str | None = None, db: Session = Depends(get_db)):
    """對帳健檢：跑一組『A 應等於 B』不變式，讓操作者不靠 AI 也能確認數字兜得起來。"""
    return query.reconcile_check(db, department=department)


@app.get("/api/owner-summary")
def api_owner_summary(department: str | None = None, due_max: int | None = None,
                      lead: int = 0, db: Session = Depends(get_db)):
    """負責人角度：誰還有幾隻＋狀態分佈。due_max=只算距到期≤N天；lead=申請提前量(行動期限=到期−lead)。"""
    return query.owner_summary(db, department=department, due_max=due_max, lead=lead)


@app.get("/api/summary")
def api_summary(department: str | None = None, lead: int = 0, db: Session = Depends(get_db)):
    return query.summary(db, department=department, lead=lead)


@app.get("/api/findings")
def api_findings(
    department: str | None = None,
    status: str = "未結案",
    owner: str | None = None,
    severity: str | None = None,
    band: str | None = None,
    keyword: str | None = None,
    sheet_key: str | None = None,
    stage: str | None = None,
    should_apply: bool = False,
    applied: bool = False,
    apply_intent: bool = False,
    progress: str | None = None,
    no_owner: bool = False,
    no_due: bool = False,
    risk: str | None = None,
    apply_universe: bool = False,
    not_apply: bool = False,
    no_target: bool = False,
    flagged: bool = False,
    reported: bool = False,
    note_no_progress: bool = False,
    due_min: int | None = None,
    due_max: int | None = None,
    lead: int = 0,
    db: Session = Depends(get_db),
):
    return query.find(db, department=department, status=status, owner=owner,
                      severity=severity, band=band, keyword=keyword, sheet_key=sheet_key,
                      stage=stage, only_should_apply=should_apply, applied=applied,
                      apply_intent=apply_intent, progress=progress, no_owner=no_owner, no_due=no_due,
                      risk=risk, apply_universe=apply_universe, not_apply=not_apply,
                      no_target=no_target, flagged=flagged, reported=reported, note_no_progress=note_no_progress,
                      due_min=due_min, due_max=due_max, lead=lead)


@app.get("/api/ranking")
def api_ranking(by: str = "owner", department: str | None = None, db: Session = Depends(get_db)):
    """負責人/部門排行榜（依逾期多寡）。by=owner|department。"""
    if by == "department":
        return query.ranking_by_department(db)
    return query.ranking_by_owner(db, department=department)


@app.get("/api/sla")
def api_sla(department: str | None = None, db: Session = Depends(get_db)):
    return query.sla(db, department=department)


@app.get("/api/close-stats")
def api_close_stats(department: str | None = None, db: Session = Depends(get_db)):
    """結案統計：本期新結案(快照 delta)＋依結案人。來源 Excel 確認為準。"""
    return query.close_stats(db, department=department)


@app.get("/api/trend")
def api_trend(department: str | None = None, limit: int = 12, db: Session = Depends(get_db)):
    """未結趨勢：每次匯入當下的未結數／逾期數（供主管週報折線）。"""
    return query.trend(db, department=department, limit=limit)


@app.get("/api/snapshot")
def api_snapshot(full: bool = False, db: Session = Depends(get_db)):
    """最新快照原封內容（各表欄序＋raw 列），供前端重建 workbook 餵回原本 render。
    預設輕量版（不含長文字欄，見 query.HEAVY_AVG_CHARS）且走伺服器端快取；?full=1 取完整版。"""
    return Response(content=query.snapshot_bytes(db, light=not full), media_type="application/json")


@app.get("/api/snapshot-meta")
def api_snapshot_meta(db: Session = Depends(get_db)):
    """輕量：最新批次識別（匯入時間/檔名/列數），供前端快取判斷『資料換了沒』，免下載整包 snapshot。"""
    b = query.latest_batch(db)
    if not b:
        return {"batch_id": None, "imported_at": None, "source_file": None, "row_count": 0}
    return {"batch_id": b.id,
            "imported_at": b.imported_at.isoformat() if b.imported_at else None,
            "source_file": b.source_file, "row_count": b.row_count}


@app.get("/api/matrix")
def api_matrix(department: str | None = None, db: Session = Depends(get_db)):
    """交叉分析：嚴重度 × 到期時間帶（未結案）。"""
    return query.matrix(db, department=department)


@app.get("/api/stage-stats")
def api_stage_stats(department: str | None = None, db: Session = Depends(get_db)):
    """例外／展延階段統計（未結案）。"""
    return query.stage_stats(db, department=department)


@app.get("/api/report")
def api_report(department: str | None = None, owner: str | None = None,
               db: Session = Depends(get_db)):
    """主管週報：應申請未申請／已申請／預計完成彙總／落後。可依部門或負責人篩選。"""
    return query.weekly_report(db, department=department, owner=owner)


@app.get("/api/cases")
def api_cases(status: str | None = None, department: str | None = None,
              orphan: bool | None = None, suspect: bool | None = None,
              db: Session = Depends(get_db)):
    """承辦案件清單（申請管線）。可篩 status/department/orphan/suspect。"""
    return cases.list_cases(db, status=status, department=department,
                            orphan=orphan, suspect=suspect)


# ── 承辦狀態備份／還原（Super Admin）：把一台的處理進度/預計完成日/備註帶到另一台 ──
@app.get("/api/cases/backup")
def api_cases_backup(request: Request, db: Session = Depends(get_db), user: User = Depends(require_super)):
    from . import casebackup
    data = casebackup.export(db)
    security.log_audit(db, username=getattr(user, "username", None), action="case_backup",
                       target="case", detail=f"items={data['count']}", ip=_client_ip(request))
    db.commit()
    name = "承辦狀態備份_" + dt.datetime.now().strftime("%Y%m%d_%H%M") + ".json"
    return Response(content=json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})


class CaseRestoreIn(BaseModel):
    data: dict
    overwrite: bool = False   # 跟現有資料衝突時是否覆蓋（預設保留現有）
    apply: bool = False       # False＝只預覽


@app.post("/api/cases/restore")
def api_cases_restore(body: CaseRestoreIn, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_super)):
    from . import casebackup
    db_path = config.DB_URL[len("sqlite:///"):] if config.DB_URL.startswith("sqlite:///") else None
    try:
        rep = casebackup.restore(db, body.data, overwrite=body.overwrite, apply=body.apply, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if rep["applied"]:
        security.log_audit(db, username=getattr(user, "username", None), action="case_restore", target="case",
                           detail=f"new={rep['new']} overwritten={rep['overwritten']} skipped={rep['skipped_conflict']} "
                                  f"unmatched={rep['unmatched']} backup={rep['db_backup']}", ip=_client_ip(request))
        db.commit()
    return rep


@app.post("/api/cases/purge-orphans")
def api_purge_orphans(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_super)):
    """清除『已消失』(orphan) 案件：來源已無此弱點的舊案件紀錄。需寫入權限，留稽核。"""
    n = cases.purge_orphans(db)
    security.log_audit(db, username=user.username, action="purge_orphans",
                       target="case", detail=f"deleted={n}", ip=_client_ip(request))
    return {"deleted": n}


class OverlayIn(BaseModel):
    owner: str | None = None   # 有給才改；空字串＝清除
    department: str | None = None  # 部門(負責人可能是別單位)；空字串＝清除
    note: str | None = None    # 管理追蹤備註
    target_date: str | None = None  # 預計完成日(ISO yyyy-mm-dd)；空字串＝清除
    progress: str | None = None  # 處理進度(處理中/等複掃)；空字串＝清除
    set_owner: bool = False     # 是否要改負責人
    set_department: bool = False  # 是否要改部門
    set_note: bool = False      # 是否要改追蹤備註
    set_target: bool = False    # 是否要改預計完成日
    set_progress: bool = False  # 是否要改處理進度


@app.post("/api/findings/{finding_id}/overlay")
def api_set_overlay(finding_id: int, body: OverlayIn, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(require_write_role)):
    """管理員在系統內改一筆弱點的可寫欄位（負責人／追蹤備註／預計完成日／處理進度）：存疊加層、重匯不洗掉、不動 Excel。"""
    _f = db.get(Finding, finding_id)
    if _f is None:
        raise HTTPException(status_code=404, detail="弱點不存在")
    _require_scope(user, _f, db)   # super 全部／dept_admin 限自己部門／user 限自己的
    fields = {}
    if body.set_owner:
        fields["owner"] = body.owner
    if body.set_department:
        fields["department"] = body.department
    if body.set_note:
        fields["note"] = body.note
    if body.set_target:
        fields["target_date"] = body.target_date
    if body.set_progress:
        fields["progress"] = body.progress
    if not fields:
        raise HTTPException(status_code=400, detail="沒有要改的欄位")
    try:
        r = cases.set_overlay(db, finding_id, fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    security.log_audit(db, username=user.username, action="set_overlay",
                       target=f"vuln:{r['vuln_key']}", detail=str({k: v for k, v in r.items() if k in fields}),
                       ip=_client_ip(request))
    return r


class BulkOverlayIn(BaseModel):
    ids: list[int]
    owner: str | None = None
    department: str | None = None
    note: str | None = None
    target_date: str | None = None
    progress: str | None = None
    set_owner: bool = False
    set_department: bool = False
    set_note: bool = False
    set_target: bool = False
    set_progress: bool = False


@app.post("/api/findings/bulk-overlay")
def api_bulk_overlay(body: BulkOverlayIn, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_write_role)):
    """批次改多筆弱點的疊加欄（負責人／部門／備註／預計完成日／處理進度）：一次套同一個值。
    逐筆過範圍檢查，無權限者略過並列出；不中斷整批。"""
    fields = {}
    if body.set_owner:
        fields["owner"] = body.owner
    if body.set_department:
        fields["department"] = body.department
    if body.set_note:
        fields["note"] = body.note
    if body.set_target:
        fields["target_date"] = body.target_date
    if body.set_progress:
        fields["progress"] = body.progress
    if not fields:
        raise HTTPException(status_code=400, detail="沒有要改的欄位")
    if not body.ids:
        raise HTTPException(status_code=400, detail="未選取任何弱點")
    if len(body.ids) > 1000:
        raise HTTPException(status_code=400, detail="一次最多 1000 筆")
    applied, skipped, failed = 0, [], []
    for fid in body.ids:
        f = db.get(Finding, fid)
        if f is None:
            failed.append({"id": fid, "error": "不存在"}); continue
        if not _scope_ok(user, f, db):
            skipped.append({"id": fid, "host": f.host, "error": "無權限"}); continue
        try:
            cases.set_overlay(db, fid, fields)
            applied += 1
        except ValueError as e:
            failed.append({"id": fid, "error": str(e)})
    security.log_audit(db, username=getattr(user, "username", None), action="bulk_overlay",
                       target=f"count:{applied}", detail="fields=%s skipped=%d failed=%d" % (
                           list(fields.keys()), len(skipped), len(failed)),
                       ip=_client_ip(request))
    return {"applied": applied, "skipped": skipped, "failed": failed, "total": len(body.ids)}


@app.post("/api/attachments/bulk")
async def api_bulk_attachment(request: Request, ids: str = "", name: str = "",
                              kind: str = "其他", db: Session = Depends(get_db),
                              user: User = Depends(require_write_role)):
    """一份檔案掛多筆弱點：raw body＝檔案位元組；ids＝逗號分隔的 finding id。
    實體檔靠 sha256 去重只存一份，建多筆 metadata。逐筆過範圍檢查，略過無權限者。"""
    id_list = [int(x) for x in ids.replace(" ", "").split(",") if x.strip().isdigit()]
    if not id_list:
        raise HTTPException(status_code=400, detail="未選取任何弱點")
    if len(id_list) > 1000:
        raise HTTPException(status_code=400, detail="一次最多 1000 筆")
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > config.UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="檔案過大（上限 %d MB）" % (config.UPLOAD_MAX_BYTES // (1024 * 1024)))
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="空檔案")
    applied, skipped, failed = 0, [], []
    for fid in id_list:
        f = db.get(Finding, fid)
        if f is None:
            failed.append({"id": fid, "error": "不存在"}); continue
        if not _scope_ok(user, f, db):
            skipped.append({"id": fid, "host": f.host, "error": "無權限"}); continue
        try:
            attachments.save_for_finding(db, fid, data, name, kind, getattr(user, "username", None))
            applied += 1
        except ValueError as e:
            failed.append({"id": fid, "error": str(e)})
    security.log_audit(db, username=getattr(user, "username", None), action="attach_bulk",
                       target=f"count:{applied}", detail="%s %dB skipped=%d failed=%d" % (
                           (name or "")[:80], len(data), len(skipped), len(failed)),
                       ip=_client_ip(request))
    return {"applied": applied, "skipped": skipped, "failed": failed, "total": len(id_list)}


# ── 申請佐證文件（展延／例外的 WBS、理由說明…）：掛弱點(vuln_key)、重匯不洗、對 Excel 唯讀 ──
@app.get("/api/findings/{finding_id}/attachments")
def api_list_attachments(finding_id: int, db: Session = Depends(get_db),
                         user: User = Depends(require_view)):
    try:
        return attachments.list_for_finding(db, finding_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/findings/{finding_id}/attachments")
async def api_upload_attachment(finding_id: int, request: Request, name: str = "",
                                kind: str = "其他", db: Session = Depends(get_db),
                                user: User = Depends(require_write_role)):
    """上傳一個附件：raw body＝檔案位元組，檔名/類型走 query（避免相依 python-multipart）。"""
    _f = db.get(Finding, finding_id)
    if _f is None:
        raise HTTPException(status_code=404, detail="弱點不存在")
    _require_scope(user, _f, db)
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > config.UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="檔案過大（上限 %d MB）" % (config.UPLOAD_MAX_BYTES // (1024 * 1024)))
    data = await request.body()
    try:
        r = attachments.save_for_finding(db, finding_id, data, name, kind, getattr(user, "username", None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    security.log_audit(db, username=getattr(user, "username", None), action="attach_add",
                       target=f"vuln:{r['vuln_key']}", detail=f"{r['kind']} {r['orig_name']} {r['size']}B",
                       ip=_client_ip(request))
    return r


@app.get("/api/attachments/{att_id}/download")
def api_download_attachment(att_id: int, db: Session = Depends(get_db),
                            user: User = Depends(require_view)):
    got = attachments.get_file(db, att_id)
    if not got:
        raise HTTPException(status_code=404, detail="附件不存在")
    a, path = got
    return FileResponse(str(path), media_type="application/octet-stream", filename=a.orig_name)


@app.delete("/api/attachments/{att_id}")
def api_delete_attachment(att_id: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_write_role)):
    ok = attachments.delete(db, att_id)
    if not ok:
        raise HTTPException(status_code=404, detail="附件不存在")
    security.log_audit(db, username=getattr(user, "username", None), action="attach_del",
                       target=f"attachment:{att_id}", ip=_client_ip(request))
    return {"deleted": True}


# ── AD／權限設定（Super Admin 專用；存 DB，畫面可編輯、免重部署）──
@app.get("/api/ad-settings")
def api_get_ad_settings(db: Session = Depends(get_db), user: User = Depends(require_super)):
    cfg = appsettings.get_ad_config(db)
    cfg["presets"] = appsettings.get_ldap_presets(db)   # 站點→IP（存 DB，可編輯；公開碼不放內網 IP）
    return cfg


@app.post("/api/ad-settings")
def api_set_ad_settings(body: dict, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_super)):
    if isinstance(body, dict) and "presets" in body:
        appsettings.set_ldap_presets(db, body.get("presets") or [])   # 站點 IP 存 DB
    cfg = appsettings.set_ad_config(db, body or {})
    security.log_audit(db, username=getattr(user, "username", None), action="ad_settings",
                       detail="enabled=%s servers=%s" % (cfg.get("enabled"), cfg.get("servers")),
                       ip=_client_ip(request))
    return cfg


class AdTestIn(BaseModel):
    login: str
    password: str
    config: dict | None = None   # 可帶「尚未儲存」的設定來試


@app.post("/api/ad-test")
def api_ad_test(body: AdTestIn, db: Session = Depends(get_db), user: User = Depends(require_super)):
    """測試連線：用給的帳密對 AD 試綁定，回成功/失敗＋讀到的 displayName/部門。"""
    cfg = appsettings.get_ad_config(db)
    if body.config:
        for k, v in body.config.items():
            if k in appsettings.AD_DEFAULTS:
                cfg[k] = v
    return ad.test_connection(cfg, body.login, body.password)


@app.get("/api/users")
def api_list_users(db: Session = Depends(get_db), user: User = Depends(require_super)):
    from sqlalchemy import select as _sel
    rows = db.execute(_sel(User).order_by(User.role, User.username)).scalars().all()
    return [{"id": u.id, "username": u.username, "display_name": u.display_name,
             "email": u.email, "department": u.department,
             "role": config.canon_role(u.role), "is_active": u.is_active,
             "weekly_report": bool(u.weekly_report), "note": u.note}
            for u in rows]


class UserRoleIn(BaseModel):
    role: str | None = None          # super_admin/dept_admin/user
    department: str | None = None    # 設定窗口負責的部門
    email: str | None = None         # 收件人涵蓋率：Super Admin 可手補負責人信箱
    is_active: bool | None = None
    weekly_report: bool | None = None  # 每週部門週報開關（通常本人自管，super 也可代設）
    note: str | None = None          # 帳號註解（員編_姓名_部門_用途）


@app.post("/api/users/{user_id}/role")
def api_set_user_role(user_id: int, body: UserRoleIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(require_super)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="帳號不存在")
    if body.role is not None:
        if body.role not in (config.ROLE_SUPER, config.ROLE_DEPT_ADMIN, config.ROLE_USER):
            raise HTTPException(status_code=400, detail="未知角色")
        u.role = body.role
    if body.department is not None:
        u.department = body.department or None
    if body.email is not None:
        u.email = body.email.strip() or None
    if body.is_active is not None:
        u.is_active = body.is_active
    if body.weekly_report is not None:
        u.weekly_report = body.weekly_report
    if body.note is not None:
        u.note = body.note.strip() or None
    db.commit()
    security.log_audit(db, username=getattr(user, "username", None), action="set_user_role",
                       target=f"user:{u.username}", detail=f"role={u.role} dept={u.department} email={'Y' if u.email else 'N'}",
                       ip=_client_ip(request))
    return {"id": u.id, "username": u.username, "role": config.canon_role(u.role),
            "department": u.department, "email": u.email, "weekly_report": bool(u.weekly_report)}


# ── Email／SMTP 設定（Super Admin 專用；存 DB，畫面可編輯、免重部署）──
@app.get("/api/email-settings")
def api_get_email_settings(db: Session = Depends(get_db), user: User = Depends(require_super)):
    return appsettings.get_email_config(db)


@app.post("/api/email-settings")
def api_set_email_settings(body: dict, request: Request, db: Session = Depends(get_db),
                           user: User = Depends(require_super)):
    cfg = appsettings.set_email_config(db, body or {})
    security.log_audit(db, username=getattr(user, "username", None), action="email_settings",
                       detail="enabled=%s host=%s" % (cfg.get("enabled"), cfg.get("smtp_host")),
                       ip=_client_ip(request))
    return cfg


class EmailTestIn(BaseModel):
    to: str | None = None   # 預設寄給自己


@app.post("/api/email-test")
def api_email_test(body: EmailTestIn, db: Session = Depends(get_db),
                   user: User = Depends(require_super)):
    """寄一封測試信（確認伺服器連得上 relay）。"""
    cfg = appsettings.get_email_config(db)
    return mailer.send_test(cfg, user, body.to)


def _require_send_role(user, db) -> str | None:
    """一鍵發送：限 Super Admin 或部門窗口(dept_admin)。回傳發送範圍部門（窗口＝自己部門；super＝None）。"""
    if _open_mode(db):
        return None
    role = config.canon_role(getattr(user, "role", ""))
    if role == config.ROLE_SUPER:
        return None
    if role == config.ROLE_DEPT_ADMIN:
        return getattr(user, "department", None)
    raise HTTPException(status_code=403, detail="需最高權限或部門窗口才能發送")


@app.get("/api/send-reminders/preview")
def api_send_reminders_preview(department: str | None = None, db: Session = Depends(get_db),
                               user: User = Depends(require_write_role)):
    """一鍵發送預覽：依負責人分組、解析收件人（本人／轉窗口／跳過），不寄信。
    部門窗口只看自己部門；super 可帶 department 篩選、否則全部。"""
    scope = _require_send_role(user, db)
    dept = scope if scope is not None else department
    cfg = appsettings.get_email_config(db)
    plan = mailer.build_plan(db, cfg, department=dept)
    from_addr = mailer.sender_from(cfg, user)
    return {
        "enabled": bool(cfg.get("enabled")),
        "configured": bool((cfg.get("smtp_host") or "").strip()),
        "from": from_addr,
        "cc_self": bool(cfg.get("cc_self")),
        "scope": dept or "全部",
        "plan": plan,
    }


class SendRemindersIn(BaseModel):
    owners: list[str] | None = None   # 只寄這些負責人；None＝全部（非 skip）
    department: str | None = None     # super 可指定；窗口一律自己部門


@app.post("/api/send-reminders")
def api_send_reminders(body: SendRemindersIn, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_write_role)):
    """一鍵發送：伺服器直連公司 relay 寄出催辦信。回寄送摘要。"""
    scope = _require_send_role(user, db)
    dept = scope if scope is not None else body.department
    cfg = appsettings.get_email_config(db)
    try:
        summary = mailer.send_plan(db, cfg, user, selected_owners=body.owners, department=dept)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    security.log_audit(db, username=getattr(user, "username", None), action="send_reminders",
                       detail="sent=%d fallback=%d skipped=%d failed=%d dept=%s" % (
                           summary["sent"], summary["fallback"], summary["skipped"],
                           summary["failed"], dept or "全部"),
                       ip=_client_ip(request))
    return summary


@app.get("/api/send-reminders/sample")
def api_send_reminders_sample(owner: str | None = None, department: str | None = None,
                             db: Session = Depends(get_db), user: User = Depends(require_write_role)):
    """寄前看範例信：回一封（預設計畫中第一位）的主旨與內文。"""
    scope = _require_send_role(user, db)
    dept = scope if scope is not None else department
    cfg = appsettings.get_email_config(db)
    return mailer.build_sample(db, cfg, owner=owner, department=dept)


# ── 我的操作紀錄（本人自查：何時申請/上傳/變更了什麼）──
_ACTION_LABEL = {
    "set_overlay": "變更弱點（負責人／備註／預計完成／進度）",
    "bulk_overlay": "批次變更弱點",
    "attach_add": "上傳佐證文件",
    "attach_bulk": "批次上傳佐證文件",
    "attach_del": "刪除佐證文件",
    "import": "匯入彙總表",
    "send_reminders": "寄送催辦信",
    "purge_orphans": "清除已消失紀錄",
    "ad_settings": "變更 AD 設定",
    "email_settings": "變更 Email 設定",
    "general_settings": "變更系統設定",
    "set_user_role": "變更帳號權限",
    "login": "登入",
    "logout": "登出",
    "login_failed": "登入失敗",
}


@app.get("/api/my-activity")
def api_my_activity(limit: int = 300, db: Session = Depends(get_db),
                    user: User = Depends(require_view)):
    """本人操作紀錄（一般＝只看自己；部門窗口＝自己＋本部門成員；super＝全部）。"""
    role = config.canon_role(getattr(user, "role", ""))
    uname = getattr(user, "username", None)
    is_open = _open_mode(db)
    q = select(AuditLog).order_by(AuditLog.at.desc()).limit(max(1, min(2000, limit)))
    if not (is_open or role == config.ROLE_SUPER):
        if role == config.ROLE_DEPT_ADMIN and getattr(user, "department", None):
            members = db.execute(select(User.username).where(User.department == user.department)).scalars().all()
            names = set(members) | ({uname} if uname else set())
            q = select(AuditLog).where(AuditLog.username.in_(names)).order_by(AuditLog.at.desc()).limit(max(1, min(2000, limit)))
        else:
            q = select(AuditLog).where(AuditLog.username == uname).order_by(AuditLog.at.desc()).limit(max(1, min(2000, limit)))
    rows = db.execute(q).scalars().all()
    out = []
    for a in rows:
        tgt = (a.target or "")
        if tgt.startswith("vuln:"):
            tgt = tgt[5:]
        out.append({"at": a.at.isoformat(timespec="seconds") if a.at else None,
                    "username": a.username, "action": a.action,
                    "action_label": _ACTION_LABEL.get(a.action, a.action),
                    "target": tgt, "detail": a.detail})
    return {"scope": "全部" if (is_open or role == config.ROLE_SUPER) else
            ("本部門" if role == config.ROLE_DEPT_ADMIN else "本人"), "items": out}


# ── 發信紀錄（Super Admin）──
@app.get("/api/mail-log")
def api_mail_log(limit: int = 300, db: Session = Depends(get_db), user: User = Depends(require_super)):
    rows = db.execute(select(MailLog).order_by(MailLog.sent_at.desc())
                      .limit(max(1, min(2000, limit)))).scalars().all()
    return [{"sent_at": m.sent_at.isoformat(timespec="seconds") if m.sent_at else None,
             "sender": m.sender, "owner": m.owner, "to": m.to, "cc": m.cc,
             "mode": m.mode, "status": m.status, "count": m.count, "error": m.error}
            for m in rows]


# ── 一般系統設定（Super Admin）：session 時數等 ──
@app.get("/api/general-settings")
def api_get_general_settings(db: Session = Depends(get_db), user: User = Depends(require_super)):
    return appsettings.get_general_config(db)


@app.post("/api/general-settings")
def api_set_general_settings(body: dict, request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_super)):
    cfg = appsettings.set_general_config(db, body or {})
    security.log_audit(db, username=getattr(user, "username", None), action="general_settings",
                       detail="session_ttl_hours=%s" % cfg.get("session_ttl_hours"),
                       ip=_client_ip(request))
    return cfg


# ── 收件人涵蓋率：Super Admin 用自己帳密，一次從 AD 補齊負責人信箱 ──
class FetchMailsIn(BaseModel):
    login: str
    password: str


@app.post("/api/users/fetch-ad-mails")
def api_fetch_ad_mails(body: FetchMailsIn, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_super)):
    """對最新快照的負責人清單，向 AD 以顯示名搜 mail，補進對應帳號（沒有帳號就建立）。"""
    names = query.owners(db)
    if not names:
        return {"ok": False, "message": "尚無負責人（請先匯入）"}
    cfg = appsettings.get_ad_config(db)
    res = ad.fetch_mails_by_names(cfg, body.login, body.password, names)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "AD 查詢失敗")
    found = res.get("found") or {}
    updated, created = 0, 0
    for name, mail in found.items():
        u = db.execute(select(User).where(User.display_name == name)).scalars().first()
        if u is None:
            db.add(User(username="mail:" + name, password_hash=None, display_name=name,
                        email=mail, role=config.ROLE_USER, is_active=True))
            created += 1
        elif (u.email or "") != mail:
            u.email = mail
            updated += 1
    db.commit()
    security.log_audit(db, username=getattr(user, "username", None), action="fetch_ad_mails",
                       detail="names=%d found=%d updated=%d created=%d" % (
                           len(names), len(found), updated, created), ip=_client_ip(request))
    return {"ok": True, "names": len(names), "found": len(found),
            "updated": updated, "created": created}


# ── #8 每週部門週報：本人開關 + 排程手動試跑 ──
class MyWeeklyIn(BaseModel):
    enabled: bool


@app.post("/api/my-weekly")
def api_set_my_weekly(body: MyWeeklyIn, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_login)):
    """部門窗口自行開/關『每週一自動收部門週報』。"""
    u = db.get(User, user.id)
    if u is None:
        raise HTTPException(status_code=404, detail="帳號不存在")
    u.weekly_report = bool(body.enabled)
    db.commit()
    security.log_audit(db, username=u.username, action="set_my_weekly",
                       detail="weekly=%s" % u.weekly_report, ip=_client_ip(request))
    return {"weekly_report": bool(u.weekly_report)}


@app.post("/api/send-weekly")
def api_send_weekly(request: Request, only: str | None = None, db: Session = Depends(get_db),
                    user: User = Depends(require_super)):
    """手動試跑每週排程（平時由 cron 於週一 08:00 跑）。only＝逗號分隔員編，只寄這些人。"""
    cfg = appsettings.get_email_config(db)
    names = [x for x in (only or "").split(",") if x.strip()] or None
    try:
        summary = mailer.send_weekly(db, cfg, only_usernames=names)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    security.log_audit(db, username=getattr(user, "username", None), action="send_weekly",
                       detail="sent=%d skipped=%d failed=%d" % (
                           summary["sent"], summary["skipped"], summary["failed"]),
                       ip=_client_ip(request))
    return summary


class LocalUserIn(BaseModel):
    username: str
    password: str
    role: str = config.ROLE_USER
    display_name: str | None = None
    department: str | None = None
    note: str | None = None


@app.post("/api/users/local")
def api_create_local_user(body: LocalUserIn, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_super)):
    """Super Admin 建立／重設一個『本地帳號』（有密碼，供測試不同角色；AD 啟用時也能登入）。
    已存在同員編＝更新其密碼／角色／部門（冪等）。"""
    uname = (body.username or "").strip()
    if not uname:
        raise HTTPException(status_code=400, detail="請填帳號")
    if not body.password or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="密碼至少 4 碼")
    role = body.role if body.role in (config.ROLE_SUPER, config.ROLE_DEPT_ADMIN, config.ROLE_USER) else config.ROLE_USER
    u = db.execute(select(User).where(User.username == uname)).scalars().first()
    created = u is None
    if u is None:
        u = User(username=uname, is_active=True)
        db.add(u)
    u.password_hash = security.hash_password(body.password)
    u.role = role
    u.is_active = True
    if body.display_name is not None:
        u.display_name = body.display_name.strip() or uname
    elif created and not u.display_name:
        u.display_name = uname
    if body.department is not None:
        u.department = body.department.strip() or None
    if body.note is not None:
        u.note = body.note.strip() or None
    db.commit()
    security.log_audit(db, username=getattr(user, "username", None), action="local_user",
                       target=f"user:{uname}", detail=f"{'create' if created else 'update'} role={role}",
                       ip=_client_ip(request))
    return {"id": u.id, "username": u.username, "role": config.canon_role(u.role),
            "display_name": u.display_name, "department": u.department, "created": created}


class SeedTestIn(BaseModel):
    password: str


@app.post("/api/users/seed-test")
def api_seed_test_users(body: SeedTestIn, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_super)):
    """一鍵建三個測試帳號：已存在就『略過、不覆蓋』，只建缺的。回每個帳號狀態。"""
    if not body.password or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="密碼至少 4 碼")
    defs = [("superadmin", config.ROLE_SUPER, "本地最高管理員"),
            ("admin", config.ROLE_DEPT_ADMIN, "本地部門窗口"),
            ("user", config.ROLE_USER, "本地一般使用者")]
    out, created = [], 0
    for uname, role, disp in defs:
        u = db.execute(select(User).where(User.username == uname)).scalars().first()
        if u is not None:
            out.append({"username": uname, "status": "exists"})
            continue
        db.add(User(username=uname, display_name=disp, role=role, is_active=True,
                    password_hash=security.hash_password(body.password)))
        created += 1
        out.append({"username": uname, "status": "created"})
    if created:
        db.commit()
        security.log_audit(db, username=getattr(user, "username", None), action="seed_test_users",
                           detail="created=%d" % created, ip=_client_ip(request))
    return {"created": created, "accounts": out}


@app.delete("/api/users/{user_id}")
def api_delete_user(user_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_super)):
    """刪除帳號（Super Admin）。防呆：不能刪自己、不能刪最後一個啟用中的 Super Admin。
    AD 帳號刪掉後，該員下次登入會重新自動建立（回到預設一般角色）。"""
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="帳號不存在")
    if getattr(user, "id", None) == u.id:
        raise HTTPException(status_code=400, detail="不能刪除你自己正在使用的帳號")
    if config.canon_role(u.role) == config.ROLE_SUPER:
        others = db.execute(select(func.count(User.id)).where(
            User.role.in_((config.ROLE_SUPER, "admin")), User.is_active.is_(True),
            User.id != u.id)).scalar() or 0
        if others == 0:
            raise HTTPException(status_code=400, detail="這是最後一個 Super Admin，不能刪除（否則沒人能管理）")
    uname = u.username
    db.delete(u)   # UserSession 以 FK ondelete=CASCADE 連帶清除
    db.commit()
    security.log_audit(db, username=getattr(user, "username", None), action="delete_user",
                       target=f"user:{uname}", ip=_client_ip(request))
    return {"deleted": True, "username": uname}


@app.get("/api/export")
def api_export(batch_id: int | None = None, department: str | None = None,
               db: Session = Depends(get_db)):
    """原封匯出 xlsx：欄位與來源 1:1，缺值標『無原始資料』，另附管理摘要頁。可依部門篩選。"""
    wb, filename = export.build_workbook(db, batch_id=batch_id, department=department)
    data = export.to_bytes(wb)
    disp = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disp},
    )


# 靜態前端掛在最後（/api/* 先比對到，其餘落到這裡）
if _FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")

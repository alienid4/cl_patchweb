"""FastAPI 進入點。查詢免登入；寫入需登入+稽核（W4，本地帳號，決策 writable-with-overlay）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import cases, config, export, importer, query, security
from .db import SessionLocal, init_db
from .models import User
from .schemas import ImportIn, ImportResult

_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="CL_WebVuln 弱點彙總（網頁版）", version="0.1.0", lifespan=lifespan)


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
    """免登入模式(WEBVULN_NO_AUTH)用的匿名寫入身分；audit 會記成『(未登入)』。"""
    username = "(未登入)"
    role = config.ROLE_ADMIN
    display_name = "(未登入)"


def require_write_role(user: User | None = Depends(current_user)) -> User:
    if config.DISABLE_WRITE:
        raise HTTPException(status_code=503, detail="寫入維護中（暫停）")
    if config.NO_AUTH:
        return user or _AnonUser()   # 暫時取消密碼：免登入即可寫入（仍留 audit）
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    if user.role not in (config.ROLE_ADMIN, config.ROLE_STAFF):
        raise HTTPException(status_code=403, detail="權限不足（需承辦或管理員）")
    return user


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── 登入 ──
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user = security.authenticate(db, body.username, body.password)
    if user is None:
        security.log_audit(db, username=body.username, action="login_failed",
                           ip=_client_ip(request))
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    token = security.create_session(db, user)
    response.set_cookie(
        config.SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=config.COOKIE_SECURE, max_age=config.SESSION_TTL_HOURS * 3600,
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


@app.get("/api/me")
def api_me(user: User | None = Depends(current_user)):
    # open_write：伺服器目前是否免登入即可寫入（WEBVULN_NO_AUTH）→ 前端據此顯示操作鈕
    if user is None:
        return {"authenticated": False, "open_write": config.NO_AUTH}
    return {"authenticated": True, "username": user.username,
            "display_name": user.display_name, "role": user.role,
            "department": user.department, "open_write": config.NO_AUTH}


@app.post("/api/import", response_model=ImportResult)
def import_data(payload: ImportIn, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_write_role)):
    """收前端解析好的一批 finding → 存成新快照，舊快照退位。
    匯入＝覆蓋最新快照，屬寫入：需登入（承辦/管理員），免登入模式才放行；動作留稽核。"""
    batch = importer.create_batch(db, payload)
    security.log_audit(db, username=user.username, action="import",
                       target=f"batch:{batch.id}",
                       detail=f"{payload.source_file or ''} {batch.row_count}筆",
                       ip=_client_ip(request))
    return ImportResult(batch_id=batch.id, row_count=batch.row_count, is_latest=batch.is_latest)


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
    no_owner: bool = False,
    no_due: bool = False,
    due_min: int | None = None,
    due_max: int | None = None,
    lead: int = 0,
    db: Session = Depends(get_db),
):
    return query.find(db, department=department, status=status, owner=owner,
                      severity=severity, band=band, keyword=keyword, sheet_key=sheet_key,
                      stage=stage, only_should_apply=should_apply, applied=applied,
                      apply_intent=apply_intent, no_owner=no_owner, no_due=no_due,
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


@app.get("/api/snapshot")
def api_snapshot(db: Session = Depends(get_db)):
    """最新快照原封內容（各表欄序＋raw 列），供前端重建 workbook 餵回原本 render。"""
    return query.snapshot(db)


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


@app.post("/api/cases/purge-orphans")
def api_purge_orphans(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_write_role)):
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

"""FastAPI 進入點。v1：匯入 + 查詢（唯讀）。無登入（決策 no-auth-v1）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

from fastapi import Depends, FastAPI, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import export, importer, query
from .db import SessionLocal, init_db
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


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/import", response_model=ImportResult)
def import_data(payload: ImportIn, db: Session = Depends(get_db)):
    """收前端解析好的一批 finding → 存成新快照，舊快照退位。"""
    batch = importer.create_batch(db, payload)
    return ImportResult(batch_id=batch.id, row_count=batch.row_count, is_latest=batch.is_latest)


@app.get("/api/departments")
def api_departments(db: Session = Depends(get_db)):
    return query.departments(db)


@app.get("/api/summary")
def api_summary(department: str | None = None, db: Session = Depends(get_db)):
    return query.summary(db, department=department)


@app.get("/api/findings")
def api_findings(
    department: str | None = None,
    status: str = "未結案",
    owner: str | None = None,
    severity: str | None = None,
    band: str | None = None,
    keyword: str | None = None,
    sheet_key: str | None = None,
    should_apply: bool = False,
    no_owner: bool = False,
    no_due: bool = False,
    db: Session = Depends(get_db),
):
    return query.find(db, department=department, status=status, owner=owner,
                      severity=severity, band=band, keyword=keyword, sheet_key=sheet_key,
                      only_should_apply=should_apply, no_owner=no_owner, no_due=no_due)


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


@app.get("/api/export")
def api_export(batch_id: int | None = None, db: Session = Depends(get_db)):
    """原封匯出 xlsx：欄位與來源 1:1，缺值標『無原始資料』，另附管理摘要頁。"""
    wb, filename = export.build_workbook(db, batch_id=batch_id)
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

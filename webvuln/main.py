"""FastAPI 進入點。v1：匯入 + 查詢（唯讀）。無登入（決策 no-auth-v1）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from . import importer, query
from .db import SessionLocal, init_db
from .schemas import ImportIn, ImportResult


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
    db: Session = Depends(get_db),
):
    return query.find(db, department=department, status=status, owner=owner,
                      severity=severity, band=band, keyword=keyword, sheet_key=sheet_key)

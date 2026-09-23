"""DB 引擎／Session。SQLite 走 WAL、開 foreign_keys；讀多寫少(匯入時單寫)。

轉 MSSQL 時只換 DB_URL 與驅動、拿掉 SQLite 專屬 PRAGMA，模型與查詢不動（ORM 抽象）。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DB_URL
from .models import Base


def _make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    eng = create_engine(
        url,
        future=True,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _pragma(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return eng


engine: Engine = _make_engine(DB_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def init_db(target_engine: Engine | None = None) -> None:
    """建表。SQLite 檔不存在時先建父資料夾。"""
    eng = target_engine or engine
    if eng.url.get_backend_name() == "sqlite" and eng.url.database not in (None, "", ":memory:"):
        Path(eng.url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(eng)


def get_session() -> Session:
    """FastAPI 依賴用；呼叫端負責 close（見 main.py 的 get_db）。"""
    return SessionLocal()

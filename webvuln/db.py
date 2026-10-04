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


# 既有 DB 用 create_all 建(非 alembic)時，create_all 不會對既有表「加欄」。
# 這裡列出後來才加的欄，啟動時缺就補(SQLite ADD COLUMN)，讓正式機免手動 ALTER。
_ENSURE_COLUMNS = {
    "case_overlay": [("owner_override", "VARCHAR(100)"), ("track_note", "TEXT"),
                     ("target_date", "DATE"), ("department_override", "VARCHAR(200)")],
    # email 後加（AD 登入時由 mail 屬性寫入，供伺服器端一鍵發送）：舊 DB 缺欄時補上
    # weekly_report：#8 每週排程本人開關（SQLite 無 bool，用 INTEGER 0/1）
    "app_user": [("email", "VARCHAR(200)"), ("weekly_report", "INTEGER DEFAULT 0")],
}


def _ensure_columns(eng: Engine) -> None:
    if eng.url.get_backend_name() != "sqlite":
        return  # 其他 DB 走 alembic
    from sqlalchemy import text
    with eng.begin() as conn:
        for table, cols in _ENSURE_COLUMNS.items():
            exists = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            have = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db(target_engine: Engine | None = None) -> None:
    """建表。SQLite 檔不存在時先建父資料夾。對既有 DB 補後加的欄位。"""
    eng = target_engine or engine
    if eng.url.get_backend_name() == "sqlite" and eng.url.database not in (None, "", ":memory:"):
        Path(eng.url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(eng)
    _ensure_columns(eng)


def get_session() -> Session:
    """FastAPI 依賴用；呼叫端負責 close（見 main.py 的 get_db）。"""
    return SessionLocal()

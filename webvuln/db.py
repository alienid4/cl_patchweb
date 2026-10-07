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
    "app_user": [("email", "VARCHAR(200)"), ("weekly_report", "INTEGER DEFAULT 0"),
                 ("note", "VARCHAR(300)")],
    # row_key：每一列的穩定識別碼（2026-10-05，見 rowkey.py）
    "finding": [("row_key", "VARCHAR(500)")],
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
        if conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name='finding'").fetchone():
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_finding_row_key ON finding (row_key)")


def init_db(target_engine: Engine | None = None) -> None:
    """建表。SQLite 檔不存在時先建父資料夾。對既有 DB 補後加的欄位。"""
    eng = target_engine or engine
    if eng.url.get_backend_name() == "sqlite" and eng.url.database not in (None, "", ":memory:"):
        Path(eng.url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(eng)
    _ensure_columns(eng)
    _migrate_rowkey(eng)
    _backfill_changes(eng)


def _backfill_changes(eng: Engine) -> None:
    """變化紀錄補算：還沒跟前一批比對過的批次補一次（冪等）。失敗不影響開機。"""
    from . import changes
    s = Session(bind=eng, autoflush=False, expire_on_commit=False)
    try:
        n = changes.backfill(s)
        if n:
            print(f"[changes] 補算 {n} 批的變化紀錄")
    except Exception as e:  # noqa: BLE001
        s.rollback()
        print(f"[changes] !! 補算失敗：{e!r}")
    finally:
        s.close()


def _migrate_rowkey(eng: Engine) -> None:
    """一次性：既有資料補 row_key、承辦記錄與附件改掛新鍵（rowkey.migrate，冪等）。
    失敗就整段 rollback：資料維持舊鍵、照舊運作（rowkey.migrated 為 False 時一律走舊鍵），不讓服務起不來。"""
    from . import rowkey
    s = Session(bind=eng, autoflush=False, expire_on_commit=False)
    try:
        db_path = eng.url.database if eng.url.get_backend_name() == "sqlite" else None
        rowkey.migrate(s, db_path=db_path)
    except Exception as e:  # noqa: BLE001
        s.rollback()
        print(f"[rowkey] !! 轉換失敗，維持舊鍵運作：{e!r}")
    finally:
        s.close()


def get_session() -> Session:
    """FastAPI 依賴用；呼叫端負責 close（見 main.py 的 get_db）。"""
    return SessionLocal()

import os

import pytest

# 測試不啟動每週自動寄信的背景執行緒（它會連真實 DB；2026-10-27）
os.environ.setdefault("WEBVULN_AUTO_MAIL_THREAD", "0")
from sqlalchemy.orm import Session, sessionmaker

from webvuln.db import _make_engine
from webvuln.models import Base


@pytest.fixture
def engine(tmp_path):
    eng = _make_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _default_noauth():
    """測試預設免登入模式(寫入放行),讓多數測試可直接匯入/建資料;
    驗證 auth 閘門的測試自行 monkeypatch config.NO_AUTH=False。"""
    from webvuln import config
    old = config.NO_AUTH
    config.NO_AUTH = True
    try:
        yield
    finally:
        config.NO_AUTH = old


@pytest.fixture
def client(engine):
    """TestClient，DB 依賴覆寫成測試用 engine（不碰真實 DB）。"""
    from fastapi.testclient import TestClient

    from webvuln import main as m

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def _get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    m.app.dependency_overrides[m.get_db] = _get_db
    with TestClient(m.app) as c:
        yield c
    m.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    """開頁快照有伺服器端快取（以批次為鍵）；每個測試是新的 DB，先清掉避免跨測試命中。"""
    from webvuln import query
    query._SNAP_CACHE.clear()
    yield
    query._SNAP_CACHE.clear()


@pytest.fixture(autouse=True)
def _rules_gate(request, monkeypatch):
    """V2.110「規則未重算不寄信」閘門：舊的寄信測試匯入時沒帶規則版本，預設關閉閘門；
    要測閘門本身的測試加 @pytest.mark.rules_gate。"""
    if request.node.get_closest_marker("rules_gate"):
        return
    from webvuln import automail
    monkeypatch.setattr(automail, "_rules_stale", lambda db: None)

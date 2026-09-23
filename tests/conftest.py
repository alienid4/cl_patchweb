import pytest
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

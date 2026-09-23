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

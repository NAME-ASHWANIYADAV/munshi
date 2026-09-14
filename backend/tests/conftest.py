"""Shared test fixtures.

Every test runs against a throwaway SQLite file under ``tmp_path`` - the real ``data/munshiji.db``
is never touched - and entirely offline (providers are forced to local mode).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from munshiji.db.base import Base
from munshiji.db.models import Merchant


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force fully offline providers and a deterministic seed for every test."""
    monkeypatch.setenv("MUNSHIJI_PROVIDER_MODE", "local")
    monkeypatch.setenv("SARVAM_API_KEY", "")
    monkeypatch.setenv("COGNEE_API_KEY", "")
    monkeypatch.setenv("N8N_WEBHOOK_TOKEN", "")
    monkeypatch.setenv("MUNSHIJI_SEED", "20260919")

    from munshiji.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    """A fresh SQLite database per test."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    eng = create_engine(
        url, future=True, connect_args={"check_same_thread": False, "timeout": 30.0}
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    db = session_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def merchant(session: Session) -> Merchant:
    """A minimal merchant to hang test data off."""
    shop = Merchant(
        owner_name="Rajesh Sharma",
        shop_name="Sharma General Store",
        category="kirana",
        city="Delhi",
        locality="Lajpat Nagar",
        language="hi-IN",
        phone="9811000000",
    )
    session.add(shop)
    session.commit()
    return shop

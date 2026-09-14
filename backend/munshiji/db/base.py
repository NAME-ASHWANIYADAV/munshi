"""SQLAlchemy engine, session factory and schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from munshiji.config import get_settings
from munshiji.logging import get_logger

__all__ = [
    "Base",
    "get_engine",
    "get_sessionmaker",
    "init_db",
    "reset_db",
    "reset_engine",
    "session_scope",
]

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Enable foreign keys and WAL; SQLite defaults are unhelpfully lax."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is not None and url is None:
        return _engine

    settings = get_settings()
    target = url or settings.sqlalchemy_url
    is_sqlite = target.startswith("sqlite")
    engine = create_engine(
        target,
        echo=echo,
        future=True,
        # SQLite + FastAPI: the same connection may be touched from the threadpool. The timeout
        # matters because providers manage their own sessions — a writer should wait for the
        # lock, not fail instantly.
        connect_args={"check_same_thread": False, "timeout": 30.0} if is_sqlite else {},
        pool_pre_ping=not is_sqlite,
    )
    if is_sqlite:
        event.listen(engine, "connect", _configure_sqlite)

    if url is None:
        _engine = engine
    return engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Return the process-wide session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _sessionmaker


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on any exception."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    """Create any missing tables. Idempotent."""
    from munshiji.db import models  # noqa: F401  (import registers the mappers)

    target = engine or get_engine()
    Base.metadata.create_all(target)
    logger.debug("schema ensured on %s", target.url.render_as_string(hide_password=True))


def reset_engine() -> None:
    """Drop the cached engine and session factory so the next call re-reads configuration.

    Used by the API tests, which repoint ``MUNSHIJI_DB_URL`` at a temporary file.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None


def reset_db(engine: Engine | None = None) -> None:
    """Drop and recreate every table. Destructive; used by ``seed --reset`` and tests."""
    from munshiji.db import models  # noqa: F401

    target = engine or get_engine()
    Base.metadata.drop_all(target)
    Base.metadata.create_all(target)
    logger.warning("database reset")

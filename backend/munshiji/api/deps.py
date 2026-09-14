"""FastAPI dependencies: database sessions, providers, and merchant resolution."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from munshiji.config import Settings, get_settings
from munshiji.db.base import get_sessionmaker
from munshiji.db.models import Merchant
from munshiji.errors import NotFoundError
from munshiji.providers.factory import ProviderBundle, get_providers
from munshiji.repositories.core import first_merchant, require_merchant

__all__ = [
    "AppSettings",
    "CurrentMerchant",
    "DbSession",
    "Providers",
    "get_db",
    "resolve_merchant",
]


def get_db() -> Iterator[Session]:
    """Per-request session. Commits on success so route handlers stay terse."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_provider_bundle(request: Request) -> ProviderBundle:
    """The bundle resolved at startup; falls back to building one for tests."""
    bundle = getattr(request.app.state, "providers", None)
    return bundle if bundle is not None else get_providers()


def resolve_merchant(merchant_id: str, session: Session) -> Merchant:
    """Look up a merchant, accepting ``"default"`` to mean "the only one in this database"."""
    if merchant_id in {"default", "me", "-"}:
        merchant = first_merchant(session)
        if merchant is None:
            raise NotFoundError("no merchant has been seeded yet; run `munshiji seed`")
        return merchant
    return require_merchant(session, merchant_id)


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Providers = Annotated[ProviderBundle, Depends(get_provider_bundle)]


def _current_merchant(merchant_id: str, session: DbSession) -> Merchant:
    return resolve_merchant(merchant_id, session)


CurrentMerchant = Annotated[Merchant, Depends(_current_merchant)]

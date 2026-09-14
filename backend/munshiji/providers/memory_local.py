"""``LocalGraphMemory`` — the offline half of :class:`MemoryProvider`.

A real knowledge graph, not a stub (SPEC.md §2.2): nodes and edges live in SQLite, retrieval
is BM25 plus multi-hop traversal, and everything works with no API key and no internet. This
is the implementation the demo runs on when venue Wi-Fi dies.

**Sync work inside async methods, on purpose.** The provider protocol is async because the
live vendor implementations do network I/O; the database layer is deliberately sync (see
``providers/base.py``). At this scale a SQLite read is sub-millisecond and a full ingest is
tens of milliseconds, so hopping to a threadpool would cost more than it saves and would make
session lifetimes harder to reason about. If the corpus ever outgrew one merchant's shop,
this is the seam where ``asyncio.to_thread`` would go.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from munshiji.db.enums import MemoryKind
from munshiji.logging import get_logger
from munshiji.memory import graph as graph_store
from munshiji.memory import retrieval
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.memory import GraphSnapshot, MemoryContext, MemoryFact

__all__ = ["LocalGraphMemory"]

logger = get_logger(__name__)


class LocalGraphMemory:
    """Persistent graph memory backed by ``MemoryNode`` / ``MemoryEdge``."""

    name: str = "local-graph"
    mode: ProviderMode = "local"

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        """``session_factory`` defaults to :func:`munshiji.db.base.get_sessionmaker`.

        Resolution is lazy so constructing the provider never forces the engine to be built
        (tests and the CLI bind their own database after import time).
        """
        self._session_factory = session_factory

    # ── session plumbing ────────────────────────────────────────────────────

    def open_session(self) -> Session:
        """A new session from the configured factory. The caller must close it.

        Public because ``CogneeMemory`` enriches its results from this same local mirror.
        """
        factory = self._session_factory
        if factory is None:
            from munshiji.db.base import get_sessionmaker

            factory = get_sessionmaker()
        return factory()

    # ── MemoryProvider ──────────────────────────────────────────────────────

    async def ingest(self, merchant_id: str, facts: Sequence[MemoryFact]) -> int:
        """Upsert facts and their edges in one transaction. Returns nodes written."""
        if not facts:
            return 0
        session = self.open_session()
        try:
            written = graph_store.write_facts(session, merchant_id, list(facts))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        logger.debug("ingested %d memory nodes for %s", written, merchant_id)
        return written

    async def search(
        self,
        merchant_id: str,
        query: str,
        *,
        limit: int = 6,
        hops: int = 1,
        kinds: Sequence[MemoryKind] | None = None,
    ) -> MemoryContext:
        """BM25 seeds → ``hops`` of graph expansion → a rendered, provenance-carrying block."""
        session = self.open_session()
        try:
            return retrieval.search(
                session, merchant_id, query, limit=limit, hops=hops, kinds=kinds
            )
        finally:
            session.close()

    async def graph(self, merchant_id: str, *, limit: int = 200) -> GraphSnapshot:
        """A bounded subgraph for the dashboard, keyed by node reference."""
        session = self.open_session()
        try:
            return graph_store.snapshot(session, merchant_id, limit=limit)
        finally:
            session.close()

    async def forget(self, merchant_id: str) -> int:
        """Delete every node and edge for one merchant. Returns rows removed."""
        session = self.open_session()
        try:
            removed = graph_store.forget(session, merchant_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        logger.info("forgot %d memory rows for %s", removed, merchant_id)
        return removed

    async def health(self) -> ProviderHealth:
        """Cheap probe: can we open a session and count nodes? Never raises."""
        started = time.perf_counter()
        try:
            session = self.open_session()
            try:
                session.execute(sql_text("SELECT 1")).scalar_one()
                detail = "graph store reachable"
            finally:
                session.close()
        except Exception as exc:  # pragma: no cover - only fires on a broken database
            return ProviderHealth(
                name=self.name,
                kind="memory",
                mode=self.mode,
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        return ProviderHealth(
            name=self.name,
            kind="memory",
            mode=self.mode,
            ok=True,
            detail=detail,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

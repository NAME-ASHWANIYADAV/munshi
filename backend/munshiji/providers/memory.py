"""Memory provider protocol — MunshiJi's long-term knowledge of the shop.

This is the capability that separates MunshiJi from a chatbot: an action taken in one
conversation is remembered, with its outcome, and recalled unprompted in the next.

Retrieval is GraphRAG-shaped: lexical scoring finds seed nodes, then graph traversal resolves
the surrounding entity–relationship neighbourhood, so an answer carries the *relationships*
around a fact and not merely text that looked similar.

Live implementation maps onto Cognee (``add`` → ``cognify`` → ``search``); the local
implementation is a real knowledge graph over ``MemoryNode`` / ``MemoryEdge`` with a BM25 index.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from munshiji.db.enums import MemoryKind
from munshiji.providers.base import ProviderHealth, ProviderMode

__all__ = [
    "GraphSnapshot",
    "MemoryContext",
    "MemoryEdgeSpec",
    "MemoryFact",
    "MemoryHit",
    "MemoryProvider",
    "node_ref",
]


def node_ref(kind: MemoryKind | str, key: str) -> str:
    """Canonical cross-reference for a node: ``"customer:cus_01H…"``."""
    value = kind.value if isinstance(kind, MemoryKind) else str(kind)
    return f"{value}:{key}"


@dataclass(slots=True)
class MemoryEdgeSpec:
    """An outgoing relationship declared alongside a fact, by node reference."""

    rel: str
    target: str  # a node_ref(), e.g. "customer:cus_01H…"
    weight: float = 1.0
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryFact:
    """One thing worth remembering, plus how it connects to the rest of the graph."""

    kind: MemoryKind
    key: str
    label: str
    text: str
    attrs: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None
    edges: list[MemoryEdgeSpec] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return node_ref(self.kind, self.key)


@dataclass(slots=True)
class MemoryHit:
    """A retrieved fact, with why it was retrieved."""

    ref: str
    kind: MemoryKind
    label: str
    text: str
    score: float
    hops: int = 0
    #: Human-readable provenance chain, e.g. ``["action:act_01H… -targeted-> customer:cus_…"]``
    path: list[str] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None


@dataclass(slots=True)
class MemoryContext:
    """Retrieval result, ready to inject into a prompt."""

    query: str
    hits: list[MemoryHit] = field(default_factory=list)
    rendered: str = ""
    provider: ProviderMode = "local"
    latency_ms: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.hits


@dataclass(slots=True)
class GraphSnapshot:
    """Nodes and edges for the dashboard's graph visualisation."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    provider: ProviderMode = "local"
    truncated: bool = False


@runtime_checkable
class MemoryProvider(Protocol):
    """Persistent, queryable memory of one merchant's business."""

    name: str
    mode: ProviderMode

    async def ingest(self, merchant_id: str, facts: Sequence[MemoryFact]) -> int:
        """Upsert facts and their edges. Returns the number of nodes written."""
        ...

    async def search(
        self,
        merchant_id: str,
        query: str,
        *,
        limit: int = 6,
        hops: int = 1,
        kinds: Sequence[MemoryKind] | None = None,
        budget_seconds: float | None = None,
    ) -> MemoryContext:
        """Retrieve seed nodes lexically, then expand ``hops`` along the graph.

        ``budget_seconds`` caps how long a *live* backend may take before the provider serves
        its local mirror instead. Callers that merely enrich a prompt pass a small budget; a
        caller whose whole answer IS the memory (the recall tool) leaves it ``None`` and grants
        the graph the full transport timeout. Instant providers ignore it.
        """
        ...

    async def graph(self, merchant_id: str, *, limit: int = 200) -> GraphSnapshot:
        """Return a bounded subgraph for visualisation."""
        ...

    async def forget(self, merchant_id: str) -> int:
        """Delete every memory for a merchant. Returns rows removed."""
        ...

    async def health(self) -> ProviderHealth: ...

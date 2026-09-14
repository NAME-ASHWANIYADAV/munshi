"""Memory search results and the knowledge-graph view."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from munshiji.db.enums import MemoryKind
from munshiji.providers.memory import GraphSnapshot, MemoryContext
from munshiji.schemas.common import ApiModel, Meta

__all__ = [
    "GraphEdgeOut",
    "GraphNodeOut",
    "GraphOut",
    "MemoryHitOut",
    "MemorySearchIn",
    "MemorySearchOut",
]


class MemorySearchIn(ApiModel):
    query: str
    limit: int = 6
    hops: int = 1
    kinds: list[MemoryKind] | None = None


class MemoryHitOut(ApiModel):
    ref: str
    kind: MemoryKind
    label: str
    text: str
    score: float
    hops: int = 0
    path: list[str] = Field(default_factory=list)
    occurred_at: datetime | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class MemorySearchOut(ApiModel):
    query: str
    hits: list[MemoryHitOut] = Field(default_factory=list)
    rendered: str = ""
    meta: Meta = Field(default_factory=Meta)

    @classmethod
    def from_context(cls, context: MemoryContext) -> MemorySearchOut:
        return cls(
            query=context.query,
            hits=[
                MemoryHitOut(
                    ref=hit.ref,
                    kind=hit.kind,
                    label=hit.label,
                    text=hit.text,
                    score=hit.score,
                    hops=hit.hops,
                    path=hit.path,
                    occurred_at=hit.occurred_at,
                    attrs=hit.attrs,
                )
                for hit in context.hits
            ],
            rendered=context.rendered,
            meta=Meta(provider=context.provider, latency_ms=context.latency_ms),
        )


class GraphNodeOut(ApiModel):
    id: str
    ref: str
    kind: str
    label: str
    text: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeOut(ApiModel):
    source: str
    target: str
    rel: str
    weight: float = 1.0


class GraphOut(ApiModel):
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)
    truncated: bool = False
    meta: Meta = Field(default_factory=Meta)

    @classmethod
    def from_snapshot(cls, snapshot: GraphSnapshot) -> GraphOut:
        return cls(
            nodes=[GraphNodeOut(**node) for node in snapshot.nodes],
            edges=[GraphEdgeOut(**edge) for edge in snapshot.edges],
            truncated=snapshot.truncated,
            meta=Meta(provider=snapshot.provider),
        )

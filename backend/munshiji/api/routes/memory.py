"""Memory search and the knowledge-graph view."""

from __future__ import annotations

from fastapi import APIRouter, Query

from munshiji.api.deps import CurrentMerchant, Providers
from munshiji.schemas.memory import GraphOut, MemorySearchIn, MemorySearchOut

router = APIRouter(tags=["memory"])


@router.post(
    "/memory/{merchant_id}/search",
    response_model=MemorySearchOut,
    summary="Search long-term memory",
)
async def search_memory(
    merchant: CurrentMerchant, payload: MemorySearchIn, providers: Providers
) -> MemorySearchOut:
    """Retrieve facts about this shop's past — including actions and what they earned.

    Lexical scoring picks seed nodes, then the graph is traversed ``hops`` steps so the answer
    carries the relationships around a fact, not just text that looked similar.
    """
    context = await providers.memory.search(
        merchant.id,
        payload.query,
        limit=payload.limit,
        hops=payload.hops,
        kinds=payload.kinds,
    )
    return MemorySearchOut.from_context(context)


@router.get(
    "/memory/{merchant_id}/graph",
    response_model=GraphOut,
    summary="Knowledge graph for visualisation",
)
async def get_graph(
    merchant: CurrentMerchant,
    providers: Providers,
    limit: int = Query(default=200, ge=10, le=1000),
) -> GraphOut:
    """A bounded subgraph of nodes and typed edges, keyed by stable refs."""
    snapshot = await providers.memory.graph(merchant.id, limit=limit)
    return GraphOut.from_snapshot(snapshot)

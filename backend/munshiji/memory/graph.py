"""The local knowledge graph — a real store over ``MemoryNode`` / ``MemoryEdge``.

This is the offline half of the dual-mode memory provider (SPEC.md §2.1, §8). It is not a
stub: nodes have a stable identity, edges are typed and weighted, traversal is breadth-first
in both directions, and everything survives a process restart because it lives in SQLite.

Identity rules
--------------
* A node is uniquely ``(merchant_id, kind, key)`` — matching ``uq_memory_node_identity``.
  Re-ingesting the same fact updates it in place rather than growing the graph.
* An edge is uniquely ``(src_id, dst_id, rel)`` — matching ``uq_memory_edge_identity``.
* Cross-references between facts travel as ``node_ref()`` strings (``"customer:cus_01H…"``),
  never as internal row ids, so a fact can point at a node that does not exist yet.
  :func:`write_facts` therefore writes in two passes: every node first, then every edge.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from munshiji.clock import now_utc
from munshiji.db.enums import MemoryKind
from munshiji.db.models import MemoryEdge, MemoryNode
from munshiji.logging import get_logger
from munshiji.providers.memory import GraphSnapshot, MemoryFact, node_ref

__all__ = [
    "forget",
    "load_nodes",
    "neighbours",
    "node_count",
    "parse_ref",
    "resolve_ref",
    "resolve_refs",
    "snapshot",
    "upsert_edge",
    "upsert_node",
    "write_facts",
]

logger = get_logger(__name__)

#: SQLite caps host parameters per statement; stay comfortably below it on every ``IN`` clause.
_CHUNK = 400


def _chunks(values: Sequence[str], size: int = _CHUNK) -> Iterator[list[str]]:
    """Yield ``values`` in slices small enough for a SQL ``IN (…)`` clause."""
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def parse_ref(ref: str) -> tuple[MemoryKind, str] | None:
    """Split ``"customer:cus_01H…"`` into ``(MemoryKind.CUSTOMER, "cus_01H…")``.

    Returns ``None`` when the string is not a well-formed reference to a known kind.
    """
    kind_part, separator, key = ref.partition(":")
    if not separator or not key:
        return None
    try:
        return MemoryKind(kind_part), key
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────────


def upsert_node(session: Session, merchant_id: str, fact: MemoryFact) -> MemoryNode:
    """Insert or update the node identified by ``(merchant_id, fact.kind, fact.key)``.

    Text, label, attrs and ``occurred_at`` are overwritten with the incoming fact and
    ``updated_at`` is bumped (UTC). The row is flushed so the caller can use ``node.id``.
    """
    existing = session.scalars(
        select(MemoryNode).where(
            MemoryNode.merchant_id == merchant_id,
            MemoryNode.kind == fact.kind,
            MemoryNode.key == fact.key,
        )
    ).first()

    if existing is None:
        node = MemoryNode(
            merchant_id=merchant_id,
            kind=fact.kind,
            key=fact.key,
            label=fact.label,
            text=fact.text,
            attrs=dict(fact.attrs),
            occurred_at=fact.occurred_at,
        )
        session.add(node)
        session.flush()
        return node

    existing.label = fact.label
    existing.text = fact.text
    existing.attrs = dict(fact.attrs)
    if fact.occurred_at is not None:
        existing.occurred_at = fact.occurred_at
    # Set explicitly: ``onupdate`` only fires when SQLAlchemy sees the row as dirty, and an
    # identical re-ingest should still record that we saw the fact again.
    existing.updated_at = now_utc()
    session.flush()
    return existing


def upsert_edge(
    session: Session,
    merchant_id: str,
    src_id: str,
    dst_id: str,
    rel: str,
    weight: float = 1.0,
    attrs: dict[str, Any] | None = None,
) -> MemoryEdge:
    """Insert or update the edge ``(src_id) -[rel]-> (dst_id)``. Idempotent on that triple."""
    existing = session.scalars(
        select(MemoryEdge).where(
            MemoryEdge.src_id == src_id,
            MemoryEdge.dst_id == dst_id,
            MemoryEdge.rel == rel,
        )
    ).first()

    if existing is None:
        edge = MemoryEdge(
            merchant_id=merchant_id,
            src_id=src_id,
            dst_id=dst_id,
            rel=rel,
            weight=float(weight),
            attrs=dict(attrs or {}),
        )
        session.add(edge)
        session.flush()
        return edge

    existing.weight = float(weight)
    if attrs:
        existing.attrs = {**existing.attrs, **attrs}
    session.flush()
    return existing


def write_facts(session: Session, merchant_id: str, facts: Sequence[MemoryFact]) -> int:
    """Persist ``facts`` and their edges. Returns the number of nodes written.

    Two passes on purpose: an edge declared on the first fact may target a node created by
    the last one, so every node is written before any edge is resolved. Targets that still
    cannot be resolved (neither in this batch nor already in the database) are skipped with
    a warning rather than aborting the whole ingest.
    """
    if not facts:
        return 0

    # Pass 1 — nodes.
    ref_to_id: dict[str, str] = {}
    for fact in facts:
        node = upsert_node(session, merchant_id, fact)
        ref_to_id[fact.ref] = node.id

    # Pass 2 — edges, now that every target in the batch has an id.
    unresolved: list[str] = []
    for fact in facts:
        src_id = ref_to_id[fact.ref]
        for spec in fact.edges:
            dst_id = ref_to_id.get(spec.target)
            if dst_id is None:
                target = resolve_ref(session, merchant_id, spec.target)
                if target is None:
                    unresolved.append(f"{fact.ref} -{spec.rel}-> {spec.target}")
                    continue
                dst_id = target.id
                ref_to_id[spec.target] = dst_id
            if dst_id == src_id:
                continue  # self-loops carry no information here
            upsert_edge(session, merchant_id, src_id, dst_id, spec.rel, spec.weight, spec.attrs)

    if unresolved:
        logger.warning(
            "memory ingest skipped %d unresolvable edge(s) for %s: %s",
            len(unresolved),
            merchant_id,
            ", ".join(unresolved[:5]),
        )
    return len(facts)


def forget(session: Session, merchant_id: str) -> int:
    """Delete every node and edge belonging to ``merchant_id``. Returns rows removed."""
    edges = session.execute(
        delete(MemoryEdge).where(MemoryEdge.merchant_id == merchant_id)
    ).rowcount
    nodes = session.execute(
        delete(MemoryNode).where(MemoryNode.merchant_id == merchant_id)
    ).rowcount
    session.flush()
    return int(edges or 0) + int(nodes or 0)


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────


def resolve_ref(session: Session, merchant_id: str, ref: str) -> MemoryNode | None:
    """Look up a node by canonical reference, e.g. ``"customer:cus_01H…"``."""
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    kind, key = parsed
    return session.scalars(
        select(MemoryNode).where(
            MemoryNode.merchant_id == merchant_id,
            MemoryNode.kind == kind,
            MemoryNode.key == key,
        )
    ).first()


def resolve_refs(session: Session, merchant_id: str, refs: Iterable[str]) -> dict[str, MemoryNode]:
    """Resolve many references at once; unknown references are simply absent from the result."""
    wanted = [ref for ref in dict.fromkeys(refs) if parse_ref(ref) is not None]
    if not wanted:
        return {}
    keys = [parse_ref(ref)[1] for ref in wanted]  # type: ignore[index]
    found: dict[str, MemoryNode] = {}
    for chunk in _chunks(keys):
        for node in session.scalars(
            select(MemoryNode).where(
                MemoryNode.merchant_id == merchant_id, MemoryNode.key.in_(chunk)
            )
        ).all():
            found[node_ref(node.kind, node.key)] = node
    return {ref: found[ref] for ref in wanted if ref in found}


def load_nodes(
    session: Session,
    merchant_id: str,
    *,
    kinds: Sequence[MemoryKind] | None = None,
    limit: int | None = None,
) -> list[MemoryNode]:
    """Every node for a merchant, newest-updated first. The retrieval index is built from this."""
    stmt = select(MemoryNode).where(MemoryNode.merchant_id == merchant_id)
    if kinds:
        stmt = stmt.where(MemoryNode.kind.in_(list(kinds)))
    # Rows written inside one batch can share an updated_at, and ids only sort by time down to the
    # millisecond — below that their random suffix decides, which would make the order differ
    # between runs. Keys break the tie instead: meaningless as a ranking, but stable, so a demo
    # replays identically and the retrieval index is built from the same list every time.
    stmt = stmt.order_by(MemoryNode.updated_at.desc(), MemoryNode.key)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def node_count(session: Session, merchant_id: str) -> int:
    """How many nodes this merchant's graph holds."""
    total = session.scalar(
        select(func.count(MemoryNode.id)).where(MemoryNode.merchant_id == merchant_id)
    )
    return int(total or 0)


def neighbours(
    session: Session,
    merchant_id: str,
    node_ids: Sequence[str],
    *,
    hops: int = 1,
) -> dict[str, list[tuple[MemoryEdge, MemoryNode]]]:
    """Breadth-first adjacency around ``node_ids``, out to ``hops`` levels.

    Traversal is **undirected**: an edge is followed from either endpoint, because
    ``(action)-[targeted]->(customer)`` should let a question about the customer surface the
    action just as readily as the reverse. A visited set bounds the walk, so cycles and the
    merchant hub node terminate instead of looping.

    Returns a map of ``node id -> [(edge, node on the other end), …]`` covering every node
    that was expanded. The caller (``memory.retrieval``) walks this map to assign hop counts
    and build provenance paths.
    """
    adjacency: dict[str, list[tuple[MemoryEdge, MemoryNode]]] = {}
    frontier = list(dict.fromkeys(node_ids))
    if not frontier or hops <= 0:
        return adjacency
    visited: set[str] = set(frontier)

    for _ in range(hops):
        if not frontier:
            break
        frontier_set = set(frontier)
        edges: list[MemoryEdge] = []
        for chunk in _chunks(frontier):
            edges.extend(
                session.scalars(
                    select(MemoryEdge).where(
                        MemoryEdge.merchant_id == merchant_id,
                        or_(MemoryEdge.src_id.in_(chunk), MemoryEdge.dst_id.in_(chunk)),
                    )
                ).all()
            )
        if not edges:
            break

        wanted = sorted({edge.src_id for edge in edges} | {edge.dst_id for edge in edges})
        nodes: dict[str, MemoryNode] = {}
        for chunk in _chunks(wanted):
            for node in session.scalars(select(MemoryNode).where(MemoryNode.id.in_(chunk))).all():
                nodes[node.id] = node

        next_frontier: list[str] = []
        for edge in edges:
            for origin, other in ((edge.src_id, edge.dst_id), (edge.dst_id, edge.src_id)):
                if origin not in frontier_set or origin == other:
                    continue
                other_node = nodes.get(other)
                if other_node is None:
                    continue
                adjacency.setdefault(origin, []).append((edge, other_node))
                if other not in visited:
                    visited.add(other)
                    next_frontier.append(other)
        frontier = next_frontier

    return adjacency


def snapshot(session: Session, merchant_id: str, limit: int = 200) -> GraphSnapshot:
    """A bounded subgraph for the dashboard's graph panel (``GET /api/memory/{id}/graph``).

    Nodes and edges are keyed by ``node_ref()`` rather than internal row ids so the frontend
    can join them to anything else the API returns. Newest-updated nodes win the cap; edges
    are included only when both endpoints survived it.
    """
    limit = max(1, int(limit))
    rows = list(
        session.scalars(
            select(MemoryNode)
            .where(MemoryNode.merchant_id == merchant_id)
            .order_by(MemoryNode.updated_at.desc(), MemoryNode.id)
            .limit(limit + 1)
        ).all()
    )
    truncated = len(rows) > limit
    rows = rows[:limit]

    by_id = {node.id: node_ref(node.kind, node.key) for node in rows}
    nodes = [
        {
            "id": node.id,
            "ref": by_id[node.id],
            "kind": node.kind.value if isinstance(node.kind, MemoryKind) else str(node.kind),
            "label": node.label,
            "text": node.text,
            "attrs": dict(node.attrs or {}),
        }
        for node in rows
    ]

    ids = list(by_id)
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for chunk in _chunks(ids):
        for edge in session.scalars(
            select(MemoryEdge).where(
                MemoryEdge.merchant_id == merchant_id, MemoryEdge.src_id.in_(chunk)
            )
        ).all():
            source = by_id.get(edge.src_id)
            target = by_id.get(edge.dst_id)
            if source is None or target is None:
                continue
            key = (source, target, edge.rel)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {"source": source, "target": target, "rel": edge.rel, "weight": edge.weight}
            )

    return GraphSnapshot(nodes=nodes, edges=edges, provider="local", truncated=truncated)

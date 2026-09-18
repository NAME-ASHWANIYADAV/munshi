"""``CogneeMemory`` — the live half of :class:`MemoryProvider`, backed by Cognee.

Same protocol, same call sites, different engine (SPEC.md §2.1). The mapping is:

===================  =========================================================
Protocol             Cognee
===================  =========================================================
``ingest``           ``add`` one document per fact → ``cognify`` the dataset
``search``           ``search`` in ``GRAPH_COMPLETION`` mode
``graph``            dataset graph export, falling back to the local snapshot
``forget``           delete the dataset (and the local mirror)
===================  =========================================================

Two deliberate choices:

* **Relationships travel in the prose.** Cognee builds its graph by extracting entities and
  relations from text, so each uploaded document ends with the fact's edges written out as
  sentences *and* as ``kind:key`` reference tokens. That gives Cognee something to extract and
  gives us something to parse back out of a result.
* **Every ingest is mirrored locally.** The local graph is cheap (one SQLite write) and it is
  what keeps the demo alive: if Cognee is unreachable mid-conversation the factory falls back
  to ``LocalGraphMemory`` and the memory is already there. It is also what ``graph()`` renders
  when the hosted graph export is unavailable.

Failures raise :class:`ProviderUnavailableError` rather than bubbling raw ``httpx`` errors, so
the factory can fall back with a logged warning instead of crashing the request.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from datetime import timedelta

from munshiji.clock import now_utc
from munshiji.db.enums import MemoryKind
from munshiji.db.models import MemoryNode
from munshiji.errors import ProviderUnavailableError
from munshiji.integrations.cognee_client import (
    SEARCH_TYPE_GRAPH_COMPLETION,
    CogneeClient,
    CogneeDocument,
    CogneeSearchResult,
)
from munshiji.logging import get_logger
from munshiji.memory import graph as graph_store
from munshiji.memory.retrieval import render_context
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.memory import (
    GraphSnapshot,
    MemoryContext,
    MemoryFact,
    MemoryHit,
    node_ref,
)
from munshiji.providers.memory_local import LocalGraphMemory

__all__ = ["CogneeMemory", "dataset_name_for"]

logger = get_logger(__name__)


def dataset_name_for(merchant_id: str) -> str:
    """One Cognee dataset per merchant: ``munshiji_mer_01H…``."""
    return f"munshiji_{merchant_id}"


#: On the first ingest of a process whose dataset already exists remotely (i.e. the seed built
#: the graph), only facts that occurred inside this window are uploaded — everything older is
#: assumed to already be in the graph.
_FRESHNESS_WINDOW = timedelta(hours=1)


def _document_for(fact: MemoryFact, labels: dict[str, str] | None = None) -> CogneeDocument:
    """Render a fact — including its edges — as one extractable document.

    ``labels`` maps node refs to human names. Cognee's extractor builds its graph from the
    entities it can READ: "This action targeted Sunita Devi (customer:cus_x)" produces an edge
    to a person; "action:act_x targeted cus_01H…" produces an edge to a serial number. The ref
    token stays in parentheses so results can still be parsed back onto our own nodes.
    """
    labels = labels or {}
    body = [fact.text]
    if fact.occurred_at is not None:
        body.append(f"This happened on {fact.occurred_at.isoformat()}.")
    if fact.edges:
        body.append("Relationships:")
        for spec in fact.edges:
            rel = spec.rel.replace("_", " ")
            name = labels.get(spec.target)
            subject = f"This {fact.kind.value}"
            if name:
                body.append(f"- {subject} {rel} {name} ({spec.target})")
            else:
                body.append(f"- {subject} {rel} {spec.target}")
    return CogneeDocument(ref=fact.ref, title=fact.label, text="\n".join(body))


class CogneeMemory:
    """Graph memory served by Cognee's hosted API, mirrored into the local store."""

    name: str = "cognee"
    mode: ProviderMode = "live"

    def __init__(
        self,
        client: CogneeClient | None = None,
        *,
        fallback: LocalGraphMemory | None = None,
        mirror_local: bool = True,
    ) -> None:
        self._client = client or CogneeClient()
        self._fallback = fallback or LocalGraphMemory()
        self._mirror_local = mirror_local
        #: dataset → {ref: sha1(document text)}. What this process has already uploaded, so a
        #: conversation turn re-ingesting the whole shop uploads only what actually changed.
        self._uploaded: dict[str, dict[str, str]] = {}

    @property
    def client(self) -> CogneeClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── MemoryProvider ──────────────────────────────────────────────────────

    async def ingest(self, merchant_id: str, facts: Sequence[MemoryFact]) -> int:
        """``add`` what changed to the merchant's dataset, then ``cognify`` it.

        The local mirror always receives the FULL fact set — it is the offline twin and it is
        cheap. The hosted side is delta-only: the agent loop re-derives every fact about the
        shop after each executed action, and re-uploading ~450 unchanged documents plus a full
        re-cognify on every turn would burn the credit budget in one rehearsal evening and add
        half a minute of latency mid-conversation.

        Three cases per call:
        * first call, dataset already exists on the tenant → the seed built it; record the
          fingerprints, upload only facts newer than the freshness window;
        * first call, dataset absent → this IS the full build (seed CLI path): upload it all
          and cognify synchronously, because the caller wants a finished graph;
        * later calls → upload only documents whose rendered text changed, cognify in the
          background so the turn is not held hostage to graph construction.
        """
        if not facts:
            return 0
        dataset = dataset_name_for(merchant_id)
        labels = {fact.ref: fact.label for fact in facts if fact.label}
        documents = {fact.ref: _document_for(fact, labels) for fact in facts}

        if self._mirror_local:
            try:
                await self._fallback.ingest(merchant_id, facts)
            except Exception as exc:  # pragma: no cover - mirroring must never break ingest
                logger.warning("local mirror of cognee ingest failed: %s", exc)

        fingerprints = {
            ref: hashlib.sha1(document.as_text().encode("utf-8")).hexdigest()
            for ref, document in documents.items()
        }

        seen = self._uploaded.get(dataset)
        first_call = seen is None
        full_build = False
        if first_call:
            try:
                existing = await self._client.dataset_id(dataset)
            except ProviderUnavailableError as exc:
                # A deployment that cannot LIST datasets can still ADD to one. Treat "cannot
                # tell" as "absent": a redundant full upload is recoverable, a silently empty
                # graph is not.
                logger.warning("cognee dataset listing unavailable (%s); assuming absent", exc)
                existing = None
            if existing is None:
                full_build = True
                changed_refs = list(documents)
            else:
                # The graph was built by an earlier process (the seed). Adopt its state and
                # send only what is genuinely new since then.
                cutoff = now_utc() - _FRESHNESS_WINDOW
                by_ref = {fact.ref: fact for fact in facts}
                changed_refs = [
                    ref
                    for ref, fact in by_ref.items()
                    if fact.occurred_at is not None and fact.occurred_at >= cutoff
                ]
        else:
            changed_refs = [ref for ref, digest in fingerprints.items() if seen.get(ref) != digest]

        self._uploaded[dataset] = fingerprints

        if not changed_refs:
            logger.info("cognee ingest: nothing changed for %s (%d facts)", dataset, len(facts))
            return 0

        # The managed /add ingests synchronously, so one giant batch is one giant timeout.
        # Chunks keep every call comfortably inside the client timeout; order is irrelevant.
        chunk_size = 25
        for start in range(0, len(changed_refs), chunk_size):
            chunk = changed_refs[start : start + chunk_size]
            await self._client.add([documents[ref] for ref in chunk], dataset_name=dataset)
        await self._client.cognify(dataset_name=dataset, background=not full_build)
        logger.info(
            "cognee ingested %d/%d documents into %s (%s)",
            len(changed_refs),
            len(documents),
            dataset,
            "full build" if full_build else "delta",
        )
        return len(changed_refs)

    async def search(
        self,
        merchant_id: str,
        query: str,
        *,
        limit: int = 6,
        hops: int = 1,
        kinds: Sequence[MemoryKind] | None = None,
    ) -> MemoryContext:
        """Ask Cognee's graph, then shape the answer into ``MemoryHit`` objects.

        ``hops`` is not passed through — Cognee performs its own graph walk in
        ``GRAPH_COMPLETION`` mode — but ``kinds`` is applied to whatever references we manage
        to recover, so callers get the same filtering semantics as the local provider.
        """
        started = time.perf_counter()
        # The completion behind GRAPH_COMPLETION follows instructions in the query. Every
        # consumer of this provider speaks or renders chat text, so ask for prose once here
        # rather than scraping markdown tables out of the answer at every call site.
        styled = (
            f"{query}\n\n"
            "Answer in one or two short sentences, in the same language as the question. "
            "Plain text only: no markdown, no tables, no bullet lists, no internal ids."
        )
        try:
            results = await self._client.search(
                styled,
                dataset_name=dataset_name_for(merchant_id),
                search_type=SEARCH_TYPE_GRAPH_COMPLETION,
                top_k=max(limit, 1),
            )
        except ProviderUnavailableError as exc:
            # A slow or absent graph must never cost the merchant the conversation. The local
            # mirror holds the same facts; serve those and SAY SO — the context's provider
            # field flips to "local", so nothing upstream can claim a live answer it did not
            # get. The dual-provider promise, applied per-call rather than only at boot.
            logger.warning("cognee search failed (%s); serving the local mirror", exc.message)
            return await self._fallback.search(
                merchant_id, query, limit=limit, hops=hops, kinds=kinds
            )
        allowed = {MemoryKind(kind) for kind in kinds} if kinds else None
        hits: list[MemoryHit] = []
        for position, result in enumerate(results):
            hit = self._to_hit(merchant_id, result, position)
            if allowed is not None and hit.kind not in allowed:
                continue
            hits.append(hit)
            if len(hits) >= limit:
                break

        return MemoryContext(
            query=query,
            hits=hits,
            rendered=render_context(hits, query=query),
            provider="live",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _to_hit(self, merchant_id: str, result: CogneeSearchResult, position: int) -> MemoryHit:
        """Map one Cognee result onto a hit, enriching from the local mirror when possible."""
        # Cognee does not always return a comparable score; preserve its ordering instead.
        score = result.score if result.score > 0 else round(1.0 - position * 0.05, 4)
        kind = MemoryKind.NOTE
        label = result.name or "Cognee recall"
        text = result.text
        ref = result.ref or f"{MemoryKind.NOTE.value}:cognee-{position}"
        occurred_at = None
        attrs: dict[str, object] = {"source": "cognee"}

        parsed = graph_store.parse_ref(ref) if result.ref else None
        if parsed is not None:
            kind = parsed[0]
            if self._mirror_local:
                node = self._lookup(merchant_id, ref)
                if node is not None:
                    label = node.label
                    text = node.text
                    occurred_at = node.occurred_at
                    attrs = {**dict(node.attrs or {}), "source": "cognee"}
        return MemoryHit(
            ref=ref,
            kind=kind,
            label=label,
            text=text,
            score=score,
            hops=0,
            path=[],
            attrs=attrs,
            occurred_at=occurred_at,
        )

    def _lookup(self, merchant_id: str, ref: str) -> MemoryNode | None:
        """Resolve a reference against the local mirror; ``None`` on any problem."""
        try:
            session = self._fallback.open_session()
            try:
                return graph_store.resolve_ref(session, merchant_id, ref)
            finally:
                session.close()
        except Exception:  # pragma: no cover - enrichment is strictly best-effort
            return None

    async def graph(self, merchant_id: str, *, limit: int = 200) -> GraphSnapshot:
        """Cognee's dataset graph if it is exposed, else the local mirror — labelled honestly.

        The returned ``provider`` field says which one actually served the snapshot, so the
        dashboard badge never claims a live graph it did not get.
        """
        try:
            payload = await self._client.graph(dataset_name=dataset_name_for(merchant_id))
        except ProviderUnavailableError as exc:
            logger.warning("cognee graph export unavailable (%s); using local mirror", exc)
            payload = None

        snapshot = _snapshot_from_payload(payload, limit=limit)
        if snapshot is not None:
            return snapshot
        local = await self._fallback.graph(merchant_id, limit=limit)
        local.provider = "local"
        return local

    async def forget(self, merchant_id: str) -> int:
        """Drop the Cognee dataset and the local mirror. Returns local rows removed."""
        try:
            deleted = await self._client.delete_dataset(dataset_name_for(merchant_id))
            logger.info("cognee dataset delete for %s: %s", merchant_id, deleted)
        except ProviderUnavailableError as exc:
            logger.warning("cognee dataset delete failed for %s: %s", merchant_id, exc)
        return await self._fallback.forget(merchant_id)

    async def health(self) -> ProviderHealth:
        """Authenticated probe. Never raises — an unhealthy vendor is a fallback, not a crash."""
        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        if not self._client.api_key:
            return ProviderHealth(
                name=self.name,
                kind="memory",
                mode=self.mode,
                ok=False,
                detail="COGNEE_API_KEY is not set",
                latency_ms=elapsed(),
            )
        try:
            detail = await self._client.ping()
        except ProviderUnavailableError as exc:
            return ProviderHealth(
                name=self.name,
                kind="memory",
                mode=self.mode,
                ok=False,
                detail=exc.message,
                latency_ms=elapsed(),
            )
        except Exception as exc:  # pragma: no cover - belt and braces; health cannot raise
            return ProviderHealth(
                name=self.name,
                kind="memory",
                mode=self.mode,
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                latency_ms=elapsed(),
            )
        return ProviderHealth(
            name=self.name,
            kind="memory",
            mode=self.mode,
            ok=True,
            detail=detail,
            latency_ms=elapsed(),
        )


# ── graph payload normalisation ─────────────────────────────────────────────

_NODE_KEYS = ("nodes", "vertices", "entities")
_EDGE_KEYS = ("edges", "links", "relationships", "relations")


def _snapshot_from_payload(payload: object, *, limit: int) -> GraphSnapshot | None:
    """Best-effort conversion of a Cognee graph export into a :class:`GraphSnapshot`.

    Returns ``None`` when the payload carries no recognisable nodes, which is the signal to
    fall back to the local mirror.
    """
    if not isinstance(payload, dict):
        return None
    raw_nodes = next(
        (payload[key] for key in _NODE_KEYS if isinstance(payload.get(key), list)), None
    )
    if not raw_nodes:
        return None
    raw_edges = next((payload[key] for key in _EDGE_KEYS if isinstance(payload.get(key), list)), [])

    nodes: list[dict[str, object]] = []
    refs: dict[str, str] = {}
    for item in raw_nodes[:limit]:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or item.get("node_id") or len(nodes))
        label = str(item.get("name") or item.get("label") or item.get("title") or identifier)
        kind = str(item.get("type") or item.get("kind") or MemoryKind.NOTE.value).lower()
        ref = item.get("ref")
        reference = str(ref) if isinstance(ref, str) and ":" in ref else node_ref(kind, identifier)
        refs[identifier] = reference
        nodes.append(
            {
                "id": identifier,
                "ref": reference,
                "kind": kind,
                "label": label,
                "text": str(item.get("text") or item.get("description") or ""),
                "attrs": item.get("properties") if isinstance(item.get("properties"), dict) else {},
            }
        )
    if not nodes:
        return None

    edges: list[dict[str, object]] = []
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        source = refs.get(str(item.get("source") or item.get("from") or item.get("src") or ""))
        target = refs.get(str(item.get("target") or item.get("to") or item.get("dst") or ""))
        if source is None or target is None:
            continue
        weight = item.get("weight")
        edges.append(
            {
                "source": source,
                "target": target,
                "rel": str(item.get("rel") or item.get("type") or item.get("label") or "related"),
                "weight": float(weight) if isinstance(weight, int | float) else 1.0,
            }
        )

    return GraphSnapshot(
        nodes=nodes,
        edges=edges,
        provider="live",
        truncated=len(raw_nodes) > limit,
    )

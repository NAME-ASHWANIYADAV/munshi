"""GraphRAG-lite retrieval: BM25 seeds, then graph expansion with provenance.

SPEC.md §8. Two stages, deliberately:

1. **Lexical seeding.** A hand-rolled BM25 index (``k1=1.5``, ``b=0.75``) over every node's
   ``label + text + searchable attrs``. No external search dependency — the whole corpus for
   one merchant is a few thousand short documents, so the index is derived wholly from the
   merchant's current nodes and is rebuilt the moment any of them change. See
   :func:`build_index` for why that is a fingerprint check rather than an unconditional
   rebuild on every keystroke.
2. **Graph expansion.** The seeds are expanded ``hops`` levels through ``MemoryEdge``, scoring
   each expanded node ``seed_score × Π(edge weights) × 0.55**hop``. This is what lets
   *"pichla offer kya hua?"* — which lexically matches only the **action** node — return the
   customers that action targeted and the day it ran, each carrying a readable provenance
   chain like ``action:act_01H… -targeted-> customer:cus_01H…``.

Every score is finally decayed by recency (``0.98 ** age_days`` off ``occurred_at``), so a
stale fact loses to a fresh one that matched equally well.

The tokeniser is the part that has to earn its keep in India: merchants type and speak a mix
of Devanagari, romanised Hindi and English, with rupee symbols and Indian digit grouping.
Note that Python's ``\\w`` is useless here — Devanagari matras and the virama are category
``Mn``, so ``re.findall(r"\\w+", "ग्राहक")`` returns ``['ग', 'र', 'हक']``. We therefore match an
explicit Devanagari codepoint range instead.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from munshiji.clock import days_between, now_utc, to_ist
from munshiji.db.enums import MemoryKind
from munshiji.db.models import MemoryNode
from munshiji.memory import graph
from munshiji.money import fmt_inr
from munshiji.providers.memory import MemoryContext, MemoryHit, node_ref

__all__ = [
    "HOP_DECAY",
    "RECENCY_DECAY_PER_DAY",
    "STOPWORDS",
    "BM25Index",
    "NodeView",
    "build_index",
    "clear_index_cache",
    "corpus_fingerprint",
    "load_index_rows",
    "render_context",
    "search",
    "searchable_text",
    "tokenise",
]

# ─────────────────────────────────────────────────────────────────────────────
# Tokenisation
# ─────────────────────────────────────────────────────────────────────────────

#: U+0966–U+096F, the Devanagari digits ०–९.
_DEVANAGARI_DIGITS = "०-९"
#: The Devanagari block (U+0900–U+097F) minus its digits and minus the danda punctuation
#: (U+0964 ।, U+0965 ॥). The danda ends most Hindi sentences, so leaving it in would make
#: "बिक्री।" a different token from "बिक्री" and silently break every sentence-final word.
_DEVANAGARI_LETTERS = "ऀ-ॣ॰-ॿ"

#: Ordered alternation, cheapest-first: a number (with Indian grouping), then a Devanagari run
#: matched by **codepoint range** so matras and the virama survive, then ASCII, then any other
#: unicode letter run. Kept to four short branches because this runs ~50k times per query.
_TOKEN_RE = re.compile(
    rf"[0-9{_DEVANAGARI_DIGITS}][0-9{_DEVANAGARI_DIGITS},.]*"
    rf"|[{_DEVANAGARI_LETTERS}]+"
    r"|[A-Za-z]+"
    r"|[^\W\d_]+"
)

_DIGIT_TABLE = str.maketrans("०१२३४५६७८९", "0123456789")

#: Small, deliberate stopword list: Hindi (Devanagari), romanised Hindi, and English function
#: words. Kept short — over-pruning a two-word merchant question is worse than a little noise.
STOPWORDS: frozenset[str] = frozenset(
    {
        # Hindi — Devanagari
        "है",
        "हैं",
        "था",
        "थी",
        "थे",
        "हो",
        "होगा",
        "का",
        "की",
        "के",
        "को",
        "में",
        "से",
        "पर",
        "और",
        "कि",
        "यह",
        "वह",
        "ये",
        "वो",
        "ही",
        "भी",
        "तो",
        "एक",
        "ने",
        "नहीं",
        "क्या",
        "कैसे",
        "हुआ",
        "हुई",
        "मैं",
        "आप",
        "मेरा",
        "मेरी",
        "अपना",
        # Hindi — romanised (how merchants actually type)
        "hai",
        "hain",
        "tha",
        "thi",
        "the",
        "ho",
        "hoga",
        "ka",
        "ki",
        "ke",
        "ko",
        "mein",
        "me",
        "se",
        "par",
        "aur",
        "yeh",
        "woh",
        "bhi",
        "toh",
        "ek",
        "ne",
        "nahi",
        "nahin",
        "kya",
        "kaise",
        "hua",
        "hui",
        "mera",
        "meri",
        "apna",
        # English
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "it",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "how",
        "did",
        "do",
        "does",
        "done",
        "with",
        "at",
        "by",
        "from",
        "my",
        "i",
        "you",
        "any",
        "all",
        "as",
        "so",
        "we",
        "us",
        "our",
        "about",
    }
)


def _normalise_number(raw: str) -> list[str]:
    """``"18,540"`` → ``["18540"]``; ``"18,540.50"`` → ``["18540.50", "18540"]``.

    Emitting the bare digit run is what makes ``"18540"`` and ``"₹18,540"`` the same token,
    so a merchant can quote a figure back at MunshiJi in either form.
    """
    text = raw.translate(_DIGIT_TABLE).replace(",", "").strip(".")
    if not text:
        return []
    tokens = [text]
    if "." in text:
        whole = text.split(".", 1)[0]
        if whole and whole != text:
            tokens.append(whole)
    return tokens


def tokenise(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Split mixed Devanagari/Latin/numeric text into comparable tokens.

    Latin is lowercased, Devanagari is kept as-is (it has no case), punctuation and the rupee
    sign are dropped, Indian digit grouping is normalised away, and single Latin characters
    are discarded as noise.
    """
    if not text:
        return []
    tokens: list[str] = []
    # ``findall`` over ``finditer``: this loop runs ~50k times per query on a full corpus and
    # skipping the Match object per token is worth roughly a third of the tokenisation budget.
    append = tokens.append
    for raw in _TOKEN_RE.findall(text):
        if raw[0].isdigit():  # ``isdigit`` is true for ०–९ as well as 0–9
            # Fast path only for bare ASCII digits: anything with a separator ("18,540") or
            # in Devanagari ("१२") still has to go through normalisation.
            if raw.isascii() and raw.isdigit():
                append(raw)
            else:
                tokens.extend(_normalise_number(raw))
            continue
        if len(raw) == 1 and raw.isascii():
            continue  # stray initials and list bullets are noise
        append(raw.lower())
    if drop_stopwords:
        kept = [token for token in tokens if token not in STOPWORDS]
        # Never return nothing: a question made entirely of function words still deserves a try.
        if kept:
            return kept
        return tokens
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# BM25
# ─────────────────────────────────────────────────────────────────────────────

K1 = 1.5
B = 0.75


@dataclass(slots=True)
class BM25Index:
    """Okapi BM25 over a small in-memory corpus. See :func:`build_index` for its lifecycle."""

    doc_ids: list[str] = field(default_factory=list)
    doc_lengths: list[int] = field(default_factory=list)
    postings: dict[str, dict[int, int]] = field(default_factory=dict)
    average_length: float = 0.0
    k1: float = K1
    b: float = B

    @classmethod
    def build(
        cls, documents: Iterable[tuple[str, str]], *, k1: float = K1, b: float = B
    ) -> BM25Index:
        """Index ``(doc_id, text)`` pairs."""
        index = cls(k1=k1, b=b)
        total_length = 0
        doc_ids, doc_lengths, postings = index.doc_ids, index.doc_lengths, index.postings
        for doc_id, text in documents:
            tokens = tokenise(text)
            position = len(doc_ids)
            doc_ids.append(doc_id)
            doc_lengths.append(len(tokens))
            total_length += len(tokens)
            # Count first, then touch each posting list once per *distinct* term rather than
            # once per occurrence — ``Counter`` does the tallying in C.
            for token, freq in Counter(tokens).items():
                postings.setdefault(token, {})[position] = freq
        count = len(doc_ids)
        index.average_length = (total_length / count) if count else 0.0
        return index

    def __len__(self) -> int:
        return len(self.doc_ids)

    def idf(self, term: str) -> float:
        """Robertson/Sparck-Jones IDF with the ``+1`` guard that keeps scores non-negative."""
        df = len(self.postings.get(term, ()))
        if df == 0:
            return 0.0
        total = len(self.doc_ids)
        return math.log(1.0 + (total - df + 0.5) / (df + 0.5))

    def search(self, query: str, *, limit: int = 20) -> list[tuple[str, float]]:
        """Top ``limit`` ``(doc_id, score)`` pairs, best first. Unmatched documents are absent."""
        terms = tokenise(query)
        if not terms or not self.doc_ids:
            return []
        scores: dict[int, float] = {}
        average = self.average_length or 1.0
        for term in terms:
            bucket = self.postings.get(term)
            if not bucket:
                continue
            idf = self.idf(term)
            for position, freq in bucket.items():
                length = self.doc_lengths[position] or 1
                denominator = freq + self.k1 * (1.0 - self.b + self.b * length / average)
                contribution = idf * freq * (self.k1 + 1.0) / denominator
                scores[position] = scores.get(position, 0.0) + contribution
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.doc_ids[item[0]]))
        return [(self.doc_ids[position], score) for position, score in ranked[:limit]]


# ─────────────────────────────────────────────────────────────────────────────
# Node → document
# ─────────────────────────────────────────────────────────────────────────────

#: Attr keys never worth indexing — internal bookkeeping, not something anyone would ask about.
_SKIP_ATTRS = frozenset({"provider", "schema", "version"})


def _flatten_attr(value: Any, depth: int = 0) -> list[str]:
    """Render a JSON attr value into indexable strings (scalars and one level of nesting)."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str | int | float):
        return [str(value)]
    if depth >= 2:
        return []
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(str(key))
            parts.extend(_flatten_attr(item, depth + 1))
        return parts
    if isinstance(value, list | tuple):
        parts = []
        for item in value[:32]:
            parts.extend(_flatten_attr(item, depth + 1))
        return parts
    return []


def searchable_text(node: NodeLike) -> str:
    """The document BM25 indexes for one node: label, text, and scalar attrs."""
    parts = [node.label, node.text]
    for key, value in (node.attrs or {}).items():
        if key in _SKIP_ATTRS or key.startswith("_"):
            continue
        parts.append(str(key).replace("_", " "))
        parts.extend(_flatten_attr(value))
    return " ".join(part for part in parts if part)


@dataclass(slots=True)
class NodeView:
    """A memory node as the index needs it — columns, not a mapped entity.

    Hydrating a few thousand ``MemoryNode`` objects is the single most expensive step in the
    search path, and retrieval never mutates a node, so it reads plain columns instead. Field
    names mirror the ORM model so the same helpers work on either (``graph.neighbours`` hands
    back real entities).
    """

    id: str
    kind: MemoryKind
    key: str
    label: str
    text: str
    attrs: dict[str, Any]
    occurred_at: datetime | None


#: Either a mapped ``MemoryNode`` or the lightweight :class:`NodeView` above.
NodeLike = MemoryNode | NodeView


def load_index_rows(session: Session, merchant_id: str) -> list[NodeView]:
    """Every node for a merchant as :class:`NodeView` rows, newest-updated first."""
    rows = session.execute(
        select(
            MemoryNode.id,
            MemoryNode.kind,
            MemoryNode.key,
            MemoryNode.label,
            MemoryNode.text,
            MemoryNode.attrs,
            MemoryNode.occurred_at,
        )
        .where(MemoryNode.merchant_id == merchant_id)
        .order_by(MemoryNode.updated_at.desc(), MemoryNode.id)
    ).all()
    return [NodeView(*row) for row in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Index cache
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _CachedIndex:
    fingerprint: tuple[int, str]
    nodes: list[NodeView]
    index: BM25Index


#: Per-merchant index cache, validated against a corpus fingerprint on every query.
_INDEX_CACHE: dict[str, _CachedIndex] = {}
#: One demo shop needs one entry; the bound only exists so a multi-tenant run cannot leak.
_CACHE_LIMIT = 8


def corpus_fingerprint(session: Session, merchant_id: str) -> tuple[int, str]:
    """``(node count, newest updated_at)`` — changes on any insert, update or delete."""
    count, newest = session.execute(
        select(func.count(MemoryNode.id), func.max(MemoryNode.updated_at)).where(
            MemoryNode.merchant_id == merchant_id
        )
    ).one()
    return int(count or 0), str(newest or "")


def clear_index_cache(merchant_id: str | None = None) -> None:
    """Drop cached indexes — for one merchant, or all of them."""
    if merchant_id is None:
        _INDEX_CACHE.clear()
    else:
        _INDEX_CACHE.pop(merchant_id, None)


def build_index(
    session: Session, merchant_id: str, *, use_cache: bool = True
) -> tuple[list[NodeView], BM25Index]:
    """The merchant's nodes and a BM25 index over them.

    The index is derived **only** from the merchant's current nodes, exactly as SPEC.md §8
    describes — but it is not thrown away between queries for no reason. A cheap
    ``COUNT(*) + MAX(updated_at)`` probe (well under a millisecond) decides whether the cached
    index still matches the corpus; any ingest, edit or deletion changes that fingerprint and
    forces a rebuild. So a query can never see stale memory, while the ~60-100 ms tokenisation
    pass is paid once per *change* rather than once per *question* — which matters when the
    caller is a voice loop the merchant is waiting on.
    """
    fingerprint = corpus_fingerprint(session, merchant_id)
    cached = _INDEX_CACHE.get(merchant_id)
    if use_cache and cached is not None and cached.fingerprint == fingerprint:
        return cached.nodes, cached.index

    nodes = load_index_rows(session, merchant_id)
    index = BM25Index.build((node.id, searchable_text(node)) for node in nodes)
    if use_cache:
        if len(_INDEX_CACHE) >= _CACHE_LIMIT and merchant_id not in _INDEX_CACHE:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)), None)
        _INDEX_CACHE[merchant_id] = _CachedIndex(fingerprint, nodes, index)
    return nodes, index


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

#: Score multiplier applied once per hop away from a lexical seed.
HOP_DECAY = 0.55
#: Per-day exponential decay on ``occurred_at``; ~0.55 after a month, ~0.16 after six.
RECENCY_DECAY_PER_DAY = 0.98
#: Seeds are expanded from, so take a few more than the caller's final ``limit``.
_SEED_FLOOR = 4


def _recency_factor(occurred_at: datetime | None, as_of: datetime) -> float:
    """``0.98 ** age_days`` in IST calendar days; 1.0 for undated (always-true) facts."""
    if occurred_at is None:
        return 1.0
    age = days_between(occurred_at, as_of)
    if age <= 0:
        return 1.0
    return RECENCY_DECAY_PER_DAY**age


def _hit_from_node(node: NodeLike, score: float, hops: int, path: list[str]) -> MemoryHit:
    return MemoryHit(
        ref=node_ref(node.kind, node.key),
        kind=node.kind if isinstance(node.kind, MemoryKind) else MemoryKind(node.kind),
        label=node.label,
        text=node.text,
        score=round(score, 6),
        hops=hops,
        path=path,
        attrs=dict(node.attrs or {}),
        occurred_at=node.occurred_at,
    )


def search(
    session: Session,
    merchant_id: str,
    query: str,
    *,
    limit: int = 6,
    hops: int = 1,
    kinds: Sequence[MemoryKind] | None = None,
    as_of: datetime | None = None,
) -> MemoryContext:
    """Retrieve lexical seeds, expand the graph around them, and render a prompt block.

    ``kinds`` filters the **seeds** only — expansion still crosses into other kinds, which is
    the whole point of graph retrieval (ask about an action, learn about its customers).
    """
    started = time.perf_counter()
    moment = as_of or now_utc()
    if not query.strip():
        return MemoryContext(
            query=query,
            hits=[],
            rendered="",
            provider="local",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    nodes, index = build_index(session, merchant_id)
    if not nodes:
        return MemoryContext(
            query=query,
            hits=[],
            rendered="",
            provider="local",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    by_id = {node.id: node for node in nodes}

    allowed = {MemoryKind(kind) for kind in kinds} if kinds else None
    seeds: list[tuple[NodeLike, float]] = []
    seed_budget = max(limit, _SEED_FLOOR)
    for doc_id, raw_score in index.search(query, limit=max(seed_budget * 4, 24)):
        node = by_id.get(doc_id)
        if node is None:
            continue
        if allowed is not None and node.kind not in allowed:
            continue
        seeds.append((node, raw_score * _recency_factor(node.occurred_at, moment)))
        if len(seeds) >= seed_budget:
            break

    if not seeds:
        return MemoryContext(
            query=query,
            hits=[],
            rendered="",
            provider="local",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # Best score per node id, plus how we got there.
    best: dict[str, tuple[float, int, list[str], NodeLike]] = {}

    def offer(node: NodeLike, score: float, hop: int, path: list[str]) -> bool:
        current = best.get(node.id)
        if current is not None and current[0] >= score:
            return False
        best[node.id] = (score, hop, path, node)
        return True

    for node, score in seeds:
        offer(node, score, 0, [])

    adjacency = (
        graph.neighbours(session, merchant_id, [node.id for node, _ in seeds], hops=hops)
        if hops > 0
        else {}
    )

    if adjacency:
        # Breadth-first from every seed, carrying the running edge-weight product and path.
        frontier: list[tuple[str, float, int, list[str]]] = [
            (node.id, score, 0, []) for node, score in seeds
        ]
        visited: set[str] = {node.id for node, _ in seeds}
        for hop in range(1, hops + 1):
            next_frontier: list[tuple[str, float, int, list[str]]] = []
            for node_id, carried, _, path in frontier:
                source = by_id.get(node_id)
                source_ref = node_ref(source.kind, source.key) if source else node_id
                for edge, other in adjacency.get(node_id, []):
                    other_ref = node_ref(other.kind, other.key)
                    step = f"{source_ref} -{edge.rel}-> {other_ref}"
                    # One HOP_DECAY per step — ``carried`` already holds the decay from the
                    # hops before this one, so ``HOP_DECAY ** hop`` here would compound it and
                    # a 2-hop node would be penalised by 0.55³ instead of the documented 0.55².
                    propagated = carried * max(edge.weight, 0.0) * HOP_DECAY
                    score = propagated * _recency_factor(other.occurred_at, moment)
                    hop_path = [*path, step]
                    if offer(other, score, hop, hop_path) and other.id not in visited:
                        visited.add(other.id)
                        next_frontier.append((other.id, propagated, hop, hop_path))
            frontier = next_frontier
            if not frontier:
                break

    hits = [_hit_from_node(node, score, hop, path) for score, hop, path, node in best.values()]
    hits.sort(key=lambda hit: (-hit.score, -_timestamp(hit.occurred_at), hit.ref))
    hits = hits[: max(1, limit)]

    return MemoryContext(
        query=query,
        hits=hits,
        rendered=render_context(hits, query=query),
        provider="local",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

#: Node text carries both an English and a Hindi sentence, so the budget has to fit both —
#: truncating at ~260 reliably cut the Hindi half off, which is the half the TTS speaks from.
_MAX_TEXT = 340
_MAX_PATH = 2


def _band(score: float, top: float) -> int:
    """Coarse relevance band (0 = strongest). Used to group before sorting by recency."""
    if top <= 0:
        return 0
    ratio = score / top
    if ratio >= 0.66:
        return 0
    if ratio >= 0.33:
        return 1
    return 2


def _when(value: datetime | None) -> str:
    """IST display date, e.g. ``"14 Sep 2026"``."""
    if value is None:
        return ""
    return to_ist(value).strftime("%d %b %Y")


def _money_suffix(hit: MemoryHit, text: str) -> str:
    """Surface money attrs the node text does not already spell out, formatted as ₹."""
    parts: list[str] = []
    for key, value in (hit.attrs or {}).items():
        if not key.endswith("_paise") or not isinstance(value, int) or isinstance(value, bool):
            continue
        if value == 0:
            continue
        rendered = fmt_inr(value)
        if rendered in text:
            continue
        parts.append(f"{key[: -len('_paise')].replace('_', ' ')} {rendered}")
        if len(parts) >= 3:
            break
    return f" [{'; '.join(parts)}]" if parts else ""


def render_context(hits: Sequence[MemoryHit], *, query: str = "") -> str:
    """A compact, prompt-injectable block: one line per hit, newest-first within score bands.

    Dates are IST and money is already formatted, so the LLM never has to do arithmetic or
    timezone conversion on the way to the merchant's ear.
    """
    if not hits:
        return ""
    top = max(hit.score for hit in hits)
    ordered = sorted(
        hits,
        key=lambda hit: (_band(hit.score, top), -_timestamp(hit.occurred_at), -hit.score, hit.ref),
    )

    header = f'Memory — recalled for "{query.strip()}":' if query.strip() else "Memory:"
    lines = [header]
    for hit in ordered:
        text = " ".join(hit.text.split())
        if len(text) > _MAX_TEXT:
            text = text[: _MAX_TEXT - 1].rstrip() + "…"
        kind = hit.kind.value if isinstance(hit.kind, MemoryKind) else str(hit.kind)
        stamp = _when(hit.occurred_at)
        tag = f"{kind} · {stamp}" if stamp else kind
        # Day nodes label themselves by date, which the tag and the text already say. Printing
        # it a third time just burns prompt budget.
        label = "" if (stamp and stamp in hit.label) or text.startswith(hit.label) else hit.label
        body = f"{label}: {text}" if label else text
        line = f"- [{tag}] {body}{_money_suffix(hit, text)}"
        if hit.path:
            trail = " › ".join(hit.path[:_MAX_PATH])
            if len(hit.path) > _MAX_PATH:
                trail += " › …"
            line += f" (via {trail})"
        lines.append(line)
    return "\n".join(lines)

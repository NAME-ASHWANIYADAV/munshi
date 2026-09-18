"""Tests for the memory layer (SPEC.md §8, §13).

Everything here runs offline against a throwaway SQLite file under ``tmp_path`` — the real
``data/munshiji.db`` is never opened — and the Cognee tests drive a ``httpx.MockTransport``
rather than the network.

The load-bearing test is :func:`test_cross_session_recall_of_action_outcome`: it is the
product's whole differentiator expressed as an assertion.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import now_utc, to_ist, today_ist, weekday_name
from munshiji.db.base import Base, get_engine
from munshiji.db.enums import (
    ActionStatus,
    ConversationChannel,
    CustomerSegment,
    InsightKind,
    MemoryKind,
    PaymentMethod,
    Severity,
    TurnRole,
)
from munshiji.db.models import (
    ActionOutcome,
    ActionRequest,
    Conversation,
    Customer,
    Insight,
    MemoryEdge,
    MemoryNode,
    Merchant,
    Product,
    Transaction,
    TransactionItem,
    Turn,
)
from munshiji.errors import ProviderUnavailableError
from munshiji.integrations.cognee_client import (
    ADD_PATH,
    COGNIFY_PATH,
    DATASETS_PATH,
    SEARCH_PATH,
    CogneeClient,
    parse_search_results,
)
from munshiji.memory import graph, ingest, retrieval
from munshiji.memory.retrieval import BM25Index
from munshiji.providers.memory import (
    MemoryEdgeSpec,
    MemoryFact,
    MemoryProvider,
    node_ref,
)
from munshiji.providers.memory_cognee import CogneeMemory, dataset_name_for
from munshiji.providers.memory_local import LocalGraphMemory

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_index_cache() -> Iterator[None]:
    """The BM25 index cache is process-wide; never let one test's corpus leak into another."""
    retrieval.clear_index_cache()
    yield
    retrieval.clear_index_cache()


@pytest.fixture()
def session_factory(tmp_path) -> Iterator[sessionmaker[Session]]:
    """A session factory bound to a fresh SQLite file inside ``tmp_path``."""
    engine = get_engine(f"sqlite:///{(tmp_path / 'memory.db').as_posix()}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def make_merchant(db: Session, *, shop_name: str = "Sharma General Store") -> Merchant:
    merchant = Merchant(
        owner_name="Ramesh Sharma",
        shop_name=shop_name,
        category="kirana",
        city="Delhi",
        locality="Lajpat Nagar",
        phone="9810012345",
        monthly_rent_paise=3_500_000,
        opened_at=now_utc() - timedelta(days=900),
    )
    db.add(merchant)
    db.flush()
    return merchant


@pytest.fixture()
def shop(session: Session) -> dict[str, object]:
    """A small but complete shop: customers, a sale, an insight, an executed action, a chat.

    Deliberately real rows rather than hand-written memory nodes — the ingest functions are
    part of what is under test.
    """
    merchant = make_merchant(session)

    customers = []
    for index in range(12):
        customer = Customer(
            merchant_id=merchant.id,
            name=f"Grahak {index:02d}",
            phone=f"98100{index:05d}",
            first_seen_at=now_utc() - timedelta(days=200),
            last_seen_at=now_utc() - timedelta(days=30 + index),
            txn_count=20 + index,
            total_spend_paise=500_000 + index * 10_000,
            segment=CustomerSegment.AT_RISK,
        )
        session.add(customer)
        customers.append(customer)
    session.flush()

    product = Product(
        merchant_id=merchant.id,
        sku="ATTA5KG",
        name="Aashirvaad Atta 5kg",
        name_hi="आशीर्वाद आटा ५ किलो",
        category="staples",
        unit="pkt",
        cost_price_paise=24_000,
        sell_price_paise=28_500,
        stock_qty=40,
        reorder_level=12,
    )
    session.add(product)
    session.flush()

    for index in range(6):
        txn = Transaction(
            merchant_id=merchant.id,
            customer_id=customers[index].id,
            amount_paise=28_500,
            occurred_at=now_utc() - timedelta(minutes=10 * index),
            payment_method=PaymentMethod.UPI if index % 2 else PaymentMethod.CASH,
        )
        session.add(txn)
        session.flush()
        session.add(
            TransactionItem(
                transaction_id=txn.id,
                product_id=product.id,
                qty=1,
                unit_price_paise=28_500,
                unit_cost_paise=24_000,
                line_total_paise=28_500,
            )
        )

    insight = Insight(
        merchant_id=merchant.id,
        kind=InsightKind.DORMANT_CUSTOMERS,
        severity=Severity.HIGH,
        title_en="12 regulars have stopped coming",
        title_hi="12 पक्के ग्राहक आना बंद कर चुके हैं",
        body_en="Twelve customers who used to visit weekly have not returned in over a month.",
        body_hi="बारह ग्राहक जो हर हफ्ते आते थे, एक महीने से नहीं आए।",
        metrics={"customer_ids": [c.id for c in customers], "dormant_count": 12},
        suggested_tool="send_winback_offer",
        suggested_params={"customer_ids": [c.id for c in customers]},
        impact_paise=1_200_000,
        score=88.0,
    )
    session.add(insight)

    conversation = Conversation(
        merchant_id=merchant.id,
        channel=ConversationChannel.VOICE,
        language="hi-IN",
        started_at=now_utc() - timedelta(hours=2),
    )
    session.add(conversation)
    session.flush()
    session.add(
        Turn(
            conversation_id=conversation.id,
            seq=1,
            role=TurnRole.MERCHANT,
            text="aaj ka collection kitna hua",
        )
    )
    session.add(
        Turn(
            conversation_id=conversation.id,
            seq=2,
            role=TurnRole.MUNSHI,
            text="Aaj abhi tak ₹1,710 aaya hai, 6 bikri.",
        )
    )

    action = ActionRequest(
        merchant_id=merchant.id,
        insight_id=insight.id,
        conversation_id=conversation.id,
        tool_name="send_winback_offer",
        params={"customer_ids": [c.id for c in customers], "discount_pct": 10},
        summary_en="Winback offer to 12 dormant customers",
        summary_hi="12 सोए हुए ग्राहकों को वापसी ऑफर",
        status=ActionStatus.EXECUTED,
        target_count=12,
        estimated_impact_paise=1_200_000,
        decided_at=now_utc() - timedelta(hours=1),
        executed_at=now_utc() - timedelta(hours=1),
    )
    session.add(action)
    session.flush()
    session.add(ActionOutcome(action_id=action.id, metric="redeemed", value_num=4))
    session.add(ActionOutcome(action_id=action.id, metric="recovered", value_paise=234_000))
    session.commit()

    return {
        "merchant": merchant,
        "customers": customers,
        "product": product,
        "insight": insight,
        "action": action,
        "conversation": conversation,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tokeniser
# ─────────────────────────────────────────────────────────────────────────────


def test_tokenise_handles_mixed_devanagari_latin_and_money() -> None:
    assert retrieval.tokenise("aaj ₹18,540 aaye — 12 ग्राहक") == [
        "aaj",
        "18540",
        "aaye",
        "12",
        "ग्राहक",
    ]


def test_tokenise_keeps_devanagari_words_whole() -> None:
    """Regression guard: Python's ``\\w`` would shred this into ['ग', 'र', 'हक']."""
    assert retrieval.tokenise("ग्राहक शुक्रवार वसूली") == ["ग्राहक", "शुक्रवार", "वसूली"]


def test_tokenise_normalises_grouped_and_devanagari_digits() -> None:
    assert retrieval.tokenise("₹18,540") == retrieval.tokenise("18540") == ["18540"]
    assert retrieval.tokenise("१२") == ["12"]


def test_tokenise_drops_stopwords_but_never_returns_nothing() -> None:
    assert "hai" not in retrieval.tokenise("kitna hai")
    assert retrieval.tokenise("kya hua") == ["kya", "hua"]  # all-stopword query survives


# ─────────────────────────────────────────────────────────────────────────────
# BM25
# ─────────────────────────────────────────────────────────────────────────────


def test_bm25_exact_phrase_outranks_partial_match() -> None:
    index = BM25Index.build(
        [
            ("exact", "winback offer sent to dormant customers"),
            ("partial", "restock order drafted because dormant stock is piling up"),
            ("noise", "peak hour is between seven and nine in the evening"),
        ]
    )
    ranked = index.search("winback offer dormant customers")
    scores = dict(ranked)
    assert ranked[0][0] == "exact"
    assert scores["exact"] > scores["partial"]
    assert "noise" not in scores


def test_bm25_matches_a_devanagari_query_to_a_devanagari_document() -> None:
    index = BM25Index.build(
        [
            ("hindi", "12 सितंबर 2026 (शुक्रवार): कुल वसूली ₹18,540, 47 बिक्री।"),
            ("english", "Stock levels for atta and rice are healthy this week."),
        ]
    )
    ranked = index.search("वसूली कितनी हुई")
    assert ranked and ranked[0][0] == "hindi"


def test_bm25_matches_a_bare_number_against_grouped_money() -> None:
    index = BM25Index.build(
        [
            ("friday", "collection ₹18,540 across 47 sales"),
            ("saturday", "collection ₹9,120 across 31 sales"),
        ]
    )
    ranked = index.search("18540")
    assert [doc for doc, _ in ranked] == ["friday"]


def test_bm25_uses_the_documented_parameters() -> None:
    index = BM25Index.build([("a", "atta rice dal")])
    assert (index.k1, index.b) == (1.5, 0.75)
    assert index.search("nothing matches here") == []


# ─────────────────────────────────────────────────────────────────────────────
# Graph store
# ─────────────────────────────────────────────────────────────────────────────


def test_upsert_node_is_idempotent_on_merchant_kind_key(session: Session) -> None:
    merchant = make_merchant(session)
    fact = MemoryFact(
        kind=MemoryKind.CUSTOMER, key="cus_x", label="Rakesh", text="first", attrs={"v": 1}
    )
    first = graph.upsert_node(session, merchant.id, fact)
    before = first.updated_at

    fact.label, fact.text, fact.attrs = "Rakesh Kumar", "second", {"v": 2}
    second = graph.upsert_node(session, merchant.id, fact)

    assert second.id == first.id
    assert session.scalar(select(func.count(MemoryNode.id))) == 1
    assert (second.label, second.text, second.attrs) == ("Rakesh Kumar", "second", {"v": 2})
    assert second.updated_at >= before


def test_upsert_edge_is_idempotent_on_src_dst_rel(session: Session) -> None:
    merchant = make_merchant(session)
    src = graph.upsert_node(session, merchant.id, MemoryFact(MemoryKind.ACTION, "act_x", "A", "a"))
    dst = graph.upsert_node(
        session, merchant.id, MemoryFact(MemoryKind.CUSTOMER, "cus_x", "C", "c")
    )

    graph.upsert_edge(session, merchant.id, src.id, dst.id, "targeted", 0.5)
    edge = graph.upsert_edge(session, merchant.id, src.id, dst.id, "targeted", 0.9)
    assert session.scalar(select(func.count(MemoryEdge.id))) == 1
    assert edge.weight == 0.9

    graph.upsert_edge(session, merchant.id, src.id, dst.id, "reminded", 1.0)
    assert session.scalar(select(func.count(MemoryEdge.id))) == 2


def test_write_facts_resolves_edges_declared_before_their_target(session: Session) -> None:
    """Two-pass write: the action is written first but points at a customer written later."""
    merchant = make_merchant(session)
    action = MemoryFact(
        kind=MemoryKind.ACTION,
        key="act_x",
        label="Winback",
        text="winback offer",
        edges=[MemoryEdgeSpec("targeted", node_ref(MemoryKind.CUSTOMER, "cus_x"), 0.9)],
    )
    customer = MemoryFact(MemoryKind.CUSTOMER, "cus_x", "Grahak", "regular buyer")

    assert graph.write_facts(session, merchant.id, [action, customer]) == 2
    edges = list(session.scalars(select(MemoryEdge)).all())
    assert len(edges) == 1
    assert edges[0].rel == "targeted"
    assert graph.resolve_ref(session, merchant.id, "customer:cus_x").id == edges[0].dst_id


def test_resolve_ref_rejects_malformed_and_unknown_references(session: Session) -> None:
    merchant = make_merchant(session)
    assert graph.resolve_ref(session, merchant.id, "not-a-ref") is None
    assert graph.resolve_ref(session, merchant.id, "nosuchkind:abc") is None
    assert graph.resolve_ref(session, merchant.id, "customer:missing") is None
    assert graph.parse_ref("customer:cus_1") == (MemoryKind.CUSTOMER, "cus_1")
    assert graph.parse_ref("customer:") is None

    graph.write_facts(
        session, merchant.id, [MemoryFact(MemoryKind.CUSTOMER, "cus_1", "Grahak", "buyer")]
    )
    resolved = graph.resolve_refs(session, merchant.id, ["customer:cus_1", "customer:nope"])
    assert list(resolved) == ["customer:cus_1"]
    assert resolved["customer:cus_1"].label == "Grahak"


def test_neighbours_traverses_both_directions_and_terminates_on_cycles(
    session: Session,
) -> None:
    merchant = make_merchant(session)
    a = graph.upsert_node(session, merchant.id, MemoryFact(MemoryKind.ACTION, "a", "A", "a"))
    b = graph.upsert_node(session, merchant.id, MemoryFact(MemoryKind.CUSTOMER, "b", "B", "b"))
    c = graph.upsert_node(session, merchant.id, MemoryFact(MemoryKind.DAY, "c", "C", "c"))
    graph.upsert_edge(session, merchant.id, a.id, b.id, "targeted")
    graph.upsert_edge(session, merchant.id, b.id, c.id, "visited_on")
    graph.upsert_edge(session, merchant.id, c.id, a.id, "loops_back")  # cycle

    # Starting at the *target* still finds the action: traversal is undirected.
    one_hop = graph.neighbours(session, merchant.id, [b.id], hops=1)
    assert {node.id for _, node in one_hop[b.id]} == {a.id, c.id}

    two_hops = graph.neighbours(session, merchant.id, [a.id], hops=2)
    reached = {node.id for pairs in two_hops.values() for _, node in pairs}
    assert reached == {a.id, b.id, c.id}


def test_snapshot_uses_refs_and_reports_truncation(session: Session) -> None:
    merchant = make_merchant(session)
    facts = [
        MemoryFact(
            kind=MemoryKind.ACTION,
            key="act_x",
            label="Winback",
            text="winback offer",
            edges=[MemoryEdgeSpec("targeted", node_ref(MemoryKind.CUSTOMER, "cus_x"), 0.9)],
        ),
        MemoryFact(MemoryKind.CUSTOMER, "cus_x", "Grahak", "buyer"),
        MemoryFact(MemoryKind.DAY, "2026-09-14", "Day", "collection"),
    ]
    graph.write_facts(session, merchant.id, facts)
    session.commit()

    full = graph.snapshot(session, merchant.id, limit=10)
    assert not full.truncated
    assert {key for node in full.nodes for key in node} == {
        "id",
        "ref",
        "kind",
        "label",
        "text",
        "attrs",
    }
    assert {key for edge in full.edges for key in edge} == {
        "source",
        "target",
        "rel",
        "weight",
    }
    refs = {node["ref"] for node in full.nodes}
    assert refs == {"action:act_x", "customer:cus_x", "day:2026-09-14"}
    assert full.edges == [
        {"source": "action:act_x", "target": "customer:cus_x", "rel": "targeted", "weight": 0.9}
    ]

    capped = graph.snapshot(session, merchant.id, limit=2)
    assert capped.truncated is True
    assert len(capped.nodes) == 2

    # Newest-updated first. The relative order of rows written inside one batch is deliberately
    # *not* asserted: their timestamps can land in the same microsecond, so any such expectation
    # is a coin flip. What is guaranteed - and what the retrieval index relies on - is that every
    # node comes back, sorted by updated_at descending, with a tie-break that is stable across
    # runs (key, never id: ids only sort by time down to the millisecond).
    ordered = graph.load_nodes(session, merchant.id)
    assert {node.key for node in ordered} == {"2026-09-14", "cus_x", "act_x"}
    stamps = [node.updated_at for node in ordered]
    assert stamps == sorted(stamps, reverse=True)
    assert [node.key for node in ordered] == [
        node.key for node in graph.load_nodes(session, merchant.id)
    ], "repeated reads must return the same order"
    customers = graph.load_nodes(session, merchant.id, kinds=[MemoryKind.CUSTOMER])
    assert [node.key for node in customers] == ["cus_x"]
    assert len(graph.load_nodes(session, merchant.id, limit=2)) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────────────


def test_multi_hop_expansion_reaches_nodes_the_query_never_matched(session: Session) -> None:
    """The query hits only the action; the customers arrive over the graph, with provenance."""
    merchant = make_merchant(session)
    facts = [
        MemoryFact(
            kind=MemoryKind.ACTION,
            key="act_1",
            label="Winback offer",
            text="Winback offer sent to two lapsed buyers with a ten percent discount.",
            occurred_at=now_utc() - timedelta(days=1),
            edges=[
                MemoryEdgeSpec("targeted", node_ref(MemoryKind.CUSTOMER, "cus_1"), 0.9),
                MemoryEdgeSpec("targeted", node_ref(MemoryKind.CUSTOMER, "cus_2"), 0.9),
            ],
        ),
        MemoryFact(MemoryKind.CUSTOMER, "cus_1", "Rakesh", "Buys atta and rice every Tuesday."),
        MemoryFact(MemoryKind.CUSTOMER, "cus_2", "Sunita", "Pays by UPI, likes dal."),
    ]
    graph.write_facts(session, merchant.id, facts)
    session.commit()

    context = retrieval.search(session, merchant.id, "winback offer", limit=6, hops=1)
    hits = {hit.ref: hit for hit in context.hits}

    assert set(hits) == {"action:act_1", "customer:cus_1", "customer:cus_2"}
    assert hits["action:act_1"].hops == 0
    assert hits["action:act_1"].path == []

    expanded = hits["customer:cus_1"]
    assert expanded.hops == 1
    assert expanded.path == ["action:act_1 -targeted-> customer:cus_1"]
    assert expanded.score < hits["action:act_1"].score
    assert "-targeted->" in context.rendered


def test_two_hop_scores_decay_exactly_once_per_hop(session: Session) -> None:
    """``seed × Π(edge weights) × 0.55**hop`` — the decay must not compound per level."""
    merchant = make_merchant(session)
    graph.write_facts(
        session,
        merchant.id,
        [
            # No occurred_at anywhere, so recency is 1.0 and the arithmetic is exact.
            MemoryFact(
                kind=MemoryKind.ACTION,
                key="act_1",
                label="Winback offer",
                text="Winback offer sent with a ten percent discount.",
                edges=[MemoryEdgeSpec("targeted", node_ref(MemoryKind.CUSTOMER, "cus_1"), 0.9)],
            ),
            MemoryFact(
                kind=MemoryKind.CUSTOMER,
                key="cus_1",
                label="Rakesh",
                text="Buys atta every Tuesday.",
                edges=[MemoryEdgeSpec("visited_on", node_ref(MemoryKind.DAY, "2026-09-12"), 0.5)],
            ),
            MemoryFact(MemoryKind.DAY, "2026-09-12", "Day", "Collection was steady."),
        ],
    )
    session.commit()

    hits = {
        hit.ref: hit
        for hit in retrieval.search(session, merchant.id, "winback offer", limit=6, hops=2).hits
    }
    seed = hits["action:act_1"].score

    assert hits["customer:cus_1"].hops == 1
    assert hits["customer:cus_1"].score == pytest.approx(seed * 0.9 * 0.55, rel=1e-4)

    assert hits["day:2026-09-12"].hops == 2
    assert hits["day:2026-09-12"].score == pytest.approx(seed * 0.9 * 0.55 * 0.5 * 0.55, rel=1e-4)
    assert hits["day:2026-09-12"].path == [
        "action:act_1 -targeted-> customer:cus_1",
        "customer:cus_1 -visited_on-> day:2026-09-12",
    ]


def test_search_applies_the_kinds_filter_to_seeds(session: Session) -> None:
    merchant = make_merchant(session)
    graph.write_facts(
        session,
        merchant.id,
        [
            MemoryFact(MemoryKind.ACTION, "act_1", "Winback", "winback offer to lapsed buyers"),
            MemoryFact(MemoryKind.INSIGHT, "ins_1", "Dormancy", "winback offer is advisable"),
        ],
    )
    session.commit()

    context = retrieval.search(
        session, merchant.id, "winback offer", hops=0, kinds=[MemoryKind.INSIGHT]
    )
    assert [hit.ref for hit in context.hits] == ["insight:ins_1"]


def test_recency_weighting_breaks_a_lexical_tie(session: Session) -> None:
    merchant = make_merchant(session)
    body = "collection was steady with no surprises worth reporting"
    graph.write_facts(
        session,
        merchant.id,
        [
            MemoryFact(
                MemoryKind.DAY, "old", "Old day", body, occurred_at=now_utc() - timedelta(days=40)
            ),
            MemoryFact(
                MemoryKind.DAY, "new", "New day", body, occurred_at=now_utc() - timedelta(days=1)
            ),
        ],
    )
    session.commit()

    context = retrieval.search(session, merchant.id, "collection steady", hops=0)
    assert [hit.ref for hit in context.hits] == ["day:new", "day:old"]
    assert context.hits[0].score > context.hits[1].score


def test_rendered_block_carries_ist_dates_and_formatted_money(session: Session) -> None:
    merchant = make_merchant(session)
    moment = now_utc() - timedelta(days=2)
    graph.write_facts(
        session,
        merchant.id,
        [
            MemoryFact(
                kind=MemoryKind.ACTION,
                key="act_1",
                label="Winback offer",
                text="Winback offer sent to 12 customers.",
                attrs={"recovered_paise": 234_000},
                occurred_at=moment,
            )
        ],
    )
    session.commit()

    context = retrieval.search(session, merchant.id, "winback offer", hops=0)
    assert to_ist(moment).strftime("%d %b %Y") in context.rendered
    assert "₹2,340" in context.rendered  # surfaced from attrs, already formatted
    assert context.rendered.splitlines()[0].startswith("Memory —")


def test_index_cache_is_invalidated_by_new_facts(session: Session) -> None:
    """A cached index must never hide a fact that was written after it was built."""
    merchant = make_merchant(session)
    graph.write_facts(
        session, merchant.id, [MemoryFact(MemoryKind.NOTE, "n1", "Note", "dal prices rising")]
    )
    session.commit()

    first = retrieval.search(session, merchant.id, "dal prices", hops=0)
    assert [hit.ref for hit in first.hits] == ["note:n1"]
    fingerprint = retrieval.corpus_fingerprint(session, merchant.id)

    graph.write_facts(
        session, merchant.id, [MemoryFact(MemoryKind.NOTE, "n2", "Note", "dal stock arrived")]
    )
    session.commit()

    assert retrieval.corpus_fingerprint(session, merchant.id) != fingerprint
    second = retrieval.search(session, merchant.id, "dal", hops=0)
    assert {hit.ref for hit in second.hits} == {"note:n1", "note:n2"}

    # An edit to an existing node also invalidates, not just an insert.
    graph.write_facts(
        session, merchant.id, [MemoryFact(MemoryKind.NOTE, "n1", "Note", "haldi prices rising")]
    )
    session.commit()
    assert [hit.ref for hit in retrieval.search(session, merchant.id, "haldi", hops=0).hits] == [
        "note:n1"
    ]


def test_search_on_an_empty_graph_is_empty_not_an_error(session: Session) -> None:
    merchant = make_merchant(session)
    context = retrieval.search(session, merchant.id, "kuch bhi")
    assert context.is_empty
    assert context.rendered == ""
    assert context.provider == "local"


# ─────────────────────────────────────────────────────────────────────────────
# Ingest
# ─────────────────────────────────────────────────────────────────────────────


def test_daily_rollup_is_bilingual_and_dated_in_ist(
    session: Session, shop: dict[str, object]
) -> None:
    merchant = shop["merchant"]
    facts = ingest.daily_rollup_facts(session, merchant.id, days=3)
    today = next(fact for fact in facts if fact.key == today_ist().isoformat())

    assert weekday_name(today_ist(), hindi=True) in today.text
    assert "collection ₹1,710" in today.text  # 6 × ₹285, straight from the transactions
    assert "कुल वसूली" in today.text
    assert today.attrs["txn_count"] == 6
    assert today.attrs["top_category"] == "staples"


def test_action_facts_spell_out_recipients_and_outcomes(
    session: Session, shop: dict[str, object]
) -> None:
    merchant, action = shop["merchant"], shop["action"]
    facts = ingest.action_facts(session, merchant.id)
    fact = next(f for f in facts if f.key == action.id)

    assert "Sent to 12 customers" in fact.text
    assert "4 redeemed" in fact.text
    assert "₹2,340 recovered" in fact.text
    assert "वसूल हुए" in fact.text

    relations = [(spec.rel, spec.target) for spec in fact.edges]
    targeted = [target for rel, target in relations if rel == "targeted"]
    assert len(targeted) == 12
    assert ("addressed", node_ref(MemoryKind.INSIGHT, shop["insight"].id)) in relations
    assert any(rel == "on_day" for rel, _ in relations)


def test_customer_and_conversation_facts_link_back(
    session: Session, shop: dict[str, object]
) -> None:
    merchant = shop["merchant"]
    customers = ingest.customer_facts(session, merchant.id)
    assert customers
    assert all(
        ("shops_at", node_ref(MemoryKind.MERCHANT, merchant.id))
        in [(spec.rel, spec.target) for spec in fact.edges]
        for fact in customers
    )

    conversations = ingest.conversation_facts(session, merchant.id)
    created = [spec.target for spec in conversations[0].edges if spec.rel == "created"]
    assert created == [node_ref(MemoryKind.ACTION, shop["action"].id)]
    # No stored summary, so one is derived from the real turns rather than invented.
    assert "aaj ka collection kitna hua" in conversations[0].text


def test_ingest_all_produces_a_connected_graph(session: Session, shop: dict[str, object]) -> None:
    merchant = shop["merchant"]
    facts = ingest.ingest_all(session, merchant.id)
    refs = {fact.ref for fact in facts}

    assert node_ref(MemoryKind.MERCHANT, merchant.id) in refs
    assert node_ref(MemoryKind.ACTION, shop["action"].id) in refs
    assert node_ref(MemoryKind.INSIGHT, shop["insight"].id) in refs

    # Every declared edge target exists in the same batch — nothing is left dangling.
    targets = {spec.target for fact in facts for spec in fact.edges}
    assert targets <= refs

    graph.write_facts(session, merchant.id, facts)
    session.commit()
    assert graph.node_count(session, merchant.id) == len(facts)


# ─────────────────────────────────────────────────────────────────────────────
# LocalGraphMemory
# ─────────────────────────────────────────────────────────────────────────────


def test_local_graph_memory_satisfies_the_protocol() -> None:
    assert isinstance(LocalGraphMemory(), MemoryProvider)


async def test_cross_session_recall_of_action_outcome(
    session: Session, session_factory: sessionmaker[Session], shop: dict[str, object]
) -> None:
    """The demo property: conversation #2 recalls what conversation #1 did, with its numbers."""
    merchant = shop["merchant"]
    memory = LocalGraphMemory(session_factory)

    # Conversation #1 happened: ingest everything the database now knows.
    written = await memory.ingest(merchant.id, ingest.ingest_all(session, merchant.id))
    assert written > 0

    # Conversation #2, a fresh session with no shared state beyond the database.
    context = await memory.search(merchant.id, "pichla offer kya hua", limit=6, hops=1)

    action_ref = node_ref(MemoryKind.ACTION, shop["action"].id)
    assert action_ref in {hit.ref for hit in context.hits}
    assert context.hits[0].ref == action_ref

    assert "4 redeemed" in context.rendered
    assert "₹2,340 recovered" in context.rendered
    assert "Sent to 12 customers" in context.rendered

    # And the customers it targeted come along over the graph.
    customer_hits = [hit for hit in context.hits if hit.kind is MemoryKind.CUSTOMER]
    assert customer_hits
    assert customer_hits[0].path[0].startswith(f"{action_ref} -targeted->")


async def test_ingest_is_idempotent_across_repeated_runs(
    session: Session, session_factory: sessionmaker[Session], shop: dict[str, object]
) -> None:
    merchant = shop["merchant"]
    memory = LocalGraphMemory(session_factory)
    facts = ingest.ingest_all(session, merchant.id)

    await memory.ingest(merchant.id, facts)
    first = graph.node_count(session, merchant.id)
    await memory.ingest(merchant.id, ingest.ingest_all(session, merchant.id))

    assert graph.node_count(session, merchant.id) == first


async def test_forget_removes_only_that_merchants_rows(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    keeper = make_merchant(session, shop_name="Verma Provisions")
    loser = make_merchant(session, shop_name="Sharma General Store")
    session.commit()

    memory = LocalGraphMemory(session_factory)
    for merchant_id in (keeper.id, loser.id):
        await memory.ingest(
            merchant_id,
            [
                MemoryFact(
                    kind=MemoryKind.ACTION,
                    key="act_1",
                    label="Winback",
                    text="winback offer",
                    edges=[MemoryEdgeSpec("targeted", node_ref(MemoryKind.CUSTOMER, "cus_1"), 0.9)],
                ),
                MemoryFact(MemoryKind.CUSTOMER, "cus_1", "Grahak", "buyer"),
            ],
        )

    removed = await memory.forget(loser.id)
    assert removed == 3  # two nodes and one edge

    assert graph.node_count(session, loser.id) == 0
    assert graph.node_count(session, keeper.id) == 2
    assert (
        session.scalar(select(func.count(MemoryEdge.id)).where(MemoryEdge.merchant_id == keeper.id))
        == 1
    )


async def test_local_health_is_ok_against_a_live_database(
    session_factory: sessionmaker[Session],
) -> None:
    health = await LocalGraphMemory(session_factory).health()
    assert health.ok is True
    assert (health.kind, health.mode) == ("memory", "local")


async def test_search_stays_fast_on_two_thousand_nodes(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """Definition of done #3. The bound is generous for CI; the real number is printed."""
    merchant = make_merchant(session)
    nodes = []
    for index in range(2000):
        day = today_ist() - timedelta(days=index % 400)
        nodes.append(
            MemoryNode(
                merchant_id=merchant.id,
                kind=MemoryKind.DAY if index % 3 else MemoryKind.CUSTOMER,
                key=f"node-{index}",
                label=f"Node {index}",
                text=(
                    f"{day.isoformat()} ({weekday_name(day, hindi=True)}): collection "
                    f"₹{18_000 + index:,} across {30 + index % 40} sales. "
                    f"कुल वसूली ₹{18_000 + index:,}, ग्राहक {30 + index % 40}। "
                    "Top category staples, payment mix UPI 62%, cash 30%."
                ),
                attrs={"collection_paise": 1_800_000 + index, "index": index},
                occurred_at=now_utc() - timedelta(days=index % 400),
            )
        )
    session.add_all(nodes)
    session.commit()

    memory = LocalGraphMemory(session_factory)
    queries = ("winback offer kya hua", "कुल वसूली कितनी", "18540", "collection staples")

    started = time.perf_counter()
    await memory.search(merchant.id, queries[0], limit=6, hops=1)
    cold_ms = (time.perf_counter() - started) * 1000

    warm: list[float] = []
    for query in queries:
        started = time.perf_counter()
        await memory.search(merchant.id, query, limit=6, hops=1)
        warm.append((time.perf_counter() - started) * 1000)

    print(
        f"\nsearch over {len(nodes)} nodes: cold (index build) {cold_ms:.1f} ms, "
        f"warm {[round(value, 1) for value in warm]} ms"
    )

    # The property that actually guarantees the speed: the index is rebuilt only when the data
    # changes, so a warm query does no tokenisation work at all. Asserting *that* is deterministic
    # on any machine; a wall-clock bound is not, and a perf test that fails because a browser was
    # open would be exactly the flake you cannot afford on demo day.
    warm_median = statistics.median(warm)
    assert warm_median < cold_ms, "a warm query must reuse the index, not rebuild it"

    # A wall-clock ceiling is still worth keeping as a smoke check, but generous enough to survive
    # a loaded laptop. The real numbers are printed above; regressions show up there first.
    assert warm_median < 250.0
    assert cold_ms < 3000.0


# ─────────────────────────────────────────────────────────────────────────────
# Cognee (mocked transport — no network)
# ─────────────────────────────────────────────────────────────────────────────


def mock_client(handler, *, api_key: str = "test-key") -> CogneeClient:
    return CogneeClient(
        api_key=api_key,
        base_url="https://cognee.test",
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


def test_parse_search_results_accepts_several_response_shapes() -> None:
    assert parse_search_results(["plain answer"])[0].text == "plain answer"

    enveloped = parse_search_results({"results": [{"content": "hi", "similarity": 0.42}]})
    assert (enveloped[0].text, enveloped[0].score) == ("hi", 0.42)

    referenced = parse_search_results(
        [{"text": "[[munshiji ref=action:act_1 title=Winback]] 4 redeemed", "score": 0.9}]
    )
    assert referenced[0].ref == "action:act_1"

    assert parse_search_results(None) == []
    assert parse_search_results({"results": []}) == []
    assert parse_search_results({"unexpected": {"deeply": "nested"}}) == []


async def test_cognee_pipeline_adds_cognifies_and_searches(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    merchant = make_merchant(session)
    session.commit()
    seen: list[tuple[str, str]] = []
    bodies: dict[str, list[bytes]] = defaultdict(list)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        bodies[request.url.path].append(request.content)
        if request.url.path == DATASETS_PATH:
            return httpx.Response(200, json={"datasets": []})
        if request.url.path == ADD_PATH:
            return httpx.Response(200, json={"dataset_id": "ds-1"})
        if request.url.path == COGNIFY_PATH:
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path == SEARCH_PATH:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "text": "[[munshiji ref=action:act_1 title=Winback]] 4 redeemed",
                            "score": 0.91,
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "no route"})

    memory = CogneeMemory(mock_client(handler), fallback=LocalGraphMemory(session_factory))
    facts = [
        MemoryFact(
            kind=MemoryKind.ACTION,
            key="act_1",
            label="Winback offer",
            text="Winback offer sent to 12 customers; 4 redeemed; ₹2,340 recovered.",
            occurred_at=now_utc(),
            edges=[MemoryEdgeSpec("targeted", node_ref(MemoryKind.CUSTOMER, "cus_1"), 0.9)],
        ),
        MemoryFact(MemoryKind.CUSTOMER, "cus_1", "Grahak", "buyer"),
    ]

    assert await memory.ingest(merchant.id, facts) == 2
    # First contact probes whether the dataset already exists (the seed may have built it),
    # then uploads everything because it does not, then cognifies synchronously.
    assert [path for _, path in seen] == [DATASETS_PATH, ADD_PATH, COGNIFY_PATH]


    context = await memory.search(merchant.id, "pichla offer kya hua", limit=3)
    assert context.provider == "live"
    assert context.hits[0].ref == "action:act_1"
    assert context.hits[0].kind is MemoryKind.ACTION
    # Enriched from the local mirror the ingest wrote.
    assert "₹2,340 recovered" in context.hits[0].text
    assert "₹2,340 recovered" in context.rendered

    # Re-ingesting the same facts must cost nothing: no add, no cognify, no credits.
    seen.clear()
    assert await memory.ingest(merchant.id, facts) == 0
    assert seen == []

    # A changed fact uploads exactly that one document, and cognify runs in the background so
    # the conversation turn is not held hostage to graph construction.
    facts[0].text = "Winback offer sent to 12 customers; 5 redeemed; ₹2,940 recovered."
    seen.clear()
    assert await memory.ingest(merchant.id, facts) == 1
    assert [path for _, path in seen] == [ADD_PATH, COGNIFY_PATH]
    cognify_body = json.loads(bodies[COGNIFY_PATH][-1])
    assert cognify_body.get("runInBackground") is True


async def test_cognee_add_retries_as_json_when_multipart_is_rejected(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    merchant = make_merchant(session)
    session.commit()
    content_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == ADD_PATH:
            content_types.append(request.headers.get("content-type", ""))
            if len(content_types) == 1:
                return httpx.Response(415, json={"detail": "unsupported media type"})
            return httpx.Response(200, json={"id": "ds-1"})
        return httpx.Response(200, json={"status": "ok"})

    memory = CogneeMemory(mock_client(handler), fallback=LocalGraphMemory(session_factory))
    await memory.ingest(merchant.id, [MemoryFact(MemoryKind.NOTE, "n1", "Note", "remember this")])

    assert content_types[0].startswith("multipart/form-data")
    assert content_types[1].startswith("application/json")


async def test_cognee_raises_provider_unavailable_on_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal explosion")

    client = mock_client(handler)
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await client.search("anything", dataset_name="munshiji_x")
    assert "500" in excinfo.value.message
    assert excinfo.value.context["provider"] == "cognee"


async def test_cognee_raises_provider_unavailable_on_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    with pytest.raises(ProviderUnavailableError):
        await mock_client(handler).cognify(dataset_name="munshiji_x")


async def test_cognee_sends_bearer_auth_and_per_merchant_dataset() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json=[])

    await mock_client(handler).search("q", dataset_name=dataset_name_for("mer_1"))
    assert captured["auth"] == "Bearer test-key"
    assert "munshiji_mer_1" in str(captured["body"])
    assert "GRAPH_COMPLETION" in str(captured["body"])


async def test_cognee_health_never_raises() -> None:
    def exploding(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    health = await CogneeMemory(mock_client(exploding)).health()
    assert health.ok is False
    assert (health.kind, health.mode) == ("memory", "live")
    assert health.detail

    missing_key = await CogneeMemory(mock_client(exploding, api_key="")).health()
    assert missing_key.ok is False
    assert "COGNEE_API_KEY" in missing_key.detail

    def healthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    assert (await CogneeMemory(mock_client(healthy)).health()).ok is True


async def test_cognee_graph_falls_back_to_the_local_snapshot(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    merchant = make_merchant(session)
    graph.write_facts(
        session, merchant.id, [MemoryFact(MemoryKind.NOTE, "n1", "Note", "remember this")]
    )
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == DATASETS_PATH:
            return httpx.Response(503, text="graph service unavailable")
        return httpx.Response(200, json={})

    memory = CogneeMemory(mock_client(handler), fallback=LocalGraphMemory(session_factory))
    snapshot = await memory.graph(merchant.id)

    assert snapshot.provider == "local"  # the badge must not claim a live graph
    assert [node["ref"] for node in snapshot.nodes] == ["note:n1"]


async def test_cognee_graph_uses_live_payload_when_available(
    session_factory: sessionmaker[Session],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == DATASETS_PATH:
            return httpx.Response(200, json=[{"id": "ds-1", "name": "munshiji_mer_1"}])
        if request.url.path.endswith("/graph"):
            return httpx.Response(
                200,
                json={
                    "nodes": [
                        {"id": "1", "name": "Winback", "type": "action"},
                        {"id": "2", "name": "Grahak", "type": "customer"},
                    ],
                    "edges": [{"source": "1", "target": "2", "type": "targeted"}],
                },
            )
        return httpx.Response(404)

    memory = CogneeMemory(mock_client(handler), fallback=LocalGraphMemory(session_factory))
    snapshot = await memory.graph("mer_1")

    assert snapshot.provider == "live"
    assert [node["ref"] for node in snapshot.nodes] == ["action:1", "customer:2"]
    assert snapshot.edges[0]["rel"] == "targeted"

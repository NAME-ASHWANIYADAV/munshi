"""Registry: fault isolation, dedupe, ranking and persistence.

These are the properties the demo leans on hardest. A single engine throwing must cost one card,
not the feed; and refreshing twice must leave one open insight per finding, not two.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import IST, to_utc
from munshiji.db.base import Base
from munshiji.db.enums import InsightKind, PaymentMethod, Severity
from munshiji.db.models import Insight, Merchant, Transaction
from munshiji.insights import registry
from munshiji.insights.base import InsightContext, InsightDraft, InsightEngine
from munshiji.insights.registry import (
    ALL_ENGINES,
    OPEN,
    SUPERSEDED,
    build_context,
    refresh,
    run_all,
)

TODAY = date(2026, 9, 14)
RUPEE = 100


def make_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def at(day: date, hour: int = 11) -> datetime:
    return to_utc(datetime.combine(day, time(hour, 0), tzinfo=IST))


def ist_at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour, 0), tzinfo=IST)


def make_merchant(session: Session) -> Merchant:
    merchant = Merchant(owner_name="Sharma ji", shop_name="Sharma General Store")
    session.add(merchant)
    session.flush()
    session.commit()
    return merchant


def ctx_for(session: Session, merchant: Merchant) -> InsightContext:
    return InsightContext(session=session, merchant_id=merchant.id, as_of=ist_at(TODAY))


@contextmanager
def captured_logs(caplog: pytest.LogCaptureFixture, level: str = "ERROR") -> Iterator[None]:
    """Let ``caplog`` see MunshiJi's logs.

    ``munshiji.logging`` deliberately sets ``propagate = False`` on its namespace so the app does
    not double-log through the root handler. caplog attaches to the root, so without re-enabling
    propagation for the duration every assertion about log output would pass vacuously.
    """
    namespace = logging.getLogger("munshiji")
    previous = namespace.propagate
    namespace.propagate = True
    try:
        with caplog.at_level(level, logger="munshiji"):
            yield
    finally:
        namespace.propagate = previous


# ── test doubles ────────────────────────────────────────────────────────────


class BoomEngine:
    """An engine that fails the way a real one would: mid-run, on real-looking data."""

    kind = InsightKind.MARGIN_LEAK

    def __init__(self) -> None:
        self.calls = 0

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        self.calls += 1
        raise ZeroDivisionError("margin denominator was zero")


class FixedEngine:
    """Emits one draft with a caller-chosen dedupe key and impact."""

    kind = InsightKind.DEAD_STOCK

    def __init__(self, dedupe_key: str, impact_paise: int, *, severity=Severity.MEDIUM) -> None:
        self.dedupe_key = dedupe_key
        self.impact_paise = impact_paise
        self.severity = severity

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        return [
            InsightDraft(
                kind=self.kind,
                severity=self.severity,
                title_en=f"Finding {self.dedupe_key}",
                title_hi=f"जानकारी {self.dedupe_key}",
                body_en="body",
                body_hi="विवरण",
                metrics={"impact_paise": self.impact_paise},
                suggested_tool="save_merchant_note",
                suggested_params={"text_en": "note", "text_hi": "नोट"},
                impact_paise=self.impact_paise,
                confidence=0.8,
                dedupe_key=self.dedupe_key,
            )
        ]


class EmptyEngine:
    kind = InsightKind.PEAK_HOUR

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        return []


class NoneEngine:
    """Returns ``None`` instead of a list — the registry must cope."""

    kind = InsightKind.PAYMENT_MIX

    def run(self, ctx: InsightContext):
        return None


# ── engine contract ─────────────────────────────────────────────────────────


def test_every_registered_engine_satisfies_the_protocol() -> None:
    assert ALL_ENGINES
    for engine in ALL_ENGINES:
        assert isinstance(engine, InsightEngine), type(engine).__name__
        assert isinstance(engine.kind, InsightKind)


def test_registered_engines_cover_every_insight_kind() -> None:
    covered = {engine.kind for engine in ALL_ENGINES}
    assert covered == set(InsightKind), set(InsightKind) - covered


# ── fault isolation ─────────────────────────────────────────────────────────


def test_a_failing_engine_does_not_break_the_run() -> None:
    session = make_session()
    merchant = make_merchant(session)
    boom = BoomEngine()
    engines = [
        FixedEngine("alpha", 100_000),
        boom,
        FixedEngine("beta", 50_000),
    ]

    drafts = run_all(ctx_for(session, merchant), engines=engines)

    assert boom.calls == 1
    assert [draft.dedupe_key for draft in drafts] == ["alpha", "beta"]


def test_a_failing_engine_is_logged_not_swallowed_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = make_session()
    merchant = make_merchant(session)
    with captured_logs(caplog):
        run_all(ctx_for(session, merchant), engines=[BoomEngine()])
    assert any("BoomEngine" in record.getMessage() for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)


def test_engines_with_nothing_to_say_are_tolerated() -> None:
    """An engine may return ``[]``, and (defensively) even ``None``."""
    session = make_session()
    merchant = make_merchant(session)
    drafts = run_all(
        ctx_for(session, merchant),
        engines=[NoneEngine(), EmptyEngine(), FixedEngine("alpha", 100_000)],
    )
    assert [draft.dedupe_key for draft in drafts] == ["alpha"]


# ── dedupe and ranking ──────────────────────────────────────────────────────


def test_dedupe_keeps_the_higher_scoring_draft() -> None:
    session = make_session()
    merchant = make_merchant(session)
    low = FixedEngine("same_key", 10_000)
    high = FixedEngine("same_key", 900_000)

    drafts = run_all(ctx_for(session, merchant), engines=[low, high])
    assert len(drafts) == 1
    assert drafts[0].impact_paise == 900_000

    # Order of registration must not change the winner.
    reversed_drafts = run_all(ctx_for(session, merchant), engines=[high, low])
    assert reversed_drafts[0].impact_paise == 900_000


def test_ranking_is_by_score_descending() -> None:
    session = make_session()
    merchant = make_merchant(session)
    engines = [
        FixedEngine("small", 5_000),
        FixedEngine("huge", 5_000_000, severity=Severity.CRITICAL),
        FixedEngine("medium", 200_000),
    ]
    drafts = run_all(ctx_for(session, merchant), engines=engines)

    scores = [draft.score for draft in drafts]
    assert scores == sorted(scores, reverse=True)
    assert [draft.dedupe_key for draft in drafts] == ["huge", "medium", "small"]


def test_ties_rank_deterministically() -> None:
    session = make_session()
    merchant = make_merchant(session)
    engines = [FixedEngine("zeta", 100_000), FixedEngine("alpha", 100_000)]
    first = run_all(ctx_for(session, merchant), engines=engines)
    second = run_all(ctx_for(session, merchant), engines=list(reversed(engines)))
    assert [draft.dedupe_key for draft in first] == [draft.dedupe_key for draft in second]


# ── persistence ─────────────────────────────────────────────────────────────


def open_insights(session: Session, merchant: Merchant) -> list[Insight]:
    return list(
        session.scalars(
            select(Insight).where(Insight.merchant_id == merchant.id, Insight.status == OPEN)
        ).all()
    )


def test_refresh_persists_ranked_rows() -> None:
    session = make_session()
    merchant = make_merchant(session)
    engines = [FixedEngine("alpha", 900_000), FixedEngine("beta", 20_000)]

    rows = refresh(session, merchant.id, as_of=ist_at(TODAY), engines=engines)

    assert [row.dedupe_key for row in rows] == ["alpha", "beta"]
    assert rows[0].score >= rows[1].score
    assert all(row.status == OPEN for row in rows)
    assert all(row.merchant_id == merchant.id for row in rows)
    assert rows[0].metrics["impact_paise"] == 900_000
    assert rows[0].expires_at is not None


def test_refresh_supersedes_rather_than_duplicating() -> None:
    session = make_session()
    merchant = make_merchant(session)
    engines = [FixedEngine("alpha", 900_000), FixedEngine("beta", 20_000)]

    first = refresh(session, merchant.id, as_of=ist_at(TODAY), engines=engines)
    second = refresh(session, merchant.id, as_of=ist_at(TODAY), engines=engines)

    still_open = open_insights(session, merchant)
    assert len(still_open) == 2
    assert {row.dedupe_key for row in still_open} == {"alpha", "beta"}
    assert {row.id for row in still_open} == {row.id for row in second}

    superseded = list(
        session.scalars(
            select(Insight).where(Insight.merchant_id == merchant.id, Insight.status == SUPERSEDED)
        ).all()
    )
    assert {row.id for row in superseded} == {row.id for row in first}
    assert session.scalar(select(Insight).where(Insight.id == first[0].id)).status == SUPERSEDED


def test_refresh_only_supersedes_matching_dedupe_keys() -> None:
    session = make_session()
    merchant = make_merchant(session)
    refresh(session, merchant.id, as_of=ist_at(TODAY), engines=[FixedEngine("alpha", 100_000)])
    refresh(session, merchant.id, as_of=ist_at(TODAY), engines=[FixedEngine("beta", 100_000)])

    still_open = {row.dedupe_key for row in open_insights(session, merchant)}
    assert still_open == {"alpha", "beta"}


def test_refresh_on_an_empty_database_commits_nothing_and_raises_nothing() -> None:
    session = make_session()
    merchant = make_merchant(session)
    assert refresh(session, merchant.id, as_of=ist_at(TODAY)) == []
    assert open_insights(session, merchant) == []


def test_refresh_survives_a_broken_engine_and_still_persists_the_rest() -> None:
    session = make_session()
    merchant = make_merchant(session)
    rows = refresh(
        session,
        merchant.id,
        as_of=ist_at(TODAY),
        engines=[BoomEngine(), FixedEngine("alpha", 100_000)],
    )
    assert [row.dedupe_key for row in rows] == ["alpha"]
    assert len(open_insights(session, merchant)) == 1


# ── the real engine set ─────────────────────────────────────────────────────


def test_the_full_engine_set_runs_clean_on_an_empty_database() -> None:
    session = make_session()
    merchant = make_merchant(session)
    assert run_all(ctx_for(session, merchant)) == []


def test_the_full_engine_set_runs_clean_on_a_thin_database(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handful of rows must not make any engine throw — the commonest demo-day failure."""
    session = make_session()
    merchant = make_merchant(session)
    for offset in range(1, 20):
        session.add(
            Transaction(
                merchant_id=merchant.id,
                amount_paise=250 * RUPEE,
                occurred_at=at(TODAY - timedelta(days=offset), 10),
                payment_method=PaymentMethod.UPI,
            )
        )
    session.commit()

    with captured_logs(caplog):
        drafts = run_all(ctx_for(session, merchant))
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    assert all(draft.dedupe_key for draft in drafts)
    assert all(draft.title_hi and draft.body_hi for draft in drafts)


def test_build_context_defaults_to_now() -> None:
    session = make_session()
    merchant = make_merchant(session)
    ctx = build_context(session, merchant.id)
    assert ctx.as_of.tzinfo is not None
    assert ctx.merchant_id == merchant.id


# ── optional integration with the seed generator ────────────────────────────

_HAS_GENERATOR = importlib.util.find_spec("munshiji.seed.generator") is not None


@pytest.mark.skipif(not _HAS_GENERATOR, reason="seed generator not available yet")
def test_planted_signals_are_found_in_the_seeded_database() -> None:
    """End-to-end: the signals SPEC.md §6 plants must be *discovered*, not hardcoded."""
    from munshiji.seed import generator as seed_generator

    session = make_session()
    result = seed_generator.generate(session)
    merchant_id = (
        getattr(result, "merchant_id", None) or session.scalars(select(Merchant.id)).first()
    )
    session.commit()

    rows = refresh(session, merchant_id)
    kinds = {row.kind for row in rows}

    assert rows, "the seeded shop produced no insights at all"
    for expected in (
        InsightKind.DORMANT_CUSTOMERS,
        InsightKind.DEAD_STOCK,
        InsightKind.UDHAAR_OVERDUE,
        InsightKind.STOCKOUT_RISK,
    ):
        assert expected in kinds, f"{expected} not discovered in the seeded data"
    assert all(row.score > 0 for row in rows)
    assert all(row.title_hi and row.body_hi for row in rows)


def test_module_exports_the_documented_names() -> None:
    for name in ("ALL_ENGINES", "run_all", "refresh", "build_context"):
        assert hasattr(registry, name), name

"""Command line interface.

    munshiji seed --reset      build the demo database
    munshiji health            which providers are serving
    munshiji insights          the ranked feed
    munshiji ask "..."         one conversational turn
    munshiji demo              replay the full two-conversation demo, offline
    munshiji serve             run the API

``demo`` is the safety net for demo day: it needs no browser, no network and no API keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from munshiji import __version__
from munshiji.clock import now_ist
from munshiji.config import get_settings
from munshiji.db.base import init_db, reset_db, session_scope
from munshiji.logging import configure_logging
from munshiji.money import fmt_inr
from munshiji.repositories.core import first_merchant

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="MunshiJi — the AI munshi for Indian merchants.",
)


def _utf8_console() -> Console:
    """A console that can print Devanagari and ₹ whatever codepage the terminal starts in.

    Demo day means an unfamiliar machine and whichever shell is already open. On Windows that is
    often still a legacy codepage, where the first tick mark or rupee sign raises
    ``UnicodeEncodeError`` and takes the whole command down. Re-encoding the streams up front
    costs nothing and removes a way for the demo to die in front of judges.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A stream that refuses re-encoding (a pipe, a captured buffer) is not worth failing
            # over — the command should still run.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    return Console()


console = _utf8_console()


def _merchant(session):
    from munshiji.repositories.core import first_merchant

    merchant = first_merchant(session)
    if merchant is None:
        console.print("[red]No merchant found.[/red] Run: [bold]munshiji seed --reset[/bold]")
        raise typer.Exit(code=1)
    return merchant


# ── seed ────────────────────────────────────────────────────────────────────


@app.command()
def seed(
    reset: Annotated[bool, typer.Option(help="Drop and recreate every table first.")] = False,
    days: Annotated[int, typer.Option(help="Days of history to generate.")] = 180,
    seed_value: Annotated[int, typer.Option("--seed", help="RNG seed (determinism).")] = 0,
    warm: Annotated[
        bool, typer.Option(help="Also compute the findings and build the memory graph.")
    ] = True,
) -> None:
    """Generate a realistic shop history for the demo merchant."""
    configure_logging()
    settings = get_settings()
    from munshiji.seed.generator import generate

    if reset:
        reset_db()
    else:
        init_db()

    with console.status("Generating shop history…"), session_scope() as session:
        result = generate(session, seed=seed_value or settings.seed, days=days)

    if warm:
        _warm_up()

    console.print(Panel.fit(str(result), title="Seeded", border_style="green"))
    console.print("Next: [bold]munshiji insights[/bold] or [bold]munshiji demo[/bold]")


def _warm_up() -> None:
    """Compute the findings and build the memory graph, so the shop is ready to be looked at.

    Generating transactions is only half of seeding. The insight engines run on demand and the
    memory graph is built by ingestion, so until something asks, ``GET /api/insights`` answers
    with an empty list and the knowledge graph has no nodes — a *successful* response carrying
    nothing, which a caller cannot tell from a shop with nothing to say.

    That is fine locally, where the first question warms everything. It is not fine on a fresh
    container: the screen loads, three panels are empty, and the product looks broken on the one
    view a stranger forms of it.
    """
    with console.status("Working out what needs attention…"), session_scope() as session:
        merchant = first_merchant(session)
        if merchant is None:
            return
        merchant_id = merchant.id
        try:
            from munshiji.insights.registry import refresh

            refresh(session, merchant_id, as_of=now_ist())
        except Exception as exc:  # pragma: no cover - a broken engine must not fail the seed
            console.print(f"[yellow]insight refresh skipped:[/yellow] {exc}")
        session.commit()

    async def ingest() -> None:
        from munshiji.memory.ingest import ingest_all
        from munshiji.providers.factory import build_providers, resolve

        bundle = await resolve(build_providers())
        with session_scope() as session:
            await bundle.memory.ingest(merchant_id, ingest_all(session, merchant_id))

    with console.status("Building the memory graph…"):
        try:
            asyncio.run(ingest())
        except Exception as exc:  # pragma: no cover - memory is not worth failing a seed over
            console.print(f"[yellow]memory ingest skipped:[/yellow] {exc}")


# ── health ──────────────────────────────────────────────────────────────────


@app.command()
def health() -> None:
    """Show which implementation is serving each capability."""
    configure_logging()
    from munshiji.providers.factory import build_providers, resolve

    async def run() -> None:
        bundle = await resolve(build_providers())
        report = await bundle.health()
        table = Table(title=f"MunshiJi {__version__} — providers")
        table.add_column("capability")
        table.add_column("implementation")
        table.add_column("mode")
        table.add_column("ok")
        table.add_column("detail", overflow="fold")
        for entry in report:
            colour = "green" if entry.mode == "live" else "yellow"
            table.add_row(
                entry.kind,
                entry.name,
                f"[{colour}]{entry.mode}[/{colour}]",
                "✓" if entry.ok else "[red]✗[/red]",
                entry.detail[:70],
            )
        console.print(table)
        sponsors = ", ".join(f"{k}={v}" for k, v in bundle.sponsor_status.items())
        console.print(f"sponsors: {sponsors}")

    asyncio.run(run())


# ── insights ────────────────────────────────────────────────────────────────


@app.command()
def insights(
    refresh: Annotated[bool, typer.Option(help="Recompute before printing.")] = True,
    limit: Annotated[int, typer.Option()] = 8,
) -> None:
    """Print the ranked insight feed."""
    configure_logging()
    with session_scope() as session:
        merchant = _merchant(session)
        if refresh:
            from munshiji.insights.registry import refresh as refresh_insights

            refresh_insights(session, merchant.id, as_of=now_ist())

        from munshiji.repositories.core import list_insights

        rows = list_insights(session, merchant.id, limit=limit)
        table = Table(title=f"{merchant.shop_name} — what needs attention")
        table.add_column("score", justify="right")
        table.add_column("severity")
        table.add_column("finding", overflow="fold")
        table.add_column("impact", justify="right")
        for insight in rows:
            table.add_row(
                f"{insight.score:.0f}",
                insight.severity.value,
                f"{insight.title_hi}\n[dim]{insight.title_en}[/dim]",
                fmt_inr(insight.impact_paise),
            )
        console.print(table)


# ── ask ─────────────────────────────────────────────────────────────────────


@app.command()
def ask(
    text: Annotated[str, typer.Argument(help="What to say to MunshiJi.")],
    language: Annotated[str, typer.Option(help="hi-IN or en-IN.")] = "",
) -> None:
    """Run a single conversational turn and print the reply."""
    configure_logging()
    from munshiji.agent.loop import AgentLoop
    from munshiji.providers.factory import build_providers, resolve

    async def run() -> None:
        bundle = await resolve(build_providers())
        with session_scope() as session:
            merchant = _merchant(session)
            result = await AgentLoop(bundle, publish_events=False).run_turn(
                session, merchant, text, language=language or None
            )
            _print_turn(text, result)

    asyncio.run(run())


def _print_turn(said: str, result) -> None:
    console.print(f"\n[bold cyan]Merchant:[/bold cyan] {said}")
    for call in result.tool_calls:
        mark = "✓" if call.ok else "✗"
        console.print(f"   [dim]{mark} {call.name} — {call.summary} ({call.latency_ms}ms)[/dim]")
    console.print(f"[bold green]MunshiJi:[/bold green] {result.reply}")
    if result.pending_action is not None:
        console.print(
            f"   [yellow]⏸ awaiting approval:[/yellow] {result.pending_action.summary_hi} "
            f"[dim](id={result.pending_action.id})[/dim]"
        )
    if result.executed_action is not None:
        delivered = result.executed_action.result.get("delivered_count", 0)
        console.print(f"   [green]✓ executed:[/green] delivered={delivered}")
    if result.memory_used:
        console.print(f"   [dim]memory: {', '.join(result.memory_used[:4])}[/dim]")


# ── demo ────────────────────────────────────────────────────────────────────


@app.command()
def demo(
    language: Annotated[str, typer.Option(help="hi-IN or en-IN.")] = "hi-IN",
) -> None:
    """Replay the full two-conversation demo. Works offline, with no API keys."""
    configure_logging("WARNING")
    from munshiji.agent.loop import AgentLoop
    from munshiji.insights.registry import refresh as refresh_insights
    from munshiji.memory.ingest import ingest_all
    from munshiji.providers.factory import build_providers, resolve

    call_one = [
        "Munshiji, aaj dhandha kaisa raha?",
        "Kaun kaun purane customer nahi aa rahe?",
        "Unhe 10% ka offer bhej do",
        "Haan, bhej do",
    ]
    call_two = ["Munshiji, pichli baar jo offer bheja tha uska kya hua?"]

    async def run() -> None:
        bundle = await resolve(build_providers())
        modes = ", ".join(f"{k}={v}" for k, v in bundle.modes.items())
        console.print(Panel.fit(f"providers: {modes}", title="MunshiJi demo", border_style="cyan"))

        with session_scope() as session:
            merchant = _merchant(session)
            refresh_insights(session, merchant.id, as_of=now_ist())
            await bundle.memory.ingest(merchant.id, ingest_all(session, merchant.id))

            loop = AgentLoop(bundle, publish_events=False)

            console.rule("[bold]Call 1 — morning[/bold]")
            conversation_id = None
            executed_id = None
            for utterance in call_one:
                result = await loop.run_turn(
                    session,
                    merchant,
                    utterance,
                    conversation_id=conversation_id,
                    language=language,
                )
                conversation_id = result.conversation_id
                _print_turn(utterance, result)
                if result.executed_action is not None:
                    executed_id = result.executed_action.id

            # Time passes: customers respond to the offer.
            #
            # Three days, not one. Redemptions arrive over about a week, so at day one barely a
            # quarter of the response has landed and the campaign has not told you anything yet.
            # Reporting it settled is both the more useful answer to "what happened with the
            # offer?" and the more honest one.
            if executed_id and hasattr(bundle.actions, "simulate_outcomes"):
                console.rule("[dim]… teen din baad …[/dim]")
                outcomes = await bundle.actions.simulate_outcomes(executed_id, days_elapsed=3)
                for outcome in outcomes:
                    value = (
                        fmt_inr(outcome.value_paise)
                        if outcome.value_paise is not None
                        else outcome.value_num
                    )
                    console.print(f"   [dim]outcome: {outcome.metric} = {value}[/dim]")

            # The outcomes were written through the action provider's own session; drop this
            # one's cached copies so the re-ingest sees them.
            session.commit()
            session.expire_all()
            await bundle.memory.ingest(merchant.id, ingest_all(session, merchant.id))

            console.rule("[bold]Call 2 — next day (new session, empty context)[/bold]")
            fresh_conversation = None
            for utterance in call_two:
                result = await loop.run_turn(
                    session,
                    merchant,
                    utterance,
                    conversation_id=fresh_conversation,
                    language=language,
                )
                fresh_conversation = result.conversation_id
                _print_turn(utterance, result)

        console.print()
        console.print(
            Panel.fit(
                "Call 2 ran in a new conversation with no shared context. The outcome came from "
                "the knowledge graph.",
                border_style="green",
            )
        )

    asyncio.run(run())


# ── serve ───────────────────────────────────────────────────────────────────


@app.command()
def serve(
    port: Annotated[int, typer.Option()] = 8000,
    reload: Annotated[bool, typer.Option()] = False,
) -> None:
    """Run the API server."""
    import uvicorn

    uvicorn.run("munshiji.main:app", port=port, reload=reload)


if __name__ == "__main__":
    app()

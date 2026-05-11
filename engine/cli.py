"""Typer CLI: `bse <command>` for everything operators need.

Examples:
    bse init                 # create DB tables, install Chromium
    bse scan                 # run a one-shot signal scan and rank
    bse render --ticker BTC  # render a sample card for inspection
    bse hook  --ticker BTC --trigger PUMP     # one-shot hook generation
    bse publish --ticker BTC --hook "$BTC..." # manual publish
    bse run-slot prime_gcc   # fire a single slot now
    bse run                  # start the 24/7 scheduler daemon
    bse report --hours 24    # print performance report
    bse pause --hours 2 --reason "manual"
    bse resume
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table
from sqlalchemy import desc, select

import engine.distribution.browser_publisher as bp_mod  # noqa: F401 - ensure loaded
from engine.analytics.post_tracker import PostTracker
from engine.analytics.reports import build_report, report_to_markdown
from engine.config import get_settings
from engine.content.hook_generator import HookGenerator, HookRequest
from engine.content.post_assembler import PostAssembler
from engine.db import init_db, session_scope
from engine.distribution.publisher import Publisher
from engine.distribution.scheduler import EngineScheduler
from engine.learning.prompt_updater import refresh_examples
from engine.learning.template_evaluator import update_template_stats
from engine.logging_setup import get_logger, setup_logging
from engine.models import Post, PublishLock
from engine.signal.opportunity_ranker import OpportunityRanker, RankedOpportunity
from engine.visuals.pipeline import VisualPipeline

app = typer.Typer(add_completion=False, help="binance-square-engine CLI")
log = get_logger("cli")


def _run(coro):
    return asyncio.run(coro)


@app.callback()
def main() -> None:
    setup_logging()


@app.command()
def init() -> None:
    """Create DB tables and install Playwright browsers."""
    async def _go():
        await init_db()
        rprint("[green]✓[/green] database initialised")
    _run(_go())
    import subprocess
    rprint("[blue]→[/blue] installing Playwright browsers (this may take a minute)…")
    rc = subprocess.call(["playwright", "install", "chromium"])
    if rc == 0:
        rprint("[green]✓[/green] chromium installed")
    else:
        rprint("[yellow]![/yellow] playwright install returned non-zero; install manually")


@app.command()
def scan() -> None:
    """Run one signal-scan pass; print the top 10 ranked opportunities."""
    async def _go():
        await init_db()
        ranker = OpportunityRanker()
        opps = await ranker.rank()
        written = await ranker.persist(opps)
        table = Table(title=f"Top 10 opportunities (persisted: {written})")
        for col in ("ticker", "trigger", "score", "Δ1h%", "Δ24h%", "template", "trend#"):
            table.add_column(col)
        for o in opps[:10]:
            table.add_row(
                o.ticker, o.trigger, f"{o.priority_score:.1f}",
                f"{o.change_1h_pct:+.2f}" if o.change_1h_pct is not None else "—",
                f"{o.change_24h_pct:+.2f}",
                o.suggested_template,
                o.binance_trend_hashtag or "—",
            )
        rprint(table)
    _run(_go())


@app.command("render")
def cmd_render(
    ticker: str = typer.Option("BTC", help="Base ticker"),
    trigger: str = typer.Option("PUMP", help="PUMP/DUMP/EXTREME_PUMP/…"),
    change_24h: float = typer.Option(8.0, help="Synthetic 24h change %"),
    last_price: float = typer.Option(60000.0, help="Last price"),
) -> None:
    """Render a sample card set for a ticker (no publishing)."""
    async def _go():
        opp = RankedOpportunity(
            ticker=ticker.upper(),
            trigger=trigger.upper(),
            change_1h_pct=2.0,
            change_24h_pct=change_24h,
            volume_ratio=None,
            binance_trend_hashtag=None,
            priority_score=9.0,
            suggested_template="trade_card",
            suggested_tendency=1 if change_24h >= 0 else 2,
            raw_payload={"symbol": f"{ticker.upper()}USDT", "last_price": last_price},
        )
        vp = VisualPipeline()
        res = await vp.produce(opp)
        for p in res.paths:
            rprint(f"[green]✓[/green] rendered {p}")
    _run(_go())


@app.command("hook")
def cmd_hook(
    ticker: str = typer.Option(...),
    trigger: str = typer.Option("PUMP"),
    template_hint: str = typer.Option("profit_card"),
    tendency: int = typer.Option(1),
) -> None:
    """Generate a single hook (text only)."""
    async def _go():
        gen = HookGenerator()
        r = await gen.generate(HookRequest(
            ticker=ticker.upper(), trigger=trigger.upper(),
            template_hint=template_hint, tendency=tendency,
        ))
        rprint(f"[bold]{r.text}[/bold]  [dim]({r.provider}/{r.model})[/dim]")
    _run(_go())


@app.command("publish")
def cmd_publish(
    ticker: str = typer.Option(...),
    hook: str = typer.Option(...),
    tendency: int = typer.Option(0),
    image: list[Path] = typer.Option(None, "--image", help="Image file to include (repeatable)"),
) -> None:
    """Publish a fully-formed post manually (bypasses opportunity queue)."""
    from engine.content.cashtag_resolver import CashtagResolver
    from engine.content.post_assembler import AssembledPost

    async def _go():
        await init_db()
        pair = await CashtagResolver().resolve(ticker)
        post = AssembledPost(
            ticker=ticker.upper(),
            body_text=hook,
            tendency=tendency,
            trading_pairs=[pair] if pair else [],
            template_name="manual",
            image_paths=list(image or []),
        )
        result = await Publisher().publish(post)
        rprint("Result:", {"ok": result.success, "id": result.external_id, "err": result.error})
    _run(_go())


@app.command("run-slot")
def cmd_run_slot(group: str = typer.Argument("power_hour")) -> None:
    """Fire a single slot immediately (testing tool)."""
    async def _go():
        await init_db()
        s = EngineScheduler()
        await s.run_slot(group)
    _run(_go())


@app.command("run")
def cmd_run(headless: bool = typer.Option(True, "--headless/--headed")) -> None:  # noqa: ARG001
    """Start the 24/7 scheduler daemon (Ctrl-C to stop)."""
    async def _go():
        await init_db()
        s = EngineScheduler()
        rprint("[bold cyan]binance-square-engine running. Ctrl-C to stop.[/bold cyan]")
        await s.run_forever()
    try:
        _run(_go())
    except KeyboardInterrupt:
        rprint("\n[yellow]shutdown requested[/yellow]")


@app.command("collect-stats")
def cmd_collect_stats() -> None:
    """One-off: fetch latest engagement data for our recent posts."""
    async def _go():
        await init_db()
        n = await PostTracker().run_once()
        rprint(f"[green]✓[/green] {n} snapshots recorded")
    _run(_go())


@app.command("report")
def cmd_report(
    hours: int = typer.Option(24),
    markdown_out: Path | None = typer.Option(None, "--out"),
) -> None:
    """Print or write a performance report."""
    async def _go():
        await init_db()
        rep = await build_report(hours)
        md = report_to_markdown(rep)
        if markdown_out:
            markdown_out.write_text(md, encoding="utf-8")
            rprint(f"[green]✓[/green] wrote {markdown_out}")
        else:
            rprint(md)
    _run(_go())


@app.command("learn")
def cmd_learn() -> None:
    """Update template stats and refresh the few-shot prompt examples."""
    async def _go():
        await init_db()
        n = await update_template_stats()
        ex = await refresh_examples()
        rprint(f"[green]✓[/green] templates updated: {n}, examples in pool: {ex}")
    _run(_go())


@app.command("pause")
def cmd_pause(
    hours: float = typer.Option(1.0),
    reason: str = typer.Option("manual"),
) -> None:
    """Globally pause publishing for N hours."""
    async def _go():
        await init_db()
        async with session_scope() as s:
            s.add(PublishLock(
                paused_until=datetime.now(tz=timezone.utc) + timedelta(hours=hours),
                reason=reason,
            ))
        rprint(f"[yellow]paused[/yellow] for {hours}h — reason: {reason}")
    _run(_go())


@app.command("resume")
def cmd_resume() -> None:
    """Clear any active pause."""
    async def _go():
        await init_db()
        async with session_scope() as s:
            s.add(PublishLock(paused_until=None, reason="resumed"))
        rprint("[green]resumed[/green]")
    _run(_go())


@app.command("recent")
def cmd_recent(limit: int = 20) -> None:
    """Show recent posts."""
    async def _go():
        await init_db()
        async with session_scope() as s:
            rows = (await s.execute(
                select(Post).order_by(desc(Post.created_at)).limit(limit)
            )).scalars().all()
        table = Table(title=f"Recent posts (limit {limit})")
        for c in ("when", "ticker", "tendency", "status", "ext_id", "body"):
            table.add_column(c)
        for r in rows:
            table.add_row(
                (r.published_at or r.created_at).strftime("%Y-%m-%d %H:%M"),
                r.ticker, str(r.tendency), r.status,
                (r.external_post_id or "—")[:12],
                r.body_text[:60],
            )
        rprint(table)
    _run(_go())


@app.command("cookies-import")
def cmd_cookies_import(path: Path) -> None:
    """Import cookies exported as JSON (array of cookie objects)."""
    settings = get_settings()
    raw = json.loads(Path(path).read_text())
    settings.binance_cookies_path.parent.mkdir(parents=True, exist_ok=True)
    settings.binance_cookies_path.write_text(json.dumps(raw, indent=2))
    rprint(f"[green]✓[/green] wrote {len(raw)} cookies → {settings.binance_cookies_path}")


if __name__ == "__main__":
    app()

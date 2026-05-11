"""Scheduler that fires the publish pipeline at configured slot times.

Design:
    - APScheduler AsyncIOScheduler is the heartbeat.
    - Each slot in `playbooks/daily_schedule.yaml` becomes a CronTrigger.
    - On each fire, we run `run_slot(group)` which:
        1. Picks the highest priority unconsumed Opportunity from the DB.
        2. Builds a post (content + visuals).
        3. Publishes it.
    - Background jobs continuously refresh signals.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import desc, select

from engine.config import get_settings
from engine.content.post_assembler import PostAssembler
from engine.db import session_scope
from engine.distribution.publisher import Publisher
from engine.logging_setup import get_logger
from engine.models import Opportunity
from engine.signal.opportunity_ranker import OpportunityRanker, RankedOpportunity
from engine.visuals.pipeline import VisualPipeline

log = get_logger(__name__)


@dataclass(slots=True)
class Slot:
    time: str        # HH:MM
    group: str
    jitter_minutes: int = 5


def load_schedule(path: Path) -> tuple[list[Slot], dict[str, list[str]]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    slots = [Slot(**s) for s in (raw.get("slots") or [])]
    slot_tpl = raw.get("slot_templates") or {}
    return slots, slot_tpl


def _hash_jitter(base: datetime, jitter_minutes: int) -> datetime:
    """Apply random jitter; deterministic per (date, slot)."""
    delta = random.randint(-jitter_minutes, jitter_minutes)
    return base + timedelta(minutes=delta)


def _cmp_threshold(value: float | None, expr: str) -> bool:
    """Parse exprs like '>= 15', '<= -10', '> 4', '< 0', '== 1', '!= 0'."""
    if value is None:
        return False
    expr = expr.strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if expr.startswith(op):
            try:
                rhs = float(expr[len(op):].strip())
            except ValueError:
                return False
            return {
                ">=": value >= rhs,
                "<=": value <= rhs,
                ">":  value > rhs,
                "<":  value < rhs,
                "==": value == rhs,
                "!=": value != rhs,
            }[op]
    # Bare number = equality
    try:
        return value == float(expr)
    except ValueError:
        return False


def _match_burst_rule(opp: Opportunity, rules: list[dict]) -> dict | None:
    """Return the first rule that matches, or None."""
    for rule in rules:
        cond = rule.get("when") or {}
        ok = True
        if "change_1h_pct" in cond and not _cmp_threshold(opp.change_1h_pct, str(cond["change_1h_pct"])):
            ok = False
        if ok and "change_24h_pct" in cond and not _cmp_threshold(opp.change_24h_pct, str(cond["change_24h_pct"])):
            ok = False
        if ok and "volume_ratio" in cond and not _cmp_threshold(opp.volume_ratio, str(cond["volume_ratio"])):
            ok = False
        if (
            ok and "binance_trend_match" in cond and bool(cond["binance_trend_match"])
            and not opp.binance_trend_hashtag
        ):
            ok = False
        if ok and "priority_score" in cond and not _cmp_threshold(opp.priority_score, str(cond["priority_score"])):
            ok = False
        if ok and "news_trigger_in" in cond:
            triggers = set(cond["news_trigger_in"] or [])
            payload = opp.raw_payload or {}
            news_triggers = set(payload.get("news_triggers") or [])
            if not (triggers & news_triggers):
                ok = False
        if ok:
            return rule
    return None


class EngineScheduler:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.scheduler = AsyncIOScheduler(timezone=self.settings.scheduler_timezone)
        self.ranker = OpportunityRanker()
        self.assembler = PostAssembler()
        self.visuals = VisualPipeline()
        self.publisher = Publisher()

    # ---------- background jobs ----------

    async def refresh_signals(self) -> None:
        try:
            opps = await self.ranker.rank()
            await self.ranker.persist(opps)
        except Exception as e:
            log.exception("refresh_signals_failed", error=str(e))

    async def maintenance(self) -> None:
        """Prune very old un-consumed opportunities + roll snapshot."""
        cutoff = datetime.now(tz=UTC) - timedelta(hours=12)
        async with session_scope() as s:
            stale = (await s.execute(
                select(Opportunity).where(
                    Opportunity.consumed.is_(False),
                    Opportunity.discovered_at < cutoff,
                )
            )).scalars().all()
            for o in stale:
                o.consumed = True
                o.consumed_at = datetime.now(tz=UTC)
        if stale:
            log.info("stale_opportunities_pruned", count=len(stale))

    async def daily_snapshot(self) -> None:
        """Compress the SQLite db to data/runtime/snapshots/YYYYMMDD.db.gz."""
        import gzip
        import shutil
        try:
            url = self.settings.database_url
            if not url.startswith("sqlite"):
                return
            src = Path(url.split("///")[-1])
            if not src.exists():
                return
            dst_dir = self.settings.runtime_dir / "snapshots"
            dst_dir.mkdir(parents=True, exist_ok=True)
            tag = datetime.now(tz=UTC).strftime("%Y%m%d")
            dst = dst_dir / f"{tag}.db.gz"
            with src.open("rb") as fin, gzip.open(dst, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            # Retention: keep last 14 days
            snaps = sorted(dst_dir.glob("*.db.gz"))
            for old in snaps[:-14]:
                with contextlib.suppress(Exception):
                    old.unlink()
            log.info("db_snapshot_written", path=str(dst), kept=min(len(snaps), 14))
        except Exception as e:  # noqa: BLE001
            log.exception("daily_snapshot_failed", error=str(e))

    async def evaluate_burst_triggers(self) -> None:
        """Check the top of the opportunity queue against playbooks/burst_triggers.yaml.

        On a match, fire one or more posts back-to-back with the configured
        gap (respecting rate_limiter as a safety net).
        """
        try:
            cfg = yaml.safe_load(self.settings.burst_triggers_file.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("burst_triggers_load_failed", error=str(e))
            return
        rules = cfg.get("triggers") or []
        if not rules:
            return

        async with session_scope() as s:
            rows = (await s.execute(
                select(Opportunity)
                .where(Opportunity.consumed.is_(False))
                .order_by(desc(Opportunity.priority_score), desc(Opportunity.discovered_at))
                .limit(20)
            )).scalars().all()

        for opp in rows:
            matched = _match_burst_rule(opp, rules)
            if not matched:
                continue
            log.info("burst_match", rule=matched["name"], ticker=opp.ticker,
                     score=opp.priority_score)
            await self._fire_burst(opp, matched)
            # one burst per evaluation cycle keeps it conservative
            return

    async def _fire_burst(self, opp: Opportunity, rule: dict) -> None:
        burst = rule.get("burst") or {}
        post_count = int(burst.get("post_count", 1))
        gap = int(burst.get("gap_seconds", 90))
        templates = burst.get("templates") or ["trade_card"]

        for i in range(post_count):
            ranked = RankedOpportunity(
                ticker=opp.ticker,
                trigger=opp.trigger,
                change_1h_pct=opp.change_1h_pct,
                change_24h_pct=opp.change_24h_pct or 0.0,
                volume_ratio=opp.volume_ratio,
                binance_trend_hashtag=opp.binance_trend_hashtag,
                priority_score=opp.priority_score,
                suggested_template=templates[i % len(templates)],
                suggested_tendency=opp.suggested_tendency,
                raw_payload=dict(opp.raw_payload or {}),
            )
            post = await self.assembler.assemble(ranked)
            visuals = await self.visuals.produce(ranked)
            post.image_paths = visuals.paths
            await self.publisher.publish(post, opportunity_id=opp.id)
            if i + 1 < post_count:
                await asyncio.sleep(max(gap, 30))
        async with session_scope() as s:
            row = await s.get(Opportunity, opp.id)
            if row is not None:
                row.consumed = True
                row.consumed_at = datetime.now(tz=UTC)

    # ---------- slot execution ----------

    async def run_slot(self, group: str) -> None:
        log.info("slot_fired", group=group, ts=datetime.now(tz=UTC).isoformat())
        try:
            await self._do_slot(group)
        except Exception as e:
            log.exception("slot_failed", group=group, error=str(e))

    async def _pick_opportunity(self) -> tuple[int, RankedOpportunity] | None:
        async with session_scope() as s:
            rows = (await s.execute(
                select(Opportunity)
                .where(Opportunity.consumed.is_(False))
                .order_by(desc(Opportunity.priority_score), desc(Opportunity.discovered_at))
                .limit(20)
            )).scalars().all()
            for row in rows:
                # Reconstruct RankedOpportunity dataclass for downstream
                ranked = RankedOpportunity(
                    ticker=row.ticker,
                    trigger=row.trigger,
                    change_1h_pct=row.change_1h_pct,
                    change_24h_pct=row.change_24h_pct or 0.0,
                    volume_ratio=row.volume_ratio,
                    binance_trend_hashtag=row.binance_trend_hashtag,
                    priority_score=row.priority_score,
                    suggested_template=row.suggested_template or "big_picture",
                    suggested_tendency=row.suggested_tendency,
                    raw_payload=dict(row.raw_payload or {}),
                )
                return row.id, ranked
        return None

    async def _do_slot(self, group: str) -> None:
        # Refresh signals first if we have none queued
        picked = await self._pick_opportunity()
        if picked is None:
            log.info("no_opportunities_running_scan", group=group)
            await self.refresh_signals()
            picked = await self._pick_opportunity()
        if picked is None:
            log.warning("slot_skipped_no_opportunity", group=group)
            return
        opp_id, ranked = picked
        log.info("opportunity_picked", id=opp_id, ticker=ranked.ticker, score=ranked.priority_score)

        post = await self.assembler.assemble(ranked)
        visuals = await self.visuals.produce(ranked)
        post.image_paths = visuals.paths

        result = await self.publisher.publish(post, opportunity_id=opp_id)
        log.info(
            "slot_completed",
            group=group,
            success=result.success,
            external_id=result.external_id,
            ticker=ranked.ticker,
        )

    # ---------- lifecycle ----------

    def configure_jobs(self) -> None:
        slots, _ = load_schedule(self.settings.daily_schedule_file)
        for slot in slots:
            hh, mm = slot.time.split(":")
            self.scheduler.add_job(
                self.run_slot,
                CronTrigger(hour=int(hh), minute=int(mm)),
                args=[slot.group],
                id=f"slot_{slot.group}_{slot.time}",
                misfire_grace_time=300,
                jitter=slot.jitter_minutes * 60,
                replace_existing=True,
            )

        self.scheduler.add_job(
            self.refresh_signals,
            IntervalTrigger(minutes=5),
            id="bg_refresh_signals",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.maintenance,
            IntervalTrigger(hours=1),
            id="bg_maintenance",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.evaluate_burst_triggers,
            IntervalTrigger(minutes=7),
            id="bg_burst_triggers",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.daily_snapshot,
            CronTrigger(hour=0, minute=10),
            id="bg_daily_snapshot",
            replace_existing=True,
        )

    async def start_async(self) -> None:
        self.configure_jobs()
        self.scheduler.start()
        log.info(
            "scheduler_started",
            slots=len([j for j in self.scheduler.get_jobs() if j.id.startswith("slot_")]),
            timezone=self.settings.scheduler_timezone,
        )

    async def run_forever(self) -> None:
        await self.start_async()
        try:
            # Keep the event loop alive
            while True:
                await asyncio.sleep(3600)
        finally:
            self.scheduler.shutdown(wait=False)

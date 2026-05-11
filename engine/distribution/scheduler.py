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
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
        """Prune very old un-consumed opportunities."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=12)
        async with session_scope() as s:
            stale = (await s.execute(
                select(Opportunity).where(
                    Opportunity.consumed.is_(False),
                    Opportunity.discovered_at < cutoff,
                )
            )).scalars().all()
            for o in stale:
                o.consumed = True
                o.consumed_at = datetime.now(tz=timezone.utc)
        if stale:
            log.info("stale_opportunities_pruned", count=len(stale))

    # ---------- slot execution ----------

    async def run_slot(self, group: str) -> None:
        log.info("slot_fired", group=group, ts=datetime.now(tz=timezone.utc).isoformat())
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

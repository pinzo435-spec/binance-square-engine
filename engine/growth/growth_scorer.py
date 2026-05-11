"""Composite *growth score* — the single number the engine optimises.

Most Binance Square Top-10 ranking factors are opaque, but in practice they
reduce to a weighted mix of *raw reach*, *high-effort engagement* (comments,
reposts), and *velocity* (how quickly those numbers accumulate). This module
defines that mix as a pure function of an `EngagementSnapshot`, plus a
lightweight EWMA updater that maintains a per-post `GrowthScoreSnapshot` row.

The weights here are deliberately exposed as constants — the self-optimiser
re-reads them every cycle, so you can A/B tune by editing `WEIGHTS` and
restarting the daemon without touching downstream code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select

from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import (
    EngagementSnapshot,
    GrowthScoreSnapshot,
    Post,
)

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Weights — tune these to bias the optimiser.
# ─────────────────────────────────────────────────────────────────────────────
WEIGHTS = {
    "views":    0.20,   # raw reach matters but not as much as engagement
    "likes":    1.00,   # cheapest engagement
    "comments": 1.30,   # higher-effort signal
    "shares":   1.60,   # the reach multiplier
    "quotes":   1.40,
    "velocity": 0.40,   # bonus for hitting numbers FAST
}

# Velocity floor — below this many views in the first hour, no bonus.
VELOCITY_VIEWS_FLOOR = 50

# CTR estimate (when we ever wire click data): assume 1.5% baseline
DEFAULT_CTR = 0.015


@dataclass(slots=True)
class GrowthSignals:
    """Inputs to `score()`. All counts are cumulative since publish."""

    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    quotes: int = 0
    age_hours: float = 0.0


def velocity_bonus(views_per_hour: float) -> float:
    """Logarithmic bonus that rewards fast accumulation, capped at ~5."""
    if views_per_hour < VELOCITY_VIEWS_FLOOR:
        return 0.0
    # log10(50) ~ 1.7, log10(50_000) ~ 4.7 — yields a bounded 0..3.0 range
    return min(3.0, math.log10(views_per_hour / VELOCITY_VIEWS_FLOOR))


def score(signals: GrowthSignals) -> tuple[float, float]:
    """Return `(growth_score, velocity_bonus)`.

    We pass back the bonus separately so callers can show it on the dashboard.
    """
    s = signals
    age = max(s.age_hours, 0.25)  # avoid div-by-zero in the first minutes
    vph = s.views / age
    vb = velocity_bonus(vph)

    component = (
        math.log1p(s.views)    * WEIGHTS["views"]
        + math.log1p(s.likes)    * WEIGHTS["likes"]
        + math.log1p(s.comments) * WEIGHTS["comments"]
        + math.log1p(s.shares)   * WEIGHTS["shares"]
        + math.log1p(s.quotes)   * WEIGHTS["quotes"]
        + vb                     * WEIGHTS["velocity"]
    )
    return component, vb


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot maintenance — called from analytics whenever a fresh EngagementSnapshot lands.
# ─────────────────────────────────────────────────────────────────────────────


async def upsert_for_post(post_id: int) -> GrowthScoreSnapshot | None:
    """Rebuild the GrowthScoreSnapshot row for a single post from its latest engagement snapshot.

    Returns the new row (or None if the post has no engagement yet).
    """
    async with session_scope() as s:
        post = await s.get(Post, post_id)
        if post is None:
            return None
        # Latest engagement snapshot for this post
        snap = (await s.execute(
            select(EngagementSnapshot)
            .where(EngagementSnapshot.post_id == post_id)
            .order_by(EngagementSnapshot.captured_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if snap is None:
            return None

        signals = GrowthSignals(
            views=snap.view_count,
            likes=snap.like_count,
            comments=snap.comment_count,
            shares=snap.share_count,
            quotes=snap.quote_count,
            age_hours=snap.age_hours,
        )
        gs, vb = score(signals)

        existing = (await s.execute(
            select(GrowthScoreSnapshot).where(GrowthScoreSnapshot.post_id == post_id)
        )).scalar_one_or_none()
        if existing is None:
            row = GrowthScoreSnapshot(
                post_id=post_id,
                growth_score=gs,
                velocity_bonus=vb,
                age_hours=snap.age_hours,
                last_view_count=snap.view_count,
                last_like_count=snap.like_count,
                last_comment_count=snap.comment_count,
                last_share_count=snap.share_count,
            )
            s.add(row)
            log.info(
                "growth_score_initialised", post_id=post_id,
                ticker=post.ticker, growth_score=round(gs, 3), velocity=round(vb, 3),
            )
            return row
        existing.growth_score = gs
        existing.velocity_bonus = vb
        existing.age_hours = snap.age_hours
        existing.last_view_count = snap.view_count
        existing.last_like_count = snap.like_count
        existing.last_comment_count = snap.comment_count
        existing.last_share_count = snap.share_count
        log.info(
            "growth_score_updated", post_id=post_id,
            ticker=post.ticker, growth_score=round(gs, 3), velocity=round(vb, 3),
        )
        return existing


async def rebuild_all() -> int:
    """Sweep every post that has at least one engagement snapshot and upsert its score.

    Called on demand or as part of `self_optimizer.run_cycle()`.
    Returns the number of rows touched.
    """
    n = 0
    async with session_scope() as s:
        post_ids = (await s.execute(
            select(EngagementSnapshot.post_id).distinct()
        )).scalars().all()
    for pid in post_ids:
        if await upsert_for_post(pid) is not None:
            n += 1
    log.info("growth_scores_rebuilt", count=n)
    return n

"""Coin Priority Engine — composite ticker score for the opportunity ranker.

Combines short-term *live* signal (volume spike, volatility, social trend,
Binance trend hashtag match) with *historical* engagement weight (how well
this ticker has performed for THIS account in the past).

Returned scores plug into `engine.signal.opportunity_ranker.rank()` as an
optional boost — backwards-compatible if the growth tables are empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import (
    CoinPerformance,
    GrowthScoreSnapshot,
    Opportunity,
    Post,
)

log = get_logger(__name__)


# Default weights — tunable.
W_VOLUME_SPIKE    = 1.2
W_VOLATILITY      = 0.8
W_BINANCE_TREND   = 2.5
W_HISTORICAL      = 1.5
W_RECENCY_PENALTY = -1.0  # subtract if we posted this ticker very recently


@dataclass(slots=True)
class CoinScore:
    ticker: str
    composite: float
    volume_component: float
    volatility_component: float
    binance_trend_component: float
    historical_component: float
    recency_penalty: float


def _normalise(value: float | None, scale: float) -> float:
    """Clamp a positive value to ~[0, 1] via x / (x + scale)."""
    if value is None or value <= 0:
        return 0.0
    return value / (value + scale)


async def score_one(opp: Opportunity, now: datetime | None = None) -> CoinScore:
    """Score a single opportunity row."""
    now = now or datetime.now(tz=UTC).replace(tzinfo=None)
    vol_c = _normalise(opp.volume_ratio, scale=5.0)
    vola_c = _normalise(abs(opp.change_1h_pct or 0.0), scale=10.0)
    trend_c = 1.0 if opp.binance_trend_hashtag else 0.0

    hist_c = 0.0
    recency_pen = 0.0
    async with session_scope() as s:
        cp = (await s.execute(
            select(CoinPerformance).where(CoinPerformance.ticker == opp.ticker)
        )).scalar_one_or_none()
        if cp is not None and cp.samples >= 3:
            # Normalise historical growth_score (typical post lands 1..8)
            hist_c = _normalise(cp.avg_growth_score, scale=4.0)
            if cp.last_posted_at is not None:
                gap_hours = (now - cp.last_posted_at).total_seconds() / 3600.0
                # Penalise if posted < 4h ago, linearly fades out by 24h
                if gap_hours < 4:
                    recency_pen = 1.0
                elif gap_hours < 24:
                    recency_pen = (24 - gap_hours) / 20.0

    composite = (
        vol_c * W_VOLUME_SPIKE
        + vola_c * W_VOLATILITY
        + trend_c * W_BINANCE_TREND
        + hist_c * W_HISTORICAL
        + recency_pen * W_RECENCY_PENALTY
    )
    return CoinScore(
        ticker=opp.ticker,
        composite=composite,
        volume_component=vol_c * W_VOLUME_SPIKE,
        volatility_component=vola_c * W_VOLATILITY,
        binance_trend_component=trend_c * W_BINANCE_TREND,
        historical_component=hist_c * W_HISTORICAL,
        recency_penalty=recency_pen * W_RECENCY_PENALTY,
    )


async def score_many(opps: list[Opportunity]) -> dict[str, CoinScore]:
    """Score many opportunities (best-effort, sequential to avoid DB contention)."""
    out: dict[str, CoinScore] = {}
    for o in opps:
        out[o.ticker] = await score_one(o)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Updater — refresh historical weights from posts in DB.
# ─────────────────────────────────────────────────────────────────────────────


_EWMA_ALPHA = 0.30


async def update_from_posts(window_hours: int = 168) -> int:
    """Refresh `CoinPerformance` rows from posts in the last `window_hours`.

    Returns the number of (ticker × post) updates processed.
    """
    from datetime import timedelta

    cutoff = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
    n = 0
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post)
            .where(Post.status == "success")
            .where(Post.published_at >= cutoff)
        )).scalars().all()
        for p in posts:
            gs = (await s.execute(
                select(GrowthScoreSnapshot).where(GrowthScoreSnapshot.post_id == p.id)
            )).scalar_one_or_none()
            growth = gs.growth_score if gs is not None else 0.0
            row = (await s.execute(
                select(CoinPerformance).where(CoinPerformance.ticker == p.ticker)
            )).scalar_one_or_none()
            if row is None:
                row = CoinPerformance(ticker=p.ticker)
                s.add(row)
                await s.flush()
            samples = row.samples
            row.avg_growth_score = (
                (row.avg_growth_score * samples + growth) / (samples + 1)
                if samples < 5
                else (1 - _EWMA_ALPHA) * row.avg_growth_score + _EWMA_ALPHA * growth
            )
            if gs is not None:
                row.avg_views = (
                    (row.avg_views * samples + gs.last_view_count) / (samples + 1)
                    if samples < 5
                    else (1 - _EWMA_ALPHA) * row.avg_views + _EWMA_ALPHA * gs.last_view_count
                )
            row.samples = samples + 1
            row.last_posted_at = p.published_at or row.last_posted_at
            n += 1
    log.info("coin_performance_updated", rows=n)
    return n

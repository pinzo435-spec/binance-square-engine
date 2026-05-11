"""Combines all signal sources into a ranked queue of `Opportunity` rows.

Inputs:
    - market_scanner: top movers + volume spikes (primary)
    - trend_scraper: Binance Square trending hashtags (boost)
    - news_feed: news headlines (boost)
    - reference_feed: what momomomo7171 just posted (mimic boost)

Output: writes ranked rows into the `opportunities` table.

Priority score (see strategy doc §9.2):
    +3   vertical candle (|change_1h_pct| ≥ 5)
    +2   top-50 by quote volume
    +2   matches a Binance Square trending hashtag
    +1   no post from us on this ticker in last MIN_GAP_SAME_TICKER_HOURS
    +1   reference account posted about it in last hour
    +1   trending in news feed
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from engine.config import get_settings
from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import Opportunity, Post
from engine.signal.market_scanner import MarketScanner, MarketSignal
from engine.signal.news_feed import NewsFeed, NewsItem
from engine.signal.reference_feed import ReferenceFeed, ReferencePostRecord
from engine.signal.trend_scraper import TrendingTag, TrendScraper

log = get_logger(__name__)


@dataclass(slots=True)
class RankedOpportunity:
    ticker: str
    trigger: str
    change_1h_pct: float | None
    change_24h_pct: float
    volume_ratio: float | None
    binance_trend_hashtag: str | None
    priority_score: float
    suggested_template: str
    suggested_tendency: int
    raw_payload: dict


def _classify_trigger(s: MarketSignal) -> tuple[str, str, int]:
    """Map a MarketSignal to (trigger, template_name, tendency)."""
    if s.is_pump and s.is_hot_1h:
        return "EXTREME_PUMP", "explosion_celebration", 1
    if s.is_pump:
        return "PUMP", "profit_card", 1
    if s.is_dump and s.is_hot_1h:
        return "EXTREME_DUMP", "warning_bearish", 2
    if s.is_dump:
        return "DUMP", "rug_warning", 2
    if s.is_hot_1h and (s.price_change_pct_1h or 0) > 0:
        return "VOLATILITY_UP", "curiosity_question", 1
    if s.is_hot_1h:
        return "VOLATILITY_DOWN", "curiosity_question", 2
    return "STEADY", "big_picture", 0


def _match_trend_hashtag(ticker: str, tags: list[TrendingTag]) -> str | None:
    t = ticker.upper()
    for tag in tags:
        n = tag.name.upper()
        if t in n or n in t:
            return tag.name
    return None


class OpportunityRanker:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def gather(self) -> tuple[
        list[MarketSignal], list[TrendingTag], list[NewsItem], list[ReferencePostRecord]
    ]:
        async with MarketScanner() as ms:
            movers = await ms.scan(min_quote_volume=1_500_000.0, top_n=80)
        async with TrendScraper() as ts:
            tags = await ts.fetch_trending()
        async with NewsFeed() as nf:
            news = await nf.fetch_all()
        ref: list[ReferencePostRecord] = []
        if self.settings.reference_square_uid:
            async with ReferenceFeed(self.settings.reference_square_uid) as rf:
                try:
                    ref = await rf.fetch_latest(max_posts=20)
                except Exception as e:
                    log.warning("reference_feed_skipped", error=str(e))
        return movers, tags, news, ref

    async def _recently_posted_tickers(self, hours: int) -> set[str]:
        cutoff = datetime.now(tz=UTC) - timedelta(hours=hours)
        async with session_scope() as s:
            res = await s.execute(
                select(Post.ticker).where(Post.published_at >= cutoff)
            )
            return {row[0].upper() for row in res.all()}

    async def rank(self) -> list[RankedOpportunity]:
        movers, tags, news, ref = await self.gather()
        recent_tickers = await self._recently_posted_tickers(self.settings.min_gap_same_ticker_hours)
        ref_recent_tickers = {
            t for r in ref for t in r.tickers
            if r.published_at and r.published_at >= datetime.now(tz=UTC) - timedelta(hours=1)
        }
        news_tickers = {t for n in news for t in n.detected_tickers}

        # Rank top-50 movers globally by quote_volume to derive a stable "top set"
        top_volume = {m.symbol for m in sorted(movers, key=lambda x: x.volume_usd_24h, reverse=True)[:50]}

        out: list[RankedOpportunity] = []
        for s in movers:
            trigger, template, tendency = _classify_trigger(s)
            score = 0.0
            if s.is_hot_1h:
                score += 3
            if s.is_pump or s.is_dump:
                score += 1
            if s.symbol in top_volume:
                score += 2
            trend_hashtag = _match_trend_hashtag(s.ticker, tags)
            if trend_hashtag:
                score += 2
            if s.ticker.upper() not in recent_tickers:
                score += 1
            if s.ticker.upper() in ref_recent_tickers:
                score += 1
            if s.ticker.upper() in news_tickers:
                score += 1

            out.append(
                RankedOpportunity(
                    ticker=s.ticker,
                    trigger=trigger,
                    change_1h_pct=s.price_change_pct_1h,
                    change_24h_pct=s.price_change_pct_24h,
                    volume_ratio=None,
                    binance_trend_hashtag=trend_hashtag,
                    priority_score=score,
                    suggested_template=template,
                    suggested_tendency=tendency,
                    raw_payload={
                        "symbol": s.symbol,
                        "venue": s.venue,
                        "last_price": s.last_price,
                        "quote_volume": s.quote_volume,
                        "in_news": s.ticker.upper() in news_tickers,
                        "ref_posted": s.ticker.upper() in ref_recent_tickers,
                    },
                )
            )

        out.sort(key=lambda o: o.priority_score, reverse=True)
        log.info("opportunities_ranked", count=len(out), top_score=out[0].priority_score if out else None)
        return out

    async def persist(self, opps: list[RankedOpportunity]) -> int:
        """Write the top opportunities to the DB (deduped against recent open ones)."""
        if not opps:
            return 0
        cutoff = datetime.now(tz=UTC) - timedelta(hours=2)
        written = 0
        async with session_scope() as s:
            existing = await s.execute(
                select(Opportunity.ticker).where(
                    Opportunity.discovered_at >= cutoff,
                    Opportunity.consumed.is_(False),
                )
            )
            recent = {r[0].upper() for r in existing.all()}
            for o in opps:
                if o.ticker.upper() in recent:
                    continue
                if o.priority_score < 3:
                    continue
                s.add(
                    Opportunity(
                        ticker=o.ticker,
                        trigger=o.trigger,
                        change_1h_pct=o.change_1h_pct,
                        change_24h_pct=o.change_24h_pct,
                        volume_ratio=o.volume_ratio,
                        binance_trend_hashtag=o.binance_trend_hashtag,
                        priority_score=o.priority_score,
                        suggested_template=o.suggested_template,
                        suggested_tendency=o.suggested_tendency,
                        raw_payload=o.raw_payload,
                    )
                )
                written += 1
        log.info("opportunities_persisted", written=written)
        return written

"""Periodically polls our own profile feed and records engagement snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from engine.config import get_settings
from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import EngagementSnapshot, Post
from engine.signal.reference_feed import ReferenceFeed, ReferencePostRecord

log = get_logger(__name__)


def _engagement_score(s: ReferencePostRecord) -> float:
    v = max(s.view_count, 1)
    return (
        s.like_count * 1
        + s.comment_count * 2
        + s.share_count * 4
        + s.quote_count * 3
    ) / v


def _match_post(scraped: ReferencePostRecord, posts: list[Post]) -> Post | None:
    """Match a scraped post to one of our own by external_id or body similarity."""
    # Prefer ID match
    by_id = next((p for p in posts if p.external_post_id and p.external_post_id == scraped.id), None)
    if by_id:
        return by_id
    # Fallback: exact body match within 24h window
    candidates = [p for p in posts if p.body_text.strip() == scraped.body_text.strip()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: abs(((p.published_at or datetime.min.replace(tzinfo=timezone.utc))
                                        - (scraped.published_at or datetime.min.replace(tzinfo=timezone.utc))).total_seconds()))
    best = candidates[0]
    if best.published_at and scraped.published_at:
        delta = abs((best.published_at - scraped.published_at).total_seconds())
        if delta > 24 * 3600:
            return None
    return best


class PostTracker:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def run_once(self, lookback_hours: int = 48) -> int:
        if not self.settings.square_uid:
            log.warning("post_tracker_disabled_no_square_uid")
            return 0
        async with ReferenceFeed(self.settings.square_uid) as feed:
            scraped = await feed.fetch_latest(max_posts=80)
        if not scraped:
            return 0

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
        async with session_scope() as s:
            our_posts = (await s.execute(
                select(Post).where(
                    Post.status.in_(["published", "published_dry_run"]),
                    Post.published_at >= cutoff,
                ).order_by(desc(Post.published_at))
            )).scalars().all()

            recorded = 0
            for sc in scraped:
                p = _match_post(sc, our_posts)
                if not p:
                    continue
                age_hours = ((datetime.now(tz=timezone.utc) - (p.published_at or datetime.now(tz=timezone.utc)))
                             .total_seconds() / 3600.0)
                # If the most recent snapshot is < 30 min old, skip
                last_snap = (await s.execute(
                    select(EngagementSnapshot)
                    .where(EngagementSnapshot.post_id == p.id)
                    .order_by(desc(EngagementSnapshot.captured_at))
                    .limit(1)
                )).scalars().first()
                if last_snap and (datetime.now(tz=timezone.utc) - last_snap.captured_at).total_seconds() < 30 * 60:
                    continue
                snap = EngagementSnapshot(
                    post_id=p.id,
                    age_hours=age_hours,
                    view_count=sc.view_count,
                    like_count=sc.like_count,
                    comment_count=sc.comment_count,
                    share_count=sc.share_count,
                    quote_count=sc.quote_count,
                    engagement_score=_engagement_score(sc),
                )
                s.add(snap)
                if not p.external_post_id and sc.id:
                    p.external_post_id = sc.id
                recorded += 1
        log.info("snapshots_recorded", count=recorded)
        return recorded

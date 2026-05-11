"""Hard safety rails that gate the publisher.

Rails (from strategy doc §11.5):
    - Max N posts/day
    - Max N posts/hour
    - No same ticker within N hours
    - Pause if 3 consecutive low-view posts
    - Honour global pause flag set by `PublishLock`
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, func, select

from engine.config import get_settings
from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import EngagementSnapshot, Post, PublishLock

log = get_logger(__name__)


@dataclass(slots=True)
class RateDecision:
    allowed: bool
    reason: str = ""


class RateLimiter:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def check(self, ticker: str) -> RateDecision:
        s = self.settings
        now = datetime.now(tz=UTC)

        def _aware(dt):
            return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt

        async with session_scope() as sess:
            # 0. Global pause?
            lock_row = await sess.execute(
                select(PublishLock).order_by(desc(PublishLock.id)).limit(1)
            )
            lock = lock_row.scalars().first()
            paused_until = _aware(lock.paused_until) if lock else None
            if lock and paused_until and paused_until > now:
                return RateDecision(
                    False,
                    f"globally_paused until {paused_until.isoformat()}: {lock.reason}",
                )

            # 1. Day cap
            day_count = (await sess.execute(
                select(func.count(Post.id)).where(
                    Post.published_at >= now - timedelta(days=1),
                    Post.status == "published",
                )
            )).scalar_one()
            if day_count >= s.max_posts_per_day:
                return RateDecision(False, f"daily_cap_hit ({day_count}/{s.max_posts_per_day})")

            # 2. Hour cap
            hour_count = (await sess.execute(
                select(func.count(Post.id)).where(
                    Post.published_at >= now - timedelta(hours=1),
                    Post.status == "published",
                )
            )).scalar_one()
            if hour_count >= s.max_posts_per_hour:
                return RateDecision(False, f"hourly_cap_hit ({hour_count}/{s.max_posts_per_hour})")

            # 3. Same-ticker gap
            recent_ticker = (await sess.execute(
                select(Post).where(
                    func.upper(Post.ticker) == ticker.upper(),
                    Post.published_at >= now - timedelta(hours=s.min_gap_same_ticker_hours),
                    Post.status == "published",
                ).limit(1)
            )).scalars().first()
            if recent_ticker:
                return RateDecision(
                    False,
                    f"same_ticker_gap_violation (last post: {recent_ticker.published_at.isoformat()})",
                )

            # 4. Auto-pause on N consecutive low-view posts
            last_n = (await sess.execute(
                select(Post)
                .where(Post.status == "published")
                .order_by(desc(Post.published_at))
                .limit(s.pause_if_n_low_views)
            )).scalars().all()
            if len(last_n) == s.pause_if_n_low_views:
                low = 0
                for p in last_n:
                    snap = (await sess.execute(
                        select(EngagementSnapshot)
                        .where(EngagementSnapshot.post_id == p.id)
                        .order_by(desc(EngagementSnapshot.captured_at))
                        .limit(1)
                    )).scalars().first()
                    if snap and snap.view_count < s.low_views_threshold and snap.age_hours >= 1:
                        low += 1
                if low == s.pause_if_n_low_views:
                    return RateDecision(
                        False,
                        f"auto_paused_low_views ({low} consecutive < {s.low_views_threshold})",
                    )

        return RateDecision(True)

    async def trip_pause(self, hours: float, reason: str) -> None:
        async with session_scope() as sess:
            sess.add(PublishLock(
                paused_until=datetime.now(tz=UTC) + timedelta(hours=hours),
                reason=reason,
            ))
        log.warning("publishing_paused", hours=hours, reason=reason)

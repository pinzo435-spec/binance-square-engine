"""Safety Layer — guards that run BEFORE the publisher commits a post.

Rules enforced (in order):

    1. Duplicate detection: identical body_text in the last 7 days.
    2. Near-duplicate detection: Jaccard similarity > 0.85 on shingles in the
       last 24h.
    3. Repetitive hook detection: same category fired 4 times in a row.
    4. Coin cooldown: same ticker posted within `min_gap_hours` (default 4)
       suppress UNLESS the opportunity score is in the top 1% (a 9.0+).
    5. Posts-per-hour ceiling that goes BEYOND the existing rate-limiter (the
       rate-limiter is a hard quota; this is a *quality* guard).

All checks are non-fatal — they return a `SafetyVerdict` with a reason; the
caller decides what to do. The default behaviour in `Publisher` is to *skip*
the post and mark the opportunity as consumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select

from engine.db import session_scope
from engine.growth.hook_intelligence import classify
from engine.logging_setup import get_logger
from engine.models import Post

log = get_logger(__name__)


@dataclass(slots=True)
class SafetyVerdict:
    allow: bool
    reason: str = ""
    detail: dict | None = None


# Tunables — exposed as constants so the self-optimiser can adjust them.
DUPLICATE_WINDOW_HOURS = 168
NEAR_DUP_WINDOW_HOURS = 24
NEAR_DUP_THRESHOLD = 0.85
REPETITIVE_CATEGORY_STREAK = 4
COIN_MIN_GAP_HOURS = 4
COIN_GAP_OVERRIDE_SCORE = 9.0
HOURLY_QUALITY_CAP = 6  # quality guard, separate from rate_limiter's hard cap


def _shingles(text: str, k: int = 3) -> set[str]:
    """k-gram word shingles for Jaccard sim."""
    words = [w for w in text.split() if w]
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / max(union, 1)


async def check(
    *,
    body_text: str,
    ticker: str,
    opportunity_score: float = 0.0,
) -> SafetyVerdict:
    """Run the full guard sequence; return at first violation."""
    now = datetime.now(tz=UTC).replace(tzinfo=None)

    async with session_scope() as s:
        # 1. Identical body in DUPLICATE_WINDOW_HOURS
        dup = (await s.execute(
            select(Post)
            .where(Post.body_text == body_text)
            .where(Post.created_at >= now - timedelta(hours=DUPLICATE_WINDOW_HOURS))
            .limit(1)
        )).scalar_one_or_none()
        if dup is not None:
            return SafetyVerdict(False, "duplicate_body", {"original_id": dup.id})

        # 2. Near-duplicate in NEAR_DUP_WINDOW_HOURS
        recent = (await s.execute(
            select(Post)
            .where(Post.created_at >= now - timedelta(hours=NEAR_DUP_WINDOW_HOURS))
            .order_by(desc(Post.created_at))
            .limit(50)
        )).scalars().all()
        for r in recent:
            sim = jaccard(body_text, r.body_text or "")
            if sim >= NEAR_DUP_THRESHOLD:
                return SafetyVerdict(
                    False, "near_duplicate", {"original_id": r.id, "similarity": round(sim, 3)}
                )

        # 3. Repetitive hook category streak
        last_n = (await s.execute(
            select(Post)
            .order_by(desc(Post.created_at))
            .limit(REPETITIVE_CATEGORY_STREAK)
        )).scalars().all()
        if len(last_n) == REPETITIVE_CATEGORY_STREAK:
            categories = {classify(p.body_text or "").category for p in last_n}
            candidate_cat = classify(body_text).category
            if len(categories) == 1 and candidate_cat in categories:
                return SafetyVerdict(
                    False, "repetitive_category", {"category": candidate_cat}
                )

        # 4. Coin cooldown with override
        coin_recent = (await s.execute(
            select(Post)
            .where(Post.ticker == ticker)
            .where(Post.created_at >= now - timedelta(hours=COIN_MIN_GAP_HOURS))
            .order_by(desc(Post.created_at))
            .limit(1)
        )).scalar_one_or_none()
        if coin_recent is not None and opportunity_score < COIN_GAP_OVERRIDE_SCORE:
            return SafetyVerdict(
                False, "coin_cooldown",
                {"ticker": ticker, "recent_post_id": coin_recent.id,
                 "min_gap_hours": COIN_MIN_GAP_HOURS},
            )

        # 5. Hourly quality cap
        cnt = (await s.execute(
            select(Post)
            .where(Post.created_at >= now - timedelta(hours=1))
        )).scalars().all()
        if len(cnt) >= HOURLY_QUALITY_CAP:
            return SafetyVerdict(
                False, "hourly_quality_cap_hit",
                {"posts_last_hour": len(cnt), "cap": HOURLY_QUALITY_CAP},
            )

    return SafetyVerdict(True, "ok")


# Synchronous helper for tests
def jaccard_sync(a: str, b: str) -> float:
    return jaccard(a, b)

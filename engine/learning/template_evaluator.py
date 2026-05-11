"""Updates Template stats from rolling engagement data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import EngagementSnapshot, Post, Template

log = get_logger(__name__)


async def update_template_stats(window_days: int = 14) -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post).where(
                Post.published_at >= cutoff,
                Post.status == "published",
            )
        )).scalars().all()

        # Aggregate per template
        per_tpl: dict[str, list[EngagementSnapshot]] = {}
        for p in posts:
            snap = (await s.execute(
                select(EngagementSnapshot)
                .where(EngagementSnapshot.post_id == p.id)
                .order_by(desc(EngagementSnapshot.captured_at))
                .limit(1)
            )).scalars().first()
            if not snap or not p.template_name:
                continue
            per_tpl.setdefault(p.template_name, []).append(snap)

        updated = 0
        for name, snaps in per_tpl.items():
            n = len(snaps)
            avg_views = sum(x.view_count for x in snaps) / n
            avg_likes = sum(x.like_count for x in snaps) / n
            avg_eng = sum(x.engagement_score for x in snaps) / n
            row = (await s.execute(
                select(Template).where(Template.name == name)
            )).scalars().first()
            if row is None:
                row = Template(
                    name=name,
                    template_text="",
                    category="auto",
                )
                s.add(row)
            row.times_used = n
            row.avg_views = avg_views
            row.avg_likes = avg_likes
            row.avg_engagement = avg_eng
            row.last_used_at = datetime.now(tz=timezone.utc)
            updated += 1
        log.info("templates_updated", count=updated)
        return updated

"""Aggregate analytics reports (daily + weekly)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select

from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import EngagementSnapshot, Post

log = get_logger(__name__)


@dataclass(slots=True)
class PerformanceRow:
    key: str
    posts: int
    avg_views: float
    avg_likes: float
    avg_engagement: float


@dataclass(slots=True)
class PerformanceReport:
    period_hours: int
    total_posts: int
    avg_views: float
    avg_likes: float
    avg_comments: float
    avg_engagement: float
    by_template: list[PerformanceRow]
    by_ticker: list[PerformanceRow]
    by_hour: list[PerformanceRow]


async def _latest_snapshot_per_post(cutoff: datetime) -> list[tuple[Post, EngagementSnapshot]]:
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post).where(
                Post.published_at >= cutoff,
                Post.status == "published",
            )
        )).scalars().all()
        out: list[tuple[Post, EngagementSnapshot]] = []
        for p in posts:
            snap = (await s.execute(
                select(EngagementSnapshot)
                .where(EngagementSnapshot.post_id == p.id)
                .order_by(desc(EngagementSnapshot.captured_at))
                .limit(1)
            )).scalars().first()
            if snap:
                out.append((p, snap))
        return out


def _group(rows: list[tuple[Post, EngagementSnapshot]], key_fn) -> list[PerformanceRow]:
    buckets: dict[str, list[tuple[Post, EngagementSnapshot]]] = {}
    for r in rows:
        buckets.setdefault(key_fn(r), []).append(r)
    out: list[PerformanceRow] = []
    for k, items in buckets.items():
        n = len(items)
        avg_views = sum(s.view_count for _, s in items) / n
        avg_likes = sum(s.like_count for _, s in items) / n
        avg_eng = sum(s.engagement_score for _, s in items) / n
        out.append(PerformanceRow(k, n, avg_views, avg_likes, avg_eng))
    out.sort(key=lambda r: r.avg_engagement, reverse=True)
    return out


async def build_report(period_hours: int = 24) -> PerformanceReport:
    cutoff = datetime.now(tz=UTC) - timedelta(hours=period_hours)
    rows = await _latest_snapshot_per_post(cutoff)
    n = len(rows) or 1
    return PerformanceReport(
        period_hours=period_hours,
        total_posts=len(rows),
        avg_views=sum(s.view_count for _, s in rows) / n,
        avg_likes=sum(s.like_count for _, s in rows) / n,
        avg_comments=sum(s.comment_count for _, s in rows) / n,
        avg_engagement=sum(s.engagement_score for _, s in rows) / n,
        by_template=_group(rows, lambda r: r[0].template_name or "unknown"),
        by_ticker=_group(rows, lambda r: r[0].ticker.upper()),
        by_hour=_group(rows, lambda r: r[0].published_at.strftime("%H") if r[0].published_at else "??"),
    )


def report_to_markdown(rep: PerformanceReport) -> str:
    lines = [
        f"# Performance — last {rep.period_hours}h",
        "",
        f"- Posts published: **{rep.total_posts}**",
        f"- Avg views: **{rep.avg_views:,.0f}**",
        f"- Avg likes: **{rep.avg_likes:,.1f}**",
        f"- Avg comments: **{rep.avg_comments:,.1f}**",
        f"- Avg engagement: **{rep.avg_engagement:.4f}**",
        "",
        "## By template",
        "| Template | Posts | Avg views | Avg likes | Avg engagement |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in rep.by_template:
        lines.append(f"| {r.key} | {r.posts} | {r.avg_views:,.0f} | {r.avg_likes:,.1f} | {r.avg_engagement:.4f} |")
    lines += ["", "## Top tickers", "| Ticker | Posts | Avg views | Avg engagement |", "|---|---:|---:|---:|"]
    for r in rep.by_ticker[:10]:
        lines.append(f"| {r.key} | {r.posts} | {r.avg_views:,.0f} | {r.avg_engagement:.4f} |")
    lines += ["", "## Best hours (UTC)", "| Hour | Posts | Avg views | Avg engagement |", "|---|---:|---:|---:|"]
    for r in sorted(rep.by_hour, key=lambda x: -x.avg_engagement)[:8]:
        lines.append(f"| {r.key} | {r.posts} | {r.avg_views:,.0f} | {r.avg_engagement:.4f} |")
    return "\n".join(lines)

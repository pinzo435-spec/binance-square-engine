"""Image Strategy Engine — map template performance into a sampling distribution.

The visuals layer currently picks templates statically from playbooks.
This module computes per-template `weight` ∈ [0.3, 2.0] from real engagement
and exposes `pick_template(opportunity_tendency)` for a weighted random pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import select

from engine.config import get_settings
from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import (
    EngagementSnapshot,
    GrowthScoreSnapshot,
    ImagePerformance,
    Post,
)

log = get_logger(__name__)


# Default templates we know about — kept in sync with `engine.visuals.card_renderer`.
DEFAULT_TEMPLATES = ["trade_card", "chart_card", "warning_card", "profit_explosion"]
TENDENCY_FALLBACK = {
    0: "trade_card",      # neutral
    1: "profit_explosion",  # bullish
    2: "warning_card",      # bearish
}

_EWMA_ALPHA = 0.30


@dataclass(slots=True)
class TemplateChoice:
    name: str
    weight: float
    reason: str


async def update_from_posts(window_hours: int = 168) -> int:
    """Recompute `ImagePerformance` from posts in the last `window_hours`."""
    cutoff = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
    n = 0
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post)
            .where(Post.status == "success")
            .where(Post.published_at >= cutoff)
        )).scalars().all()
        for p in posts:
            tname = p.template_name or "trade_card"
            snap = (await s.execute(
                select(EngagementSnapshot)
                .where(EngagementSnapshot.post_id == p.id)
                .order_by(EngagementSnapshot.captured_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if snap is None:
                continue
            gs = (await s.execute(
                select(GrowthScoreSnapshot).where(GrowthScoreSnapshot.post_id == p.id)
            )).scalar_one_or_none()
            growth = gs.growth_score if gs is not None else 0.0

            row = (await s.execute(
                select(ImagePerformance).where(ImagePerformance.template_name == tname)
            )).scalar_one_or_none()
            if row is None:
                row = ImagePerformance(template_name=tname)
                s.add(row)
                await s.flush()
            samples = row.samples
            row.avg_views = (
                (row.avg_views * samples + snap.view_count) / (samples + 1)
                if samples < 5
                else (1 - _EWMA_ALPHA) * row.avg_views + _EWMA_ALPHA * snap.view_count
            )
            row.avg_likes = (
                (row.avg_likes * samples + snap.like_count) / (samples + 1)
                if samples < 5
                else (1 - _EWMA_ALPHA) * row.avg_likes + _EWMA_ALPHA * snap.like_count
            )
            row.avg_engagement = (
                (row.avg_engagement * samples + snap.engagement_score) / (samples + 1)
                if samples < 5
                else (1 - _EWMA_ALPHA) * row.avg_engagement + _EWMA_ALPHA * snap.engagement_score
            )
            row.avg_growth_score = (
                (row.avg_growth_score * samples + growth) / (samples + 1)
                if samples < 5
                else (1 - _EWMA_ALPHA) * row.avg_growth_score + _EWMA_ALPHA * growth
            )
            row.samples = samples + 1
            n += 1
    await recompute_weights()
    log.info("image_performance_updated", rows=n)
    return n


async def recompute_weights() -> dict[str, float]:
    async with session_scope() as s:
        rows = (await s.execute(select(ImagePerformance))).scalars().all()
        if not rows:
            return {}
        eligible = [r for r in rows if r.samples >= 3]
        if not eligible:
            for r in rows:
                r.weight = 1.0
            return {r.template_name: 1.0 for r in rows}
        scores = [r.avg_growth_score for r in eligible]
        max_s = max(scores)
        min_s = min(scores)
        spread = max_s - min_s
        out: dict[str, float] = {}
        for r in rows:
            if r.samples < 3 or spread <= 1e-6:
                r.weight = 1.0
            else:
                norm = (r.avg_growth_score - min_s) / spread
                r.weight = 0.3 + norm * 1.7
            out[r.template_name] = r.weight
    await _emit_yaml(out)
    return out


async def _emit_yaml(weights: dict[str, float]) -> Path:
    settings = get_settings()
    path = settings.runtime_dir / "adaptive_images.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "templates": {k: round(v, 3) for k, v in weights.items()},
    }
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


def load_weights() -> dict[str, float]:
    settings = get_settings()
    path = settings.runtime_dir / "adaptive_images.yaml"
    if not path.exists():
        return dict.fromkeys(DEFAULT_TEMPLATES, 1.0)
    try:
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tpls = body.get("templates") or {}
        return {t: float(tpls.get(t, 1.0)) for t in DEFAULT_TEMPLATES}
    except Exception as e:  # noqa: BLE001
        log.warning("adaptive_images_load_failed", error=str(e))
        return dict.fromkeys(DEFAULT_TEMPLATES, 1.0)


def pick_template(tendency: int = 0) -> TemplateChoice:
    """Weighted-random template pick. Tendency guides the default if no data."""
    import random

    weights = load_weights()
    # Filter to templates available in our renderer
    available = {k: v for k, v in weights.items() if k in DEFAULT_TEMPLATES}
    if not available:
        return TemplateChoice(TENDENCY_FALLBACK.get(tendency, "trade_card"), 1.0, "fallback")
    names = list(available.keys())
    vals = list(available.values())
    total = sum(vals)
    if total <= 0:
        return TemplateChoice(TENDENCY_FALLBACK.get(tendency, "trade_card"), 1.0, "fallback")
    r = random.random() * total
    acc = 0.0
    for name, w in zip(names, vals, strict=False):
        acc += w
        if r <= acc:
            return TemplateChoice(name, w, "adaptive")
    return TemplateChoice(names[-1], vals[-1], "adaptive")

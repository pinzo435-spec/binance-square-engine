"""Adaptive Scheduler — learn the best UTC hours to post and weight slots dynamically.

Reads from `PostingWindowPerformance` (one row per UTC hour) and produces:
    1. Per-hour `weight` ∈ [0.2, 2.0].
    2. A recommended *next-N-slots* schedule that the daemon can layer ON TOP
       of the static `daily_schedule.yaml` (we never overwrite the static
       schedule — we only add bonus slots in the best-performing windows and
       suppress slots in the worst).

The decision logic is deliberately conservative — we only trust a window
after at least `MIN_SAMPLES` posts have been observed there.
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
from engine.models import GrowthScoreSnapshot, Post, PostingWindowPerformance

log = get_logger(__name__)


MIN_SAMPLES = 4
_EWMA_ALPHA = 0.30


@dataclass(slots=True)
class WindowVerdict:
    hour_utc: int
    samples: int
    weight: float
    rank: int          # 0 = best, 23 = worst
    verdict: str       # "promote" / "suppress" / "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# Updater
# ─────────────────────────────────────────────────────────────────────────────


async def update_from_posts(window_hours: int = 336) -> int:
    """Recompute per-hour aggregates from posts within the last `window_hours` (14d default)."""
    cutoff = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
    n = 0
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post)
            .where(Post.status == "success")
            .where(Post.published_at >= cutoff)
        )).scalars().all()
        # Pre-fetch growth-score map
        gs_rows = (await s.execute(
            select(GrowthScoreSnapshot)
        )).scalars().all()
        gs_by_post = {g.post_id: g for g in gs_rows}

        for p in posts:
            if p.published_at is None:
                continue
            h = p.published_at.hour
            gs = gs_by_post.get(p.id)
            if gs is None:
                continue
            row = (await s.execute(
                select(PostingWindowPerformance).where(PostingWindowPerformance.hour_utc == h)
            )).scalar_one_or_none()
            if row is None:
                row = PostingWindowPerformance(hour_utc=h)
                s.add(row)
                await s.flush()
            samples = row.samples
            row.avg_views = (
                (row.avg_views * samples + gs.last_view_count) / (samples + 1)
                if samples < 5
                else (1 - _EWMA_ALPHA) * row.avg_views + _EWMA_ALPHA * gs.last_view_count
            )
            row.avg_growth_score = (
                (row.avg_growth_score * samples + gs.growth_score) / (samples + 1)
                if samples < 5
                else (1 - _EWMA_ALPHA) * row.avg_growth_score + _EWMA_ALPHA * gs.growth_score
            )
            row.samples = samples + 1
            n += 1
    await recompute_weights()
    log.info("window_performance_updated", rows=n)
    return n


async def recompute_weights() -> list[WindowVerdict]:
    async with session_scope() as s:
        rows = (await s.execute(select(PostingWindowPerformance))).scalars().all()
        if len(rows) < 4:
            return []
        # Rank by growth score (ignore rows with too few samples).
        eligible = [r for r in rows if r.samples >= MIN_SAMPLES]
        if not eligible:
            return []
        sorted_rows = sorted(eligible, key=lambda r: r.avg_growth_score, reverse=True)
        max_g = sorted_rows[0].avg_growth_score or 0.0
        min_g = sorted_rows[-1].avg_growth_score or 0.0
        spread = max_g - min_g
        verdicts: list[WindowVerdict] = []
        for rank, r in enumerate(sorted_rows):
            if spread <= 1e-6:
                w = 1.0
            else:
                norm = (r.avg_growth_score - min_g) / spread
                w = 0.2 + norm * 1.8
            r.weight = w
            if rank < len(sorted_rows) * 0.25:
                verdict = "promote"
            elif rank > len(sorted_rows) * 0.75:
                verdict = "suppress"
            else:
                verdict = "neutral"
            verdicts.append(WindowVerdict(r.hour_utc, r.samples, w, rank, verdict))
    await _emit_yaml(verdicts)
    log.info(
        "window_weights_recomputed",
        promote=sum(1 for v in verdicts if v.verdict == "promote"),
        suppress=sum(1 for v in verdicts if v.verdict == "suppress"),
    )
    return verdicts


async def _emit_yaml(verdicts: list[WindowVerdict]) -> Path:
    settings = get_settings()
    path = settings.runtime_dir / "adaptive_schedule.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "windows": [
            {
                "hour_utc": v.hour_utc,
                "samples": v.samples,
                "weight": round(v.weight, 3),
                "rank": v.rank,
                "verdict": v.verdict,
            }
            for v in verdicts
        ],
    }
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


def load_weights() -> dict[int, float]:
    settings = get_settings()
    path = settings.runtime_dir / "adaptive_schedule.yaml"
    if not path.exists():
        return dict.fromkeys(range(24), 1.0)
    try:
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        wins = body.get("windows") or []
        out = dict.fromkeys(range(24), 1.0)
        for w in wins:
            h = int(w.get("hour_utc", -1))
            if 0 <= h < 24:
                out[h] = float(w.get("weight", 1.0))
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("adaptive_schedule_load_failed", error=str(e))
        return dict.fromkeys(range(24), 1.0)


def is_promoted(hour_utc: int) -> bool:
    """True if `hour_utc` is in the top quartile of historical performance."""
    return load_weights().get(hour_utc, 1.0) >= 1.4


def is_suppressed(hour_utc: int) -> bool:
    return load_weights().get(hour_utc, 1.0) <= 0.6

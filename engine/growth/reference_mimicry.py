"""Reference Mimicry — extract *patterns* (not text) from the reference creator.

Reads `ReferencePost` rows (populated by `signal.reference_feed`) and produces
a `reference_patterns.yaml` describing the creator's rhythm:

    - cadence_per_day      : avg posts/day over the last 14 days
    - typical_burst_size   : avg #posts in clusters that fire within 5 min
    - peak_hours_utc       : top 3 UTC hours by historical post count
    - avg_body_length      : average body_text length
    - emoji_density        : emojis-per-100-chars (median)
    - ticker_diversity     : distinct tickers / total posts
    - top_tickers          : top 10 tickers with frequency
    - tone_mix             : { hook_category -> share }

The hook generator and adaptive scheduler can consult this file to *nudge*
toward (but not copy) the reference creator's behaviour.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import select

from engine.config import get_settings
from engine.db import session_scope
from engine.growth.hook_intelligence import classify
from engine.logging_setup import get_logger
from engine.models import ReferencePost

log = get_logger(__name__)


_EMOJI_RE = None  # lazy compiled


def _emoji_density(text: str) -> float:
    """Crude emoji-per-100-chars density using the regex pattern.

    We use a tiny built-in approach because the `emoji` package is heavy.
    """
    if not text:
        return 0.0
    # Emojis are largely in 0x1F300+ block — count surrogate-pair-y chars.
    count = sum(1 for c in text if ord(c) >= 0x1F000)
    return count * 100 / max(len(text), 1)


@dataclass(slots=True)
class ReferencePatterns:
    cadence_per_day: float = 0.0
    typical_burst_size: float = 0.0
    peak_hours_utc: list[int] = field(default_factory=list)
    avg_body_length: float = 0.0
    median_emoji_density: float = 0.0
    ticker_diversity: float = 0.0
    top_tickers: list[tuple[str, int]] = field(default_factory=list)
    tone_mix: dict[str, float] = field(default_factory=dict)
    sample_size: int = 0


async def extract(window_hours: int = 24 * 14) -> ReferencePatterns | None:
    """Walk the cached reference posts and emit a patterns summary."""
    cutoff = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
    async with session_scope() as s:
        rows = (await s.execute(
            select(ReferencePost)
            .where(ReferencePost.published_at >= cutoff)
        )).scalars().all()
    if len(rows) < 5:
        log.info("reference_patterns_skipped", reason="insufficient_data", n=len(rows))
        return None

    by_hour: Counter[int] = Counter()
    lengths: list[int] = []
    densities: list[float] = []
    tickers_total: list[str] = []
    tone_counter: Counter[str] = Counter()

    # Sort posts chronologically to detect bursts (≥2 posts within 5 min).
    sorted_rows = sorted(rows, key=lambda r: r.published_at or datetime.min)

    bursts: list[int] = []
    cur_burst = 1
    prev_t: datetime | None = None
    for r in sorted_rows:
        if r.published_at is None:
            continue
        by_hour[r.published_at.hour] += 1
        lengths.append(len(r.body_text or ""))
        densities.append(_emoji_density(r.body_text or ""))
        tickers_total.extend(r.tickers or [])
        tone_counter[classify(r.body_text or "").category] += 1
        if prev_t is not None and (r.published_at - prev_t) <= timedelta(minutes=5):
            cur_burst += 1
        else:
            if cur_burst >= 2:
                bursts.append(cur_burst)
            cur_burst = 1
        prev_t = r.published_at
    if cur_burst >= 2:
        bursts.append(cur_burst)

    days_span = max(1.0, (sorted_rows[-1].published_at - sorted_rows[0].published_at).total_seconds() / 86400)
    cadence = len(rows) / days_span

    densities.sort()
    median_d = densities[len(densities) // 2] if densities else 0.0
    tone_total = sum(tone_counter.values()) or 1
    tone_mix = {k: round(v / tone_total, 3) for k, v in tone_counter.items()}

    out = ReferencePatterns(
        cadence_per_day=round(cadence, 2),
        typical_burst_size=round(sum(bursts) / len(bursts), 2) if bursts else 1.0,
        peak_hours_utc=[h for h, _ in by_hour.most_common(3)],
        avg_body_length=round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
        median_emoji_density=round(median_d, 2),
        ticker_diversity=round(len(set(tickers_total)) / max(1, len(rows)), 3),
        top_tickers=Counter(tickers_total).most_common(10),
        tone_mix=tone_mix,
        sample_size=len(rows),
    )
    await _emit_yaml(out)
    log.info(
        "reference_patterns_extracted",
        cadence=out.cadence_per_day, bursts=out.typical_burst_size, n=out.sample_size,
    )
    return out


async def _emit_yaml(p: ReferencePatterns) -> Path:
    settings = get_settings()
    path = settings.runtime_dir / "reference_patterns.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "sample_size": p.sample_size,
        "cadence_per_day": p.cadence_per_day,
        "typical_burst_size": p.typical_burst_size,
        "peak_hours_utc": p.peak_hours_utc,
        "avg_body_length": p.avg_body_length,
        "median_emoji_density": p.median_emoji_density,
        "ticker_diversity": p.ticker_diversity,
        "top_tickers": [{"ticker": t, "count": c} for t, c in p.top_tickers],
        "tone_mix": p.tone_mix,
    }
    path.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def load_patterns() -> dict | None:
    settings = get_settings()
    path = settings.runtime_dir / "reference_patterns.yaml"
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("reference_patterns_load_failed", error=str(e))
        return None


# Helper used by other modules: "is this hour one of the reference's peak hours?"
def is_reference_peak_hour(hour_utc: int) -> bool:
    p = load_patterns()
    if not p:
        return False
    return hour_utc in (p.get("peak_hours_utc") or [])


# Suggested target cadence per day, derived from the reference (×1.0 by default).
def suggested_cadence(multiplier: float = 1.0) -> float | None:
    p = load_patterns()
    if not p:
        return None
    base = float(p.get("cadence_per_day") or 0.0)
    return round(base * multiplier, 2) if base else None


_ = defaultdict  # silence unused-import if simplified above

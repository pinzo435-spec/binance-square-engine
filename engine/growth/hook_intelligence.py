"""Hook Intelligence — classify hooks into emotional categories and learn weights.

Stage 1: classify each existing Post into one of:
    curiosity, greed, fear, contrarian, humor, self_deprecation, neutral.
Stage 2: aggregate engagement per category into `HookPerformance`.
Stage 3: emit `data/runtime/adaptive_hooks.yaml` containing the current
         weight distribution — read by `engine.content.hook_generator` at
         request time to bias which template family it samples from.

We deliberately use a tiny rule-based classifier (lexicon-driven) rather than
an LLM call: it must be fast, free, and deterministic. The lexicons live in
constants at the top of this file — the self-optimiser can tune the weights
but not the lexicons (those are the editorial DNA).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select

from engine.config import get_settings
from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import (
    EngagementSnapshot,
    GrowthScoreSnapshot,
    HookPerformance,
    Post,
)

log = get_logger(__name__)


HOOK_CATEGORIES: list[str] = [
    "curiosity",
    "greed",
    "fear",
    "contrarian",
    "humor",
    "self_deprecation",
    "neutral",
]


# Arabic + English lexicons. Order matters: first hit wins.
_LEXICONS: dict[str, list[str]] = {
    "self_deprecation": [
        "بعت", "بكير", "خسرت", "ندمت", "غبي", "هبلت", "ضيعت", "كنت لقمت",
        "ما لحقت", "لو رجع الزمن", "loss porn", "i sold",
    ],
    "greed": [
        "انفجار", "انفجرت", "ضربة", "ربح", "ارباح", "10x", "100x", "moonshot",
        "to the moon", "🤑", "💰", "pump", "x10", "x100", "+200%", "+500%",
        "ضاعفت", "تضاعف",
    ],
    "fear": [
        "خطر", "احذر", "ينهار", "انهار", "خسارة", "تحذير", "warning", "danger",
        "crash", "rugpull", "rug pull", "scam", "🚨", "⚠️", "🔥",
    ],
    "contrarian": [
        "عكس", "لا تشتري", "do not buy", "don't buy", "everyone wrong",
        "كذبة", "خدعة", "وهم", "myth",
    ],
    "humor": [
        "😂", "🤣", "ههه", "وش هالشي", "wtf", "lmao", "lol", "هبال",
        "مدمن", "كله سنع",
    ],
    "curiosity": [
        "تعرفون", "تعرف", "شفتوا", "هل تعلم", "وش الجاي", "did you know",
        "guess", "لاحظت", "لاحظتو", "؟", "?",
    ],
}


@dataclass(slots=True)
class HookClassification:
    category: str
    confidence: float  # 0..1, how many lexicon hits


def classify(text: str) -> HookClassification:
    """Return the highest-scoring category for `text` (or 'neutral')."""
    if not text:
        return HookClassification("neutral", 0.0)
    lower = text.lower()
    best_cat = "neutral"
    best_hits = 0
    for cat in HOOK_CATEGORIES:
        if cat == "neutral":
            continue
        hits = sum(1 for kw in _LEXICONS.get(cat, []) if kw in lower)
        if hits > best_hits:
            best_hits = hits
            best_cat = cat
    if best_hits == 0:
        return HookClassification("neutral", 0.0)
    # Normalise by lexicon size so larger lexicons don't dominate
    conf = min(1.0, best_hits / max(1, len(_LEXICONS[best_cat]) ** 0.5))
    return HookClassification(best_cat, conf)


# ─────────────────────────────────────────────────────────────────────────────
# EWMA accumulator — runs every self-optimisation cycle.
# ─────────────────────────────────────────────────────────────────────────────


_EWMA_ALPHA = 0.25   # weight for newest sample


def _update_ewma(prev_avg: float, new_value: float, samples_before: int) -> float:
    """EWMA that warms up linearly for the first 5 samples then becomes proper EWMA."""
    if samples_before < 5:
        return (prev_avg * samples_before + new_value) / max(1, samples_before + 1)
    return (1 - _EWMA_ALPHA) * prev_avg + _EWMA_ALPHA * new_value


async def update_from_recent_posts(window_hours: int = 168) -> int:
    """Walk every post in the last `window_hours`, classify it, accumulate into HookPerformance.

    Returns the number of posts processed.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
    n = 0
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post)
            .where(Post.status == "success")
            .where(Post.published_at.is_not(None))
            .where(Post.published_at >= cutoff)
        )).scalars().all()
        for p in posts:
            cat = classify(p.body_text).category

            # Latest engagement
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
                select(HookPerformance).where(HookPerformance.category == cat)
            )).scalar_one_or_none()
            if row is None:
                row = HookPerformance(category=cat)
                s.add(row)
                await s.flush()

            row.avg_views      = _update_ewma(row.avg_views, snap.view_count, row.samples)
            row.avg_likes      = _update_ewma(row.avg_likes, snap.like_count, row.samples)
            row.avg_engagement = _update_ewma(row.avg_engagement, snap.engagement_score, row.samples)
            row.avg_growth_score = _update_ewma(row.avg_growth_score, growth, row.samples)
            row.samples += 1
            n += 1
    # Recompute the weights *after* all categories have current numbers.
    await recompute_weights()
    log.info("hook_performance_updated", posts=n)
    return n


async def recompute_weights() -> dict[str, float]:
    """Normalise growth scores across categories into selector weights ∈ [0.1, 2.0]."""
    async with session_scope() as s:
        rows = (await s.execute(select(HookPerformance))).scalars().all()
        if not rows:
            return {}
        scores = [r.avg_growth_score for r in rows]
        max_score = max(scores)
        min_score = min(scores)
        spread = max_score - min_score
        weights: dict[str, float] = {}
        for r in rows:
            if spread <= 1e-6 or r.samples < 3:
                # Not enough signal — keep neutral 1.0
                r.weight = 1.0
            else:
                normalised = (r.avg_growth_score - min_score) / spread  # 0..1
                # Map to [0.4, 2.0] — never fully disable a category
                r.weight = 0.4 + normalised * 1.6
            weights[r.category] = r.weight
    await _emit_adaptive_yaml(weights)
    return weights


async def _emit_adaptive_yaml(weights: dict[str, float]) -> Path:
    """Write `data/runtime/adaptive_hooks.yaml` consumed by hook_generator."""
    settings = get_settings()
    out_path = settings.runtime_dir / "adaptive_hooks.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "generated_at": _now_iso(),
        "categories": {cat: round(weights.get(cat, 1.0), 3) for cat in HOOK_CATEGORIES},
    }
    out_path.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    log.info("adaptive_hooks_written", path=str(out_path), categories=len(weights))
    return out_path


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def load_weights() -> dict[str, float]:
    """Read the latest weights file; falls back to uniform if missing."""
    settings = get_settings()
    path = settings.runtime_dir / "adaptive_hooks.yaml"
    if not path.exists():
        return dict.fromkeys(HOOK_CATEGORIES, 1.0)
    try:
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cats = body.get("categories") or {}
        return {c: float(cats.get(c, 1.0)) for c in HOOK_CATEGORIES}
    except Exception as e:  # noqa: BLE001
        log.warning("adaptive_hooks_load_failed", error=str(e))
        return dict.fromkeys(HOOK_CATEGORIES, 1.0)


def softmax_pick(weights: dict[str, float], temperature: float = 1.0) -> str:
    """Sample a category according to softmax(weights). Used by the hook generator.

    The temperature lets the self-optimiser anneal: high T (>1.5) = explore,
    low T (<0.5) = exploit. Default 1.0 = balanced.
    """
    import random

    keys = list(weights.keys())
    vals = [weights[k] / max(temperature, 1e-3) for k in keys]
    m = max(vals)
    exps = [math.exp(v - m) for v in vals]
    s = sum(exps)
    if s <= 0:
        return random.choice(keys)
    probs = [e / s for e in exps]
    r = random.random()
    acc = 0.0
    for k, p in zip(keys, probs, strict=False):
        acc += p
        if r <= acc:
            return k
    return keys[-1]

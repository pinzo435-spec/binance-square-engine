"""Growth Intelligence Layer (Phase D).

This package elevates the engine from "publishing automation" into an
autonomous *growth operator* whose explicit objective is to land posts in the
Binance Square "Top 10".

The layer has 8 specialised modules. They share the same data substrate
(rolling-aggregate tables in `engine.models`) but each owns one decision
dimension:

    growth_scorer.py        -> per-post composite score (the optimisation target)
    hook_intelligence.py    -> hook-category classifier + adaptive weights
    coin_priority_engine.py -> ticker selection (live signal x historical pull)
    adaptive_scheduler.py   -> learn the best posting windows over time
    image_strategy_engine.py-> template selector bias from real engagement
    reference_mimicry.py    -> pattern-extract from the reference account
    safety_layer.py         -> duplicate / repetition / cooldown guards
    self_optimizer.py       -> 24h orchestrator that runs the whole loop

Wiring:
    - The scheduler runs `self_optimizer.run_cycle()` once every 24h.
    - The publisher consults `safety_layer.should_block()` before each post.
    - The hook generator consults `hook_intelligence.suggested_category()`.
    - The visuals layer consults `image_strategy_engine.choose_template()`.
    - The opportunity ranker consults `coin_priority_engine.score()`.
"""

from __future__ import annotations

__all__ = [
    "growth_scorer",
    "hook_intelligence",
    "coin_priority_engine",
    "adaptive_scheduler",
    "image_strategy_engine",
    "reference_mimicry",
    "safety_layer",
    "self_optimizer",
]

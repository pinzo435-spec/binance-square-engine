"""Refreshes the few-shot examples used by `hook_generator`.

Strategy: take our own top-performing recent posts (by engagement_score) and
mix them into the JSON example pool so the LLM steers toward what's working.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import desc, select

from engine.db import session_scope
from engine.logging_setup import get_logger
from engine.models import EngagementSnapshot, Post

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES_PATH = ROOT / "prompts" / "few_shot_examples.json"
EXAMPLES_BACKUP_PATH = ROOT / "prompts" / "few_shot_examples.baseline.json"

CASHTAG = re.compile(r"\$([A-Z][A-Z0-9]{1,15})")


async def collect_top_posts(days: int = 14, top_n: int = 20) -> list[Post]:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post).where(
                Post.published_at >= cutoff,
                Post.status == "published",
            )
        )).scalars().all()
        scored: list[tuple[float, Post]] = []
        for p in posts:
            snap = (await s.execute(
                select(EngagementSnapshot)
                .where(EngagementSnapshot.post_id == p.id)
                .order_by(desc(EngagementSnapshot.captured_at))
                .limit(1)
            )).scalars().first()
            if snap is None:
                continue
            scored.append((snap.engagement_score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored[:top_n]]


def _post_to_example(p: Post) -> dict:
    ticker = "BTC"
    m = CASHTAG.search(p.body_text)
    if m:
        ticker = m.group(1)
    return {
        "trigger": "STEADY",
        "template_hint": p.template_name or "big_picture",
        "ticker": ticker,
        "tendency": p.tendency,
        "context": "from_own_top_performers",
        "output": p.body_text,
    }


async def refresh_examples(*, days: int = 14, max_examples: int = 12) -> int:
    if not EXAMPLES_BACKUP_PATH.exists():
        EXAMPLES_BACKUP_PATH.write_text(EXAMPLES_PATH.read_text(encoding="utf-8"))

    baseline = json.loads(EXAMPLES_BACKUP_PATH.read_text(encoding="utf-8"))
    top = await collect_top_posts(days=days, top_n=max_examples)
    new_examples = [_post_to_example(p) for p in top]
    # Keep half baseline / half new to avoid drift to off-style content
    keep_baseline = baseline[: max_examples // 2 + 1]
    merged = (new_examples + keep_baseline)[:max_examples]
    if not merged:
        log.info("prompt_update_skipped_no_data")
        return 0
    EXAMPLES_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    log.info("prompt_examples_refreshed", new=len(new_examples), kept=len(keep_baseline))
    return len(merged)

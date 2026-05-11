"""Assembles a publishable post from an Opportunity + generated hook + images."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from engine.content.cashtag_resolver import CashtagResolver
from engine.content.hook_generator import HookGenerator, HookRequest
from engine.content.tendency_classifier import classify
from engine.logging_setup import get_logger
from engine.signal.opportunity_ranker import RankedOpportunity

log = get_logger(__name__)


@dataclass(slots=True)
class AssembledPost:
    ticker: str
    body_text: str
    tendency: int
    trading_pairs: list[str]
    template_name: str
    image_paths: list[Path] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)


class PostAssembler:
    def __init__(self) -> None:
        self.hook_gen = HookGenerator()
        self.resolver = CashtagResolver()

    async def assemble(self, opp: RankedOpportunity) -> AssembledPost:
        tendency = classify(opp)
        ctx_parts: list[str] = []
        if opp.change_1h_pct is not None:
            ctx_parts.append(f"تغير ساعة {opp.change_1h_pct:+.1f}%")
        if opp.change_24h_pct is not None:
            ctx_parts.append(f"تغير يوم {opp.change_24h_pct:+.1f}%")
        if opp.binance_trend_hashtag:
            ctx_parts.append(f"ترند #{opp.binance_trend_hashtag}")
        context = " | ".join(ctx_parts)

        hook = await self.hook_gen.generate(
            HookRequest(
                ticker=opp.ticker,
                trigger=opp.trigger,
                template_hint=opp.suggested_template,
                tendency=tendency,
                context=context,
            )
        )

        pair = await self.resolver.resolve(opp.ticker)
        pairs = [pair] if pair else []

        log.info(
            "post_assembled",
            ticker=opp.ticker,
            tendency=tendency,
            pair=pair,
            chars=len(hook.text),
        )
        return AssembledPost(
            ticker=opp.ticker,
            body_text=hook.text,
            tendency=tendency,
            trading_pairs=pairs,
            template_name=opp.suggested_template,
        )

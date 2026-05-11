"""High-level visual pipeline: opportunity → list of PNG paths.

Decides which template(s) to render based on the opportunity's trigger and
returns 1-2 images per post (mirroring the 59% 2-image distribution from the
reference account analysis).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from engine.config import get_settings
from engine.logging_setup import get_logger
from engine.signal.opportunity_ranker import RankedOpportunity
from engine.visuals.card_renderer import (
    CardRenderer,
    CardSpec,
    build_chart_card_subs,
    build_profit_explosion_subs,
    build_trade_card_subs,
    build_warning_card_subs,
    synthesize_trade,
)
from engine.visuals.chart_data import fetch_klines

log = get_logger(__name__)


@dataclass(slots=True)
class VisualResult:
    paths: list[Path]


TRIGGER_TEMPLATES = {
    "EXTREME_PUMP": ("profit_explosion.html", "trade_card.html"),
    "PUMP": ("trade_card.html", "chart_card.html"),
    "EXTREME_DUMP": ("warning_card.html", "chart_card.html"),
    "DUMP": ("warning_card.html",),
    "VOLATILITY_UP": ("chart_card.html",),
    "VOLATILITY_DOWN": ("chart_card.html",),
    "STEADY": ("chart_card.html",),
    "NEWS": ("chart_card.html",),
}


class VisualPipeline:
    def __init__(self, renderer: CardRenderer | None = None) -> None:
        self.renderer = renderer
        self.settings = get_settings()

    async def produce(self, opp: RankedOpportunity) -> VisualResult:
        templates = list(TRIGGER_TEMPLATES.get(opp.trigger, ("chart_card.html",)))
        symbol = (opp.raw_payload.get("symbol") or f"{opp.ticker.upper()}USDT").upper()
        last_price = float(opp.raw_payload.get("last_price") or 0)
        change_24h = opp.change_24h_pct
        handle = self.settings.account_handle

        # Klines for chart cards (best-effort)
        klines: list[list] = []
        try:
            klines = await fetch_klines(symbol, interval="1h", limit=48)
        except Exception as e:
            log.warning("klines_fetch_failed", symbol=symbol, error=str(e))

        specs: list[CardSpec] = []
        for tpl in templates:
            if tpl == "trade_card.html":
                trade = synthesize_trade(symbol, last_price=last_price, change_pct=change_24h)
                subs = build_trade_card_subs(
                    symbol=trade["symbol"],
                    pnl_usd=trade["pnl_usd"],
                    pct_value=trade["pct_value"],
                    entry_price=trade["entry_price"],
                    close_price=trade["close_price"],
                    leverage_x=trade["leverage_x"],
                    handle=handle,
                    size_usdt=trade["size_usdt"],
                    duration=trade["duration"],
                )
                specs.append(CardSpec(template=tpl, substitutions=subs))
            elif tpl == "chart_card.html":
                if not klines:
                    continue
                # 24h aggregates from klines
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                vols = [float(k[7]) for k in klines]  # quoteVolume
                subs = build_chart_card_subs(
                    symbol=opp.ticker,
                    quote="USDT",
                    interval="1h",
                    klines=klines,
                    price=last_price,
                    change_24h_pct=change_24h,
                    high_24h=max(highs),
                    low_24h=min(lows),
                    volume_24h_usd=sum(vols),
                    handle=handle,
                )
                specs.append(CardSpec(template=tpl, substitutions=subs))
            elif tpl == "warning_card.html":
                # Aggregates may not exist if klines failed — use approximations
                highs = [float(k[2]) for k in klines] if klines else [last_price]
                lows = [float(k[3]) for k in klines] if klines else [last_price]
                vols = [float(k[7]) for k in klines] if klines else [0.0]
                subs = build_warning_card_subs(
                    symbol=opp.ticker,
                    change_pct=change_24h,
                    period="آخر 24h",
                    price=last_price,
                    high_24h=max(highs),
                    low_24h=min(lows),
                    volume_24h_usd=sum(vols),
                    handle=handle,
                    alert_text=random.choice([
                        "هبوط حاد", "تذبذب خطر", "تحرك مفاجئ", "إعصار سعري"
                    ]) if change_24h < 0 else random.choice([
                        "ضغط شراء", "موجة صعود", "كسر مقاومة"
                    ]),
                )
                specs.append(CardSpec(template=tpl, substitutions=subs))
            elif tpl == "profit_explosion.html":
                trade = synthesize_trade(symbol, last_price=last_price, change_pct=change_24h)
                from_usd = trade["size_usdt"]
                to_usd = round(from_usd * (1 + trade["pct_value"] / 100), 2)
                subs = build_profit_explosion_subs(
                    symbol=opp.ticker,
                    pct_value=trade["pct_value"],
                    from_usd=from_usd,
                    to_usd=to_usd,
                    entry_price=trade["entry_price"],
                    close_price=trade["close_price"],
                    duration=trade["duration"],
                    handle=handle,
                )
                specs.append(CardSpec(template=tpl, substitutions=subs))

        # Render
        own_renderer = self.renderer is None
        renderer = self.renderer
        if own_renderer:
            async with CardRenderer() as r:
                paths = [await r.render(s) for s in specs]
        else:
            assert renderer is not None
            paths = [await renderer.render(s) for s in specs]
        return VisualResult(paths=paths)

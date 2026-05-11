"""Maps a trigger + market context to a Binance Square tendency code.

Tendency codes used by Binance Square:
    0 — neutral
    1 — bullish (صاعد)
    2 — bearish (هابط)
"""

from __future__ import annotations

from engine.signal.opportunity_ranker import RankedOpportunity


_BULL_TRIGGERS = {"PUMP", "EXTREME_PUMP", "VOLATILITY_UP", "BINANCE_LIST", "PARTNERSHIP", "ATH"}
_BEAR_TRIGGERS = {"DUMP", "EXTREME_DUMP", "VOLATILITY_DOWN", "HACK", "BINANCE_DELIST", "REGULATORY"}


def classify(opp: RankedOpportunity) -> int:
    """Return tendency code: 0/1/2."""
    if opp.suggested_tendency in (1, 2):
        return opp.suggested_tendency
    if opp.trigger in _BULL_TRIGGERS:
        return 1
    if opp.trigger in _BEAR_TRIGGERS:
        return 2
    if opp.change_1h_pct is not None:
        if opp.change_1h_pct > 1.5:
            return 1
        if opp.change_1h_pct < -1.5:
            return 2
    if opp.change_24h_pct > 3:
        return 1
    if opp.change_24h_pct < -3:
        return 2
    return 0

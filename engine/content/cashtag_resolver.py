"""Resolves a base ticker (e.g. `SOL`) to a tradable Binance pair (e.g. `SOLUSDT`).

Binance Square's `tradingPairs` field expects the full pair symbol. We cache
the list of exchange-listed symbols and pick the most liquid quote match.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from engine.logging_setup import get_logger

log = get_logger(__name__)

EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
CACHE_TTL_SECONDS = 3600

# Order matters: USDT is preferred (deepest liquidity on Binance), then BUSD/FDUSD/BTC.
QUOTE_PRIORITY = ("USDT", "FDUSD", "BUSD", "USDC", "BTC", "ETH")


class CashtagResolver:
    def __init__(self) -> None:
        self._symbols_by_base: dict[str, list[str]] = {}
        self._loaded_at: float = 0.0

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(EXCHANGE_INFO_URL)
            r.raise_for_status()
            data: dict[str, Any] = r.json()
        by_base: dict[str, list[str]] = {}
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            base = s.get("baseAsset", "").upper()
            quote = s.get("quoteAsset", "").upper()
            if not base or not quote:
                continue
            by_base.setdefault(base, []).append(quote)
        self._symbols_by_base = by_base
        self._loaded_at = time.time()
        log.info("exchange_info_loaded", base_count=len(by_base))

    async def resolve(self, ticker: str) -> str | None:
        if time.time() - self._loaded_at > CACHE_TTL_SECONDS or not self._symbols_by_base:
            try:
                await self._refresh()
            except Exception as e:
                log.warning("exchange_info_refresh_failed", error=str(e))
                if not self._symbols_by_base:
                    # Fallback: try USDT pair blindly
                    return f"{ticker.upper()}USDT"
        base = ticker.upper().lstrip("$")
        quotes = self._symbols_by_base.get(base, [])
        if not quotes:
            return None
        for q in QUOTE_PRIORITY:
            if q in quotes:
                return f"{base}{q}"
        return f"{base}{quotes[0]}"

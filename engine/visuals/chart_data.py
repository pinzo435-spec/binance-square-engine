"""Fetches recent kline (candlestick) data from Binance public API."""

from __future__ import annotations

from typing import Any

import httpx

from engine.logging_setup import get_logger

log = get_logger(__name__)

KLINES_URL = "https://api.binance.com/api/v3/klines"


async def fetch_klines(symbol: str, *, interval: str = "1h", limit: int = 48) -> list[list[Any]]:
    """Returns Binance klines: [openTime, open, high, low, close, vol, closeTime, ...]"""
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(KLINES_URL, params={"symbol": symbol, "interval": interval, "limit": limit})
        r.raise_for_status()
        return r.json()

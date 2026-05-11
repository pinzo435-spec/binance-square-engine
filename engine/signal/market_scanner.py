"""Binance Spot & Futures market scanner.

Pulls 24h ticker statistics from Binance public endpoints, filters by volume
and % change, and emits raw market signals (top movers + volume spikes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from engine.logging_setup import get_logger

log = get_logger(__name__)

SPOT_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Quote currencies we care about (USDT-margined pairs dominate Binance Square)
ALLOWED_QUOTES = ("USDT",)


@dataclass(slots=True)
class MarketSignal:
    """A single market observation about a trading pair."""

    symbol: str  # full pair, e.g. BTCUSDT
    ticker: str  # base only, e.g. BTC
    venue: str  # spot | futures
    last_price: float
    price_change_pct_24h: float
    price_change_pct_1h: float | None  # computed from klines
    volume_usd_24h: float
    quote_volume: float
    trade_count: int
    high_24h: float
    low_24h: float
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pump(self) -> bool:
        return self.price_change_pct_24h >= 8.0

    @property
    def is_dump(self) -> bool:
        return self.price_change_pct_24h <= -8.0

    @property
    def is_hot_1h(self) -> bool:
        return self.price_change_pct_1h is not None and abs(self.price_change_pct_1h) >= 5.0


class MarketScanner:
    """Fetches 24h tickers and computes derived signals."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "MarketScanner":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                http2=True,
                headers={"User-Agent": "binance-square-engine/0.1"},
            )
            self._owns_client = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _get_json(self, url: str, params: dict | None = None) -> Any:
        assert self._client is not None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                r = await self._client.get(url, params=params)
                r.raise_for_status()
                return r.json()

    async def fetch_spot_tickers(self) -> list[dict]:
        data = await self._get_json(SPOT_TICKER_URL)
        return [t for t in data if any(t["symbol"].endswith(q) for q in ALLOWED_QUOTES)]

    async def fetch_futures_tickers(self) -> list[dict]:
        data = await self._get_json(FUTURES_TICKER_URL)
        return [t for t in data if any(t["symbol"].endswith(q) for q in ALLOWED_QUOTES)]

    async def fetch_1h_change(self, symbol: str) -> float | None:
        """Compute % change over the last 1 hour using 60m kline."""
        try:
            data = await self._get_json(
                SPOT_KLINES_URL, params={"symbol": symbol, "interval": "1h", "limit": 1}
            )
            if not data:
                return None
            open_, close = float(data[0][1]), float(data[0][4])
            if open_ == 0:
                return None
            return (close - open_) / open_ * 100.0
        except Exception as e:  # pragma: no cover - best-effort
            log.debug("kline_fetch_failed", symbol=symbol, error=str(e))
            return None

    @staticmethod
    def _parse_ticker(t: dict, venue: str) -> MarketSignal:
        symbol = t["symbol"]
        for q in ALLOWED_QUOTES:
            if symbol.endswith(q):
                ticker = symbol[: -len(q)]
                break
        else:
            ticker = symbol
        return MarketSignal(
            symbol=symbol,
            ticker=ticker,
            venue=venue,
            last_price=float(t.get("lastPrice", 0) or 0),
            price_change_pct_24h=float(t.get("priceChangePercent", 0) or 0),
            price_change_pct_1h=None,
            volume_usd_24h=float(t.get("quoteVolume", 0) or 0),
            quote_volume=float(t.get("quoteVolume", 0) or 0),
            trade_count=int(t.get("count", 0) or 0),
            high_24h=float(t.get("highPrice", 0) or 0),
            low_24h=float(t.get("lowPrice", 0) or 0),
            raw=t,
        )

    async def scan(
        self,
        *,
        min_quote_volume: float = 1_000_000.0,
        top_n: int = 50,
        enrich_with_1h: bool = True,
    ) -> list[MarketSignal]:
        """Return the most interesting movers across spot + futures."""
        log.info("market_scan_started", min_quote_volume=min_quote_volume, top_n=top_n)
        spot_raw, fut_raw = [], []
        try:
            spot_raw = await self.fetch_spot_tickers()
        except Exception as e:
            log.warning("spot_fetch_failed", error=str(e))
        try:
            fut_raw = await self.fetch_futures_tickers()
        except Exception as e:
            log.warning("futures_fetch_failed", error=str(e))

        signals: dict[str, MarketSignal] = {}
        for t in spot_raw:
            s = self._parse_ticker(t, "spot")
            if s.quote_volume >= min_quote_volume:
                signals[s.symbol] = s
        for t in fut_raw:
            s = self._parse_ticker(t, "futures")
            if s.quote_volume >= min_quote_volume:
                # Prefer futures when both exist (deeper liquidity, what traders watch)
                signals[s.symbol] = s

        # Rank: absolute 24h move (positive or negative)
        ranked = sorted(
            signals.values(),
            key=lambda x: abs(x.price_change_pct_24h),
            reverse=True,
        )[:top_n]

        if enrich_with_1h:
            for s in ranked[:25]:  # only enrich the top-25 to limit API calls
                s.price_change_pct_1h = await self.fetch_1h_change(s.symbol)

        log.info("market_scan_done", returned=len(ranked))
        return ranked

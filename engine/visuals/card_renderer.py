"""Renders HTML/CSS templates into Binance-style PNG images using Playwright.

All renderers produce 1440×3088 vertical mobile PNGs by default — that's the
ratio that performs best on Binance Square (per strategy analysis).

Inputs/outputs are deliberately simple — the caller provides a dict of
substitutions and gets back a `Path` to a PNG. Heavy lifting (chart SVG,
number formatting) is done by helper functions to keep templates dumb.
"""

from __future__ import annotations

import asyncio
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.config import get_settings
from engine.logging_setup import get_logger

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "templates" / "visuals"

VIEWPORT_W = 1440
VIEWPORT_H = 3088

_SUB_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def _render_template(name: str, substitutions: dict[str, Any]) -> str:
    template_path = TEMPLATES_DIR / name
    raw = template_path.read_text(encoding="utf-8")
    def replace(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(substitutions.get(key, ""))
    return _SUB_RE.sub(replace, raw)


@dataclass(slots=True)
class CardSpec:
    """User-facing description of a card to render."""

    template: str   # filename, e.g. "trade_card.html"
    substitutions: dict[str, Any] = field(default_factory=dict)
    output_name: str | None = None  # optional override; otherwise uuid


# ---------- formatters ----------

def fmt_money(value: float, *, max_decimals: int = 2) -> str:
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1000:
        return f"{sign}{v:,.{max_decimals}f}".rstrip("0").rstrip(".")
    if v >= 1:
        return f"{sign}{v:.{max_decimals}f}".rstrip("0").rstrip(".")
    # tiny prices
    return f"{sign}{v:.6f}".rstrip("0").rstrip(".")


def fmt_compact(value: float) -> str:
    units = [("B", 1e9), ("M", 1e6), ("K", 1e3)]
    sign = "-" if value < 0 else ""
    v = abs(value)
    for s, u in units:
        if v >= u:
            return f"{sign}{v / u:.2f}{s}"
    return f"{sign}{v:.2f}"


def fmt_pct(value: float) -> tuple[str, str]:
    sign = "+" if value >= 0 else "-"
    return sign, f"{abs(value):.2f}"


# ---------- chart SVG renderer (lightweight, no external libs) ----------

def render_candlestick_svg(klines: list[list[Any]], *, width: int = 1240, height: int = 1450) -> str:
    """Build a simple candlestick SVG from Binance kline data.

    Each kline: [openTime, open, high, low, close, ...].
    """
    if not klines:
        return f'<svg viewBox="0 0 {width} {height}" />'
    opens, highs, lows, closes = [], [], [], []
    for k in klines:
        opens.append(float(k[1]))
        highs.append(float(k[2]))
        lows.append(float(k[3]))
        closes.append(float(k[4]))
    n = len(klines)
    pad_top, pad_bot = 80, 80
    chart_h = height - pad_top - pad_bot
    y_min, y_max = min(lows), max(highs)
    if y_max == y_min:
        y_max = y_min * 1.01 + 0.01
    span = y_max - y_min

    def y(price: float) -> float:
        return pad_top + (1 - (price - y_min) / span) * chart_h

    candle_w = max(4.0, (width - 40) / n * 0.7)
    gap = (width - 40 - candle_w * n) / max(n - 1, 1)
    parts: list[str] = []
    parts.append(f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">')
    # horizontal grid lines
    for i in range(1, 5):
        gy = pad_top + chart_h * i / 5
        parts.append(
            f'<line x1="20" y1="{gy:.1f}" x2="{width - 20}" y2="{gy:.1f}" '
            'stroke="#1c2026" stroke-width="1.4"/>'
        )
    for i in range(n):
        x_center = 20 + i * (candle_w + gap) + candle_w / 2
        o, c, h, lo = opens[i], closes[i], highs[i], lows[i]
        color = "#0ECB81" if c >= o else "#F6465D"
        # wick
        parts.append(
            f'<line x1="{x_center:.1f}" y1="{y(h):.1f}" x2="{x_center:.1f}" y2="{y(lo):.1f}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
        # body
        top, bot = (y(max(o, c)), y(min(o, c)))
        body_h = max(2.0, bot - top)
        parts.append(
            f'<rect x="{(x_center - candle_w / 2):.1f}" y="{top:.1f}" '
            f'width="{candle_w:.1f}" height="{body_h:.1f}" fill="{color}" rx="2"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------- main renderer ----------

class CardRenderer:
    """Manages a single Playwright browser process and renders multiple cards."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._settings = get_settings()
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "CardRenderer":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def render(self, spec: CardSpec) -> Path:
        async with self._lock:
            assert self._browser is not None
            html = _render_template(spec.template, spec.substitutions)
            name = spec.output_name or f"{spec.template.replace('.html', '')}_{uuid.uuid4().hex[:10]}.png"
            out = self._settings.images_dir / name
            ctx = await self._browser.new_context(
                viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                device_scale_factor=1,
            )
            page = await ctx.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=5000)
            await page.screenshot(path=str(out), full_page=False, omit_background=False)
            await ctx.close()
            log.info("card_rendered", template=spec.template, path=str(out))
            return out


# ---------- high-level builders ----------

def build_trade_card_subs(
    *,
    symbol: str,
    pnl_usd: float,
    pct_value: float,
    entry_price: float,
    close_price: float,
    leverage_x: int,
    handle: str,
    size_usdt: float | None = None,
    duration: str = "—",
) -> dict[str, Any]:
    pnl_sign, _ = fmt_pct(pnl_usd)
    pct_sign, pct_v = fmt_pct(pct_value)
    color = "pos" if pct_value >= 0 else "neg"
    return {
        "SYMBOL": symbol.upper(),
        "LEVERAGE": f"{leverage_x}x",
        "QUOTE_META": "USDT-M Perpetual",
        "COLOR_CLASS": color,
        "PNL_SIGN": pnl_sign,
        "PNL_USD": f"{abs(pnl_usd):,.2f}",
        "PCT_SIGN": pct_sign,
        "PCT_VALUE": pct_v,
        "ENTRY_PRICE": f"${fmt_money(entry_price, max_decimals=4)}",
        "CLOSE_PRICE": f"${fmt_money(close_price, max_decimals=4)}",
        "SIZE": f"${fmt_compact(size_usdt)}" if size_usdt else "—",
        "DURATION": duration,
        "HANDLE": handle,
        "TIMESTAMP": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def build_chart_card_subs(
    *,
    symbol: str,
    quote: str = "USDT",
    interval: str = "1h",
    klines: list[list[Any]],
    price: float,
    change_24h_pct: float,
    high_24h: float,
    low_24h: float,
    volume_24h_usd: float,
    handle: str,
) -> dict[str, Any]:
    sign, change_v = fmt_pct(change_24h_pct)
    color = "pos" if change_24h_pct >= 0 else "neg"
    svg = render_candlestick_svg(klines)
    return {
        "SYMBOL": symbol.upper(),
        "QUOTE": quote,
        "INTERVAL": interval.upper(),
        "PRICE": fmt_money(price, max_decimals=4),
        "CHANGE_SIGN": sign,
        "CHANGE_PCT": change_v,
        "COLOR_CLASS": color,
        "CHART_SVG": svg,
        "HIGH_24H": fmt_money(high_24h, max_decimals=4),
        "LOW_24H": fmt_money(low_24h, max_decimals=4),
        "VOL_24H": fmt_compact(volume_24h_usd),
        "HANDLE": handle,
        "TIMESTAMP": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def build_warning_card_subs(
    *,
    symbol: str,
    change_pct: float,
    period: str,
    price: float,
    high_24h: float,
    low_24h: float,
    volume_24h_usd: float,
    handle: str,
    alert_text: str = "تحذير: تحركات حادة",
) -> dict[str, Any]:
    sign, change_v = fmt_pct(change_pct)
    return {
        "ALERT_TEXT": alert_text,
        "SYMBOL": symbol.upper(),
        "PERIOD": period,
        "CHANGE_SIGN": sign,
        "CHANGE_PCT": change_v,
        "PRICE": fmt_money(price, max_decimals=4),
        "HIGH_24H": fmt_money(high_24h, max_decimals=4),
        "LOW_24H": fmt_money(low_24h, max_decimals=4),
        "VOL_24H": fmt_compact(volume_24h_usd),
        "HANDLE": handle,
        "TIMESTAMP": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def build_profit_explosion_subs(
    *,
    symbol: str,
    pct_value: float,
    from_usd: float,
    to_usd: float,
    entry_price: float,
    close_price: float,
    duration: str,
    handle: str,
) -> dict[str, Any]:
    sign, pct_v = fmt_pct(pct_value)
    return {
        "SYMBOL": symbol.upper(),
        "PCT_SIGN": sign,
        "PCT_VALUE": pct_v,
        "FROM_USD": fmt_money(from_usd),
        "TO_USD": fmt_money(to_usd),
        "ENTRY_PRICE": fmt_money(entry_price, max_decimals=4),
        "CLOSE_PRICE": fmt_money(close_price, max_decimals=4),
        "DURATION": duration,
        "HANDLE": handle,
        "TIMESTAMP": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ---------- realistic-but-synthetic data helpers ----------

def synthesize_trade(
    symbol: str, *, last_price: float, change_pct: float
) -> dict[str, Any]:
    """Generate plausible PnL/entry/close numbers from a market signal.

    NOTE: by default these are SYNTHETIC visuals. Operators who want
    truthfulness should pipe in real trade history from their futures account.
    The strategy doc explicitly warns: never fabricate trades if Binance can
    audit them. We default to modest, plausible numbers.
    """
    pct = round(change_pct + random.uniform(-1.5, 1.5), 2)
    size = round(random.choice([100, 200, 500, 1000, 2000]), 2)
    pnl = round(size * pct / 100, 2)
    entry = last_price / (1 + pct / 100) if pct != 0 else last_price
    leverage = random.choice([5, 10, 20, 25, 50])
    duration = random.choice(["12m", "37m", "1h 12m", "2h 40m", "4h 18m"])
    return {
        "symbol": symbol,
        "pnl_usd": pnl,
        "pct_value": pct,
        "entry_price": round(entry, 6),
        "close_price": round(last_price, 6),
        "leverage_x": leverage,
        "size_usdt": size,
        "duration": duration,
    }

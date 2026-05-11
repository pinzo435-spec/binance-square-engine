from engine.visuals.card_renderer import (
    _render_template,
    build_trade_card_subs,
    fmt_compact,
    fmt_money,
    fmt_pct,
    render_candlestick_svg,
)


def test_fmt_helpers():
    assert fmt_money(1234.5) == "1,234.5"
    assert fmt_money(0.000123) == "0.000123"
    assert fmt_compact(2_500_000) == "2.50M"
    sign, v = fmt_pct(-12.345)
    assert sign == "-" and v == "12.35"


def test_template_substitution_for_trade_card():
    subs = build_trade_card_subs(
        symbol="BTCUSDT", pnl_usd=540.25, pct_value=27.5,
        entry_price=60000, close_price=76500, leverage_x=10,
        handle="KinzoTech", size_usdt=1000, duration="1h 12m",
    )
    rendered = _render_template("trade_card.html", subs)
    assert "BTCUSDT" in rendered
    assert "540.25" in rendered
    assert "27.50%" in rendered
    assert "@KinzoTech" in rendered
    assert "{{" not in rendered  # everything substituted


def test_candlestick_svg_returns_svg():
    klines = [
        [0, 100, 105, 98, 103, 1000, 0],
        [0, 103, 108, 102, 107, 1500, 0],
        [0, 107, 110, 104, 105, 800, 0],
    ]
    svg = render_candlestick_svg(klines)
    assert svg.startswith("<svg")
    assert "</svg>" in svg
    assert "#0ECB81" in svg  # bullish candle
    assert "#F6465D" in svg  # bearish candle

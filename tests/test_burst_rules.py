"""Unit tests for burst-trigger rule matching in scheduler."""

from __future__ import annotations

from dataclasses import dataclass

from engine.distribution.scheduler import _cmp_threshold, _match_burst_rule


@dataclass
class _Opp:
    ticker: str = "BTC"
    trigger: str = "PUMP"
    change_1h_pct: float | None = 0.0
    change_24h_pct: float | None = 0.0
    volume_ratio: float | None = 1.0
    binance_trend_hashtag: str | None = None
    priority_score: float = 1.0
    suggested_tendency: int = 0
    raw_payload: dict | None = None


def test_cmp_threshold_operators() -> None:
    assert _cmp_threshold(15, ">= 15") is True
    assert _cmp_threshold(14.9, ">= 15") is False
    assert _cmp_threshold(-10, "<= -10") is True
    assert _cmp_threshold(-9, "<= -10") is False
    assert _cmp_threshold(1, "== 1") is True
    assert _cmp_threshold(2, "!= 1") is True
    assert _cmp_threshold(None, ">= 0") is False


def test_extreme_pump_rule_matches() -> None:
    rules = [
        {"name": "extreme_pump", "when": {"change_1h_pct": ">= 15", "volume_ratio": ">= 5"}},
    ]
    opp = _Opp(change_1h_pct=20.0, volume_ratio=7.0)
    matched = _match_burst_rule(opp, rules)
    assert matched is not None
    assert matched["name"] == "extreme_pump"


def test_extreme_pump_rule_fails_on_volume() -> None:
    rules = [
        {"name": "extreme_pump", "when": {"change_1h_pct": ">= 15", "volume_ratio": ">= 5"}},
    ]
    opp = _Opp(change_1h_pct=20.0, volume_ratio=3.0)
    assert _match_burst_rule(opp, rules) is None


def test_extreme_dump_rule() -> None:
    rules = [{"name": "extreme_dump", "when": {"change_1h_pct": "<= -10"}}]
    assert _match_burst_rule(_Opp(change_1h_pct=-15), rules) is not None
    assert _match_burst_rule(_Opp(change_1h_pct=-5), rules) is None


def test_trending_hashtag_rule() -> None:
    rules = [{"name": "trending_hashtag", "when": {"binance_trend_match": True}}]
    assert _match_burst_rule(_Opp(binance_trend_hashtag="BTC"), rules) is not None
    assert _match_burst_rule(_Opp(binance_trend_hashtag=None), rules) is None


def test_news_trigger_rule() -> None:
    rules = [{"name": "listing", "when": {"news_trigger_in": ["BINANCE_LIST"]}}]
    assert _match_burst_rule(
        _Opp(raw_payload={"news_triggers": ["BINANCE_LIST"]}), rules
    ) is not None
    assert _match_burst_rule(_Opp(raw_payload={"news_triggers": []}), rules) is None


def test_first_match_wins() -> None:
    rules = [
        {"name": "a", "when": {"change_1h_pct": ">= 20"}},
        {"name": "b", "when": {"change_1h_pct": ">= 10"}},
    ]
    matched = _match_burst_rule(_Opp(change_1h_pct=15), rules)
    assert matched is not None
    assert matched["name"] == "b"

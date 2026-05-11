"""Unit tests for growth_scorer (pure-function part)."""

from engine.growth.growth_scorer import GrowthSignals, score, velocity_bonus


def test_zero_signals_returns_zero():
    gs, vb = score(GrowthSignals())
    assert gs == 0.0
    assert vb == 0.0


def test_higher_engagement_beats_higher_views():
    """An interaction-heavy post should outrank a views-only post."""
    a = GrowthSignals(views=2000, likes=2, comments=0, shares=0, age_hours=1.0)
    b = GrowthSignals(views=2000, likes=80, comments=40, shares=20, age_hours=1.0)
    sa, _ = score(a)
    sb, _ = score(b)
    assert sb > sa


def test_velocity_bonus_zero_below_floor():
    assert velocity_bonus(10) == 0.0
    assert velocity_bonus(49.9) == 0.0


def test_velocity_bonus_increases_with_rate():
    assert velocity_bonus(60) >= 0.0
    assert velocity_bonus(500) > velocity_bonus(60)
    assert velocity_bonus(5000) > velocity_bonus(500)
    # Capped at ~3.0
    assert velocity_bonus(1_000_000) <= 3.0


def test_age_zero_does_not_crash():
    gs, vb = score(GrowthSignals(views=100, likes=5, age_hours=0.0))
    assert gs > 0.0
    assert vb >= 0.0

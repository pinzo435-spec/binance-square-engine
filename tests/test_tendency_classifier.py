from engine.content.tendency_classifier import classify
from engine.signal.opportunity_ranker import RankedOpportunity


def _opp(**kw):
    base = dict(
        ticker="BTC", trigger="STEADY", change_1h_pct=0.0, change_24h_pct=0.0,
        volume_ratio=None, binance_trend_hashtag=None, priority_score=5.0,
        suggested_template="big_picture", suggested_tendency=0, raw_payload={},
    )
    base.update(kw)
    return RankedOpportunity(**base)


def test_explicit_tendency_wins():
    assert classify(_opp(suggested_tendency=1)) == 1
    assert classify(_opp(suggested_tendency=2)) == 2


def test_trigger_overrides_neutral():
    assert classify(_opp(trigger="EXTREME_PUMP")) == 1
    assert classify(_opp(trigger="EXTREME_DUMP")) == 2
    assert classify(_opp(trigger="HACK")) == 2


def test_1h_fallback():
    assert classify(_opp(trigger="STEADY", change_1h_pct=2.0)) == 1
    assert classify(_opp(trigger="STEADY", change_1h_pct=-2.0)) == 2
    assert classify(_opp(trigger="STEADY", change_1h_pct=0.5)) == 0

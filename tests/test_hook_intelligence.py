"""Unit tests for hook_intelligence classifier + sampling."""

from engine.growth.hook_intelligence import (
    HOOK_CATEGORIES,
    classify,
    softmax_pick,
)


def test_classify_greed():
    assert classify("$BTC انفجار في الأسعار 🤑 $BTC").category == "greed"
    assert classify("$BTC moonshot 10x $BTC").category == "greed"


def test_classify_fear():
    assert classify("$LUNA انهار السعر تحذير $LUNA").category == "fear"
    assert classify("warning crash incoming").category == "fear"


def test_classify_self_deprecation():
    assert classify("بعت بكير و ندمت $BTC").category == "self_deprecation"


def test_classify_curiosity_question_mark():
    # Question mark alone is a curiosity hit
    assert classify("هل تعلم وش صار اليوم؟").category == "curiosity"


def test_classify_neutral_when_no_hits():
    assert classify("$BTC السعر مستقر").category == "neutral"


def test_classify_empty_returns_neutral():
    assert classify("").category == "neutral"
    assert classify(None or "").category == "neutral"


def test_softmax_pick_returns_a_valid_category():
    weights = dict.fromkeys(HOOK_CATEGORIES, 1.0)
    picked = softmax_pick(weights)
    assert picked in HOOK_CATEGORIES


def test_softmax_pick_biases_toward_heavier_weight():
    weights = dict.fromkeys(HOOK_CATEGORIES, 0.1)
    weights["greed"] = 10.0
    # Run many trials; greed should dominate ≥ 70% of the time.
    counts = dict.fromkeys(HOOK_CATEGORIES, 0)
    for _ in range(400):
        counts[softmax_pick(weights, temperature=0.5)] += 1
    assert counts["greed"] > 280  # 70%

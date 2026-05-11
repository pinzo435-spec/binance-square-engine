from engine.content.hook_generator import _sanitise


def test_adds_cashtag_bookends_when_missing():
    out = _sanitise("الوضع مو طبيعي اليوم والكل متفاجئ من الحركة", "BTC")
    assert out.startswith("$BTC")
    assert out.endswith("$BTC")


def test_strips_exclamation_marks():
    out = _sanitise("$SOL ولعت!!! $SOL", "SOL")
    assert "!" not in out


def test_rejects_too_short():
    assert _sanitise("$BTC قصير $BTC", "BTC") == ""


def test_rejects_too_long():
    long = "$BTC " + "اب" * 200 + " $BTC"
    assert _sanitise(long, "BTC") == ""


def test_rejects_urls():
    assert _sanitise("$BTC http://x.com نص $BTC", "BTC") == ""


def test_strips_leading_markdown():
    raw = "`$BTC الوضع مو طبيعي اليوم 😄 $BTC`"
    out = _sanitise(raw, "BTC")
    assert out.startswith("$BTC")
    assert "`" not in out

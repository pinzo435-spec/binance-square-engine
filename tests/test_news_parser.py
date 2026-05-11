from engine.signal.news_feed import NewsFeed

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test</title>
  <item>
    <title>Binance will list NEWCOIN on Spot</title>
    <link>https://example.com/1</link>
    <description>NEWCOIN gets listed.</description>
    <pubDate>Tue, 22 Apr 2025 14:30:00 +0000</pubDate>
  </item>
  <item>
    <title>SOLANA hacked? Exchange reports exploit</title>
    <link>https://example.com/2</link>
    <description>Details of exploit.</description>
    <pubDate>Tue, 22 Apr 2025 13:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


def test_parses_rss_and_detects_triggers():
    items = NewsFeed._parse(SAMPLE_RSS, source="test")
    assert len(items) == 2
    assert items[0].trigger == "BINANCE_LIST"
    assert "NEWCOIN" in items[0].detected_tickers
    assert items[1].trigger == "HACK"
    assert "SOLANA" in items[1].detected_tickers

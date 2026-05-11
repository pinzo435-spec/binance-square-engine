"""News scanner that pulls crypto headlines from configurable RSS feeds.

Used by the opportunity ranker as a tertiary signal — when a major headline
hits (listing, hack, regulatory action) we boost the score of related tickers.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from engine.config import get_settings
from engine.logging_setup import get_logger

log = get_logger(__name__)

# Map common phrases → trigger types
NEWS_TRIGGERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bbinance\s+(will\s+)?list", re.I), "BINANCE_LIST"),
    (re.compile(r"\bbinance\s+(will\s+)?delist", re.I), "BINANCE_DELIST"),
    (re.compile(r"\bhack(ed)?\b|\bexploit\b|\brug\s*pull", re.I), "HACK"),
    (re.compile(r"\bATH\b|\ball.?time\s*high", re.I), "ATH"),
    (re.compile(r"\bSEC\b|\bregulat", re.I), "REGULATORY"),
    (re.compile(r"\bairdrop", re.I), "AIRDROP"),
    (re.compile(r"\bpartnership|\bintegrat", re.I), "PARTNERSHIP"),
]

TICKER_RE = re.compile(r"\b([A-Z]{2,10})\b")


@dataclass(slots=True)
class NewsItem:
    title: str
    link: str
    summary: str
    published_at: datetime | None
    source: str
    trigger: str | None
    detected_tickers: list[str]


class NewsFeed:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> NewsFeed:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
            self._owns_client = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def fetch_all(self) -> list[NewsItem]:
        assert self._client is not None
        urls = get_settings().news_rss_list
        items: list[NewsItem] = []
        for url in urls:
            try:
                r = await self._client.get(url)
                r.raise_for_status()
                items.extend(self._parse(r.text, source=url))
            except Exception as e:
                log.warning("news_feed_failed", url=url, error=str(e))
        items.sort(key=lambda x: x.published_at or datetime.fromtimestamp(0, tz=UTC),
                   reverse=True)
        log.info("news_fetched", count=len(items))
        return items[:50]

    @staticmethod
    def _parse(xml_text: str, source: str) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return items
        # Both RSS 2.0 (<rss><channel><item>) and Atom (<feed><entry>) supported
        for it in root.iter():
            tag = it.tag.split("}")[-1].lower()
            if tag not in ("item", "entry"):
                continue
            title = NewsFeed._text(it, "title")
            link = NewsFeed._text(it, "link") or NewsFeed._attr(it, "link", "href")
            desc = NewsFeed._text(it, "description") or NewsFeed._text(it, "summary")
            pub = NewsFeed._text(it, "pubDate") or NewsFeed._text(it, "published")
            published_at: datetime | None = None
            if pub:
                for fmt in (
                    "%a, %d %b %Y %H:%M:%S %z",
                    "%a, %d %b %Y %H:%M:%S %Z",
                    "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%SZ",
                ):
                    try:
                        published_at = datetime.strptime(pub, fmt)
                        break
                    except ValueError:
                        continue
            if not title:
                continue
            haystack = f"{title} {desc}"
            trigger = next((label for pat, label in NEWS_TRIGGERS if pat.search(haystack)), None)
            tickers = sorted({m for m in TICKER_RE.findall(title) if 2 <= len(m) <= 10})
            items.append(
                NewsItem(
                    title=title,
                    link=link,
                    summary=(desc or "")[:300],
                    published_at=published_at,
                    source=source,
                    trigger=trigger,
                    detected_tickers=tickers,
                )
            )
        return items

    @staticmethod
    def _text(parent: ET.Element, name: str) -> str:
        for child in parent.iter():
            if child.tag.split("}")[-1].lower() == name.lower():
                return (child.text or "").strip()
        return ""

    @staticmethod
    def _attr(parent: ET.Element, name: str, attr: str) -> str:
        for child in parent.iter():
            if child.tag.split("}")[-1].lower() == name.lower():
                return child.attrib.get(attr, "").strip()
        return ""

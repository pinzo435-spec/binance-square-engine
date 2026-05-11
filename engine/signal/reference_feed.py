"""Reference feed: pulls posts from a reference creator's Binance Square profile.

We use this to (a) train the few-shot prompt, (b) detect when the reference
account publishes about a ticker so we can react quickly with our own angle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
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

URL = "https://www.binance.com/bapi/composite/v2/friendly/pgc/content/queryUserProfilePageContentsWithFilter"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "clienttype": "web",
    "lang": "ar",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,15})\b")


@dataclass(slots=True)
class ReferencePostRecord:
    id: str
    body_text: str
    tickers: list[str]
    view_count: int
    like_count: int
    comment_count: int
    share_count: int
    quote_count: int
    published_at: datetime | None
    raw: dict[str, Any]

    def engagement_score(self) -> float:
        v = max(self.view_count, 1)
        return (
            self.like_count * 1
            + self.comment_count * 2
            + self.share_count * 4
            + self.quote_count * 3
        ) / v


class ReferenceFeed:
    def __init__(self, square_uid: str, client: httpx.AsyncClient | None = None) -> None:
        self.square_uid = square_uid
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "ReferenceFeed":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                http2=True,
                headers=DEFAULT_HEADERS,
            )
            self._owns_client = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def fetch_page(self, time_offset: int = -1) -> dict:
        """Fetch one page. `time_offset` paginates backwards via the API."""
        assert self._client is not None

        # Endpoint accepts both GET and POST forms; POST is what the web app uses.
        payload = {
            "targetSquareUid": self.square_uid,
            "timeOffset": time_offset,
            "filterType": "ALL",
            "size": 20,
        }
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.6, min=0.5, max=4.0),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                r = await self._client.post(URL, json=payload)
                r.raise_for_status()
                return r.json()
        return {}  # pragma: no cover

    async def fetch_latest(self, max_posts: int = 40) -> list[ReferencePostRecord]:
        """Fetch the most recent `max_posts` from the reference profile."""
        out: list[ReferencePostRecord] = []
        time_offset = -1
        seen_ids: set[str] = set()
        while len(out) < max_posts:
            try:
                data = await self.fetch_page(time_offset)
            except Exception as e:
                log.warning("reference_feed_fetch_failed", error=str(e))
                break
            items = (data.get("data") or {}).get("vos") or data.get("data") or []
            if not items:
                break
            batch = []
            for item in items:
                rec = self._parse_item(item)
                if rec.id in seen_ids:
                    continue
                seen_ids.add(rec.id)
                batch.append(rec)
            if not batch:
                break
            out.extend(batch)
            # Use earliest post's timestamp as next offset
            last = batch[-1]
            if last.published_at is None:
                break
            time_offset = int(last.published_at.timestamp() * 1000)
        return out[:max_posts]

    @staticmethod
    def _parse_item(item: dict) -> ReferencePostRecord:
        body = item.get("bodyTextOnly") or item.get("title") or item.get("content") or ""
        tickers = list({m.group(1).upper() for m in CASHTAG_RE.finditer(body)})

        pub_ts = item.get("createTime") or item.get("publishTime") or item.get("date")
        published_at: datetime | None = None
        if pub_ts:
            try:
                # Heuristic: ms or seconds
                ts = int(pub_ts)
                if ts > 1e12:
                    ts /= 1000
                published_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                published_at = None

        stats = item.get("stats") or {}
        return ReferencePostRecord(
            id=str(item.get("id") or item.get("postId") or item.get("contentId") or ""),
            body_text=body,
            tickers=tickers,
            view_count=int(item.get("viewCount") or stats.get("viewCount") or 0),
            like_count=int(item.get("likeCount") or stats.get("likeCount") or 0),
            comment_count=int(item.get("commentCount") or stats.get("commentCount") or 0),
            share_count=int(item.get("shareCount") or stats.get("shareCount") or 0),
            quote_count=int(item.get("quoteCount") or stats.get("quoteCount") or 0),
            published_at=published_at,
            raw=item,
        )

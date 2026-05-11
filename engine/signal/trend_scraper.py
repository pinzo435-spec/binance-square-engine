"""Binance Square trending hashtags scraper.

Calls the public trend endpoint that powers the "Trending Topics" widget in the
Binance Square UI. The exact shape of the response varies; we normalise to a
list of {name, post_count, view_count}.

If the endpoint changes (Binance updates it), this falls back to an empty list
gracefully so the pipeline can still run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from engine.logging_setup import get_logger

log = get_logger(__name__)

# Known working candidates as of 2026-05 (Binance rotates these; we try in order).
# If you've recorded a working endpoint from a logged-in session, prepend it
# here or put it in BINANCE_TRENDING_URL_OVERRIDE (env var, read on import).

_OVERRIDE = os.environ.get("BINANCE_TRENDING_URL_OVERRIDE", "").strip()

TREND_ENDPOINTS: list[str] = [
    *([_OVERRIDE] if _OVERRIDE else []),
    "https://www.binance.com/bapi/composite/v1/public/cms/square/trend/list",
    "https://www.binance.com/bapi/composite/v1/public/feed/trending-topics",
    "https://www.binance.com/bapi/composite/v1/public/cms/feature/trending",
    "https://www.binance.com/bapi/composite/v1/friendly/pgc/content/trending/list",
    "https://www.binance.com/bapi/composite/v1/friendly/pgc/square/trending/topics",
    "https://www.binance.com/bapi/composite/v2/friendly/pgc/content/queryTopics",
]


@dataclass(slots=True)
class TrendingTag:
    name: str
    post_count: int
    view_count: int


class TrendScraper:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> TrendScraper:
        if self._client is None:
            cookies = self._load_cookies()
            headers = {
                "Accept": "application/json",
                "clienttype": "web",
                "lang": "ar",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
                ),
            }
            if cookies:
                if (csrf := cookies.get("csrftoken")):
                    headers["csrftoken"] = csrf
                headers.setdefault("Referer", "https://www.binance.com/en/square")
            self._client = httpx.AsyncClient(
                timeout=15.0,
                http2=True,
                headers=headers,
                cookies=cookies or None,
            )
            self._owns_client = True
        return self

    @staticmethod
    def _load_cookies() -> dict[str, str]:
        try:
            import json
            from pathlib import Path

            from engine.config import get_settings
            path = get_settings().binance_cookies_path
            if not Path(path).exists():
                return {}
            return {c["name"]: c["value"] for c in json.loads(Path(path).read_text())
                    if c.get("name") and c.get("value")}
        except Exception:  # noqa: BLE001
            return {}

    async def __aexit__(self, *args: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def fetch_trending(self) -> list[TrendingTag]:
        assert self._client is not None
        for url in TREND_ENDPOINTS:
            try:
                r = await self._client.get(url)
                if r.status_code >= 400:
                    continue
                data = r.json()
            except Exception as e:
                log.debug("trend_endpoint_failed", url=url, error=str(e))
                continue
            tags = self._normalise(data)
            if tags:
                log.info("trending_fetched", source=url, count=len(tags))
                return tags
        log.warning("trending_unavailable")
        return []

    @staticmethod
    def _normalise(data: Any) -> list[TrendingTag]:
        """Try to extract tags from any of the known response shapes."""
        candidates: list[dict] = []
        if isinstance(data, dict):
            for k in ("data", "items", "list", "result"):
                v = data.get(k)
                if isinstance(v, list):
                    candidates = v
                    break
                if isinstance(v, dict):
                    for kk in ("items", "list", "vos", "result"):
                        if isinstance(v.get(kk), list):
                            candidates = v[kk]
                            break
                    if candidates:
                        break
        elif isinstance(data, list):
            candidates = data

        tags: list[TrendingTag] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("name")
                or item.get("hashtag")
                or item.get("title")
                or item.get("tag")
                or ""
            )
            if not name:
                continue
            tags.append(
                TrendingTag(
                    name=str(name).lstrip("#"),
                    post_count=int(item.get("postCount") or item.get("count") or 0),
                    view_count=int(item.get("viewCount") or item.get("views") or 0),
                )
            )
        return tags

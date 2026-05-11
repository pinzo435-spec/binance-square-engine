"""Uploads PNG images to a CDN so Binance Square posts can reference them by URL.

Primary backend: ImgBB (free, simple API).
Fallback: a no-op that returns a `file://` URL — useful for dry-run mode.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from engine.config import get_settings
from engine.logging_setup import get_logger

log = get_logger(__name__)


class ImageHost(ABC):
    name: str = "abstract"

    @abstractmethod
    async def upload(self, path: Path) -> str:
        ...


class ImgBBHost(ImageHost):
    name = "imgbb"
    URL = "https://api.imgbb.com/1/upload"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def upload(self, path: Path) -> str:
        encoded = base64.b64encode(path.read_bytes()).decode()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=1, max=8),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=60.0) as c:
                    r = await c.post(
                        self.URL,
                        params={"key": self.api_key},
                        data={"image": encoded},
                    )
                    r.raise_for_status()
                    body = r.json()
                    url = body["data"]["image"]["url"]
                    log.info("image_uploaded", host="imgbb", url=url)
                    return url
        raise RuntimeError("imgbb upload failed")  # pragma: no cover


class LocalHost(ImageHost):
    """No-op host that returns a file URL. For dev / dry-run."""

    name = "local"

    async def upload(self, path: Path) -> str:
        url = path.resolve().as_uri()
        log.info("image_local", url=url)
        return url


def build_image_host() -> ImageHost:
    s = get_settings()
    if s.imgbb_api_key:
        return ImgBBHost(api_key=s.imgbb_api_key)
    log.warning("no_image_host_credentials_using_local")
    return LocalHost()

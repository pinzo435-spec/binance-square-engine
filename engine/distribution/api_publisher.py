"""Binance Square OpenAPI publisher.

Uses the official content/add endpoint with an `X-Square-OpenAPI-Key` header.
This is the primary publishing path when an OpenAPI key is configured.

NOTE: The image URL handling on this endpoint is undocumented externally. We
include `imageList` (a list of CDN URLs) and `tradingPairs`; if Binance does
not accept either, we surface a clear error so the caller can fall back to the
browser publisher.
"""

from __future__ import annotations

from dataclasses import dataclass

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

ENDPOINT = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"


@dataclass(slots=True)
class PublishResult:
    success: bool
    external_id: str | None
    raw_response: dict
    error: str | None = None


class ApiPublisher:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def publish(
        self,
        *,
        body_text: str,
        image_urls: list[str] | None = None,
        trading_pairs: list[str] | None = None,
        tendency: int = 0,
    ) -> PublishResult:
        if not self.settings.x_square_openapi_key:
            return PublishResult(
                success=False, external_id=None, raw_response={},
                error="no_openapi_key_configured",
            )

        payload: dict = {"bodyTextOnly": body_text}
        if image_urls:
            payload["imageList"] = image_urls
        if trading_pairs:
            payload["tradingPairs"] = trading_pairs
        if tendency in (1, 2):
            payload["tendency"] = tendency

        headers = {
            "X-Square-OpenAPI-Key": self.settings.x_square_openapi_key,
            "Content-Type": "application/json",
            "clienttype": "binanceSkill",
            "lang": "ar",
        }

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.6, min=1, max=8),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=30.0) as c:
                    r = await c.post(ENDPOINT, headers=headers, json=payload)
                    try:
                        body = r.json()
                    except Exception:
                        body = {"raw_text": r.text}
                    if r.status_code >= 400:
                        log.warning("api_publish_http_error", status=r.status_code, body=body)
                        return PublishResult(
                            success=False, external_id=None, raw_response=body,
                            error=f"http_{r.status_code}",
                        )
                    code = str(body.get("code") or "000000")
                    if code != "000000":
                        log.warning("api_publish_business_error", code=code, body=body)
                        return PublishResult(
                            success=False, external_id=None, raw_response=body,
                            error=f"business_{code}_{body.get('message', '')}",
                        )
                    data = body.get("data") or {}
                    ext = str(data.get("id") or data.get("contentId") or "")
                    log.info("api_publish_ok", external_id=ext)
                    return PublishResult(
                        success=True, external_id=ext or None, raw_response=body,
                    )
        return PublishResult(False, None, {}, "retry_exhausted")  # pragma: no cover

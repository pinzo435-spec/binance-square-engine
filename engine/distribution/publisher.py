"""Top-level Publisher: ties together API + Browser + rate limiting + persistence.

Modes:
    api      → use only OpenAPI (fail if not available)
    browser  → use only browser automation
    hybrid   → try API first; fall back to browser on image-related failures
    dry_run  → log what would be sent without contacting Binance
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from engine.config import get_settings
from engine.content.post_assembler import AssembledPost
from engine.db import session_scope
from engine.distribution.api_publisher import ApiPublisher, PublishResult
from engine.distribution.browser_publisher import BrowserPublisher
from engine.distribution.rate_limiter import RateLimiter
from engine.logging_setup import get_logger
from engine.models import Opportunity, Post
from engine.visuals.image_uploader import build_image_host

log = get_logger(__name__)


class Publisher:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.rate_limiter = RateLimiter()
        self.api = ApiPublisher()
        self.browser = BrowserPublisher()
        self.image_host = build_image_host()

    async def publish(
        self,
        post: AssembledPost,
        *,
        opportunity_id: int | None = None,
    ) -> PublishResult:
        decision = await self.rate_limiter.check(post.ticker)
        if not decision.allowed:
            log.warning("publish_rate_blocked", ticker=post.ticker, reason=decision.reason)
            await self._record_post(post, opportunity_id, "blocked", decision.reason)
            return PublishResult(False, None, {}, f"rate_blocked: {decision.reason}")

        # 1. Upload images → URLs
        image_urls: list[str] = []
        for p in post.image_paths:
            try:
                image_urls.append(await self.image_host.upload(p))
            except Exception as e:
                log.warning("image_upload_failed", path=str(p), error=str(e))
        post.image_urls = image_urls

        mode = self.settings.publish_mode
        if mode == "dry_run":
            log.info(
                "dry_run_publish",
                ticker=post.ticker,
                body=post.body_text,
                images=len(image_urls),
                pair=post.trading_pairs,
            )
            await self._record_post(post, opportunity_id, "published_dry_run", external_id="dryrun")
            return PublishResult(True, "dryrun", {"dry_run": True})

        # 2. Try API first
        api_result: PublishResult | None = None
        if mode in ("api", "hybrid"):
            api_result = await self.api.publish(
                body_text=post.body_text,
                image_urls=image_urls,
                trading_pairs=post.trading_pairs,
                tendency=post.tendency,
            )
            if api_result.success:
                await self._record_post(
                    post, opportunity_id, "published",
                    external_id=api_result.external_id, publish_mode="api",
                )
                return api_result

        # 3. Fall back to browser
        if mode in ("browser", "hybrid"):
            br = await self.browser.publish(
                body_text=post.body_text,
                image_paths=post.image_paths,
                trading_pairs=post.trading_pairs,
            )
            if br.success:
                await self._record_post(
                    post, opportunity_id, "published",
                    external_id=br.external_id, publish_mode="browser",
                )
                return br
            else:
                err = br.error or "browser_failed"
                if api_result and api_result.error:
                    err = f"api:{api_result.error} | browser:{err}"
                await self._record_post(post, opportunity_id, "failed", err, publish_mode="browser")
                return br

        # mode == api and api failed
        err = api_result.error if api_result else "no_publisher_available"
        await self._record_post(post, opportunity_id, "failed", err, publish_mode="api")
        return api_result or PublishResult(False, None, {}, err)

    async def _record_post(
        self,
        post: AssembledPost,
        opportunity_id: int | None,
        status: str,
        error: str | None = None,
        *,
        external_id: str | None = None,
        publish_mode: str | None = None,
    ) -> None:
        async with session_scope() as s:
            row = Post(
                opportunity_id=opportunity_id,
                ticker=post.ticker,
                body_text=post.body_text,
                tendency=post.tendency,
                trading_pairs=post.trading_pairs,
                image_paths=[str(p) for p in post.image_paths],
                image_urls=post.image_urls,
                template_name=post.template_name,
                publish_mode=publish_mode or self.settings.publish_mode,
                status=status,
                error=error,
                external_post_id=external_id,
                published_at=datetime.now(tz=timezone.utc) if status.startswith("published") else None,
            )
            s.add(row)
            # Mark opportunity consumed if we published or blocked permanently
            if opportunity_id is not None:
                opp = (await s.execute(
                    select(Opportunity).where(Opportunity.id == opportunity_id)
                )).scalars().first()
                if opp and status in ("published", "published_dry_run", "blocked"):
                    opp.consumed = True
                    opp.consumed_at = datetime.now(tz=timezone.utc)

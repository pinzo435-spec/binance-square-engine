"""Browser-automation publisher (Playwright).

Used when the OpenAPI path is unavailable or when a post requires native image
upload. The flow:

    1. Launch Chromium (headless or headed).
    2. Load saved cookies for `binance.com` to authenticate.
    3. Open the Square write page.
    4. Type the post body (human-paced).
    5. Upload image file(s) via the hidden <input type="file"> element.
    6. Click "Publish".

IMPORTANT — TERMS OF SERVICE
============================
Programmatic posting at scale may violate Binance's TOS and can cause your
account to be flagged or permanently banned. This module is provided for
operators who understand and accept that risk. Selectors must be updated as
Binance changes its UI.

What this module DOES NOT do:
    - Spoof browser fingerprints (WebGL, canvas, audio context, etc.).
    - Rotate user agents to fake different machines.
    - Inject anti-detection scripts (e.g. playwright-stealth).

What it DOES do:
    - Use realistic human-paced delays between keystrokes/clicks.
    - Persist a single browser profile so behaviour looks consistent.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.config import get_settings
from engine.distribution.api_publisher import PublishResult
from engine.logging_setup import get_logger

log = get_logger(__name__)

SQUARE_HOME = "https://www.binance.com/en/square"
SQUARE_WRITE = "https://www.binance.com/en/square/post-create"

# Selector candidates. Binance frequently changes class names — we keep
# several fallbacks so the publisher degrades gracefully.
SELECTORS = {
    "editor_textarea": [
        'div[contenteditable="true"][data-placeholder]',
        'div[contenteditable="true"][role="textbox"]',
        'textarea[placeholder*="Share"]',
        'div[contenteditable="true"]',
    ],
    "image_upload_input": [
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
    ],
    "publish_button": [
        'button[data-bn-type="button"]:has-text("Publish")',
        'button:has-text("Publish")',
        'button:has-text("Post")',
        'button:has-text("نشر")',
    ],
}


@dataclass(slots=True)
class BrowserPublishOptions:
    headless: bool = True
    slow_mo_ms: int = 50  # Playwright slow-motion to give the UI time to react
    persist_profile_dir: Path | None = None


class BrowserPublisher:
    def __init__(self, options: BrowserPublishOptions | None = None) -> None:
        self.settings = get_settings()
        self.options = options or BrowserPublishOptions(
            persist_profile_dir=self.settings.runtime_dir / "browser_profile"
        )

    async def _human_pause(self, lo_ms: int = 400, hi_ms: int = 1200) -> None:
        await asyncio.sleep(random.uniform(lo_ms, hi_ms) / 1000.0)

    async def _human_type(self, locator, text: str) -> None:
        # Type in chunks with jitter to look natural
        i = 0
        while i < len(text):
            chunk = random.randint(2, 6)
            piece = text[i : i + chunk]
            await locator.type(piece, delay=random.uniform(30, 110))
            i += chunk
            if random.random() < 0.15:
                await self._human_pause(80, 250)

    async def _load_cookies(self, context) -> bool:
        path = self.settings.binance_cookies_path
        if not Path(path).exists():
            log.warning("cookies_missing", path=str(path))
            return False
        cookies = json.loads(Path(path).read_text())
        await context.add_cookies(cookies)
        log.info("cookies_loaded", count=len(cookies))
        return True

    async def _find_first(self, page, candidates: list[str]):
        for sel in candidates:
            try:
                el = await page.wait_for_selector(sel, timeout=4000, state="visible")
                if el:
                    return el
            except Exception:
                continue
        return None

    async def publish(
        self,
        *,
        body_text: str,
        image_paths: list[Path],
        trading_pairs: list[str] | None = None,  # noqa: ARG002 - reserved for future UI work
    ) -> PublishResult:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.options.headless,
                slow_mo=self.options.slow_mo_ms,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1366, "height": 800},
                locale="ar-SA",
            )
            try:
                if not await self._load_cookies(context):
                    return PublishResult(False, None, {}, "missing_cookies")
                page = await context.new_page()

                log.info("opening_square_home")
                await page.goto(SQUARE_HOME, wait_until="domcontentloaded", timeout=45_000)
                await self._human_pause(800, 1800)
                # Verify login: if the page redirects to /login or shows it, bail.
                if "/login" in page.url:
                    return PublishResult(False, None, {}, "not_authenticated")

                log.info("opening_write_page")
                await page.goto(SQUARE_WRITE, wait_until="networkidle", timeout=45_000)
                await self._human_pause(900, 2200)

                editor = await self._find_first(page, SELECTORS["editor_textarea"])
                if not editor:
                    return PublishResult(False, None, {}, "editor_not_found")
                await editor.click()
                await self._human_pause(150, 450)
                await self._human_type(editor, body_text)
                await self._human_pause(500, 1200)

                if image_paths:
                    file_input = await self._find_first(page, SELECTORS["image_upload_input"])
                    if not file_input:
                        return PublishResult(False, None, {}, "file_input_not_found")
                    await file_input.set_input_files([str(p) for p in image_paths])
                    log.info("images_uploaded", count=len(image_paths))
                    await page.wait_for_timeout(3000)  # let preview render
                    await self._human_pause(800, 1500)

                publish_btn = await self._find_first(page, SELECTORS["publish_button"])
                if not publish_btn:
                    return PublishResult(False, None, {}, "publish_button_not_found")
                await publish_btn.click()
                log.info("clicked_publish")
                await page.wait_for_timeout(4000)

                # Best-effort confirmation
                url_after = page.url
                return PublishResult(
                    success=True,
                    external_id=None,  # browser flow doesn't surface a content id
                    raw_response={"url_after": url_after},
                )
            except Exception as e:
                log.exception("browser_publish_failed", error=str(e))
                return PublishResult(False, None, {}, f"exception:{type(e).__name__}:{e}")
            finally:
                await context.close()
                await browser.close()

    @staticmethod
    def save_cookies_helper(cookies_json: list[dict[str, Any]], out: Path) -> None:
        """Utility for one-time cookie export from a logged-in browser."""
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cookies_json, indent=2))

"""Browser-automation publisher (Playwright).

Used when the OpenAPI path is unavailable or when a post requires native image
upload.

Flow:
    1. Launch Chromium (headless or headed) — optionally a persistent context
       so the browser profile + cookies survive between publishes.
    2. Load saved cookies for `binance.com` to authenticate.
    3. Open the Square write page.
    4. Type the post body (human-paced).
    5. Upload image file(s) via the hidden <input type="file"> element.
    6. Click "Publish".
    7. Wait for confirmation toast OR URL change OR profile-feed appearance.
    8. Take a screenshot on any failure (saved to data/runtime/screenshots/).

IMPORTANT — TERMS OF SERVICE
============================
Programmatic posting at scale may violate Binance's TOS and can cause your
account to be flagged or permanently banned. This module is provided for
operators who understand and accept that risk. Selectors live in
`data/selectors/binance_square.yaml` and must be updated as Binance changes
its UI; use `bse selectors-tune` to probe the live DOM.

What this module does NOT do:
    - Spoof browser fingerprints (WebGL, canvas, audio context, etc.).
    - Rotate user agents to fake different machines.
    - Inject anti-detection scripts (e.g. playwright-stealth).

What it DOES do:
    - Realistic human-paced delays between keystrokes/clicks.
    - Single persistent profile per account.
    - Pre-flight cookie validation (detects login wall before touching the
      compose UI).
    - Screenshot every failure path for forensic debugging.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from engine.config import get_settings
from engine.distribution.api_publisher import PublishResult
from engine.logging_setup import get_logger

log = get_logger(__name__)

SQUARE_HOME = "https://www.binance.com/en/square"
SQUARE_WRITE = "https://www.binance.com/en/square/post-create"
SQUARE_PROFILE_TPL = "https://www.binance.com/en/square/profile/{uid}"

# Default user agent — match a stable, common Chrome version.
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


def _load_selectors(path: Path) -> dict[str, list[str]]:
    """Load selector candidates from YAML; return {role: [selector,…]} dict."""
    if not path.exists():
        log.warning("selectors_file_missing", path=str(path))
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: [str(s) for s in (v or [])] for k, v in raw.items()}


def _parse_selector(sel: str) -> tuple[str, str]:
    """Split a prefixed selector ("css:foo", "text:Bar", "role:button|Bar") into (kind, value)."""
    if ":" not in sel:
        return "css", sel
    head, _, tail = sel.partition(":")
    return head, tail


@dataclass(slots=True)
class BrowserPublishOptions:
    headless: bool = True
    slow_mo_ms: int = 50              # Playwright slow-motion
    persist_profile_dir: Path | None = None
    screenshot_on_failure: bool = True
    reuse_context: bool = False       # if True, caller manages the context
    pre_flight_login_check: bool = True


@dataclass(slots=True)
class _ContextHolder:
    """Wraps a long-lived Playwright context for reuse across publishes."""

    playwright: Any = None
    browser: Any = None
    context: Any = None
    started_at: float = field(default_factory=time.monotonic)

    async def close(self) -> None:
        for thing in (self.context, self.browser, self.playwright):
            if thing is None:
                continue
            close = getattr(thing, "close", None) or getattr(thing, "stop", None)
            if close is None:
                continue
            try:
                res = close()
                if hasattr(res, "__await__"):
                    await res
            except Exception:  # noqa: BLE001
                pass


class BrowserPublisher:
    """High-level publisher driven by a YAML selector registry."""

    def __init__(self, options: BrowserPublishOptions | None = None) -> None:
        self.settings = get_settings()
        self.options = options or BrowserPublishOptions(
            persist_profile_dir=self.settings.runtime_dir / "browser_profile"
        )
        self._selectors_path = self.settings.root_dir / "data" / "selectors" / "binance_square.yaml"
        self._selectors = _load_selectors(self._selectors_path)
        self._held_context: _ContextHolder | None = None

    # ------------------------------------------------------------------
    # human-behavior helpers
    # ------------------------------------------------------------------

    async def _human_pause(self, lo_ms: int = 400, hi_ms: int = 1200) -> None:
        await asyncio.sleep(random.uniform(lo_ms, hi_ms) / 1000.0)

    async def _human_type(self, locator: Any, text: str) -> None:
        # Type in 2-6 char chunks with jitter; occasional micro-pauses to look
        # natural. A 70-char post takes ~2-5s, well within human range.
        i = 0
        while i < len(text):
            chunk = random.randint(2, 6)
            piece = text[i : i + chunk]
            await locator.type(piece, delay=random.uniform(30, 110))
            i += chunk
            if random.random() < 0.15:
                await self._human_pause(80, 250)

    async def _human_move_then_click(self, page: Any, target: Any) -> None:
        """Move cursor toward the target with a short curve, then click."""
        try:
            box = await target.bounding_box()
        except Exception:  # noqa: BLE001
            await target.click()
            return
        if not box:
            await target.click()
            return
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        # 2-3 intermediate hops
        steps = random.randint(2, 3)
        for k in range(1, steps + 1):
            jx = cx + random.uniform(-12, 12) * (steps - k) / steps
            jy = cy + random.uniform(-12, 12) * (steps - k) / steps
            await page.mouse.move(jx, jy, steps=random.randint(8, 16))
            await asyncio.sleep(random.uniform(0.02, 0.08))
        await page.mouse.move(cx, cy, steps=random.randint(6, 12))
        await asyncio.sleep(random.uniform(0.04, 0.16))
        await target.click()

    # ------------------------------------------------------------------
    # cookies & context
    # ------------------------------------------------------------------

    async def _load_cookies(self, context: Any) -> bool:
        path = self.settings.binance_cookies_path
        if not Path(path).exists():
            log.warning("cookies_missing", path=str(path))
            return False
        try:
            cookies = json.loads(Path(path).read_text())
        except Exception as e:  # noqa: BLE001
            log.error("cookies_invalid_json", error=str(e))
            return False
        await context.add_cookies(cookies)
        log.info("cookies_loaded", count=len(cookies))
        return True

    async def _find_first(
        self,
        page: Any,
        role: str,
        timeout_ms: int = 4000,
        log_misses: bool = False,
    ) -> Any | None:
        candidates = self._selectors.get(role, [])
        if not candidates:
            log.warning("no_selectors_for_role", role=role)
            return None
        for sel in candidates:
            kind, val = _parse_selector(sel)
            try:
                if kind == "css":
                    el = await page.wait_for_selector(val, timeout=timeout_ms, state="visible")
                elif kind == "text":
                    el = await page.get_by_text(val, exact=False).first.element_handle()
                elif kind == "role":
                    name = None
                    rname = val
                    if "|" in val:
                        rname, _, name = val.partition("|")
                    el = await page.get_by_role(rname, name=name).first.element_handle()
                elif kind == "xpath":
                    el = await page.wait_for_selector(f"xpath={val}", timeout=timeout_ms, state="visible")
                else:
                    continue
                if el:
                    log.debug("selector_matched", role=role, selector=sel)
                    return el
            except Exception as e:  # noqa: BLE001
                if log_misses:
                    log.debug("selector_miss", role=role, selector=sel, error=str(e)[:120])
                continue
        log.warning("all_selectors_failed", role=role, tried=len(candidates))
        return None

    async def _is_login_wall(self, page: Any) -> bool:
        if "/login" in page.url:
            return True
        el = await self._find_first(page, "login_wall", timeout_ms=1200, log_misses=False)
        return el is not None

    async def _save_screenshot(self, page: Any, label: str) -> str | None:
        if not self.options.screenshot_on_failure:
            return None
        try:
            d = self.settings.runtime_dir / "screenshots"
            d.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = d / f"{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=True)
            log.info("screenshot_saved", path=str(path), label=label)
            return str(path)
        except Exception as e:  # noqa: BLE001
            log.warning("screenshot_failed", error=str(e))
            return None

    # ------------------------------------------------------------------
    # browser lifecycle
    # ------------------------------------------------------------------

    async def _ensure_context(self) -> _ContextHolder:
        """Build (or reuse) a Playwright context with our cookies preloaded."""
        from playwright.async_api import async_playwright

        if self._held_context is not None:
            return self._held_context

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=self.options.headless,
            slow_mo=self.options.slow_mo_ms,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 800},
            locale="ar-SA",
            user_agent=DEFAULT_UA,
        )
        await self._load_cookies(context)
        self._held_context = _ContextHolder(pw, browser, context)
        return self._held_context

    async def close(self) -> None:
        if self._held_context is not None:
            await self._held_context.close()
            self._held_context = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def publish(
        self,
        *,
        body_text: str,
        image_paths: list[Path],
        trading_pairs: list[str] | None = None,  # noqa: ARG002 - reserved
    ) -> PublishResult:
        owns_context = not self.options.reuse_context
        if owns_context and self._held_context is not None:
            # Caller switched modes — clean up the held context.
            await self.close()

        from playwright.async_api import async_playwright

        async with async_playwright() if owns_context else _NullCtx() as pw:
            holder: _ContextHolder
            if owns_context:
                browser = await pw.chromium.launch(
                    headless=self.options.headless,
                    slow_mo=self.options.slow_mo_ms,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context = await browser.new_context(
                    viewport={"width": 1366, "height": 800},
                    locale="ar-SA",
                    user_agent=DEFAULT_UA,
                )
                cookies_ok = await self._load_cookies(context)
                holder = _ContextHolder(pw, browser, context)
            else:
                holder = await self._ensure_context()
                context = holder.context
                cookies_ok = True  # already loaded when context was built

            if not cookies_ok:
                if owns_context:
                    await holder.close()
                return PublishResult(False, None, {}, "missing_cookies")

            page = await context.new_page()
            try:
                # 1. Pre-flight cookie validation
                if self.options.pre_flight_login_check:
                    log.info("preflight_loading_home")
                    await page.goto(SQUARE_HOME, wait_until="domcontentloaded", timeout=45_000)
                    await self._human_pause(700, 1500)
                    if await self._is_login_wall(page):
                        await self._save_screenshot(page, "login_wall")
                        return PublishResult(False, None, {}, "cookies_invalid_or_expired")

                # 2. Navigate to write page
                log.info("opening_write_page")
                await page.goto(SQUARE_WRITE, wait_until="networkidle", timeout=45_000)
                await self._human_pause(900, 2200)
                if await self._is_login_wall(page):
                    await self._save_screenshot(page, "login_wall_on_write")
                    return PublishResult(False, None, {}, "cookies_invalid_or_expired")

                # 3. Locate editor
                editor = await self._find_first(page, "editor_textarea", timeout_ms=8000, log_misses=True)
                if not editor:
                    await self._save_screenshot(page, "editor_not_found")
                    return PublishResult(False, None, {}, "editor_not_found")
                await self._human_move_then_click(page, editor)
                await self._human_pause(150, 450)
                await self._human_type(editor, body_text)
                await self._human_pause(500, 1200)

                # 4. Upload image(s)
                if image_paths:
                    file_input = await self._find_first(
                        page, "image_upload_input", timeout_ms=4000, log_misses=True
                    )
                    if not file_input:
                        await self._save_screenshot(page, "file_input_not_found")
                        return PublishResult(False, None, {}, "file_input_not_found")
                    await file_input.set_input_files([str(p) for p in image_paths])
                    log.info("images_uploaded", count=len(image_paths))
                    # Give the preview time to render.
                    await page.wait_for_timeout(3000)
                    await self._human_pause(800, 1500)

                # 5. Click Publish
                publish_btn = await self._find_first(
                    page, "publish_button", timeout_ms=4000, log_misses=True
                )
                if not publish_btn:
                    await self._save_screenshot(page, "publish_button_not_found")
                    return PublishResult(False, None, {}, "publish_button_not_found")
                pre_url = page.url
                await self._human_move_then_click(page, publish_btn)
                log.info("clicked_publish")

                # 6. Confirmation
                confirmation = await self._wait_for_confirmation(page, pre_url=pre_url)
                if not confirmation["success"]:
                    await self._save_screenshot(page, "no_confirmation")
                    return PublishResult(
                        False, None, confirmation, "no_publish_confirmation"
                    )

                return PublishResult(
                    success=True,
                    external_id=confirmation.get("external_id"),
                    raw_response=confirmation,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("browser_publish_failed", error=str(e))
                await self._save_screenshot(page, "exception")
                return PublishResult(False, None, {}, f"exception:{type(e).__name__}:{e}")
            finally:
                with contextlib.suppress(Exception):
                    await page.close()
                if owns_context:
                    await holder.close()

    async def _wait_for_confirmation(self, page: Any, *, pre_url: str) -> dict[str, Any]:
        """Confirm publication: toast OR URL change OR known confirmation selector."""
        deadline = time.monotonic() + 15.0
        last_err: str | None = None
        while time.monotonic() < deadline:
            try:
                el = await self._find_first(
                    page, "publish_confirmation", timeout_ms=1500, log_misses=False
                )
                if el:
                    return {"success": True, "via": "toast", "url": page.url}
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:120]
            if page.url != pre_url and "post-create" not in page.url:
                return {"success": True, "via": "url_change", "url": page.url}
            await asyncio.sleep(0.6)
        return {"success": False, "last_url": page.url, "last_err": last_err}

    # ------------------------------------------------------------------
    # static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def save_cookies_helper(cookies_json: list[dict[str, Any]], out: Path) -> None:
        """Utility for one-time cookie export from a logged-in browser."""
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cookies_json, indent=2))


class _NullCtx:
    """Async context manager that yields a placeholder. Used when we reuse a held playwright context."""

    async def __aenter__(self) -> Any:
        return None

    async def __aexit__(self, *_: Any) -> None:
        return None

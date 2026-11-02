from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

from .config import Settings
from .parser import GuestVerificationRequired, is_guest_verification

LOGGER = logging.getLogger(__name__)
SHARED_PAGE_NAME = "smth-reader-gateway"


class SMTHUnavailable(RuntimeError):
    pass


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._shared_browser = bool(settings.browser_cdp_url)
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._status_lock = asyncio.Lock()
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        self._verification_block_until = 0.0

    @property
    def mode(self) -> str:
        return "shared-cdp" if self._shared_browser else "local"

    @property
    def page(self) -> Page:
        if not self._page:
            raise SMTHUnavailable("browser page is not ready")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise SMTHUnavailable("browser context is not ready")
        return self._context

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        if self._shared_browser:
            await self._start_shared_browser()
        else:
            await self._start_local_browser()
        self._context.set_default_timeout(30_000)
        self._context.set_default_navigation_timeout(45_000)
        LOGGER.info("Chromium browser is ready in %s mode", self.mode)

    async def _start_local_browser(self) -> None:
        Path(self.settings.profile_dir).parent.mkdir(parents=True, exist_ok=True)
        assert self._playwright is not None
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            accept_downloads=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--window-position=0,0",
                "--window-size=1280,800",
            ],
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        await self._page.goto(
            f"{self.settings.smth_base}/nForum/board/NewExpress?ajax",
            wait_until="domcontentloaded",
        )

    async def _start_shared_browser(self) -> None:
        assert self._playwright is not None
        self._browser = await self._playwright.chromium.connect_over_cdp(
            self.settings.browser_cdp_url
        )
        if not self._browser.contexts:
            raise SMTHUnavailable("shared Chromium has no default browser context")
        self._context = self._browser.contexts[0]
        self._page = await self._find_reader_page()

    async def _find_reader_page(self) -> Page:
        assert self._context is not None
        for page in self._context.pages:
            try:
                name = await page.evaluate("() => window.name")
            except PlaywrightError:
                continue
            if name == SHARED_PAGE_NAME:
                LOGGER.info("reusing task-owned tab in shared Chromium")
                return page

        page = await self._context.new_page()
        try:
            await page.evaluate(
                "(name) => { window.name = name; }",
                SHARED_PAGE_NAME,
            )
        except PlaywrightError as exc:
            raise SMTHUnavailable("could not mark the shared Chromium tab") from exc
        LOGGER.info("created task-owned tab in shared Chromium")
        return page

    async def stop(self) -> None:
        if self._context and not self._shared_browser:
            await self._context.close()
        self._context = None
        self._page = None
        self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def open_login(self) -> None:
        await self.page.goto(
            "https://m.newsmth.net/index",
            wait_until="domcontentloaded",
        )

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.settings.request_interval_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    async def fetch_article_html(self, board: str, topic: str, page: int) -> str:
        async with self._request_lock:
            if time.monotonic() < self._verification_block_until:
                raise GuestVerificationRequired(
                    "SMTH login session is still marked invalid"
                )
            try:
                html = await self._fetch_article_html_locked(board, topic, page)
            except GuestVerificationRequired:
                self._verification_block_until = (
                    time.monotonic()
                    + self.settings.guest_verification_cooldown_seconds
                )
                raise
            self._verification_block_until = 0.0
            return html

    async def _fetch_article_html_locked(
        self, board: str, topic: str, page: int
    ) -> str:
        await self._throttle()
        url = f"{self.settings.smth_base}/nForum/article/{board}/{topic}?ajax&p={page}"
        try:
            response = await self.context.request.get(
                url,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{self.settings.smth_base}/nForum/board/{board}",
                    "Accept": "text/html,application/xhtml+xml",
                },
                fail_on_status_code=False,
                timeout=30_000,
            )
        except PlaywrightError as exc:
            raise SMTHUnavailable(f"article request failed: {exc}") from exc

        body = await response.body()
        text = body.decode("gb18030", errors="replace")
        if response.status in {403, 429}:
            raise SMTHUnavailable(f"SMTH rate limit response: HTTP {response.status}")
        if response.status >= 400:
            raise SMTHUnavailable(f"SMTH article response: HTTP {response.status}")
        if "IP已被封禁" in text or "ip banned" in text.lower():
            raise SMTHUnavailable("SMTH temporarily blocked this server IP")
        if is_guest_verification(text):
            raise GuestVerificationRequired("SMTH login session is not valid")
        return text

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        async with self._request_lock:
            await self._throttle()
            try:
                response = await self.context.request.get(
                    url,
                    headers={
                        "Referer": f"{self.settings.smth_base}/",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                    fail_on_status_code=False,
                    timeout=30_000,
                )
            except PlaywrightError as exc:
                raise SMTHUnavailable(f"image request failed: {exc}") from exc
            if response.status >= 400:
                raise SMTHUnavailable(f"image response: HTTP {response.status}")
            content_type = response.headers.get("content-type", "").split(";")[0]
            if not content_type.startswith("image/"):
                raise SMTHUnavailable("upstream response was not an image")
            return await response.body(), content_type

    async def session_status(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and self._status_cache
            and now - self._status_cache[0] < 20
        ):
            return self._status_cache[1]

        async with self._status_lock:
            if (
                not force
                and self._status_cache
                and time.monotonic() - self._status_cache[0] < 20
            ):
                return self._status_cache[1]
            result: dict[str, Any] = {
                "valid": False,
                "user": "",
                "checked_at": int(time.time()),
            }
            try:
                await self._throttle()
                response = await self.context.request.get(
                    f"{self.settings.smth_base}/nForum/user/ajax_session.json",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    fail_on_status_code=False,
                    timeout=20_000,
                )
                if response.ok:
                    data = await response.json()
                    user = str(data.get("id") or data.get("user") or "").strip()
                    result["valid"] = bool(user and user.lower() != "guest")
                    result["user"] = user
                    if result["valid"]:
                        self._verification_block_until = 0.0
                else:
                    result["error"] = f"HTTP {response.status}"
            except (PlaywrightError, json.JSONDecodeError, SMTHUnavailable) as exc:
                result["error"] = type(exc).__name__
            self._status_cache = (time.monotonic(), result)
            return result

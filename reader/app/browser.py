from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


class SMTHRateLimited(SMTHUnavailable):
    pass


SMTH_COOKIE_URLS = (
    "https://www.newsmth.net",
    "https://m.newsmth.net",
)
SMTH_COOKIE_NAMES = {
    "main[UTMPUSERID]",
    "main[UTMPKEY]",
    "main[UTMPNUM]",
}
RATE_LIMIT_MARKERS = (
    "ip已被封禁",
    "访问过于频繁",
    "请求过于频繁",
    "因为访问过于频繁",
    "系统封禁",
    "temporarily blocked",
    "rate limit",
)
PAGE_STATE_SCRIPT = """
() => {
  const text = (document.body?.innerText || "").slice(0, 30000);
  const html = (document.documentElement?.outerHTML || "").slice(0, 400000);
  return {
    url: location.href,
    title: document.title || "",
    text,
    html,
    article_ready: Boolean(document.querySelector(
      "table.article, .a-content, .article-content, .article, .post-content, [data-article]"
    )),
    logged_in: /注销|退出登录|个人中心/.test(text),
  };
}
"""


def is_rate_limit_page(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def _is_smth_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lower().lstrip(".")
    return domain == "newsmth.net" or domain.endswith(".newsmth.net")


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
        await self._restore_cookie_snapshot()
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
        await self._restore_cookie_snapshot()

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

    @property
    def mobile_base(self) -> str:
        return self.settings.smth_base.replace("//www.", "//m.")

    async def open_login(self) -> None:
        await self._navigate("https://m.newsmth.net/index")

    async def open_target(self, board: str, topic: str, page: int = 1) -> None:
        await self._navigate(
            f"{self.mobile_base}/article/{board}/{topic}?p={max(1, min(page, 999))}"
        )

    async def _navigate(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            if "ERR_ABORTED" not in str(exc).upper():
                raise SMTHUnavailable(f"navigation failed: {exc}") from exc
            current_url = self.page.url
            actual = urlsplit(current_url)
            expected = urlsplit(url)
            if (
                actual.scheme == expected.scheme
                and actual.netloc == expected.netloc
                and actual.path == expected.path
            ):
                LOGGER.info("navigation reported ERR_ABORTED after reaching page")
                return
            raise SMTHUnavailable(f"navigation failed: {exc}") from exc

    async def _smth_cookies(self) -> list[dict[str, Any]]:
        cookies = await self.context.cookies(list(SMTH_COOKIE_URLS))
        return [cookie for cookie in cookies if _is_smth_cookie(cookie)]

    async def _has_smth_session_cookie(self) -> bool:
        try:
            cookies = await self._smth_cookies()
        except PlaywrightError:
            return False
        return any(str(cookie.get("name") or "") in SMTH_COOKIE_NAMES for cookie in cookies)

    async def _restore_cookie_snapshot(self) -> None:
        if await self._has_smth_session_cookie():
            return
        snapshot_path = self.settings.cookie_snapshot_path
        if not snapshot_path.is_file():
            return
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            cookies = [
                cookie
                for cookie in payload
                if isinstance(cookie, dict) and _is_smth_cookie(cookie)
            ]
            if not cookies:
                return
            await self.context.add_cookies(cookies)
            LOGGER.info("restored SMTH cookies from protected snapshot")
        except (OSError, ValueError, TypeError, PlaywrightError):
            LOGGER.warning("could not restore SMTH cookie snapshot")

    async def _save_cookie_snapshot(self) -> None:
        try:
            cookies = await self._smth_cookies()
            if not cookies:
                return
            snapshot_path = self.settings.cookie_snapshot_path
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = snapshot_path.with_name(f".{snapshot_path.name}.tmp")
            descriptor = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(cookies, handle, ensure_ascii=True, separators=(",", ":"))
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, snapshot_path)
            os.chmod(snapshot_path, 0o600)
        except (OSError, PlaywrightError):
            LOGGER.warning("could not save SMTH cookie snapshot")

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.settings.request_interval_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    async def fetch_article_html(self, board: str, topic: str, page: int) -> str:
        async with self._request_lock:
            if time.monotonic() < self._verification_block_until:
                state = await self.page_status()
                if state["rate_limited"]:
                    raise SMTHRateLimited("SMTH is rate limiting this session")
                recovered = bool(state["article_ready"] and not state["challenge"])
                if not recovered:
                    status = await self.session_status(force=True)
                    recovered = bool(status.get("valid"))
                if recovered:
                    self._verification_block_until = 0.0
                else:
                    raise GuestVerificationRequired(
                        "SMTH still requires guest read verification"
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
            await self._save_cookie_snapshot()
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
            raise SMTHRateLimited(f"SMTH rate limit response: HTTP {response.status}")
        if response.status >= 400:
            raise SMTHUnavailable(f"SMTH article response: HTTP {response.status}")
        if is_rate_limit_page(text) or "ip banned" in text.lower():
            raise SMTHRateLimited("SMTH temporarily blocked this server IP")
        if is_guest_verification(text):
            raise GuestVerificationRequired("SMTH returned the guest read challenge")
        return text

    async def page_status(self) -> dict[str, Any]:
        try:
            raw = await self.page.evaluate(PAGE_STATE_SCRIPT)
        except PlaywrightError as exc:
            raise SMTHUnavailable(f"could not inspect browser page: {exc}") from exc
        data = raw if isinstance(raw, dict) else {}
        page_html = str(data.get("html") or "")
        page_text = str(data.get("text") or "")
        combined = f"{page_html}\n{page_text}"
        url = str(data.get("url") or "")
        challenge = is_guest_verification(combined)
        rate_limited = is_rate_limit_page(combined)
        article_ready = bool(data.get("article_ready"))
        if not article_ready and "/article/" in url and not challenge:
            article_ready = len(page_text.strip()) >= 120
        return {
            "url": url,
            "title": str(data.get("title") or "")[:160],
            "challenge": challenge,
            "rate_limited": rate_limited,
            "article_ready": article_ready and not challenge and not rate_limited,
            "logged_in": bool(data.get("logged_in")),
        }

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
                        await self._save_cookie_snapshot()
                else:
                    body = await response.body()
                    text = body.decode("gb18030", errors="replace")
                    result["rate_limited"] = (
                        response.status in {403, 429} or is_rate_limit_page(text)
                    )
                    result["error"] = f"HTTP {response.status}"
            except (PlaywrightError, json.JSONDecodeError, SMTHUnavailable) as exc:
                result["error"] = type(exc).__name__
            self._status_cache = (time.monotonic(), result)
            return result

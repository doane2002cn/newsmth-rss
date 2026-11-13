from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import suppress
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
from .parser import (
    GuestVerificationRequired,
    SMTHLoginRequired,
    is_guest_verification,
    is_login_required,
)

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
SMTH_GUEST_USER_IDS = {"", "0", "anonymous", "guest"}
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


def _has_complete_smth_session(cookies: list[dict[str, Any]]) -> bool:
    values = {
        str(cookie.get("name") or ""): str(cookie.get("value") or "").strip()
        for cookie in cookies
    }
    if not SMTH_COOKIE_NAMES.issubset(values):
        return False
    user_id = values["main[UTMPUSERID]"].lower()
    return (
        user_id not in SMTH_GUEST_USER_IDS
        and bool(values["main[UTMPKEY]"])
        and bool(values["main[UTMPNUM]"])
    )


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._shared_browser = bool(settings.browser_cdp_url)
        self._close_context_on_stop = False
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._status_lock = asyncio.Lock()
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        self._verification_block_until = 0.0
        self._last_cookie_snapshot_digest = b""
        self._session_watch_task: asyncio.Task[None] | None = None
        self._session_restore_pause_until = 0.0

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

    def _proxy_settings(self) -> dict[str, str] | None:
        if not self.settings.browser_proxy_server:
            return None
        return {
            "server": self.settings.browser_proxy_server,
            "bypass": "localhost,127.0.0.1",
        }

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        if self._shared_browser:
            await self._start_shared_browser()
        else:
            await self._start_local_browser()
        self._context.set_default_timeout(30_000)
        self._context.set_default_navigation_timeout(45_000)
        self._session_watch_task = asyncio.create_task(
            self._watch_session_cookies()
        )
        LOGGER.info("Chromium browser is ready in %s mode", self.mode)

    async def _start_local_browser(self) -> None:
        Path(self.settings.profile_dir).parent.mkdir(parents=True, exist_ok=True)
        assert self._playwright is not None
        proxy = self._proxy_settings()
        options: dict[str, Any] = {}
        if proxy:
            options["proxy"] = proxy
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            accept_downloads=False,
            **options,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--window-position=0,0",
                "--window-size=1280,800",
            ],
        )
        self._close_context_on_stop = True
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
        proxy = self._proxy_settings()
        options: dict[str, Any] = {}
        if proxy:
            options["proxy"] = proxy
        self._browser = await self._playwright.chromium.connect_over_cdp(
            self.settings.browser_cdp_url
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            accept_downloads=False,
            **options,
        )
        self._close_context_on_stop = True
        self._page = await self._find_reader_page()
        await self._restore_cookie_snapshot()
        LOGGER.info("created isolated browser context for SMTH reader")

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
        if self._session_watch_task:
            self._session_watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._session_watch_task
            self._session_watch_task = None
        if self._context and self._close_context_on_stop:
            await self._context.close()
        self._context = None
        self._page = None
        self._browser = None
        self._close_context_on_stop = False
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    @property
    def mobile_base(self) -> str:
        return self.settings.smth_base.replace("//www.", "//m.")

    async def open_login(self) -> None:
        self._pause_session_restore()
        await self._navigate("https://m.newsmth.net/index")

    async def open_target(self, board: str, topic: str, page: int = 1) -> None:
        self._pause_session_restore()
        await self._navigate(
            f"{self.settings.smth_base}/nForum/article/{board}/{topic}"
            f"?p={max(1, min(page, 999))}"
        )

    def _pause_session_restore(self) -> None:
        self._session_restore_pause_until = max(
            self._session_restore_pause_until,
            time.monotonic() + self.settings.admin_login_ttl_seconds,
        )

    def _session_restore_is_paused(self) -> bool:
        return time.monotonic() < self._session_restore_pause_until

    async def _navigate(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
            await self.page.bring_to_front()
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
        return _has_complete_smth_session(cookies)

    async def _restore_cookie_snapshot(self) -> bool:
        if await self._has_smth_session_cookie():
            return False
        snapshot_path = self.settings.cookie_snapshot_path
        if not snapshot_path.is_file():
            return False
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            cookies = [
                cookie
                for cookie in payload
                if isinstance(cookie, dict) and _is_smth_cookie(cookie)
            ]
            if not _has_complete_smth_session(cookies):
                return False
            await self.context.add_cookies(cookies)
            LOGGER.info("restored SMTH cookies from protected snapshot")
            return True
        except (OSError, ValueError, TypeError, PlaywrightError):
            LOGGER.warning("could not restore SMTH cookie snapshot")
            return False

    async def _save_cookie_snapshot(
        self, cookies: list[dict[str, Any]] | None = None
    ) -> bool:
        try:
            if cookies is None:
                cookies = await self._smth_cookies()
            if not _has_complete_smth_session(cookies):
                return False
            snapshot_path = self.settings.cookie_snapshot_path
            ordered_cookies = sorted(
                cookies,
                key=lambda cookie: (
                    str(cookie.get("domain") or ""),
                    str(cookie.get("path") or ""),
                    str(cookie.get("name") or ""),
                ),
            )
            payload = json.dumps(
                ordered_cookies,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            digest = hashlib.sha256(payload).digest()
            if self._last_cookie_snapshot_digest == digest and snapshot_path.is_file():
                return True
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{snapshot_path.name}.",
                suffix=".tmp",
                dir=snapshot_path.parent,
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, snapshot_path)
            finally:
                temp_path.unlink(missing_ok=True)
            os.chmod(snapshot_path, 0o600)
            self._last_cookie_snapshot_digest = digest
            return True
        except (OSError, PlaywrightError):
            LOGGER.warning("could not save SMTH cookie snapshot")
            return False

    async def _watch_session_cookies(self) -> None:
        while True:
            try:
                cookies = await self._smth_cookies()
                complete = _has_complete_smth_session(cookies)
                if not complete and not self._session_restore_is_paused():
                    if await self._restore_cookie_snapshot():
                        cookies = await self._smth_cookies()
                        complete = _has_complete_smth_session(cookies)
                if complete:
                    self._session_restore_pause_until = 0.0
                    await self._save_cookie_snapshot(cookies)
            except asyncio.CancelledError:
                raise
            except PlaywrightError:
                LOGGER.debug("could not inspect SMTH cookies", exc_info=True)
            await asyncio.sleep(3)

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
        if not self._session_restore_is_paused():
            await self._restore_cookie_snapshot()
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
        if is_login_required(text):
            if await self._has_smth_session_cookie():
                status = await self.session_status(force=True)
                if status.get("rate_limited"):
                    raise SMTHRateLimited("SMTH is rate limiting this session")
            raise SMTHLoginRequired("SMTH requires login")
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
        try:
            cookies = await self._smth_cookies()
        except PlaywrightError:
            cookies = []
        session_cookie_present = _has_complete_smth_session(cookies)
        logged_in = bool(data.get("logged_in")) or session_cookie_present
        session_persisted = self.settings.cookie_snapshot_path.is_file()
        if (article_ready or logged_in) and not challenge and not rate_limited:
            if session_cookie_present:
                self._session_restore_pause_until = 0.0
            persisted_now = await self._save_cookie_snapshot(cookies)
            session_persisted = session_persisted or persisted_now
            if persisted_now and article_ready:
                self._verification_block_until = 0.0
        return {
            "url": url,
            "title": str(data.get("title") or "")[:160],
            "challenge": challenge,
            "rate_limited": rate_limited,
            "article_ready": article_ready and not challenge and not rate_limited,
            "logged_in": logged_in,
            "session_cookie_present": session_cookie_present,
            "snapshot_available": session_persisted,
            "session_restore_paused": self._session_restore_is_paused(),
            "session_persisted": session_persisted,
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
                if not self._session_restore_is_paused():
                    await self._restore_cookie_snapshot()
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

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from app.browser import BrowserManager, SMTHLoginRequired, SMTHRateLimited
from app.config import Settings
from app.parser import GuestVerificationRequired


SECRET = "browser-test-secret-" + "z" * 40
SESSION_COOKIES = [
    {
        "name": "main[UTMPUSERID]",
        "value": "alice",
        "domain": ".newsmth.net",
        "path": "/",
    },
    {
        "name": "main[UTMPKEY]",
        "value": "secret-key",
        "domain": ".newsmth.net",
        "path": "/",
    },
    {
        "name": "main[UTMPNUM]",
        "value": "12345",
        "domain": ".newsmth.net",
        "path": "/",
    },
]
GUEST_SESSION_COOKIES = [
    {
        **cookie,
        "value": "guest" if cookie["name"] == "main[UTMPUSERID]" else cookie["value"],
    }
    for cookie in SESSION_COOKIES
]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        secret=SECRET,
        public_base="https://reader.example",
        data_dir=tmp_path,
        article_ttl_seconds=1800,
        stale_ttl_seconds=604800,
        reader_token_ttl_seconds=7776000,
        image_token_ttl_seconds=604800,
        image_cache_ttl_seconds=604800,
        admin_session_ttl_seconds=1800,
        admin_login_ttl_seconds=900,
        request_interval_seconds=0.0,
        cookie_secure=False,
        browser_enabled=True,
        browser_cdp_url="",
        smth_base="https://www.newsmth.net",
        vnc_http_url="http://127.0.0.1:6081",
        vnc_websocket_url="ws://127.0.0.1:6081/websockify",
        guest_verification_cooldown_seconds=600,
    )


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", data=None) -> None:
        self.status = status
        self._body = body
        self._data = data
        self.headers = {"content-type": "text/html; charset=gb18030"}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    async def body(self) -> bytes:
        return self._body

    async def json(self):
        return self._data


class FakeRequest:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def get(self, url: str, *args, **kwargs) -> FakeResponse:
        self.calls.append(url)
        return self.responses.pop(0)


class FakeContext:
    def __init__(
        self,
        responses: list[FakeResponse] | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.request = FakeRequest(responses or [])
        self.cookie_data = cookies or []
        self.added_cookies: list[dict[str, Any]] = []
        self.pages: list[FakePage] = []
        self.new_page_calls = 0
        self.default_timeout = 0
        self.default_navigation_timeout = 0
        self.closed = False

    async def cookies(self, urls):
        return list(self.cookie_data)

    async def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)
        for incoming in cookies:
            key = (
                incoming.get("domain"),
                incoming.get("path"),
                incoming.get("name"),
            )
            self.cookie_data = [
                cookie
                for cookie in self.cookie_data
                if (
                    cookie.get("domain"),
                    cookie.get("path"),
                    cookie.get("name"),
                )
                != key
            ]
            self.cookie_data.append(incoming)

    async def new_page(self):
        self.new_page_calls += 1
        page = FakePage()
        self.pages.append(page)
        return page

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout):
        self.default_navigation_timeout = timeout

    async def close(self):
        self.closed = True


class FakePage:
    def __init__(
        self,
        *,
        url: str = "https://m.newsmth.net/article/NewExpress/1?p=1",
        state: dict[str, Any] | None = None,
        goto_error: Exception | None = None,
    ) -> None:
        self.url = url
        self.state = state or {
            "url": url,
            "title": "文章",
            "text": "文章正文" * 80,
            "html": "<table class='article'><td>文章正文</td></table>",
            "article_ready": True,
            "logged_in": True,
        }
        self.goto_error = goto_error
        self.goto_calls: list[str] = []

    async def evaluate(self, script: str, *args):
        if script.startswith("(name)"):
            return None
        return self.state

    async def goto(self, url: str, **kwargs):
        self.goto_calls.append(url)
        self.url = url
        if self.goto_error:
            raise self.goto_error

    async def bring_to_front(self):
        return None


class FakeChromium:
    def __init__(self, browser: "FakeBrowser") -> None:
        self.browser = browser
        self.cdp_urls: list[str] = []

    async def connect_over_cdp(self, url: str):
        self.cdp_urls.append(url)
        return self.browser


class FakePlaywright:
    def __init__(self, browser: "FakeBrowser") -> None:
        self.chromium = FakeChromium(browser)


class FakeBrowser:
    def __init__(self, default_context: FakeContext) -> None:
        self.contexts = [default_context]
        self.default_context = default_context
        self.context_options: list[dict[str, Any]] = []
        self.created_contexts: list[FakeContext] = []

    async def new_context(self, **kwargs):
        self.context_options.append(kwargs)
        context = FakeContext()
        self.created_contexts.append(context)
        return context


class RecordingPersistentChromium:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def launch_persistent_context(
        self, user_data_dir: str, **kwargs: Any
    ) -> FakeContext:
        self.calls.append(kwargs)
        return FakeContext()


class RecordingPersistentPlaywright:
    def __init__(self) -> None:
        self.chromium = RecordingPersistentChromium()


def test_verification_cooldown_recovers_after_page_is_readable(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    manager = BrowserManager(settings)
    manager._context = FakeContext(  # type: ignore[assignment]
        [
            FakeResponse(body=b"Read verification"),
            FakeResponse(body=b"<table class='article'>article</table>"),
        ],
        cookies=SESSION_COOKIES,
    )
    manager._page = FakePage()  # type: ignore[assignment]

    with pytest.raises(GuestVerificationRequired):
        asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))

    html = asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))

    assert "table" in html
    assert manager._context.request.calls == [
        "https://www.newsmth.net/nForum/article/NewExpress/1?ajax&p=1",
        "https://www.newsmth.net/nForum/article/NewExpress/1?ajax&p=1",
    ]
    snapshot = settings.cookie_snapshot_path
    assert snapshot.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert "alice" in snapshot.read_text(encoding="utf-8")


def test_open_target_uses_mobile_article_not_index(tmp_path: Path) -> None:
    manager = BrowserManager(make_settings(tmp_path))
    page = FakePage()
    manager._page = page  # type: ignore[assignment]

    asyncio.run(manager.open_target("NewExpress", "47582736", 3))

    assert page.goto_calls == [
        "https://www.newsmth.net/nForum/article/NewExpress/47582736?p=3"
    ]


def test_err_aborted_is_accepted_when_navigation_reached_target(
    tmp_path: Path,
) -> None:
    manager = BrowserManager(make_settings(tmp_path))
    target = "https://www.newsmth.net/nForum/article/NewExpress/47582736?p=1"
    page = FakePage(
        url=target,
        goto_error=PlaywrightError("net::ERR_ABORTED at navigation"),
    )
    manager._page = page  # type: ignore[assignment]

    asyncio.run(manager.open_target("NewExpress", "47582736", 1))

    assert page.goto_calls == [target]


def test_http_403_is_rate_limit_not_login_expiry(tmp_path: Path) -> None:
    manager = BrowserManager(make_settings(tmp_path))
    manager._context = FakeContext(  # type: ignore[assignment]
        [FakeResponse(status=403, body=b"forbidden")]
    )

    with pytest.raises(SMTHRateLimited):
        asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))


def test_rate_limit_page_is_classified_without_clearing_cookie(
    tmp_path: Path,
) -> None:
    manager = BrowserManager(make_settings(tmp_path))
    manager._context = FakeContext(  # type: ignore[assignment]
        [FakeResponse(body="因为访问过于频繁，已被系统封禁".encode("gb18030"))]
    )

    with pytest.raises(SMTHRateLimited):
        asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))
    assert manager._verification_block_until == 0


def test_login_required_with_cookies_is_rate_limit(tmp_path: Path) -> None:
    manager = BrowserManager(make_settings(tmp_path))
    manager._context = FakeContext(  # type: ignore[assignment]
        [
            FakeResponse(body="您未登录,请登录后继续操作".encode("gb18030")),
            FakeResponse(status=403, body=b"forbidden"),
        ],
        cookies=SESSION_COOKIES,
    )

    with pytest.raises(SMTHRateLimited):
        asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))


def test_login_required_without_cookies_is_not_challenge(
    tmp_path: Path,
) -> None:
    manager = BrowserManager(make_settings(tmp_path))
    manager._context = FakeContext(  # type: ignore[assignment]
        [FakeResponse(body="您未登录,请登录后继续操作".encode("gb18030"))],
        cookies=[],
    )

    with pytest.raises(SMTHLoginRequired):
        asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))


def test_cookie_snapshot_restores_only_when_browser_has_no_smth_cookie(
    tmp_path: Path,
) -> None:
    settings = replace(make_settings(tmp_path), request_interval_seconds=0)
    snapshot = settings.cookie_snapshot_path
    snapshot.write_text(
        json.dumps(
            [
                *SESSION_COOKIES,
                {
                    "name": "unrelated",
                    "value": "ignored",
                    "domain": ".example.com",
                    "path": "/",
                },
            ]
        ),
        encoding="utf-8",
    )
    manager = BrowserManager(settings)
    context = FakeContext()
    manager._context = context  # type: ignore[assignment]

    asyncio.run(manager._restore_cookie_snapshot())

    assert [cookie["name"] for cookie in context.added_cookies] == [
        "main[UTMPUSERID]",
        "main[UTMPKEY]",
        "main[UTMPNUM]",
    ]


def test_local_browser_uses_configured_proxy(tmp_path: Path) -> None:
    settings = replace(
        make_settings(tmp_path),
        browser_proxy_server="socks5://172.19.0.1:1080",
    )
    manager = BrowserManager(settings)
    manager._playwright = RecordingPersistentPlaywright()  # type: ignore[assignment]

    asyncio.run(manager._start_local_browser())

    assert manager._playwright.chromium.calls[0]["proxy"] == {
        "server": "socks5://172.19.0.1:1080",
        "bypass": "localhost,127.0.0.1",
    }


def test_page_status_persists_complete_session_without_upstream_request(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    manager = BrowserManager(settings)
    context = FakeContext(cookies=SESSION_COOKIES)
    manager._context = context  # type: ignore[assignment]
    manager._page = FakePage()  # type: ignore[assignment]

    status = asyncio.run(manager.page_status())

    assert status["logged_in"] is True
    assert status["session_persisted"] is True
    assert context.request.calls == []
    snapshot = settings.cookie_snapshot_path
    assert snapshot.is_file()
    assert "secret-key" in snapshot.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_page_status_does_not_persist_partial_guest_cookies(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    manager = BrowserManager(settings)
    manager._context = FakeContext(  # type: ignore[assignment]
        cookies=[SESSION_COOKIES[0]]
    )
    manager._page = FakePage(  # type: ignore[assignment]
        state={
            "url": "https://m.newsmth.net/index",
            "title": "水木社区",
            "text": "游客页面",
            "html": "<html>游客页面</html>",
            "article_ready": False,
            "logged_in": False,
        }
    )

    status = asyncio.run(manager.page_status())

    assert status["logged_in"] is False
    assert status["session_persisted"] is False
    assert not settings.cookie_snapshot_path.exists()


def test_complete_guest_cookie_names_are_not_persisted(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    manager = BrowserManager(settings)
    manager._context = FakeContext(  # type: ignore[assignment]
        cookies=GUEST_SESSION_COOKIES
    )
    manager._page = FakePage(  # type: ignore[assignment]
        state={
            "url": "https://m.newsmth.net/index",
            "title": "水木社区",
            "text": "游客页面",
            "html": "<html>游客页面</html>",
            "article_ready": False,
            "logged_in": False,
        }
    )

    status = asyncio.run(manager.page_status())

    assert status["logged_in"] is False
    assert status["session_persisted"] is False
    assert not settings.cookie_snapshot_path.exists()


def test_partial_session_cookie_is_completed_from_snapshot(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.cookie_snapshot_path.write_text(
        json.dumps(SESSION_COOKIES),
        encoding="utf-8",
    )
    manager = BrowserManager(settings)
    context = FakeContext(cookies=[SESSION_COOKIES[0]])
    manager._context = context  # type: ignore[assignment]

    asyncio.run(manager._restore_cookie_snapshot())

    assert [cookie["name"] for cookie in context.added_cookies] == [
        "main[UTMPUSERID]",
        "main[UTMPKEY]",
        "main[UTMPNUM]",
    ]


def test_complete_live_session_is_not_overwritten_by_snapshot(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.cookie_snapshot_path.write_text(
        json.dumps(SESSION_COOKIES),
        encoding="utf-8",
    )
    manager = BrowserManager(settings)
    context = FakeContext(cookies=SESSION_COOKIES)
    manager._context = context  # type: ignore[assignment]

    asyncio.run(manager._restore_cookie_snapshot())

    assert context.added_cookies == []


def test_guest_snapshot_is_not_restored(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.cookie_snapshot_path.write_text(
        json.dumps(GUEST_SESSION_COOKIES),
        encoding="utf-8",
    )
    manager = BrowserManager(settings)
    context = FakeContext()
    manager._context = context  # type: ignore[assignment]

    asyncio.run(manager._restore_cookie_snapshot())

    assert context.added_cookies == []


def test_shared_browser_uses_isolated_context_not_default_context(
    tmp_path: Path,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        browser_cdp_url="http://ai-workstation:9223",
    )
    default_context = FakeContext(cookies=GUEST_SESSION_COOKIES)
    browser = FakeBrowser(default_context)
    manager = BrowserManager(settings)
    manager._playwright = FakePlaywright(browser)  # type: ignore[assignment]

    asyncio.run(manager._start_shared_browser())

    assert manager.context is browser.created_contexts[0]
    assert manager.context is not default_context
    assert browser.context_options == [
        {
            "viewport": {"width": 1280, "height": 800},
            "device_scale_factor": 1,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "accept_downloads": False,
        }
    ]
    assert browser.created_contexts[0].new_page_calls == 1
    assert default_context.new_page_calls == 0


def test_session_watcher_persists_cookies_without_network_request(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    manager = BrowserManager(settings)
    context = FakeContext(cookies=SESSION_COOKIES)
    manager._context = context  # type: ignore[assignment]

    async def run_watcher() -> None:
        task = asyncio.create_task(manager._watch_session_cookies())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_watcher())

    assert context.request.calls == []
    snapshot = settings.cookie_snapshot_path
    assert snapshot.is_file()
    assert "alice" in snapshot.read_text(encoding="utf-8")


def test_session_watcher_restores_guest_cookies_from_snapshot(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.cookie_snapshot_path.write_text(
        json.dumps(SESSION_COOKIES),
        encoding="utf-8",
    )
    manager = BrowserManager(settings)
    context = FakeContext(cookies=GUEST_SESSION_COOKIES)
    manager._context = context  # type: ignore[assignment]

    async def run_watcher() -> None:
        task = asyncio.create_task(manager._watch_session_cookies())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_watcher())

    restored = {
        cookie["name"]: cookie["value"] for cookie in context.added_cookies
    }
    assert restored["main[UTMPUSERID]"] == "alice"
    assert context.request.calls == []


def test_article_request_restores_snapshot_before_upstream_call(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.cookie_snapshot_path.write_text(
        json.dumps(SESSION_COOKIES),
        encoding="utf-8",
    )
    manager = BrowserManager(settings)
    context = FakeContext(
        [FakeResponse(body=b"<table class='article'>article</table>")],
        cookies=GUEST_SESSION_COOKIES,
    )
    manager._context = context  # type: ignore[assignment]

    html = asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))

    assert "article" in html
    restored = {
        cookie["name"]: cookie["value"] for cookie in context.added_cookies
    }
    assert restored["main[UTMPUSERID]"] == "alice"


def test_session_watcher_does_not_interrupt_active_login(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.cookie_snapshot_path.write_text(
        json.dumps(SESSION_COOKIES),
        encoding="utf-8",
    )
    manager = BrowserManager(settings)
    context = FakeContext(cookies=GUEST_SESSION_COOKIES)
    manager._context = context  # type: ignore[assignment]
    manager._pause_session_restore()

    async def run_watcher() -> None:
        task = asyncio.create_task(manager._watch_session_cookies())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_watcher())

    assert context.added_cookies == []

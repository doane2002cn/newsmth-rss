from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.browser import BrowserManager
from app.config import Settings
from app.parser import GuestVerificationRequired


SECRET = "browser-test-secret-" + "z" * 40


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
    status = 200

    async def body(self) -> bytes:
        return "Read verification".encode()


class FakeRequest:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, *args, **kwargs) -> FakeResponse:
        self.calls += 1
        return FakeResponse()


class FakeContext:
    def __init__(self) -> None:
        self.request = FakeRequest()


def test_guest_verification_trips_fast_fail_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.browser.is_guest_verification", lambda text: True)
    manager = BrowserManager(
        replace(make_settings(tmp_path), request_interval_seconds=0)
    )
    manager._context = FakeContext()  # type: ignore[assignment]

    with pytest.raises(GuestVerificationRequired):
        asyncio.run(manager.fetch_article_html("NewExpress", "1", 1))
    with pytest.raises(GuestVerificationRequired):
        asyncio.run(manager.fetch_article_html("NewExpress", "2", 1))

    assert manager._context.request.calls == 1

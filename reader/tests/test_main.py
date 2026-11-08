from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.browser import SMTHRateLimited
from app.config import Settings
from app.main import _article_cache_key, create_app
from app.parser import GuestVerificationRequired
from app.signing import admin_login_token, article_token, image_token


SECRET = "main-test-secret-" + "y" * 40


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
        browser_enabled=False,
        browser_cdp_url="",
        smth_base="https://www.newsmth.net",
        vnc_http_url="http://127.0.0.1:6081",
        vnc_websocket_url="ws://127.0.0.1:6081/websockify",
    )


class GuestBrowser:
    async def fetch_article_html(self, board: str, topic: str, page: int) -> str:
        raise GuestVerificationRequired("challenge")


class RateLimitedBrowser:
    async def fetch_article_html(self, board: str, topic: str, page: int) -> str:
        raise SMTHRateLimited("HTTP 403")


class TargetBrowser:
    mode = "test"

    def __init__(self, *, valid: bool = False) -> None:
        self.valid = valid
        self.open_target_calls: list[tuple[str, str, int]] = []
        self.open_login_calls = 0

    async def open_target(self, board: str, topic: str, page: int) -> None:
        self.open_target_calls.append((board, topic, page))

    async def open_login(self) -> None:
        self.open_login_calls += 1

    async def session_status(self, force: bool = False):
        return {"valid": self.valid, "user": "alice" if self.valid else ""}

    async def page_status(self):
        return {
            "challenge": False,
            "rate_limited": False,
            "article_ready": True,
            "logged_in": True,
            "url": "https://m.newsmth.net/article/NewExpress/47582736?p=1",
        }


def test_health_and_admin_protection(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["browser_ready"] is False
        favicon = client.get("/favicon.ico")
        assert favicon.status_code == 204
        admin = client.get("/admin")
        assert admin.status_code == 401
        assert "guest-read-challenge" not in admin.text


def test_login_token_is_one_time_and_sets_session(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    token = admin_login_token(SECRET, int(time.time()) + 900, jti="b" * 32)
    with TestClient(app) as first:
        response = first.get(f"/login/{token}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin?open=1")
        admin = first.get("/admin")
        assert admin.status_code == 200
        assert "水木远程登录" in admin.text

    with TestClient(app) as second:
        replay = second.get(f"/login/{token}", follow_redirects=False)
        assert replay.status_code == 409


def test_login_session_defaults_to_thirty_days(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        admin_session_ttl_seconds=30 * 24 * 60 * 60,
    )
    app = create_app(settings)
    token = admin_login_token(SECRET, int(time.time()) + 900, jti="c" * 32)
    with TestClient(app) as client:
        response = client.get(f"/login/{token}", follow_redirects=False)
        assert "Max-Age=2592000" in response.headers["set-cookie"]
        admin = client.get("/admin")
        assert admin.status_code == 200
        assert "30 天" in admin.text


def test_article_without_login_does_not_leak_challenge(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    token = article_token(
        SECRET,
        "NewExpress",
        "47582736",
        "标题",
        int(time.time()) + 900,
    )
    with TestClient(app) as client:
        app.state.browser = GuestBrowser()
        response = client.get(f"/a/{token}")
        assert response.status_code == 401
        assert "完成阅读验证" in response.text
        assert "guest-read-challenge" not in response.text
        assert "请完成验证" not in response.text


def test_rate_limit_is_not_rendered_as_login_expiry(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    token = article_token(
        SECRET,
        "NewExpress",
        "47582736",
        "标题",
        int(time.time()) + 900,
    )
    with TestClient(app) as client:
        app.state.browser = RateLimitedBrowser()
        response = client.get(f"/a/{token}")
        assert response.status_code == 502
        assert "暂时限制了访问频率" in response.text
        assert "这不是登录失效" in response.text
        assert "/relogin/" not in response.text


def test_relogin_opens_the_current_article_challenge(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    token = article_token(
        SECRET,
        "NewExpress",
        "47582736",
        "标题",
        int(time.time()) + 900,
    )
    with TestClient(app) as client:
        response = client.get(
            f"/relogin/{token}?challenge=1&p=2",
            follow_redirects=False,
        )

    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    params = parse_qs(location.query)
    assert location.path == "/admin"
    assert params["open"] == ["1"]
    assert params["challenge"] == ["1"]
    assert params["board"] == ["NewExpress"]
    assert params["topic"] == ["47582736"]
    assert params["page"] == ["2"]
    assert params["next"] == [f"/a/{token}?p=2"]
    assert "smth_reader_admin=" in response.headers["set-cookie"]


def test_admin_open_uses_target_article_for_challenge(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    login = admin_login_token(SECRET, int(time.time()) + 900, jti="d" * 32)
    with TestClient(app) as client:
        client.get(f"/login/{login}")
        browser = TargetBrowser()
        app.state.browser = browser
        response = client.get(
            "/admin?"
            "open=1&next=%2Fa%2Ftoken%3Fp%3D1"
            "&board=NewExpress&topic=47582736&page=1&challenge=1"
        )

    assert response.status_code == 200
    assert browser.open_target_calls == [("NewExpress", "47582736", 1)]
    assert browser.open_login_calls == 0
    assert "已直接打开当前文章" in response.text


def test_admin_reuses_valid_session_without_opening_login(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    login = admin_login_token(SECRET, int(time.time()) + 900, jti="e" * 32)
    with TestClient(app) as client:
        client.get(f"/login/{login}")
        browser = TargetBrowser(valid=True)
        app.state.browser = browser
        response = client.get(
            "/admin?open=1&next=%2Fa%2Ftoken%3Fp%3D1",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/a/token?p=1"
    assert browser.open_login_calls == 0


def test_admin_page_status_is_available_for_auto_return(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    login = admin_login_token(SECRET, int(time.time()) + 900, jti="f" * 32)
    with TestClient(app) as client:
        client.get(f"/login/{login}")
        app.state.browser = TargetBrowser()
        response = client.get("/admin/page-status")

    assert response.status_code == 200
    assert response.json()["article_ready"] is True


def test_stale_cache_is_used_when_login_expires(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    token = article_token(
        SECRET,
        "NewExpress",
        "47582736",
        "缓存标题",
        int(time.time()) + 900,
    )
    cached = {
        "board": "NewExpress",
        "topic": "47582736",
        "title": "缓存标题",
        "page": 1,
        "total_posts": 2,
        "total_pages": 1,
        "posts": [
            {
                "author": "alice",
                "position": "楼主",
                "body_html": (
                    "<p><br><br><br>缓存正文"
                    "<br><br>第二行</p>"
                ),
                "post_id": "1",
                "date": "2026-10-25 10:00",
                "likes": [],
            },
            {
                "author": "bob",
                "position": "第2楼",
                "body_html": "<p>缓存回复</p>",
                "post_id": "2",
                "date": "2026-10-25 10:01",
                "likes": [],
            },
        ],
        "fetched_at": int(time.time()) - 4000,
    }
    cache_path = (
        settings.cache_dir
        / f"{_article_cache_key('NewExpress', '47582736', 1)}.json"
    )
    app.state.article_cache.write(
        _article_cache_key("NewExpress", "47582736", 1),
        cached,
    )
    old = time.time() - 4000
    os.utime(cache_path, (old, old))

    with TestClient(app) as client:
        app.state.browser = GuestBrowser()
        response = client.get(f"/a/{token}")
        assert response.status_code == 200
        assert response.headers["X-Reader-Cache"] == "STALE"
        assert "缓存正文" in response.text
        assert "<p>缓存正文<br/><br/>第二行</p>" in response.text
        assert "显示上次缓存" in response.text
        assert "打开水木原帖" not in response.text
        assert "/nForum/article/" not in response.text


def test_image_proxy_rejects_untrusted_hosts(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    token = image_token(
        SECRET,
        "https://evil.example/a.png",
        int(time.time()) + 900,
    )
    with TestClient(app) as client:
        response = client.get(f"/i/{token}")
        assert response.status_code == 400

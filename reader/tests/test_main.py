from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

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
        assert "重新登录水木" in response.text
        assert "guest-read-challenge" not in response.text
        assert "请完成验证" not in response.text


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

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import _article_cache_key, create_app
from app.signing import (
    admin_login_token,
    article_token,
    image_token,
    request_signature,
)


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


def signed_ingest_headers(
    body: bytes,
    *,
    timestamp: str | None = None,
    secret: str = SECRET,
) -> dict[str, str]:
    stamp = timestamp or str(int(time.time()))
    return {
        "Content-Type": "application/json",
        "X-Reader-Timestamp": stamp,
        "X-Reader-Signature": request_signature(secret, stamp, body),
    }


def test_health_and_admin_protection(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["browser_ready"] is False
        assert health.json()["snapshot_entries"] == 0
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


def test_article_without_snapshot_never_shows_challenge(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    token = article_token(
        SECRET,
        "NewExpress",
        "47582736",
        "标题",
        int(time.time()) + 900,
    )
    with TestClient(app) as client:
        response = client.get(f"/a/{token}")

    assert response.status_code == 202
    assert "还没有生成阅读副本" in response.text
    assert "READ CHALLENGE" not in response.text
    assert "完成阅读验证" not in response.text
    assert "guest-read-challenge" not in response.text
    assert "/relogin/" not in response.text
    assert "/admin" not in response.text


def test_signed_snapshot_ingest_and_read(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    payload = {
        "items": [
            {
                "board": "NewExpress",
                "topic": "47582736",
                "title": "朝阳 惠新西街 61平 5层 成交227万",
                "summary": "房东挂牌 260 万，最终成交 227 万。",
                "author": "alice",
                "published_at": int(time.time()) - 60,
                "source_url": (
                    "https://www.newsmth.net/nForum/article/"
                    "NewExpress/47582736"
                ),
            }
        ]
    }
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/snapshots",
            content=body,
            headers=signed_ingest_headers(body),
        )
        assert response.status_code == 200
        stored = response.json()["snapshots"][0]
        snapshot_id = stored["snapshot_id"]
        assert len(snapshot_id) == 64
        assert client.get("/healthz").json()["snapshot_entries"] == 1

        token = article_token(
            SECRET,
            "NewExpress",
            "47582736",
            "标题",
            int(time.time()) + 900,
            snapshot_id=snapshot_id,
        )
        article = client.get(f"/a/{token}")

    assert article.status_code == 200
    assert article.headers["X-Reader-Cache"] == "SNAPSHOT"
    assert "房东挂牌 260 万" in article.text
    assert "RSS 内容快照" in article.text
    assert "READ CHALLENGE" not in article.text
    assert "完成阅读验证" not in article.text
    assert "/relogin/" not in article.text
    assert "/nForum/article/" not in article.text


def test_snapshot_ingest_rejects_bad_signature(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    body = b'{"items":[{"board":"Test","topic":"1","title":"x"}]}'
    headers = signed_ingest_headers(body, secret="wrong-secret-" + "z" * 40)
    with TestClient(app) as client:
        response = client.post("/api/snapshots", content=body, headers=headers)
        assert response.status_code == 401
        assert client.get("/healthz").json()["snapshot_entries"] == 0


def test_fulltext_cache_is_preferred_over_snapshot(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    snapshot_id = app.state.snapshot_store.put(
        board="NewExpress",
        topic="47582736",
        title="快照标题",
        summary="快照摘要",
    )
    token = article_token(
        SECRET,
        "NewExpress",
        "47582736",
        "快照标题",
        int(time.time()) + 900,
        snapshot_id=snapshot_id,
    )
    cached = {
        "board": "NewExpress",
        "topic": "47582736",
        "title": "全文标题",
        "page": 1,
        "total_posts": 2,
        "total_pages": 1,
        "posts": [
            {
                "author": "alice",
                "position": "楼主",
                "body_html": "<p><br><br><br>全文正文<br><br>第二行</p>",
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
    app.state.article_cache.write(
        _article_cache_key("NewExpress", "47582736", 1),
        cached,
    )

    with TestClient(app) as client:
        response = client.get(f"/a/{token}")

    assert response.status_code == 200
    assert response.headers["X-Reader-Cache"] == "FULLTEXT"
    assert "全文正文" in response.text
    assert "<p>全文正文<br/><br/>第二行</p>" in response.text
    assert "已保存全文" in response.text
    assert "打开水木原帖" not in response.text
    assert "/nForum/article/" not in response.text


def test_relogin_is_not_reachable_from_article_links(tmp_path) -> None:
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

    assert response.status_code == 403
    assert "普通阅读链接不能进入管理会话" in response.text
    assert "set-cookie" not in response.headers


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

from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import smth_digest


SECRET = "unused-secret-" + "s" * 40


def item(topic: str = "193603") -> dict:
    return {
        "title": "[记忆] 真正的财务自由",
        "link": f"https://www.newsmth.net/nForum/article/Memory/{topic}",
        "guid": f"https://www.newsmth.net/nForum/article/Memory/{topic}",
        "board": "Memory",
        "pub": "",
        "ts": 1_790_000_000,
        "author": "tester",
        "desc": "一篇讨论财务自由的帖子。",
        "image_urls": [],
    }


def feed(description: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <lastBuildDate>Thu, 26 Nov 2026 10:00:00 +0800</lastBuildDate>
  <item>
    <title>[记忆] 真正的财务自由</title>
    <link>https://www.newsmth.net/nForum/article/Memory/193603</link>
    <guid>Memory/193603</guid>
    <author>tester</author>
    <pubDate>Thu, 26 Nov 2026 09:00:00 +0800</pubDate>
    <description><![CDATA[{description}]]></description>
  </item>
</channel></rss>""".encode("utf-8")


def test_extract_image_urls_runs_before_html_cleaning() -> None:
    raw = (
        '<img src="/nForum/img/face/smile.gif">'
        '<img src="https://static.mysmth.net/nForum/img/hotpic/Memory_1.jpg">'
        '<img src="https://evil.example/tracker.jpg">'
        '<img src="//static.mysmth.net/nForum/img/hotpic/Memory_1.jpg">'
    )
    urls = smth_digest.extract_image_urls(
        raw, "https://www.newsmth.net/nForum/article/Memory/1"
    )
    assert urls == ["https://static.mysmth.net/nForum/img/hotpic/Memory_1.jpg"]


def test_parse_feed_keeps_image_metadata_and_plain_summary() -> None:
    items, last_build = smth_digest.parse_feed(
        feed(
            '<img src="https://static.mysmth.net/nForum/img/hotpic/'
            'Memory_193603.jpg"><br/>正文第一行<br/>正文第二行'
        )
    )
    assert last_build.tzinfo is not None
    assert items[0]["desc"] == "正文第一行 正文第二行"
    assert items[0]["image_urls"] == [
        "https://static.mysmth.net/nForum/img/hotpic/Memory_193603.jpg"
    ]
    assert items[0]["author"] == "tester"


def test_gateway_link_is_stable_and_requests_external_browser(monkeypatch) -> None:
    monkeypatch.setenv("SMTH_GATEWAY_BASE", "https://smth-go.example/")
    link = smth_digest.gateway_link(item())
    assert link == (
        "https://smth-go.example/r/Memory/193603?"
        "lk_jump_to_browser=true&lk_mobile_jump_to_browser=true"
    )


def test_gateway_link_rejects_insecure_base(monkeypatch) -> None:
    monkeypatch.setenv("SMTH_GATEWAY_BASE", "http://smth-go.example")
    with pytest.raises(RuntimeError, match="HTTPS"):
        smth_digest.gateway_link(item())


def test_card_validation_rejects_raw_smth_links(monkeypatch) -> None:
    monkeypatch.setenv("SMTH_GATEWAY_BASE", "https://smth-go.example")
    card = {
        "elements": [
            {"tag": "markdown", "content": f"[原帖]({item()['link']})"}
        ]
    }
    with pytest.raises(RuntimeError, match="未转换"):
        smth_digest.validate_card(card)


def test_card_renders_gateway_links_and_uploaded_image(monkeypatch) -> None:
    monkeypatch.setenv("SMTH_GATEWAY_BASE", "https://smth-go.example")
    selected = item()
    sec = {
        "topten": [],
        "newexpress": [],
        "sensibility": [],
        "pictures": [],
    }
    card = smth_digest.render_card(
        sec,
        [{"n": 1, "why": "讨论具体"}],
        "",
        datetime(2026, 11, 26, 10, tzinfo=timezone.utc),
        False,
        [selected],
        False,
        {"Memory": ("", "记忆")},
        {"Memory/193603": "img_v3_test"},
    )
    links = smth_digest.validate_card(card)
    assert len(links) == 1
    assert any(
        element.get("tag") == "img"
        and element.get("img_key") == "img_v3_test"
        for element in card["elements"]
    )


def test_image_kind_uses_magic_bytes() -> None:
    assert smth_digest.image_kind(b"\x89PNG\r\n\x1a\npayload")[0] == ".png"
    with pytest.raises(ValueError, match="不支持"):
        smth_digest.image_kind(b"not-an-image")


def test_image_dimensions_are_read_from_magic_bytes() -> None:
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", 640, 480)
    )
    jpeg = (
        b"\xff\xd8\xff\xc0\x00\x11\x08"
        + struct.pack(">HH", 900, 1200)
        + b"\x00" * 11
    )
    gif = b"GIF89a" + struct.pack("<HH", 320, 200) + b"\x00\x00\x00"
    webp = (
        b"RIFF\x00\x00\x00\x00WEBP"
        b"VP8X\x0a\x00\x00\x00"
        b"\x00\x00\x00\x00"
        + (799).to_bytes(3, "little")
        + (599).to_bytes(3, "little")
    )
    ico = b"\x00\x00\x01\x00\x01\x00\x40\x40\x00\x00\x00\x00"

    assert smth_digest.image_dimensions(png, ".png") == (640, 480)
    assert smth_digest.image_dimensions(jpeg, ".jpg") == (1200, 900)
    assert smth_digest.image_dimensions(gif, ".gif") == (320, 200)
    assert smth_digest.image_dimensions(webp, ".webp") == (800, 600)
    assert smth_digest.image_dimensions(ico, ".ico") == (64, 64)


def test_image_dimensions_apply_gif_specific_limit() -> None:
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", 12001, 1)
    )
    gif = b"GIF89a" + struct.pack("<HH", 2001, 1) + b"\x00\x00\x00"
    valid_gif = b"GIF89a" + struct.pack("<HH", 2000, 2000) + b"\x00\x00\x00"

    assert smth_digest.validate_image_dimensions(valid_gif, ".gif") == (
        2000,
        2000,
    )
    with pytest.raises(ValueError, match="12000"):
        smth_digest.validate_image_dimensions(png, ".png")
    with pytest.raises(ValueError, match="2000"):
        smth_digest.validate_image_dimensions(gif, ".gif")


def test_safe_image_url_rejects_non_smth_hosts() -> None:
    assert smth_digest.safe_image_url(
        "https://static.mysmth.net/nForum/img/hotpic/a.jpg"
    )
    assert smth_digest.safe_image_url("https://evil.example/a.jpg") == ""
    assert smth_digest.safe_image_url(
        "https://static.mysmth.net:444/nForum/img/hotpic/a.jpg"
    ) == ""
    assert smth_digest.safe_image_url(
        "https://user@static.mysmth.net/nForum/img/hotpic/a.jpg"
    ) == ""


def test_image_redirect_handler_rejects_cross_host_redirects() -> None:
    class RequestStub:
        full_url = "https://static.mysmth.net/a.jpg"

    handler = smth_digest._SafeImageRedirectHandler()
    with pytest.raises(smth_digest.urllib.error.URLError, match="白名单"):
        handler.redirect_request(
            RequestStub(),
            None,
            302,
            "Found",
            {},
            "https://evil.example/a.jpg",
        )


def test_missing_feishu_credentials_degrades_without_network(monkeypatch) -> None:
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    with patch("smth_digest.urllib.request.urlopen") as call:
        assert smth_digest.prepare_card_images([item()]) == {}
    call.assert_not_called()


def test_uploaded_image_key_is_cached(monkeypatch, tmp_path) -> None:
    selected = item()
    selected["image_urls"] = [
        "https://static.mysmth.net/nForum/img/hotpic/Memory_193603.png"
    ]
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("SMTH_IMAGE_CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(
        smth_digest,
        "download_image",
        lambda url, timeout: (
            b"\x89PNG\r\n\x1a\npayload",
            ".png",
            "image/png",
        ),
    )
    monkeypatch.setattr(
        smth_digest,
        "upload_feishu_image",
        lambda payload, extension, content_type, deadline=None: "img_v3_cached",
    )
    monkeypatch.setattr(
        smth_digest, "feishu_tenant_token", lambda timeout=15: "token"
    )
    keys = smth_digest.prepare_card_images([selected])
    assert keys == {"Memory/193603": "img_v3_cached"}
    saved = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert next(iter(saved.values()))["image_key"] == "img_v3_cached"


def test_prepare_card_images_stops_before_network_after_budget(
    monkeypatch, tmp_path
) -> None:
    selected = item()
    selected["image_urls"] = [
        "https://static.mysmth.net/nForum/img/hotpic/Memory_193603.png"
    ]
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("SMTH_IMAGE_CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(smth_digest, "load_image_cache", lambda: {})
    monkeypatch.setattr(smth_digest, "save_image_cache", lambda cache: None)
    clock = iter([
        100.0,
        100.0 + smth_digest.IMAGE_TOTAL_BUDGET + 1,
        100.0 + smth_digest.IMAGE_TOTAL_BUDGET + 1,
    ])
    monkeypatch.setattr(smth_digest.time, "monotonic", lambda: next(clock))

    with patch("smth_digest.download_image") as download:
        assert smth_digest.prepare_card_images([selected]) == {}
    download.assert_not_called()

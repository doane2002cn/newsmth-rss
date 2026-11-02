from __future__ import annotations

import time

import pytest

from app.parser import (
    GuestVerificationRequired,
    is_allowed_image_url,
    parse_article,
    sanitize_html,
)


ARTICLE_HTML = """
<!doctype html>
<html>
<head><title>测试主题 - 水木社区</title></head>
<body>
  <span class="a-title">一个真实形态的测试主题</span>
  <li class="page-pre">共 12 篇</li>
  <table class="article">
    <tr>
      <td>
        <span class="a-u-name">alice</span>
        <span class="a-pos">楼主</span>
        <span class="a-date">2026-10-25 10:30:12</span>
        <ul><li><a class="a-post" href="/nForum/article/Test/123">楼层</a></li></ul>
      </td>
    </tr>
    <tr>
      <td class="a-content">
        发信人: alice (Alice), 信区: Test<br>
        标 题: 一个真实形态的测试主题<br>
        发信站: 水木社区 (Sat Oct 25 10:30:12 2026), 站内<br>
        <p onclick="alert(1)">第一段正文 <strong>重点</strong></p>
        <script>alert("xss")</script>
        <img src="https://att.newsmth.net/photo.jpg" onerror="alert(2)" alt="照片">
        <img src="https://evil.example/tracker.png" alt="坏图片">
        <div class="likes"><ul><li>bob</li><li>carol</li></ul></div>
      </td>
    </tr>
  </table>
  <table class="article">
    <tr>
      <td>
        <span class="a-u-name">bob</span>
        <span class="a-pos">第2楼</span>
        <span class="a-date">2026-10-25 10:35:00</span>
        <ul><li><a class="a-post" href="/nForum/article/Test/123?p=2">楼层</a></li></ul>
      </td>
    </tr>
    <tr><td class="a-content"><p>回复内容</p></td></tr>
  </table>
</body>
</html>
"""


def image_factory(_url: str) -> str:
    return "/i/opaque-hmac-token"


def test_parse_article_shape_and_sanitization() -> None:
    article = parse_article(
        ARTICLE_HTML,
        board="Test",
        topic="123",
        page=1,
        fallback_title="fallback",
        base_url="https://www.newsmth.net/nForum/article/Test/123",
        image_url_factory=image_factory,
        fetched_at=int(time.time()),
    )
    assert article.title == "一个真实形态的测试主题"
    assert article.total_posts == 12
    assert article.total_pages == 2
    assert len(article.posts) == 2

    body = article.posts[0].body_html
    assert "第一段正文" in body
    assert "<script" not in body
    assert "onclick" not in body
    assert "onerror" not in body
    assert "https://att.newsmth.net" not in body
    assert "/i/opaque-hmac-token" in body
    assert "图片未加载" in body
    assert "发信人" not in body
    assert "发信站" not in body
    assert "https://www.newsmth.net/nForum/article/Test/123" not in body
    assert article.posts[0].likes == ["bob", "carol"]
    assert article.posts[1].author == "bob"
    assert article.posts[1].position == "第2楼"


def test_guest_challenge_is_detected() -> None:
    with pytest.raises(GuestVerificationRequired):
        parse_article(
            '<div class="guest-read-challenge">请完成验证</div>',
            board="Test",
            topic="123",
            page=1,
            fallback_title="",
            base_url="https://www.newsmth.net",
            image_url_factory=image_factory,
            fetched_at=int(time.time()),
        )


def test_sanitize_html_removes_leading_and_excess_line_breaks() -> None:
    body = sanitize_html(
        "<p><br><br><br> <br>第一行<br>第二行<br><br><br>第三行</p>",
        base_url="https://www.newsmth.net",
        image_url_factory=image_factory,
    )
    assert body == "<p>第一行<br/>第二行<br/><br/>第三行</p>"


def test_sanitize_html_preserves_preformatted_whitespace() -> None:
    body = sanitize_html(
        "<p><br>正文</p><pre>\n  first\n\n  second\n</pre>",
        base_url="https://www.newsmth.net",
        image_url_factory=image_factory,
    )
    assert body.startswith("<p>正文</p><pre>")
    assert "\n  first\n\n  second\n" in body


def test_sanitize_html_removes_html_comments() -> None:
    body = sanitize_html(
        "<p><!-- hidden -->正文</p>",
        base_url="https://www.newsmth.net",
        image_url_factory=image_factory,
    )
    assert body == "<p>正文</p>"


def test_sanitize_html_keeps_smth_links_as_text() -> None:
    body = sanitize_html(
        (
            '<p><a href="https://www.newsmth.net/nForum/article/Test/123">'
            "站内帖子</a> "
            '<a href="https://example.com/story">外部链接</a></p>'
        ),
        base_url="https://www.newsmth.net",
        image_url_factory=image_factory,
    )
    assert "站内帖子" in body
    assert "newsmth.net" not in body
    assert 'href="https://example.com/story"' in body


def test_login_required_page_is_detected() -> None:
    with pytest.raises(GuestVerificationRequired):
        parse_article(
            "<div>产生错误的可能原因：您未登录,请登录后继续操作</div>",
            board="Test",
            topic="123",
            page=1,
            fallback_title="",
            base_url="https://www.newsmth.net",
            image_url_factory=image_factory,
            fetched_at=int(time.time()),
        )


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://att.newsmth.net/a.jpg", True),
        ("https://static.mysmth.net/a.png", True),
        ("http://att.newsmth.net/a.jpg", False),
        ("https://evil.example/a.jpg", False),
        ("https://att.newsmth.net.evil.example/a.jpg", False),
    ],
)
def test_image_host_allowlist(url: str, allowed: bool) -> None:
    assert is_allowed_image_url(url) is allowed

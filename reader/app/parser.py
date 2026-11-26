from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .models import ArticlePage, Post

SMTH_HOSTS = {
    "www.newsmth.net",
    "m.newsmth.net",
    "newsmth.net",
    "mysmth.net",
    "static.newsmth.net",
    "static.mysmth.net",
    "att.newsmth.net",
}
IMAGE_HOSTS = {
    "static.newsmth.net",
    "static.mysmth.net",
    "att.newsmth.net",
    "www.newsmth.net",
}

REMOVE_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "svg",
    "math",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "option",
    "link",
    "meta",
    "base",
    "video",
    "audio",
    "canvas",
}
KEEP_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "div",
    "em",
    "font",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "small",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
INLINE_WRAPPERS = {
    "a",
    "b",
    "code",
    "del",
    "em",
    "font",
    "i",
    "s",
    "small",
    "span",
    "strong",
    "u",
}
LEADING_BREAK_CONTAINERS = {
    "blockquote",
    "div",
    "li",
    "p",
    "td",
    "th",
}
DATE_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?"),
    re.compile(r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?"),
)


class GuestVerificationRequired(RuntimeError):
    pass


class SMTHLoginRequired(GuestVerificationRequired):
    pass


class ArticleParseError(RuntimeError):
    pass


class SMTHArticleNotFound(RuntimeError):
    pass


def is_article_not_found(html: str) -> bool:
    return (
        "指定的文章不存在" in html
        or "文章不存在或链接错误" in html
    )


def is_login_required(html: str) -> bool:
    return (
        "您未登录" in html
        or "请登录后继续操作" in html
        or "用户未登录" in html
    )


def is_guest_verification(html: str) -> bool:
    """Detect the guest read challenge without rejecting ordinary article text.

    SMTH challenge pages contain an explicit challenge marker or captcha form
    and no article markup.  A normal post may legitimately discuss
    "阅读验证" or "请完成验证", so text alone must not classify it.
    """
    lowered = html.lower()
    soup = BeautifulSoup(html, "html.parser")
    has_article = bool(
        soup.select_one(
            "table.article, td.a-content, .a-content, .article-content, "
            ".post-content, [data-article]"
        )
    )
    if has_article:
        return False
    has_strong_marker = (
        "guest-read-challenge" in lowered
        or "read verification" in lowered
        or "read challenge" in lowered
    )
    if has_strong_marker:
        return True
    has_weak_marker = any(
        marker in html
        for marker in ("请完成验证", "完成阅读验证", "阅读验证")
    )
    has_challenge_form = bool(
        soup.select_one(
            "form [name*=captcha i], form input[type=text], "
            "form input[type=image], .captcha, .challenge, "
            "[class*=captcha i], [id*=captcha i]"
        )
    )
    return has_weak_marker and has_challenge_form


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _normalise_url(value: str, base_url: str) -> str:
    value = (value or "").strip()
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("/"):
        value = urljoin(base_url, value)
    return value


def is_allowed_image_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.hostname.lower() in IMAGE_HOSTS
    )


def _extract_date(table: Tag) -> str:
    for selector in (".a-date", ".a-time", "time", "[class*=date]"):
        node = table.select_one(selector)
        if node:
            value = _clean_text(node.get_text(" ", strip=True))
            if value:
                return value[:40]
    text = _clean_text(table.get_text(" ", strip=True))
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def _extract_total_posts(soup: BeautifulSoup) -> int:
    page_pre = soup.select_one("li.page-pre")
    if page_pre:
        match = re.search(r"\d+", page_pre.get_text(" ", strip=True))
        if match:
            return max(1, int(match.group(0)))
    return 1


def _extract_title(soup: BeautifulSoup, fallback: str) -> str:
    for selector in (
        "span.a-title",
        "h1",
        ".article-title",
        ".topic-title",
        "title",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        title = _clean_text(node.get_text(" ", strip=True))
        title = re.sub(r"\s*[-|]\s*水木社区.*$", "", title)
        if title:
            return title[:160]
    return fallback


def _extract_content(table: Tag) -> Tag:
    content = table.select_one("td.a-content")
    if not content:
        content = table
    container = BeautifulSoup(
        f"<div>{content.decode_contents()}</div>", "html.parser"
    ).div
    assert container is not None

    for selector in ("div.likes", "button.add_like", "font[style*='display: none']"):
        for node in container.select(selector):
            node.decompose()

    header_re = re.compile(
        r"^\s*(发信人|寄信人|标\s*题|发信站|来\s*源)\s*[:：]"
    )
    for node in list(container.find_all(string=True)):
        if header_re.match(str(node)):
            node.replace_with(NavigableString(""))
    return container


def _extract_likes(table: Tag) -> list[str]:
    likes: list[str] = []
    for item in table.select("div.likes li"):
        text = _clean_text(item.get_text(" ", strip=True))
        if text:
            likes.append(text)
    return likes


def _safe_color(value: str) -> str:
    value = (value or "").strip().lower()
    named = {
        "black",
        "blue",
        "gray",
        "green",
        "grey",
        "orange",
        "purple",
        "red",
        "teal",
        "white",
    }
    if value in named:
        return value
    if re.fullmatch(r"#[0-9a-f]{3}([0-9a-f]{3})?", value):
        return value
    if re.fullmatch(r"rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)", value):
        return value
    return ""


def _strip_leading_breaks(container: Tag | BeautifulSoup) -> None:
    if container.name == "pre" or container.find_parent("pre"):
        return
    while container.contents:
        first = container.contents[0]
        if isinstance(first, Comment):
            first.extract()
            continue
        if isinstance(first, NavigableString):
            if str(first).strip():
                break
            first.extract()
            continue
        if not isinstance(first, Tag):
            break
        if first.name == "br":
            first.extract()
            continue
        if first.name in INLINE_WRAPPERS:
            _strip_leading_breaks(first)
            if (
                not first.get_text(strip=True)
                and not first.find(["br", "img"])
            ):
                first.extract()
                continue
        break


def normalize_line_breaks(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()

    for container in [
        soup,
        *soup.find_all(LEADING_BREAK_CONTAINERS),
    ]:
        _strip_leading_breaks(container)

    # Preserve intentional line breaks while collapsing runs of three or more.
    for br in list(soup.find_all("br")):
        if br.find_parent("pre"):
            continue
        previous = br.previous_sibling
        preceding_breaks = 0
        while previous is not None:
            if isinstance(previous, NavigableString) and not str(previous).strip():
                previous = previous.previous_sibling
                continue
            if isinstance(previous, Tag) and previous.name == "br":
                preceding_breaks += 1
                if preceding_breaks >= 2:
                    br.decompose()
                    break
                previous = previous.previous_sibling
                continue
            break
    return str(soup)


def sanitize_html(
    html: str,
    *,
    base_url: str,
    image_url_factory,
) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.name not in KEEP_TAGS:
            tag.unwrap()
            continue

        clean_attrs: dict[str, str] = {}
        if tag.name == "a":
            href = _normalise_url(tag.get("href", ""), base_url)
            parsed = urlparse(href)
            # Keep SMTH-only links as plain text inside the reader. Opening the
            # upstream page directly can replace the cached article with a
            # guest verification challenge.
            if parsed.scheme in {"http", "https"} and parsed.hostname not in SMTH_HOSTS:
                clean_attrs["href"] = href
                clean_attrs["target"] = "_blank"
                clean_attrs["rel"] = "noreferrer nofollow"
                clean_attrs["referrerpolicy"] = "no-referrer"

        if tag.name == "img":
            src = _normalise_url(
                tag.get("src", "") or tag.get("data-original", ""),
                base_url,
            )
            if is_allowed_image_url(src):
                clean_attrs["src"] = image_url_factory(src)
                clean_attrs["loading"] = "lazy"
                clean_attrs["decoding"] = "async"
                clean_attrs["referrerpolicy"] = "no-referrer"
                original_href = _normalise_url(tag.get("data-original", ""), base_url)
                clean_attrs["alt"] = _clean_text(
                    tag.get("alt") or tag.get("title") or "帖子图片"
                )[:120]
                if is_allowed_image_url(original_href):
                    clean_attrs["data-original"] = image_url_factory(original_href)
            else:
                placeholder = soup.new_tag(
                    "span",
                    attrs={"class": "blocked-image"},
                )
                placeholder.string = "图片未加载"
                tag.replace_with(placeholder)
                continue

        if tag.name == "font":
            color = _safe_color(tag.get("color", ""))
            if color:
                clean_attrs["style"] = f"color:{color}"

        if tag.name in {"td", "th"}:
            for attr in ("colspan", "rowspan"):
                value = tag.get(attr, "")
                if str(value).isdigit():
                    clean_attrs[attr] = str(value)

        tag.attrs = clean_attrs

    for node in soup.select("div, p, span"):
        if not _clean_text(node.get_text("", strip=False)) and not node.find("img"):
            node.decompose()

    # SMTH posts often begin with several line breaks. They create a large
    # blank field above the first line once rendered with reader line-height.
    return normalize_line_breaks(str(soup))


def parse_article(
    html: str,
    *,
    board: str,
    topic: str,
    page: int,
    fallback_title: str,
    base_url: str,
    image_url_factory,
    fetched_at: int,
) -> ArticlePage:
    if is_article_not_found(html):
        raise SMTHArticleNotFound("SMTH article was not found")
    if is_login_required(html):
        raise SMTHLoginRequired("SMTH requires login")
    if is_guest_verification(html):
        raise GuestVerificationRequired("SMTH returned the guest read challenge")

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table.article")
    if not tables:
        error = soup.select_one("div.error")
        if error:
            raise ArticleParseError(_clean_text(error.get_text(" ", strip=True)))
        raise ArticleParseError("article markup was not found")

    posts: list[Post] = []
    for table in tables:
        author_node = table.select_one("span.a-u-name")
        position_node = table.select_one("span.a-pos")
        post_link = table.select_one("li a.a-post[href]")
        post_id = ""
        if post_link:
            post_id = str(post_link.get("href", "")).rstrip("/").split("/")[-1]

        author = _clean_text(author_node.get_text(" ", strip=True)) if author_node else ""
        position = (
            _clean_text(position_node.get_text(" ", strip=True))
            if position_node
            else f"第{len(posts) + (page - 1) * 10 + 1}楼"
        )
        content = _extract_content(table)
        body_html = sanitize_html(
            content.decode_contents(),
            base_url=base_url,
            image_url_factory=image_url_factory,
        )
        posts.append(
            Post(
                author=author or "匿名",
                position=position,
                body_html=body_html,
                post_id=post_id,
                date=_extract_date(table),
                likes=_extract_likes(table),
            )
        )

    total_posts = _extract_total_posts(soup)
    total_pages = max(1, (total_posts + 9) // 10)
    return ArticlePage(
        board=board,
        topic=topic,
        title=_extract_title(soup, fallback_title),
        page=max(1, min(page, total_pages)),
        total_posts=total_posts,
        total_pages=total_pages,
        posts=posts,
        fetched_at=fetched_at,
    )


def format_cache_age(fetched_at: int, now: datetime | None = None) -> str:
    current = now.timestamp() if now else datetime.now().timestamp()
    seconds = max(0, int(current - fetched_at))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"

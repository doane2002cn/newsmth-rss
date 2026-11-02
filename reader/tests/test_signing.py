from __future__ import annotations

import time

import pytest

from app.signing import (
    InvalidToken,
    admin_login_token,
    article_token,
    image_token,
    loads,
    loads_typed,
)


SECRET = "signing-secret-" + "x" * 40


def test_article_token_round_trip() -> None:
    now = int(time.time())
    token = article_token(SECRET, "NewExpress", "47582736", "标题", now + 60)
    payload = loads_typed(token, SECRET, "article", now=now)
    assert payload["board"] == "NewExpress"
    assert payload["topic"] == "47582736"
    assert payload["title"] == "标题"


def test_expired_token_is_rejected() -> None:
    now = int(time.time())
    token = article_token(SECRET, "NewExpress", "1", "标题", now - 1)
    with pytest.raises(InvalidToken, match="expired"):
        loads(token, SECRET, now=now)


def test_tampered_token_is_rejected() -> None:
    now = int(time.time())
    token = image_token(SECRET, "https://att.newsmth.net/a.jpg", now + 60)
    encoded_body, signature = token.split(".", 1)
    tampered = f"{encoded_body[:-1]}A.{signature}"
    with pytest.raises(InvalidToken):
        loads(tampered, SECRET, now=now)


def test_token_type_is_enforced() -> None:
    now = int(time.time())
    token = admin_login_token(SECRET, now + 60, jti="a" * 32)
    payload = loads_typed(token, SECRET, "admin-login", now=now)
    assert payload["jti"] == "a" * 32
    with pytest.raises(InvalidToken, match="expected article"):
        loads_typed(token, SECRET, "article", now=now)

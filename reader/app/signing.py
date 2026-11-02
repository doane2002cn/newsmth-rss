from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any


class InvalidToken(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def dumps(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).digest()
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def loads(token: str, secret: str, now: int | None = None) -> dict[str, Any]:
    try:
        encoded_body, encoded_signature = token.split(".", 1)
        body = _b64decode(encoded_body)
        supplied_signature = _b64decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise InvalidToken("malformed token") from exc

    expected_signature = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise InvalidToken("invalid signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidToken("invalid payload") from exc
    if not isinstance(payload, dict):
        raise InvalidToken("invalid payload type")

    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        raise InvalidToken("missing expiry")
    if expires_at <= (now if now is not None else int(time.time())):
        raise InvalidToken("expired token")
    return payload


def article_token(
    secret: str,
    board: str,
    topic: str,
    title: str,
    expires_at: int,
) -> str:
    return dumps(
        {
            "v": 1,
            "typ": "article",
            "board": board,
            "topic": topic,
            "title": title[:160],
            "exp": expires_at,
        },
        secret,
    )


def loads_typed(
    token: str,
    secret: str,
    expected_type: str,
    now: int | None = None,
) -> dict[str, Any]:
    payload = loads(token, secret, now=now)
    if payload.get("typ") != expected_type:
        raise InvalidToken(f"expected {expected_type} token")
    return payload


def admin_login_token(secret: str, expires_at: int, jti: str | None = None) -> str:
    nonce = jti or uuid.uuid4().hex
    return dumps(
        {"v": 1, "typ": "admin-login", "jti": nonce, "exp": expires_at},
        secret,
    )


def admin_session_token(secret: str, expires_at: int) -> str:
    return dumps({"v": 1, "typ": "admin-session", "exp": expires_at}, secret)


def image_token(secret: str, url: str, expires_at: int) -> str:
    return dumps({"v": 1, "typ": "image", "url": url, "exp": expires_at}, secret)

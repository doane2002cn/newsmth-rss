from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    secret: str
    public_base: str
    data_dir: Path
    article_ttl_seconds: int
    stale_ttl_seconds: int
    reader_token_ttl_seconds: int
    image_token_ttl_seconds: int
    image_cache_ttl_seconds: int
    admin_session_ttl_seconds: int
    admin_login_ttl_seconds: int
    request_interval_seconds: float
    cookie_secure: bool
    browser_enabled: bool
    browser_cdp_url: str
    smth_base: str
    vnc_http_url: str
    vnc_websocket_url: str
    guest_verification_cooldown_seconds: int = 600
    browser_proxy_server: str = ""
    snapshot_refresh_cooldown_seconds: int = 900

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache" / "articles"

    @property
    def image_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "images"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def login_nonce_dir(self) -> Path:
        return self.data_dir / "login-nonces"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "chromium-profile"

    @property
    def cookie_snapshot_path(self) -> Path:
        return self.data_dir / "chromium-cookies.json"

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.getenv("READER_SIGNING_SECRET", "").strip()
        if len(secret) < 32:
            raise RuntimeError("READER_SIGNING_SECRET must contain at least 32 characters")

        data_dir = Path(os.getenv("READER_DATA_DIR", "/data"))
        return cls(
            secret=secret,
            public_base=os.getenv(
                "READER_PUBLIC_BASE", "https://smth-reader.12879737.xyz"
            ).rstrip("/"),
            data_dir=data_dir,
            article_ttl_seconds=_env_int("ARTICLE_TTL_SECONDS", 1800),
            stale_ttl_seconds=_env_int("ARTICLE_STALE_TTL_SECONDS", 604800),
            reader_token_ttl_seconds=_env_int("READER_TOKEN_TTL_SECONDS", 7776000),
            image_token_ttl_seconds=_env_int("IMAGE_TOKEN_TTL_SECONDS", 604800),
            image_cache_ttl_seconds=_env_int("IMAGE_CACHE_TTL_SECONDS", 604800),
            admin_session_ttl_seconds=_env_int(
                "ADMIN_SESSION_TTL_SECONDS", 30 * 24 * 60 * 60
            ),
            admin_login_ttl_seconds=_env_int("ADMIN_LOGIN_TTL_SECONDS", 900),
            request_interval_seconds=float(
                os.getenv("SMTH_REQUEST_INTERVAL_SECONDS", "2.5")
            ),
            cookie_secure=_env_bool("READER_COOKIE_SECURE", True),
            browser_enabled=_env_bool("READER_BROWSER_ENABLED", True),
            browser_cdp_url=os.getenv("BROWSER_CDP_URL", "").strip(),
            browser_proxy_server=os.getenv("BROWSER_PROXY_SERVER", "").strip(),
            smth_base=os.getenv("SMTH_BASE_URL", "https://www.newsmth.net").rstrip(
                "/"
            ),
            vnc_http_url=os.getenv("VNC_HTTP_URL", "http://127.0.0.1:6081").rstrip(
                "/"
            ),
            vnc_websocket_url=os.getenv(
                "VNC_WEBSOCKET_URL", "ws://127.0.0.1:6081/websockify"
            ),
            guest_verification_cooldown_seconds=_env_int(
                "GUEST_VERIFICATION_COOLDOWN_SECONDS", 600
            ),
            snapshot_refresh_cooldown_seconds=_env_int(
                "SNAPSHOT_REFRESH_COOLDOWN_SECONDS", 900
            ),
        )

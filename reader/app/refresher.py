from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from .browser import BrowserManager, SMTHRateLimited, SMTHUnavailable
from .cache import FileCache
from .config import Settings
from .parser import (
    ArticleParseError,
    GuestVerificationRequired,
    SMTHArticleNotFound,
    SMTHLoginRequired,
    parse_article,
)
from .signing import image_token

LOGGER = logging.getLogger(__name__)


class SnapshotRefresher:
    """Best-effort full-text collector isolated from the user-facing read path."""

    def __init__(
        self,
        settings: Settings,
        manager: BrowserManager,
        article_cache: FileCache,
    ) -> None:
        self.settings = settings
        self.manager = manager
        self.article_cache = article_cache
        self._queue: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue(
            maxsize=100
        )
        self._pending: set[tuple[str, str, int]] = set()
        self._last_attempt: dict[tuple[str, str, int], float] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def enqueue(self, board: str, topic: str, page: int = 1) -> bool:
        key = (board, topic, page)
        now = time.monotonic()
        last = self._last_attempt.get(key, 0.0)
        if key in self._pending or now - last < self.settings.snapshot_refresh_cooldown_seconds:
            return False
        try:
            self._queue.put_nowait(key)
        except asyncio.QueueFull:
            return False
        self._pending.add(key)
        return True

    async def _run(self) -> None:
        while True:
            board, topic, page = await self._queue.get()
            try:
                await self._refresh(board, topic, page)
            finally:
                self._pending.discard((board, topic, page))
                self._last_attempt[(board, topic, page)] = time.monotonic()
                self._queue.task_done()

    async def _refresh(self, board: str, topic: str, page: int) -> None:
        try:
            html = await self.manager.fetch_article_html(board, topic, page)
            parsed = parse_article(
                html,
                board=board,
                topic=topic,
                page=page,
                fallback_title="",
                base_url=f"{self.settings.smth_base}/nForum/article/{board}/{topic}",
                image_url_factory=lambda source: "/i/"
                + image_token(
                    self.settings.secret,
                    source,
                    int(time.time()) + self.settings.image_token_ttl_seconds,
                ),
                fetched_at=int(time.time()),
            )
            self.article_cache.write(
                _article_cache_key(board, topic, page),
                parsed.to_dict(),
            )
            LOGGER.info("stored background full-text snapshot for %s/%s", board, topic)
        except SMTHRateLimited:
            LOGGER.info("background full-text refresh is rate limited")
        except (SMTHLoginRequired, GuestVerificationRequired):
            LOGGER.info("background full-text refresh requires SMTH verification")
        except SMTHArticleNotFound:
            LOGGER.info("background full-text refresh found no article")
        except (SMTHUnavailable, ArticleParseError):
            LOGGER.info("background full-text refresh is temporarily unavailable")
        except Exception:  # noqa: BLE001
            LOGGER.exception("background full-text refresh failed")


def _article_cache_key(board: str, topic: str, page: int) -> str:
    import hashlib

    raw = f"{board}\0{topic}\0{page}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

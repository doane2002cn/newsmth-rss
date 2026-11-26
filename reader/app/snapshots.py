from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .cache import FileCache


class SnapshotStore:
    """Immutable RSS snapshots with a small, atomic "latest" index.

    Snapshot objects are content-addressed and never overwritten.  The head
    file only selects which immutable object a signed article link should use.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._objects = FileCache(directory / "objects")
        self._heads = FileCache(directory / "heads")

    @staticmethod
    def article_key(board: str, topic: str) -> str:
        raw = f"{board}\0{topic}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _content_id(payload: dict[str, Any]) -> str:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def put(
        self,
        *,
        board: str,
        topic: str,
        title: str,
        summary: str,
        author: str = "",
        published_at: int = 0,
        source_url: str = "",
    ) -> str:
        content = {
            "board": board,
            "topic": topic,
            "title": title.strip()[:200],
            "summary": summary.strip()[:4000],
            "author": author.strip()[:80],
            "published_at": max(0, int(published_at)),
            "source_url": source_url.strip()[:1000],
        }
        snapshot_id = self._content_id(content)
        existing = self._objects.read(snapshot_id)
        if not existing:
            snapshot = {
                "v": 1,
                **content,
                "snapshot_id": snapshot_id,
                "created_at": int(time.time()),
            }
            self._objects.write(snapshot_id, snapshot)
        self._heads.write(
            self.article_key(board, topic),
            {
                "snapshot_id": snapshot_id,
                "board": board,
                "topic": topic,
                "updated_at": int(time.time()),
            },
        )
        return snapshot_id

    def get(
        self,
        board: str,
        topic: str,
        snapshot_id: str = "",
    ) -> dict[str, Any] | None:
        selected = snapshot_id
        if not selected:
            head = self._heads.read(self.article_key(board, topic))
            if not head:
                return None
            selected = str(head[0].get("snapshot_id") or "")
        if not selected:
            return None
        stored = self._objects.read(selected)
        if not stored:
            return None
        snapshot = stored[0]
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("snapshot_id") != selected
            or snapshot.get("board") != board
            or snapshot.get("topic") != topic
        ):
            return None
        return snapshot

    def count(self) -> int:
        return self._objects.count()

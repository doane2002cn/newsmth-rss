from __future__ import annotations

import os
import time

from app.cache import BinaryFileCache, FileCache


def test_json_file_cache_round_trip(tmp_path) -> None:
    cache = FileCache(tmp_path / "json")
    cache.write("article-1", {"title": "正文", "posts": 2})
    data, age = cache.read("article-1")
    assert data["title"] == "正文"
    assert age >= 0
    assert cache.count() == 1

    old = time.time() - 120
    os.utime(tmp_path / "json" / "article-1.json", (old, old))
    _, age = cache.read("article-1")
    assert age >= 100


def test_binary_file_cache_round_trip(tmp_path) -> None:
    cache = BinaryFileCache(tmp_path / "binary")
    cache.write("image-1", b"\x89PNG-body", {"content_type": "image/png"})
    item = cache.read("image-1")
    assert item is not None
    assert item.body == b"\x89PNG-body"
    assert item.metadata["content_type"] == "image/png"
    assert cache.count() == 1

    old = time.time() - 120
    os.utime(tmp_path / "binary" / "image-1.bin", (old, old))
    item = cache.read("image-1")
    assert item is not None
    assert item.age_seconds >= 100

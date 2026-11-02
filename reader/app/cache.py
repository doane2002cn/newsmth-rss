from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FileCache:
    def __init__(self, directory: Path, suffix: str = ".json") -> None:
        self.directory = directory
        self.suffix = suffix
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe_key = "".join(char for char in key if char.isalnum() or char in "._-")
        return self.directory / f"{safe_key}{self.suffix}"

    def read(self, key: str) -> tuple[Any, int] | None:
        path = self._path(key)
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            age = max(0, int(time.time() - path.stat().st_mtime))
            return data, age
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def write(self, key: str, data: Any) -> None:
        path = self._path(key)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def age(self, key: str) -> int | None:
        path = self._path(key)
        try:
            return max(0, int(time.time() - path.stat().st_mtime))
        except OSError:
            return None

    def count(self) -> int:
        try:
            return sum(1 for path in self.directory.iterdir() if path.is_file())
        except OSError:
            return 0


@dataclass(frozen=True)
class BinaryCacheEntry:
    body: bytes
    metadata: dict[str, Any]
    age_seconds: int


class BinaryFileCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, suffix: str) -> Path:
        safe_key = "".join(char for char in key if char.isalnum() or char in "._-")
        return self.directory / f"{safe_key}{suffix}"

    def read(self, key: str) -> BinaryCacheEntry | None:
        body_path = self._path(key, ".bin")
        metadata_path = self._path(key, ".json")
        try:
            body = body_path.read_bytes()
            with metadata_path.open("r", encoding="utf-8") as handle:
                metadata = json.load(handle)
            age = max(0, int(time.time() - body_path.stat().st_mtime))
            return BinaryCacheEntry(body=body, metadata=metadata, age_seconds=age)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def write(self, key: str, body: bytes, metadata: dict[str, Any]) -> None:
        body_path = self._path(key, ".bin")
        metadata_path = self._path(key, ".json")
        fd, temp_body = tempfile.mkstemp(
            prefix=f".{body_path.stem}-", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.replace(temp_body, body_path)
        finally:
            try:
                os.unlink(temp_body)
            except FileNotFoundError:
                pass

        fd, temp_metadata = tempfile.mkstemp(
            prefix=f".{metadata_path.stem}-", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_metadata, metadata_path)
        finally:
            try:
                os.unlink(temp_metadata)
            except FileNotFoundError:
                pass

    def count(self) -> int:
        try:
            return sum(
                1
                for path in self.directory.iterdir()
                if path.is_file() and path.suffix == ".bin"
            )
        except OSError:
            return 0

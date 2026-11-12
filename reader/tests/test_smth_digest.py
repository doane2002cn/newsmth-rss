from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "hermes" / "smth_digest.py"
)
SPEC = importlib.util.spec_from_file_location("smth_digest_test_module", MODULE_PATH)
assert SPEC and SPEC.loader
smth_digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smth_digest)


class OkResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_prewarm_stops_after_first_http_error(monkeypatch) -> None:
    calls = []

    def fail(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "guest verification required",
            None,
            None,
        )

    monkeypatch.setattr(smth_digest.urllib.request, "urlopen", fail)
    monkeypatch.setenv("READER_PREWARM_LIMIT", "3")
    monkeypatch.setenv("READER_PREWARM_INTERVAL_SECONDS", "0")
    ready, total = smth_digest.prewarm_reader_links(
        [f"https://reader.example/a/{index}" for index in range(6)]
    )

    assert (ready, total) == (0, 3)
    assert calls == ["https://reader.example/a/0"]


def test_prewarm_is_sequential_and_limits_candidates(monkeypatch) -> None:
    calls = []

    def succeed(request, timeout):
        calls.append(request.full_url)
        return OkResponse()

    monkeypatch.setattr(smth_digest.urllib.request, "urlopen", succeed)
    monkeypatch.setenv("READER_PREWARM_LIMIT", "3")
    monkeypatch.setenv("READER_PREWARM_INTERVAL_SECONDS", "0")
    ready, total = smth_digest.prewarm_reader_links(
        [f"https://reader.example/a/{index}" for index in range(6)]
    )

    assert (ready, total) == (3, 3)
    assert calls == [
        "https://reader.example/a/0",
        "https://reader.example/a/1",
        "https://reader.example/a/2",
    ]


def test_prewarm_default_covers_a_full_card(monkeypatch) -> None:
    calls = []

    def succeed(request, timeout):
        calls.append(request.full_url)
        return OkResponse()

    monkeypatch.setattr(smth_digest.urllib.request, "urlopen", succeed)
    monkeypatch.delenv("READER_PREWARM_LIMIT", raising=False)
    monkeypatch.setenv("READER_PREWARM_INTERVAL_SECONDS", "0")
    links = [f"https://reader.example/a/{index}" for index in range(34)]

    ready, total = smth_digest.prewarm_reader_links(links)

    assert (ready, total) == (34, 34)
    assert calls == links

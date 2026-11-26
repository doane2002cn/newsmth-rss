from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

import smth_digest


SECRET = "hermes-reader-secret-" + "s" * 40


class FakeResponse:
    status = 200

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def item(topic: str = "47582736") -> dict:
    return {
        "title": "[二手房交流] 朝阳 惠新西街 61平 5层 成交227万",
        "link": (
            "https://www.newsmth.net/nForum/article/"
            f"House/{topic}"
        ),
        "guid": f"https://www.newsmth.net/nForum/article/House/{topic}",
        "board": "House",
        "pub": "",
        "ts": 1_790_000_000,
        "desc": "房东挂牌 260 万，最终成交 227 万。",
    }


def test_ingest_reader_snapshots_deduplicates_and_signs(monkeypatch) -> None:
    monkeypatch.setenv("READER_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("READER_PUBLIC_BASE", "https://reader.example")
    snapshot_id = "a" * 64
    response = FakeResponse(
        {
            "ok": True,
            "stored": 1,
            "snapshots": [
                {
                    "board": "House",
                    "topic": "47582736",
                    "snapshot_id": snapshot_id,
                }
            ],
        }
    )

    with patch(
        "smth_digest.urllib.request.urlopen",
        return_value=response,
    ) as call:
        ids = smth_digest.ingest_reader_snapshots([item(), item()])

    assert ids == {("House", "47582736"): snapshot_id}
    assert call.call_count == 1
    request = call.call_args.args[0]
    timestamp = request.get_header("X-reader-timestamp")
    signature = request.get_header("X-reader-signature")
    expected = hmac.new(
        SECRET.encode("utf-8"),
        timestamp.encode("ascii") + b"\n" + request.data,
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected
    decoded = json.loads(request.data.decode("utf-8"))
    assert len(decoded["items"]) == 1
    assert decoded["items"][0]["source_url"].endswith("/House/47582736")


def test_reader_link_binds_immutable_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("READER_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("READER_PUBLIC_BASE", "https://reader.example")
    snapshot_id = "b" * 64
    link = smth_digest.reader_link(
        item(),
        {("House", "47582736"): snapshot_id},
    )

    assert link.startswith("https://reader.example/a/")
    encoded = link.rsplit("/", 1)[-1].split(".", 1)[0]
    encoded += "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded))
    assert payload["board"] == "House"
    assert payload["topic"] == "47582736"
    assert payload["snapshot"] == snapshot_id


def test_reader_link_rejects_missing_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("READER_SIGNING_SECRET", SECRET)
    with pytest.raises(RuntimeError, match="缺少有效快照"):
        smth_digest.reader_link(item(), {})


def test_prewarm_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("READER_PREWARM_LIMIT", raising=False)
    with patch("smth_digest.urllib.request.urlopen") as call:
        ready, total = smth_digest.prewarm_reader_links(
            ["https://reader.example/a/one"]
        )
    assert (ready, total) == (0, 0)
    call.assert_not_called()

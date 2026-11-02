from __future__ import annotations

from unittest.mock import patch

import smth_digest


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_prewarm_reader_links_deduplicates() -> None:
    links = ["https://reader.example/a/one", "https://reader.example/a/one"]
    with patch("smth_digest.urllib.request.urlopen", return_value=FakeResponse()) as call:
        ready, total = smth_digest.prewarm_reader_links(links)
    assert ready == 1
    assert total == 1
    assert call.call_count == 1

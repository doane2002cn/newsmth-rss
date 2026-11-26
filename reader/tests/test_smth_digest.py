from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "hermes" / "smth_digest.py"
)
SPEC = importlib.util.spec_from_file_location("smth_digest_test_module", MODULE_PATH)
assert SPEC and SPEC.loader
smth_digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smth_digest)


def test_new_reading_path_uses_gateway_not_reader_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("SMTH_GATEWAY_BASE", "https://smth-go.example")
    link = smth_digest.gateway_link(
        {
            "title": "标题",
            "link": "https://www.newsmth.net/nForum/article/NewExpress/47582810",
        }
    )
    assert link.startswith("https://smth-go.example/r/NewExpress/47582810?")
    assert "smth-reader" not in link

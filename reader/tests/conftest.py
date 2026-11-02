from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("READER_SIGNING_SECRET", "test-secret-" + ("x" * 48))
os.environ.setdefault(
    "READER_DATA_DIR",
    str(Path(tempfile.gettempdir()) / "smth-reader-pytest-import"),
)

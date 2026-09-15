"""docs/differences.md must match what scripts/gen_differences_doc.py renders."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from gen_differences_doc import OUTPUT, render  # noqa: E402


def test_differences_doc_is_up_to_date():
    expected = render()
    actual = OUTPUT.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/differences.md is stale; run "
        "`uv run python scripts/gen_differences_doc.py` and commit the result"
    )

"""Fail when the Action's drift report skipped the owner comparison.

Reads the drift JSON the Action wrote, by default `drift.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    """Return 0 when no identity note is present, 1 otherwise."""
    target = Path(argv[0] if argv else "drift.json")
    loaded: object = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        print(f"::error::{target} is not a JSON object", file=sys.stderr)
        return 1
    notes = loaded.get("notes")
    skipped = (
        [note for note in notes if isinstance(note, str) and "commit emails" in note]
        if isinstance(notes, list)
        else []
    )
    if skipped:
        print(f"::error::owner comparison was skipped: {skipped}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

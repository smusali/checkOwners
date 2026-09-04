#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _normalize(raw: str) -> str:
    return raw[1:] if raw.startswith(("v", "V")) else raw


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_changelog.py <version>")
        return 2

    version = _normalize(argv[1])
    if version.lower() == "unreleased":
        print("::error::Unreleased is not a dated release entry")
        return 1

    heading = re.compile(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$")
    text = (_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    if any(heading.match(line) for line in text.splitlines()):
        return 0

    print(f"::error::No dated changelog heading for {version}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

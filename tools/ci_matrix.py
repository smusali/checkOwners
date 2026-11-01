"""Write the CI test matrix for the current event to GITHUB_OUTPUT.

Reads GITHUB_EVENT_NAME. A pull request gets one cell per Python on Ubuntu
plus the newest Python on macOS and Windows. Every other event gets the full
grid plus the two pinned Git versions.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PYTHONS = ("3.11", "3.12", "3.13", "3.14", "3.15")
SYSTEMS = ("ubuntu-latest", "macos-latest", "windows-latest")
PINNED_GIT = ("2.23.0", "2.39.3")
NEWEST = PYTHONS[-1]


def _cell(system: str, python: str, git: str = "latest") -> dict[str, str]:
    return {"os": system, "python-version": python, "git-version": git}


def cells(event: str) -> list[dict[str, str]]:
    """Return the matrix cells for `event`."""
    if event == "pull_request":
        return [
            *(_cell("ubuntu-latest", python) for python in PYTHONS),
            *(_cell(system, NEWEST) for system in SYSTEMS[1:]),
        ]
    return [
        *(_cell(system, python) for system in SYSTEMS for python in PYTHONS),
        *(_cell("ubuntu-latest", NEWEST, git) for git in PINNED_GIT),
    ]


def main() -> int:
    """Write `matrix=<json>` to GITHUB_OUTPUT. Return the exit code."""
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        print("::error::GITHUB_OUTPUT is required", file=sys.stderr)
        return 1
    payload = json.dumps({"include": cells(os.environ.get("GITHUB_EVENT_NAME", ""))})
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(f"matrix={payload}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

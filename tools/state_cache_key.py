"""Write the GitHub Actions cache key for the checkowners state directory.

Reads CHECKOWNERS_BASE_SHA (the pull-request base commit, when the event has
one), RUNNER_OS and GITHUB_OUTPUT from the environment.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from checkowners import __version__
from checkowners.config import load_config
from checkowners.storage import (
    config_hash,
    repository_identity,
)

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _git(*args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _merge_base() -> str:
    base = os.environ.get("CHECKOWNERS_BASE_SHA", "").strip()
    if base and _git("cat-file", "-e", f"{base}^{{commit}}") == "":
        merged = _git("merge-base", "HEAD", base)
        if merged:
            return merged
    return _git("rev-parse", "HEAD")


def main() -> int:
    """Write `key` and `restore-keys` to GITHUB_OUTPUT. Return the exit code."""
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        print("::error::GITHUB_OUTPUT is required", file=sys.stderr)
        return 1
    commit = _merge_base()
    identity = repository_identity(Path.cwd())
    digest = config_hash(load_config())
    if not commit or not identity or not digest:
        print("::error::could not compute the state cache key", file=sys.stderr)
        return 1
    runner = _UNSAFE.sub("-", os.environ.get("RUNNER_OS", "unknown"))
    prefix = f"checkowners-{runner}-{_UNSAFE.sub('-', identity)}"
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(f"key={prefix}-{commit}-{digest}-{__version__}\n")
        handle.write("restore-keys<<EOF\n")
        handle.write(f"{prefix}-{commit}-{digest}-\n")
        handle.write(f"{prefix}-\n")
        handle.write("EOF\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

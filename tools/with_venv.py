#!/usr/bin/env python3
"""Run `argv` with the project virtualenv on PATH."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> None:
    """Run `argv` with `.venv` or `venv` on PATH and exit with its status."""
    if not argv:
        sys.exit("usage: with_venv.py COMMAND [ARG ...]")
    path = os.environ.get("PATH", "")
    for name in (".venv", "venv"):
        bin_dir = _ROOT / name / ("Scripts" if os.name == "nt" else "bin")
        if bin_dir.is_dir():
            path = f"{bin_dir}{os.pathsep}{path}"
            os.environ["PATH"] = path
            break
    executable = shutil.which(argv[0], path=path)
    if executable is None:
        sys.exit(f"{argv[0]} was not found")
    completed = subprocess.run([executable, *argv[1:]], check=False)  # noqa: S603
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main(sys.argv[1:])

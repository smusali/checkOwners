#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

_VERSION = re.compile(r'(?m)^__version__ = "([^"]+)"')


def main(root: Path) -> int:
    wheels = list((root / "dist").glob("checkowners-*-py3-none-any.whl"))
    if len(wheels) != 1:
        print(f"expected one wheel, found {wheels}", file=sys.stderr)
        return 1
    wheel = wheels[0]
    init = (root / "checkowners" / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION.search(init)
    if match is None:
        print("__version__ missing from checkowners/__init__.py", file=sys.stderr)
        return 1
    version = match.group(1)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if "checkowners/py.typed" not in names:
            print("wheel is missing checkowners/py.typed", file=sys.stderr)
            return 1
        meta_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(meta_names) != 1:
            print("wheel is missing METADATA", file=sys.stderr)
            return 1
        meta = archive.read(meta_names[0]).decode("utf-8")
    if f"Version: {version}" not in meta.splitlines():
        print(f"wheel METADATA Version does not match {version!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path.cwd()))

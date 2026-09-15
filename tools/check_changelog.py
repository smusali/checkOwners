#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ALL_EXTRA = ["checkowners[graph,github]"]
_PY314 = "Programming Language :: Python :: 3.14"
_MARKETPLACE_DESC_LIMIT = 125


def _normalize(raw: str) -> str:
    return raw[1:] if raw.startswith(("v", "V")) else raw


def _init_version() -> str:
    text = (_ROOT / "checkowners" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'(?m)^__version__ = "([^"]+)"', text)
    if not match:
        raise ValueError("__version__ missing from checkowners/__init__.py")
    return match.group(1)


def _action_input_default(name: str) -> str:
    text = (_ROOT / "action.yml").read_text(encoding="utf-8")
    match = re.search(
        rf"(?m)^  {re.escape(name)}:\n(?:    .*\n)*?    default: \"([^\"]+)\"",
        text,
    )
    if not match:
        raise ValueError(f"{name} default missing from action.yml")
    return match.group(1)


def _action_pinned_version() -> str:
    text = (_ROOT / "action.yml").read_text(encoding="utf-8")
    match = re.search(r'CHECKOWNERS_PINNED_VERSION: "([^"]+)"', text)
    if not match:
        raise ValueError("CHECKOWNERS_PINNED_VERSION missing from action.yml")
    return match.group(1)


def _action_description() -> str:
    lines = (_ROOT / "action.yml").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        rest = line[len("description:") :].strip()
        if rest in {">", ">-", "|", "|-"}:
            parts: list[str] = []
            for cont in lines[i + 1 :]:
                if not cont.startswith(" "):
                    break
                parts.append(cont.strip())
            return " ".join(part for part in parts if part)
        if len(rest) >= 2 and rest[0] in {'"', "'"} and rest[-1] == rest[0]:
            return rest[1:-1]
        return rest
    raise ValueError("description missing from action.yml")


def _check_packaging() -> list[str]:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    errors: list[str] = []
    if re.search(r'(?m)^version = "', text):
        errors.append("pyproject.toml must not set a static version")
    project = tomllib.loads(text).get("project", {})
    if "version" not in project.get("dynamic", []):
        errors.append('pyproject.toml must list "version" in project.dynamic')
    extras = project.get("optional-dependencies", {})
    all_extra = extras.get("all")
    if all_extra != _ALL_EXTRA:
        errors.append(f"all extra is {all_extra!r}, expected {_ALL_EXTRA!r}")
    if _PY314 not in project.get("classifiers", []):
        errors.append("missing Python 3.14 classifier")
    desc = _action_description()
    if len(desc) >= _MARKETPLACE_DESC_LIMIT:
        errors.append(
            f"action.yml description is {len(desc)} characters; "
            f"Marketplace requires fewer than {_MARKETPLACE_DESC_LIMIT}"
        )
    return errors


def _check_pins(expected: str) -> list[str]:
    errors: list[str] = []
    sources = {
        "checkowners/__init__.py": _init_version(),
        "action.yml checkowners_version default": _action_input_default("checkowners_version"),
        "action.yml CHECKOWNERS_PINNED_VERSION": _action_pinned_version(),
    }
    for label, value in sources.items():
        if value != expected:
            errors.append(f"{label} is {value!r}, expected {expected!r}")

    wheel = _ROOT / f"checkowners-{expected}-py3-none-any.whl"
    if not wheel.is_file():
        errors.append(f"missing {wheel.name}")

    for name in ("requirements.lock", "requirements-dev.lock"):
        lock = _ROOT / name
        if not lock.is_file() or lock.stat().st_size == 0:
            errors.append(f"{name} is missing or empty")
    return errors


def _check_changelog(version: str) -> int:
    heading = re.compile(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$")
    text = (_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    if any(heading.match(line) for line in text.splitlines()):
        return 0
    print(f"::error::No dated changelog heading for {version}")
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_changelog.py <version>")
        print("       check_changelog.py --check-pins")
        return 2

    if argv[1] == "--check-pins":
        try:
            errors = _check_packaging() + _check_pins(_init_version())
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        for err in errors:
            print(f"::error::{err}")
        return 1 if errors else 0

    version = _normalize(argv[1])
    if version.lower() == "unreleased":
        print("::error::Unreleased is not a dated release entry")
        return 1

    changelog_rc = _check_changelog(version)
    try:
        pin_errors = _check_packaging() + _check_pins(version)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    for err in pin_errors:
        print(f"::error::{err}")
    if changelog_rc or pin_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

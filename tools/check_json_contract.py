"""Validate command JSON documents against the published command schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "schemas" / "commands-1.0.json"


def load_schema() -> dict[str, object]:
    """Return the parsed schema document."""
    loaded: object = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = "schema document must be an object"
        raise TypeError(msg)
    return loaded


def validator_for(command: str) -> Draft202012Validator:
    """Return a Draft 2020-12 validator for `command`."""
    document = load_schema()
    defs = document.get("$defs")
    if not isinstance(defs, dict) or command not in defs:
        msg = f"unknown command schema: {command}"
        raise KeyError(msg)
    wrapped: dict[str, object] = {
        "$schema": document.get("$schema"),
        "$defs": defs,
        "$ref": f"#/$defs/{command}",
    }
    return Draft202012Validator(wrapped)


def validate_instance(command: str, instance: object) -> None:
    """Raise ValidationError when `instance` does not match `command`."""
    validator_for(command).validate(instance)


def main(argv: list[str]) -> int:
    """Validate each `command=path` argument. Return 0 on success."""
    if not argv:
        print("usage: check_json_contract.py command=path ...", file=sys.stderr)
        return 2
    for item in argv:
        command, separator, raw_path = item.partition("=")
        if not separator or not command or not raw_path:
            print(f"expected command=path, got {item}", file=sys.stderr)
            return 2
        instance: object = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        try:
            validate_instance(command, instance)
        except ValidationError as exc:
            print(f"{raw_path} failed {command}: {exc.message}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

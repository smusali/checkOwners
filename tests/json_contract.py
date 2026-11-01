"""Validate command JSON documents against the published command schema."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "commands.schema.json"


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

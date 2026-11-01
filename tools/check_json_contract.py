"""Validate command JSON documents against the published command schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.json_contract import validate_instance  # noqa: E402


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

#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_METHODOLOGY = _ROOT / "docs" / "METHODOLOGY.md"
_METRIC_KEYS = (
    "ownership_score",
    "evidence_quality",
    "confidence",
    "top_owner_share",
    "effective_owners",
    "truck_factor_50",
    "truck_factor_75",
    "qualified_owner_count",
    "bus_factor",
    "historical_confidence",
    "avg_top_confidence",
    "avg_qualified_owner_count",
    "analysis_completeness",
    "confidence_delta",
    "max_confidence_delta",
)


def missing_keys(text: str) -> tuple[str, ...]:
    """Return metric keys that do not appear as their own token in `text`."""
    missing: list[str] = []
    for key in _METRIC_KEYS:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])"
        if re.search(pattern, text) is None:
            missing.append(key)
    return tuple(missing)


def main() -> int:
    """Exit 1 when METHODOLOGY.md omits a shipped metric key."""
    text = _METHODOLOGY.read_text(encoding="utf-8")
    missing = missing_keys(text)
    if missing:
        joined = ", ".join(missing)
        print(f"METHODOLOGY.md is missing metric keys: {joined}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

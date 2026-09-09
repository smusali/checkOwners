#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path

REPORT = Path("checkowners-report.md")
DIAGNOSTIC = (
    "## CheckOwners\n\n"
    "Drift analysis did not finish. See the **Run checkowners drift** step logs.\n"
)
MAX_RISK_PATHS = 8
SOLO_LINE = "One human contributor has qualified ownership. Single-owner paths are expected here."


def load(path: str) -> dict[str, object] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def fmt_delta(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def entry_lines(entries: object, kind: str) -> list[str]:
    if not isinstance(entries, list):
        return []
    lines: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        reason = item.get("reason")
        lines.append(f"⚠ `{path}` ({kind})")
        if isinstance(reason, str) and reason:
            lines.append(reason)
        lines.append(f"Δ {fmt_delta(item.get('confidence_delta', 0))}")
        lines.append("")
    return lines


def _is_bot(handle: str) -> bool:
    return "[bot]" in handle.lower()


def _display_handle(handle: str) -> str:
    if handle.startswith("@") or "@" in handle:
        return handle
    return f"@{handle}"


def _human_handles(handles: object) -> list[str]:
    if not isinstance(handles, list):
        return []
    return [item for item in handles if isinstance(item, str) and item and not _is_bot(item)]


def _bus_entries(bus: dict[str, object] | None) -> list[dict[str, object]]:
    if bus is None:
        return []
    raw = bus.get("entries")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _decay_reports(decay: dict[str, object] | None) -> list[dict[str, object]]:
    if decay is None:
        return []
    raw = decay.get("reports")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def qualified_humans(bus: dict[str, object] | None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in _bus_entries(bus):
        for handle in _human_handles(entry.get("contributors_above_threshold")):
            key = handle.casefold()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(handle)
    return ordered


def is_solo_repo(bus: dict[str, object] | None) -> bool:
    return bus is not None and len(qualified_humans(bus)) <= 1


def single_owner_entries(bus: dict[str, object] | None) -> list[dict[str, object]]:
    entries = [
        entry
        for entry in _bus_entries(bus)
        if len(_human_handles(entry.get("contributors_above_threshold"))) == 1
    ]

    def backup_count(entry: dict[str, object]) -> int:
        raw = entry.get("recommended_backups")
        return len(raw) if isinstance(raw, list) else 0

    def path_key(entry: dict[str, object]) -> str:
        path = entry.get("path")
        return path if isinstance(path, str) else ""

    entries.sort(key=lambda entry: (backup_count(entry) > 0, path_key(entry)))
    return entries


def has_actionable_knowledge_risk(
    bus: dict[str, object] | None,
    decay: dict[str, object] | None,
) -> bool:
    if bus is None or is_solo_repo(bus):
        return False
    return bool(single_owner_entries(bus) or _decay_reports(decay))


def has_actionable_findings(
    drift: dict[str, object] | None,
    bus: dict[str, object] | None,
    decay: dict[str, object] | None,
) -> bool:
    if drift is not None and drift.get("drift_detected"):
        return True
    return has_actionable_knowledge_risk(bus, decay)


def _decay_lines(decay: dict[str, object] | None) -> list[str]:
    reports = _decay_reports(decay)
    lines: list[str] = []
    for report in reports[:MAX_RISK_PATHS]:
        handle = report.get("handle")
        path = report.get("path")
        days = report.get("days_since_last_commit")
        if not isinstance(handle, str) or not isinstance(path, str) or not path:
            continue
        if isinstance(days, (int, float)):
            count = int(days)
            noun = "day" if count == 1 else "days"
            lines.append(
                f"{_display_handle(handle)} last committed to `{path}` {count} {noun} ago."
            )
        else:
            lines.append(f"{_display_handle(handle)} last committed to `{path}` a while ago.")
        lines.append("")
    extra = len(reports) - min(len(reports), MAX_RISK_PATHS)
    if extra > 0:
        lines.append(f"and {extra} more expertise-decay warnings.")
        lines.append("")
    return lines


def knowledge_risk_lines(
    bus: dict[str, object] | None,
    decay: dict[str, object] | None,
) -> list[str]:
    if bus is None:
        return []
    if is_solo_repo(bus):
        return [SOLO_LINE]
    lines: list[str] = []
    singles = single_owner_entries(bus)
    for entry in singles[:MAX_RISK_PATHS]:
        path = entry.get("path")
        humans = _human_handles(entry.get("contributors_above_threshold"))
        if not isinstance(path, str) or not path or not humans:
            continue
        lines.append(f"Only {_display_handle(humans[0])} is a qualified owner of `{path}`.")
        backups = _human_handles(entry.get("recommended_backups"))
        if backups:
            names = ", ".join(_display_handle(handle) for handle in backups)
            lines.append(f"Suggested backups: {names}.")
        else:
            lines.append("No suggested backups.")
        lines.append("")
    extra = len(singles) - min(len(singles), MAX_RISK_PATHS)
    if extra > 0:
        lines.append(f"and {extra} more single-owner paths.")
        lines.append("")
    lines.extend(_decay_lines(decay))
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def build() -> str:
    drift = load("drift.json")
    if drift is None:
        return DIAGNOSTIC

    parts: list[str] = ["## CheckOwners", "", "### CODEOWNERS drift"]
    notes = drift.get("notes")
    if isinstance(notes, list):
        for note in notes:
            parts.append(f"note: {note}")
    stale = entry_lines(drift.get("stale"), "stale")
    missing = entry_lines(drift.get("missing"), "missing")
    changed = entry_lines(drift.get("changed"), "changed")
    entries = stale + missing + changed
    if entries:
        if isinstance(notes, list) and notes:
            parts.append("")
        parts.extend(entries)
        if parts[-1] == "":
            parts.pop()
    else:
        parts.append("No drift detected.")

    risk = knowledge_risk_lines(load("bus_factor.json"), load("decay.json"))
    if risk:
        parts.append("")
        parts.append("### Knowledge risk")
        parts.extend(risk)

    return "\n".join(parts) + "\n"


def main() -> int:
    try:
        text = build()
    except Exception:
        text = DIAGNOSTIC
    try:
        REPORT.write_text(text, encoding="utf-8")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as fh:
                fh.write(text)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

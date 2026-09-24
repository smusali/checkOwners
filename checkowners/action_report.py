"""Job-summary markdown and bounded GITHUB_OUTPUT assembly for CI."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import TypeVar

from checkowners.models import completeness_label, models_payload
from checkowners.privacy import KNOWLEDGE_RISK_NOTICE

_PROVENANCE_KEYS = (
    "checkowners_version",
    "repository",
    "head_sha",
    "generated_at",
    "analysis_completeness",
    "github_evidence_collected_at",
    "repository_head",
    "team_snapshot",
)


def _copied_provenance(data: dict[str, object]) -> dict[str, object]:
    return {key: data[key] for key in _PROVENANCE_KEYS if key in data}


REPORT = Path("checkowners-report.md")
DIAGNOSTIC = (
    "## CheckOwners\n\n"
    "Drift analysis did not finish. See the **Run checkowners** step logs.\n\n"
    f"{KNOWLEDGE_RISK_NOTICE}\n"
)
MAX_RISK_PATHS = 8
MAX_PATH_DISPLAY = 80
SOLO_LINE = "One human contributor has qualified ownership. Single-owner paths are expected here."
OUTPUT_SCHEMA_VERSION = 1
DEFAULT_MAX_OUTPUT_ENTRIES = 50
DEFAULT_ARTIFACT_NAME = "checkowners-reports"
T = TypeVar("T")


def md_cell(text: str, max_len: int = 0) -> str:
    flattened = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    safe = flattened.replace("`", "'").replace("|", "/").replace("<", "(").replace(">", ")")
    if max_len and len(safe) > max_len:
        return safe[: max_len - 1] + "…"
    return safe


def _path_cell(path: str) -> tuple[str, bool]:
    shown = md_cell(path, MAX_PATH_DISPLAY)
    return shown, shown != md_cell(path)


def load(path: str) -> dict[str, object] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def fmt_delta(value: object) -> str:
    if not isinstance(value, (int, float, str)):
        return "0.00"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _drift_entries(entries: object, kind: str) -> list[tuple[str, dict[str, object]]]:
    if not isinstance(entries, list):
        return []
    return [
        (kind, item)
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("path")
    ]


def entry_lines(pairs: list[tuple[str, dict[str, object]]]) -> tuple[list[str], bool]:
    lines: list[str] = []
    truncated = False
    for kind, item in pairs:
        path = item.get("path")
        if not isinstance(path, str):
            continue
        shown, cut = _path_cell(path)
        truncated = truncated or cut
        lines.append(f"⚠ `{shown}` ({kind})")
        reason = item.get("reason")
        if isinstance(reason, str) and reason:
            lines.append(md_cell(reason))
        lines.append(f"Δ {fmt_delta(item.get('confidence_delta', 0))}")
        lines.append("")
    return lines, truncated


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


def _decay_lines(decay: dict[str, object] | None) -> tuple[list[str], bool]:
    reports = _decay_reports(decay)
    lines: list[str] = []
    truncated = False
    for report in reports[:MAX_RISK_PATHS]:
        handle = report.get("handle")
        path = report.get("path")
        days = report.get("days_since_last_commit")
        if not isinstance(handle, str) or not isinstance(path, str) or not path:
            continue
        shown, cut = _path_cell(path)
        truncated = truncated or cut
        owner = md_cell(_display_handle(handle))
        if isinstance(days, (int, float)):
            count = int(days)
            noun = "day" if count == 1 else "days"
            lines.append(f"{owner} last committed to `{shown}` {count} {noun} ago.")
        else:
            lines.append(f"{owner} last committed to `{shown}` a while ago.")
        lines.append("")
    extra = len(reports) - min(len(reports), MAX_RISK_PATHS)
    if extra > 0:
        lines.append(f"and {extra} more continuity-risk warnings.")
        lines.append("")
    return lines, truncated


def _max_output_entries() -> int:
    raw = os.environ.get("MAX_OUTPUT_ENTRIES", str(DEFAULT_MAX_OUTPUT_ENTRIES))
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"max_output_entries must be a positive integer, got {raw!r}") from None
    if value < 1:
        raise SystemExit(f"max_output_entries must be a positive integer, got {raw!r}")
    return value


def _resolve_limit(limit: int | None) -> int:
    return _max_output_entries() if limit is None else limit


def _trim(items: list[T], limit: int) -> tuple[list[T], bool]:
    """Return the first `limit` items and whether any were dropped."""
    if len(items) > limit:
        return items[:limit], True
    return items, False


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _ratchet_counts(data: dict[str, object]) -> dict[str, int]:
    stale = data.get("stale_baseline")
    stale_count = len(stale) if isinstance(stale, list) else _as_int(stale)
    return {
        "baselined": _as_int(data.get("baselined")),
        "stale_baseline": stale_count,
        "suppressed": _as_int(data.get("suppressed")),
    }


def _ratchet_line(data: dict[str, object]) -> str:
    counts = _ratchet_counts(data)
    return (
        f"Baselined: {counts['baselined']}. "
        f"Suppressed: {counts['suppressed']}. "
        f"Stale baseline: {counts['stale_baseline']}."
    )


def summarize_drift(data: dict[str, object], limit: int) -> dict[str, object]:
    """Build a bounded drift summary from full CLI drift JSON (`data`, `limit`)."""
    missing = _as_list(data.get("missing"))
    stale = _as_list(data.get("stale"))
    changed = _as_list(data.get("changed"))
    notes = _as_list(data.get("notes"))
    trimmed_missing, missing_cut = _trim(missing, limit)
    trimmed_stale, stale_cut = _trim(stale, limit)
    trimmed_changed, changed_cut = _trim(changed, limit)
    trimmed_notes, notes_cut = _trim(notes, limit)
    return {
        **_copied_provenance(data),
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "drift_detected": bool(data.get("drift_detected")),
        "severity": data.get("severity"),
        "max_confidence_delta": data.get("max_confidence_delta"),
        "notes": trimmed_notes,
        "counts": {
            "missing": len(missing),
            "stale": len(stale),
            "changed": len(changed),
            **_ratchet_counts(data),
        },
        "missing": trimmed_missing,
        "stale": trimmed_stale,
        "changed": trimmed_changed,
        "truncated": missing_cut or stale_cut or changed_cut or notes_cut,
        "models": models_payload(),
    }


def summarize_bus_factor(data: dict[str, object], limit: int) -> dict[str, object]:
    """Build a bounded qualified-owner summary from full CLI JSON (`data`, `limit`)."""
    entries = _bus_entries(data)
    counts = {"critical": 0, "warning": 0, "ok": 0, "entries": len(entries)}
    for entry in entries:
        tier = entry.get("tier")
        if tier in ("critical", "warning", "ok"):
            counts[tier] += 1
    raw_paths = data.get("critical_paths")
    if isinstance(raw_paths, list):
        paths = [path for path in raw_paths if isinstance(path, str)]
    else:
        paths = []
    trimmed_paths, paths_cut = _trim(paths, limit)
    cap = data.get("qualified_owner_count_cap")
    if not isinstance(cap, int) or isinstance(cap, bool):
        cap = 3
    return {
        **_copied_provenance(data),
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "repo_average": data.get("repo_average"),
        "qualified_owner_count_cap": cap,
        "counts": {**counts, **_ratchet_counts(data)},
        "critical_paths": trimmed_paths,
        "truncated": paths_cut or bool(entries),
        "models": models_payload(),
    }


def summarize_balance(data: dict[str, object], limit: int) -> dict[str, object]:
    """Build a bounded balance summary from full CLI JSON (`data`, `limit`)."""
    loads = _as_list(data.get("loads"))
    overloaded = _as_list(data.get("overloaded"))
    suggestions = _as_list(data.get("suggestions"))
    trimmed_loads, loads_cut = _trim(loads, limit)
    trimmed_overloaded, overloaded_cut = _trim(overloaded, limit)
    trimmed_suggestions, suggestions_cut = _trim(suggestions, limit)
    return {
        **_copied_provenance(data),
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "source": data.get("source"),
        "average": data.get("average"),
        "fallback_reason": data.get("fallback_reason"),
        "counts": {
            "loads": len(loads),
            "overloaded": len(overloaded),
            "suggestions": len(suggestions),
        },
        "loads": trimmed_loads,
        "overloaded": trimmed_overloaded,
        "suggestions": trimmed_suggestions,
        "truncated": loads_cut or overloaded_cut or suggestions_cut,
        "models": models_payload(),
    }


def summarize_decay(data: dict[str, object], limit: int) -> dict[str, object]:
    """Build a bounded decay summary from full CLI decay JSON (`data`, `limit`)."""
    reports = _decay_reports(data)
    trimmed, cut = _trim(reports, limit)
    return {
        **_copied_provenance(data),
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "counts": {"reports": len(reports)},
        "reports": trimmed,
        "truncated": cut,
        "models": models_payload(),
    }


def _write_multiline_output(name: str, payload: str) -> None:
    """Append `name` / `payload` to GITHUB_OUTPUT with a random delimiter."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    delim = f"ghadelim_{secrets.token_hex(16)}"
    while f"\n{delim}\n" in f"\n{payload}\n":
        delim = f"ghadelim_{secrets.token_hex(16)}"
    with Path(output_file).open("a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n")
        fh.write(payload)
        fh.write(f"\n{delim}\n")


def publish_outputs(*, limit: int | None = None) -> None:
    resolved = _resolve_limit(limit)
    artifact = os.environ.get("CHECKOWNERS_ARTIFACT_NAME", DEFAULT_ARTIFACT_NAME)
    _write_multiline_output("artifact_name", artifact)

    drift = load("drift.json")
    if drift is not None:
        _write_multiline_output(
            "drift_summary",
            json.dumps(summarize_drift(drift, resolved), separators=(",", ":")),
        )

    bus = load("bus_factor.json")
    if bus is not None:
        _write_multiline_output(
            "bus_factor_summary",
            json.dumps(summarize_bus_factor(bus, resolved), separators=(",", ":")),
        )

    decay = load("decay.json")
    if decay is not None:
        _write_multiline_output(
            "decay_summary",
            json.dumps(summarize_decay(decay, resolved), separators=(",", ":")),
        )

    balance = load("balance.json")
    if balance is not None:
        _write_multiline_output(
            "balance_summary",
            json.dumps(summarize_balance(balance, resolved), separators=(",", ":")),
        )


def knowledge_risk_lines(
    bus: dict[str, object] | None,
    decay: dict[str, object] | None,
) -> tuple[list[str], bool]:
    if bus is None:
        return [], False
    if is_solo_repo(bus):
        return [SOLO_LINE], False
    lines: list[str] = []
    truncated = False
    singles = single_owner_entries(bus)
    for entry in singles[:MAX_RISK_PATHS]:
        path = entry.get("path")
        humans = _human_handles(entry.get("contributors_above_threshold"))
        if not isinstance(path, str) or not path or not humans:
            continue
        shown, cut = _path_cell(path)
        truncated = truncated or cut
        owner = md_cell(_display_handle(humans[0]))
        lines.append(f"Only {owner} is a qualified owner of `{shown}`.")
        backups = _human_handles(entry.get("recommended_backups"))
        if backups:
            names = ", ".join(md_cell(_display_handle(handle)) for handle in backups)
            lines.append(f"Candidate backup reviewers: {names}.")
        else:
            lines.append("No candidate backup reviewers.")
        lines.append("")
    extra = len(singles) - min(len(singles), MAX_RISK_PATHS)
    if extra > 0:
        lines.append(f"and {extra} more single-owner paths.")
        lines.append("")
    decay_lines, decay_cut = _decay_lines(decay)
    truncated = truncated or decay_cut
    lines.extend(decay_lines)
    while lines and lines[-1] == "":
        lines.pop()
    return lines, truncated


def _artifact_name() -> str:
    return os.environ.get("CHECKOWNERS_ARTIFACT_NAME", DEFAULT_ARTIFACT_NAME)


def _completeness_lines(drift: dict[str, object]) -> list[str]:
    raw = drift.get("analysis_completeness")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return []
    lines = [completeness_label(raw * 1.0)]
    gaps = drift.get("analysis_gaps")
    if not isinstance(gaps, list):
        return lines
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        reason = gap.get("reason")
        if isinstance(reason, str) and reason:
            lines.append(reason)
    return lines


def _more_line(extra: int) -> str:
    artifact = _artifact_name()
    if extra > 0:
        return f"and {extra} more. Full report is in the {artifact} artifact."
    return f"Full report is in the {artifact} artifact."


def build(*, limit: int | None = None) -> str:
    drift = load("drift.json")
    if drift is None:
        return DIAGNOSTIC

    resolved = _resolve_limit(limit)
    parts: list[str] = ["## CheckOwners", "", "### CODEOWNERS drift"]
    parts.extend(_completeness_lines(drift))
    raw_notes = drift.get("notes")
    notes = (
        [note for note in raw_notes if isinstance(note, str)] if isinstance(raw_notes, list) else []
    )
    shown_notes, notes_cut = _trim(notes, resolved)
    for note in shown_notes:
        parts.append(f"note: {md_cell(note)}")

    pairs = (
        _drift_entries(drift.get("stale"), "stale")
        + _drift_entries(drift.get("missing"), "missing")
        + _drift_entries(drift.get("changed"), "changed")
    )
    shown_pairs, entries_cut = _trim(pairs, resolved)
    entries, path_cut = entry_lines(shown_pairs)
    extra = 0
    if notes_cut:
        extra += len(notes) - resolved
    if entries_cut:
        extra += len(pairs) - resolved
    if entries:
        if shown_notes:
            parts.append("")
        parts.extend(entries)
        if parts[-1] == "":
            parts.pop()
    else:
        parts.append("No drift detected.")
    parts.append(_ratchet_line(drift))

    need_artifact = extra > 0 or path_cut
    if need_artifact:
        parts.append("")
        parts.append(_more_line(extra))

    risk, risk_cut = knowledge_risk_lines(load("bus_factor.json"), load("decay.json"))
    if risk:
        parts.append("")
        parts.append("### Knowledge risk")
        parts.extend(risk)
        if risk_cut and not need_artifact:
            parts.append("")
            parts.append(_more_line(0))

    parts.append("")
    parts.append(_models_line())
    parts.append("")
    parts.append(KNOWLEDGE_RISK_NOTICE)
    return "\n".join(parts) + "\n"


def _models_line() -> str:
    payload = models_payload()
    body = ", ".join(f"{name} {payload[name]}" for name in ("ownership", "risk"))
    return f"models: {body}"


def write_step_summary(text: str) -> None:
    try:
        REPORT.write_text(text, encoding="utf-8")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as fh:
                fh.write(text)
    except OSError:
        pass

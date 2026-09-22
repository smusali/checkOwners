"""Accepted-findings baseline and explicit suppressions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from checkowners.busfactor import BusFactorReport
from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    DriftEntry,
    DriftResult,
    Finding,
    FindingRule,
    RatchetCounts,
    Suppression,
)
from checkowners.patterns import pattern_matches

DEFAULT_BASELINE_PATH = ".checkowners-baseline.json"

_RULES: dict[str, FindingRule] = {
    "changed": "changed",
    "missing": "missing",
    "single-expert": "single-expert",
    "stale": "stale",
}


@dataclass(frozen=True)
class RatchetOutcome:
    drift: DriftResult
    new: tuple[Finding, ...]
    hidden_bus_paths: frozenset[str]
    counts: RatchetCounts
    expired: tuple[Suppression, ...]
    stale_baseline: tuple[Finding, ...]


def findings_from_drift(result: DriftResult) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for entry in result.missing:
        findings.append(_entry_finding("missing", entry))
    for entry in result.stale:
        findings.append(_entry_finding("stale", entry))
    for entry in result.changed:
        findings.append(_entry_finding("changed", entry))
    return tuple(findings)


def findings_from_bus(report: BusFactorReport) -> tuple[Finding, ...]:
    critical = set(report.critical_paths)
    return tuple(
        Finding(
            rule="single-expert",
            path=entry.path,
            owners=entry.contributors_above_threshold,
        )
        for entry in report.entries
        if entry.path in critical
    )


def write_baseline(path: Path, findings: tuple[Finding, ...]) -> None:
    """Write `findings` as a sorted, diff-stable accepted-findings file at `path`."""
    ordered = _sorted_findings(findings)
    payload = {
        "schema_version": COMMAND_SCHEMA_VERSION,
        "findings": [finding_payload(item) for item in ordered],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> tuple[Finding, ...]:
    """Load accepted findings from `path` (`schema_version` plus a findings list)."""
    if not path.exists():
        msg = f"Baseline file not found: {path}"
        raise ValueError(msg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"Invalid baseline file {path}: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"Invalid baseline file {path}: expected a JSON object"
        raise ValueError(msg)
    version = raw.get("schema_version")
    if version != COMMAND_SCHEMA_VERSION:
        msg = (
            f"Invalid baseline file {path}: "
            f"schema_version {version!r} (expected {COMMAND_SCHEMA_VERSION!r})"
        )
        raise ValueError(msg)
    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list):
        msg = f"Invalid baseline file {path}: findings must be a list"
        raise ValueError(msg)
    findings: list[Finding] = []
    for index, item in enumerate(findings_raw):
        findings.append(_parse_finding(item, path, index))
    return tuple(findings)


def finding_payload(finding: Finding) -> dict[str, object]:
    return {
        "owners": list(_sorted_owners(finding.owners)),
        "path": finding.path,
        "rule": finding.rule,
    }


def active_suppressions(
    suppressions: tuple[Suppression, ...],
    as_of: date,
) -> tuple[tuple[Suppression, ...], tuple[Suppression, ...]]:
    """Split `suppressions` into active and expired relative to `as_of`."""
    active: list[Suppression] = []
    expired: list[Suppression] = []
    for item in suppressions:
        if item.expires is not None and as_of > item.expires:
            expired.append(item)
        else:
            active.append(item)
    return tuple(active), tuple(expired)


def suppression_matches(suppression: Suppression, finding: Finding) -> bool:
    if suppression.rule != finding.rule:
        return False
    if suppression.path == finding.path:
        return True
    return pattern_matches(suppression.path, finding.path)


def apply_ratchet(
    result: DriftResult,
    *,
    baseline: tuple[Finding, ...],
    suppressions: tuple[Suppression, ...],
    as_of: date,
    bus: BusFactorReport | None = None,
) -> RatchetOutcome:
    """Drop suppressed and baselined findings; report stale baseline entries.

    `as_of` is the analysis calendar date. `bus` is omitted when qualified-owner
    findings were not evaluated; single-expert baseline rows are then ignored
    for staleness.
    """
    current = findings_from_drift(result)
    if bus is not None:
        current = (*current, *findings_from_bus(bus))
    active, expired = active_suppressions(suppressions, as_of)
    suppressed = tuple(item for item in current if _is_suppressed(item, active))
    remaining = tuple(item for item in current if item not in suppressed)
    baseline_keys = {item.identity() for item in baseline}
    current_keys = {item.identity() for item in current}
    baselined = tuple(item for item in remaining if item.identity() in baseline_keys)
    new = tuple(item for item in remaining if item.identity() not in baseline_keys)
    evaluated_rules = {"missing", "stale", "changed"}
    if bus is not None:
        evaluated_rules.add("single-expert")
    stale = tuple(
        item
        for item in baseline
        if item.rule in evaluated_rules and item.identity() not in current_keys
    )
    drop = {item.identity() for item in (*suppressed, *baselined)}
    filtered = _rebuild_drift(result, drop)
    hidden_bus = frozenset(
        item.path for item in (*suppressed, *baselined) if item.rule == "single-expert"
    )
    return RatchetOutcome(
        drift=filtered,
        new=new,
        hidden_bus_paths=hidden_bus,
        counts=RatchetCounts(
            baselined=len(baselined),
            suppressed=len(suppressed),
            stale_baseline=len(stale),
        ),
        expired=expired,
        stale_baseline=stale,
    )


def _entry_finding(rule: FindingRule, entry: DriftEntry) -> Finding:
    owners = () if rule == "missing" else entry.owners
    return Finding(rule=rule, path=entry.path, owners=owners)


def _is_suppressed(finding: Finding, active: tuple[Suppression, ...]) -> bool:
    return any(suppression_matches(item, finding) for item in active)


def _rebuild_drift(
    result: DriftResult,
    drop: set[tuple[str, str, tuple[str, ...]]],
) -> DriftResult:
    stale = _filter_bucket(result.stale, "stale", drop)
    missing = _filter_bucket(result.missing, "missing", drop)
    changed = _filter_bucket(result.changed, "changed", drop)
    return DriftResult(
        stale=stale,
        missing=missing,
        changed=changed,
        drift_detected=bool(stale or missing or changed),
        notes=result.notes,
    )


def _filter_bucket(
    entries: tuple[DriftEntry, ...],
    rule: FindingRule,
    drop: set[tuple[str, str, tuple[str, ...]]],
) -> tuple[DriftEntry, ...]:
    return tuple(entry for entry in entries if _entry_finding(rule, entry).identity() not in drop)


def _sorted_owners(owners: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(owners, key=str.casefold))


def _sorted_findings(findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    decorated = [(item.rule, item.path, _sorted_owners(item.owners), item) for item in findings]
    decorated.sort(key=lambda row: (row[0], row[1], row[2]))
    return tuple(
        Finding(rule=item.rule, path=item.path, owners=owners)
        for _rule, _path, owners, item in decorated
    )


def _parse_finding(raw: object, path: Path, index: int) -> Finding:
    prefix = f"{path} findings[{index}]"
    if not isinstance(raw, dict):
        msg = f"Invalid {prefix}: expected an object"
        raise ValueError(msg)
    rule = raw.get("rule")
    finding_path = raw.get("path")
    owners_raw = raw.get("owners", [])
    if not isinstance(rule, str) or not rule:
        msg = f"Invalid {prefix}: rule is required"
        raise ValueError(msg)
    parsed_rule = _RULES.get(rule)
    if parsed_rule is None:
        msg = f"Invalid {prefix}: unsupported rule {rule!r}"
        raise ValueError(msg)
    if not isinstance(finding_path, str) or not finding_path:
        msg = f"Invalid {prefix}: path is required"
        raise ValueError(msg)
    if not isinstance(owners_raw, list) or not all(isinstance(item, str) for item in owners_raw):
        msg = f"Invalid {prefix}: owners must be a list of strings"
        raise ValueError(msg)
    return Finding(rule=parsed_rule, path=finding_path, owners=tuple(owners_raw))

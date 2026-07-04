"""Drift detection between inferred ownership and current CODEOWNERS.

The committed CODEOWNERS is parsed into ordered pattern rules and compared
against the inferred per-file ownership with real CODEOWNERS matching
semantics (last matching rule wins), so directory- and glob-level rules are
honored instead of being string-compared against file paths:

- ``missing``: an inferred file that no rule covers.
- ``stale``: a rule whose pattern matches no tracked file (dead rule).
- ``changed``: a rule whose owners disagree with the inferred owners of the
  files it covers (aggregated per rule, ranked by the worst file delta).

Owner comparison is case-insensitive. When the inferred side only has raw
commit emails but CODEOWNERS uses @handles, the sets are incomparable; the
comparison is skipped and a note explains how to enable handle resolution
instead of reporting 100% false drift.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from checkowners.models import (
    Config,
    DriftEntry,
    DriftMode,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.patterns import CodeownersRule, match_path, parse_rules, pattern_matches

_DEFAULT_CODEOWNERS_PATH = ".github/CODEOWNERS"

_IDENTITY_NOTE = (
    "inferred owners are commit emails but CODEOWNERS uses @handles; "
    "owner comparison skipped. Set GITHUB_TOKEN (github.resolve_handles) "
    "to compare owner sets."
)
_TEAM_NOTE = "rules owned by teams (@org/team) are not compared against inferred individuals."


def detect_drift(
    repo_root: Path,
    ownership: OwnershipMap,
    config: Config,
    *,
    codeowners_path: Path | None = None,
) -> DriftResult:
    """Compare inferred ownership against current CODEOWNERS."""
    target = codeowners_path or (repo_root / _DEFAULT_CODEOWNERS_PATH)
    rules = _load_rules(target)
    tracked = _tracked_files(repo_root)
    result = _compare(
        rules,
        ownership.paths,
        tracked,
        config.drift.mode,
        config.drift.min_confidence_delta,
    )
    _write_github_output(result)
    return result


def _load_rules(codeowners_path: Path) -> tuple[CodeownersRule, ...]:
    if not codeowners_path.exists():
        return ()
    return parse_rules(codeowners_path.read_text(encoding="utf-8"))


def _tracked_files(repo_root: Path) -> tuple[str, ...]:
    """List tracked files; used to tell dead rules from merely quiet ones."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def _compare(
    rules: tuple[CodeownersRule, ...],
    inferred: dict[str, PathOwnership],
    tracked: tuple[str, ...],
    mode: DriftMode,
    min_delta: float,
) -> DriftResult:
    stale: list[DriftEntry] = []
    missing: list[DriftEntry] = []
    changed: list[DriftEntry] = []
    notes: list[str] = []

    coverage = {path: match_path(rules, path.lstrip("/")) for path in inferred}

    if mode in ("commit", "both"):
        missing = _find_missing(inferred, coverage)
        changed, notes = _find_changed(rules, inferred, coverage, min_delta)

    if mode in ("repo", "both"):
        stale = _find_stale(rules, tracked)
        if mode == "repo":
            changed, notes = _find_changed(rules, inferred, coverage, min_delta)

    stale_sorted = _sort_by_delta(stale)
    missing_sorted = _sort_by_delta(missing)
    changed_sorted = _sort_by_delta(changed)
    return DriftResult(
        stale=stale_sorted,
        missing=missing_sorted,
        changed=changed_sorted,
        drift_detected=bool(stale_sorted or missing_sorted or changed_sorted),
        notes=tuple(notes),
    )


def _find_missing(
    inferred: dict[str, PathOwnership],
    coverage: dict[str, CodeownersRule | None],
) -> list[DriftEntry]:
    """Inferred files no rule covers. Owner-less rules count as intentional."""
    entries: list[DriftEntry] = []
    for path, po in inferred.items():
        if coverage[path] is not None:
            continue
        entries.append(
            DriftEntry(
                path=path,
                confidence_delta=_top_confidence(po.owners),
                reason="path not covered by any CODEOWNERS rule",
                bus_factor=po.bus_factor,
                decay=bool(po.decay_warnings),
            )
        )
    return entries


def _find_stale(
    rules: tuple[CodeownersRule, ...],
    tracked: tuple[str, ...],
) -> list[DriftEntry]:
    """Rules whose pattern no longer matches any tracked file."""
    if not tracked:
        return []
    entries: list[DriftEntry] = []
    for rule in rules:
        if any(pattern_matches(rule.pattern, path) for path in tracked):
            continue
        entries.append(
            DriftEntry(
                path=rule.pattern,
                confidence_delta=1.0,
                reason=f"pattern matches no tracked file (line {rule.line_number})",
            )
        )
    return entries


def _find_changed(
    rules: tuple[CodeownersRule, ...],
    inferred: dict[str, PathOwnership],
    coverage: dict[str, CodeownersRule | None],
    min_delta: float,
) -> tuple[list[DriftEntry], list[str]]:
    """Per-rule owner disagreement, aggregated over the files the rule covers."""
    notes: list[str] = []
    if _identities_incomparable(rules, inferred):
        return [], [_IDENTITY_NOTE]

    per_rule: dict[CodeownersRule, list[tuple[str, PathOwnership]]] = {}
    for path, rule in coverage.items():
        if rule is not None and rule.owners:
            per_rule.setdefault(rule, []).append((path, inferred[path]))

    entries: list[DriftEntry] = []
    team_rules_skipped = False
    for rule, covered in per_rule.items():
        if any("/" in owner for owner in rule.owners):
            team_rules_skipped = True
            continue
        current = _normalize_owners(rule.owners)
        worst_delta = 0.0
        diverging = 0
        bus_factor: int | None = None
        decay = False
        for _path, po in covered:
            inferred_handles = _normalize_owners(o.handle for o in po.owners)
            if current == inferred_handles:
                continue
            delta = _confidence_delta(rule.owners, po.owners)
            diverging += 1
            if delta > worst_delta:
                worst_delta = delta
                bus_factor = po.bus_factor
                decay = bool(po.decay_warnings)
        if diverging == 0 or worst_delta < min_delta:
            continue
        entries.append(
            DriftEntry(
                path=rule.pattern,
                confidence_delta=worst_delta,
                reason=(
                    f"owners diverge on {diverging} of {len(covered)} covered path(s) "
                    f"(line {rule.line_number})"
                ),
                bus_factor=bus_factor,
                decay=decay,
            )
        )
    if team_rules_skipped:
        notes.append(_TEAM_NOTE)
    return entries, notes


def _identities_incomparable(
    rules: tuple[CodeownersRule, ...],
    inferred: dict[str, PathOwnership],
) -> bool:
    """True when CODEOWNERS uses @handles but inference only has emails."""
    rule_owners = [o for rule in rules for o in rule.owners]
    inferred_handles = [o.handle for po in inferred.values() for o in po.owners]
    if not rule_owners or not inferred_handles:
        return False
    rules_use_handles = all(owner.startswith("@") for owner in rule_owners)
    inferred_all_emails = not any(handle.startswith("@") for handle in inferred_handles)
    return rules_use_handles and inferred_all_emails


def _normalize_owners(owners: Iterable[str]) -> frozenset[str]:
    return frozenset(owner.casefold() for owner in owners)


def _confidence_delta(
    current_owners: tuple[str, ...],
    inferred_owners: tuple[OwnerEntry, ...],
) -> float:
    """Aggregate per-owner confidence delta between current and inferred sets."""
    inferred_map = {o.handle.casefold(): o.confidence for o in inferred_owners}
    inferred_set = set(inferred_map)
    current_set = {owner.casefold() for owner in current_owners}
    added = inferred_set - current_set
    removed = current_set - inferred_set
    delta_added = sum(inferred_map[h] for h in added)
    delta_removed = float(len(removed))
    if not added and not removed:
        return 0.0
    total = delta_added + delta_removed
    return min(1.0, total)


def _top_confidence(owners: tuple[OwnerEntry, ...]) -> float:
    if not owners:
        return 0.0
    return max(o.confidence for o in owners)


def _sort_by_delta(entries: list[DriftEntry]) -> tuple[DriftEntry, ...]:
    entries.sort(key=lambda e: (-abs(e.confidence_delta), e.path))
    return tuple(entries)


def _write_github_output(result: DriftResult) -> None:
    """Write drift result to GITHUB_OUTPUT if running in Actions."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    payload = json.dumps(
        {
            "drift_detected": result.drift_detected,
            "max_confidence_delta": result.max_confidence_delta,
            "stale": [_entry_payload(e) for e in result.stale],
            "missing": [_entry_payload(e) for e in result.missing],
            "changed": [_entry_payload(e) for e in result.changed],
            "notes": list(result.notes),
        }
    )
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(f"checkowners_drift={payload}\n")


def _entry_payload(entry: DriftEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": entry.path,
        "confidence_delta": entry.confidence_delta,
        "reason": entry.reason,
    }
    if entry.bus_factor is not None:
        payload["bus_factor"] = entry.bus_factor
    if entry.decay:
        payload["decay"] = entry.decay
    return payload

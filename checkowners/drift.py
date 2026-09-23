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

from checkowners.busfactor import qualified_owner_count_fields
from checkowners.github import external_evidence_payload
from checkowners.models import (
    IDENTITY_COMPARISON_REASON,
    TEAM_MEMBERSHIP_REASON,
    AnalysisGap,
    AnalysisGapJson,
    Config,
    DriftEntry,
    DriftEntryJson,
    DriftKind,
    DriftMode,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    RecommendationAction,
    Severity,
    repository_label,
    stamp_json,
)
from checkowners.patterns import CodeownersRule, match_path, parse_rules, pattern_matches

_DEFAULT_CODEOWNERS_PATH = ".github/CODEOWNERS"

_IDENTITY_NOTE = (
    "inferred owners are commit emails but CODEOWNERS uses @handles; "
    "owner comparison skipped. Set GITHUB_TOKEN (github.resolve_handles) "
    "to compare owner sets."
)
_TEAM_NOTE = "rules owned by teams (@org/team) are not compared against inferred individuals."


def evidence_gaps(result: DriftResult) -> tuple[AnalysisGap, ...]:
    """Return completeness gaps for unverifiable comparisons in `result`."""
    gaps: list[AnalysisGap] = []
    if _IDENTITY_NOTE in result.notes:
        gaps.append(AnalysisGap("ambiguous_identity", IDENTITY_COMPARISON_REASON))
    if _TEAM_NOTE in result.notes:
        gaps.append(AnalysisGap("team_membership", TEAM_MEMBERSHIP_REASON))
    return tuple(gaps)


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
    return _compare(
        rules,
        ownership.paths,
        tracked,
        config.drift.mode,
        config.drift.min_confidence_delta,
    )


def _load_rules(codeowners_path: Path) -> tuple[CodeownersRule, ...]:
    if not codeowners_path.exists():
        return ()
    return parse_rules(codeowners_path.read_text(encoding="utf-8"))


def _tracked_files(repo_root: Path) -> tuple[str, ...]:
    """List tracked files; used to tell dead rules from merely quiet ones."""
    result = subprocess.run(
        ["git", "ls-files"],  # noqa: S607  # git from PATH; argv is a literal list
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
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
                qualified_owner_count=po.qualified_owner_count,
                decay=bool(po.decay_warnings),
                observed_owners=tuple(owner.handle for owner in po.owners),
                drift_type="missing_observed_expert",
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
                owners=rule.owners,
                drift_type="stale_rule",
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
        qualified_owner_count: int | None = None
        decay = False
        observed_owners: tuple[str, ...] = ()
        for _path, po in covered:
            inferred_handles = _normalize_owners(o.handle for o in po.owners)
            if current == inferred_handles:
                continue
            delta = _confidence_delta(rule.owners, po.owners)
            diverging += 1
            if delta > worst_delta:
                worst_delta = delta
                qualified_owner_count = po.qualified_owner_count
                decay = bool(po.decay_warnings)
                observed_owners = tuple(owner.handle for owner in po.owners)
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
                qualified_owner_count=qualified_owner_count,
                decay=decay,
                owners=rule.owners,
                observed_owners=observed_owners,
                drift_type=_changed_drift_type(rule.owners, observed_owners, decay),
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


def write_github_output(
    result: DriftResult,
    cap: int,
    *,
    analysis_ref: str = "",
    analysis_epoch: str = "",
    config: Config | None = None,
    analysis_completeness: float | None = None,
    analysis_gaps: list[AnalysisGapJson] | None = None,
) -> None:
    """Write drift result to GITHUB_OUTPUT if running in Actions."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    payload = json.dumps(
        stamp_json(
            {
                "drift_detected": result.drift_detected,
                "max_confidence_delta": result.max_confidence_delta,
                "stale": [drift_entry_payload(e, cap, config) for e in result.stale],
                "missing": [drift_entry_payload(e, cap, config) for e in result.missing],
                "changed": [drift_entry_payload(e, cap, config) for e in result.changed],
                "notes": list(result.notes),
            },
            repository=repository_label(Path.cwd()),
            head_sha=analysis_ref,
            generated_at=analysis_epoch,
            analysis_completeness=analysis_completeness,
            evidence=external_evidence_payload(analysis_ref),
            analysis_gaps=analysis_gaps,
        ),
        sort_keys=True,
    )
    with Path(output_file).open("a", encoding="utf-8") as f:
        f.write(f"drift_summary={payload}\n")


def owner_overlap(declared: tuple[str, ...], observed: tuple[str, ...]) -> float:
    """Return the Jaccard overlap of `declared` and `observed` (case-insensitive)."""
    left = {owner.casefold() for owner in declared}
    right = {owner.casefold() for owner in observed}
    union = left | right
    if not union:
        return 0.0
    return round(len(left & right) / len(union), 4)


def _changed_drift_type(
    declared: tuple[str, ...],
    observed: tuple[str, ...],
    decay: bool,
) -> DriftKind:
    if owner_overlap(declared, observed) == 0.0 and declared and observed:
        return "complete_ownership_replacement"
    if decay:
        return "stale_declared_owner"
    return "owner_mismatch"


def _recommendation_action(kind: DriftKind) -> RecommendationAction:
    if kind == "stale_rule":
        return "remove_stale_rule"
    return "review_codeowners_rule"


def compute_severity(result: DriftResult, config: Config | None = None) -> Severity:
    """Map `result` and optional `config` to `low`, `medium`, `high`, or `critical`.

    `config.bus_factor.critical_threshold` sets the critical qualified-owner
    cutoff. Without `config` the cutoff is 1.
    """
    critical_threshold = config.bus_factor.critical_threshold if config is not None else 1
    if _has_critical_signal(result, critical_threshold):
        return "critical"
    delta = result.max_confidence_delta
    if delta >= 0.7:
        return "high"
    if delta >= 0.3:
        return "medium"
    return "low"


def apply_severity_hysteresis(
    raw: Severity,
    max_delta: float,
    config: Config,
    previous: tuple[Severity | None, Severity | None, int],
) -> tuple[Severity, Severity, int]:
    """Return `(reported, pending, streak)` after applying `config.drift` hysteresis.

    `previous` is `(reported, pending, streak)`. `hysteresis_runs <= 1` or a
    missing prior report returns `raw`. A delta of at least
    `2 * min_confidence_delta` accepts `raw` immediately.
    """
    runs = config.drift.hysteresis_runs
    reported, pending, streak = previous
    if runs <= 1 or reported is None:
        return raw, raw, 1
    if max_delta >= config.drift.min_confidence_delta * 2:
        return raw, raw, 1
    if raw == reported:
        return raw, raw, 1
    if raw == pending:
        streak += 1
    else:
        streak = 1
        pending = raw
    if streak >= runs:
        return raw, raw, streak
    return reported, pending, streak


def _has_critical_signal(result: DriftResult, critical_threshold: int) -> bool:
    for entries in (result.stale, result.missing, result.changed):
        for entry in entries:
            if (
                entry.qualified_owner_count is not None
                and entry.qualified_owner_count <= critical_threshold
            ):
                return True
            if entry.decay:
                return True
    return False


def drift_entry_payload(
    entry: DriftEntry,
    cap: int,
    config: Config | None = None,
) -> DriftEntryJson:
    """Return the machine-readable drift entry for `entry` at qualified-owner `cap`."""
    lone = DriftResult(stale=(), missing=(), changed=(entry,), drift_detected=True)
    payload: DriftEntryJson = {
        "path": entry.path,
        "declared": {"owners": list(entry.owners)},
        "observed": {"owners": list(entry.observed_owners), "teams": list(entry.observed_teams)},
        "drift": {
            "severity": compute_severity(lone, config),
            "type": entry.drift_type,
            "declared_observed_overlap": owner_overlap(entry.owners, entry.observed_owners),
        },
        "recommendation": {"action": _recommendation_action(entry.drift_type)},
        "confidence_delta": round(entry.confidence_delta, 4),
        "reason": entry.reason,
    }
    if entry.observed_teams:
        payload["recommendation"]["suggested_team"] = entry.observed_teams[0]
    if entry.qualified_owner_count is not None:
        counts = qualified_owner_count_fields(entry.qualified_owner_count, cap)
        payload["qualified_owner_count"] = counts["qualified_owner_count"]
        payload["bus_factor"] = counts["bus_factor"]
        payload["qualified_owner_count_cap"] = counts["qualified_owner_count_cap"]
    if entry.decay:
        payload["decay"] = True
    return payload

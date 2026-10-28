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
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from checkowners.analyze import run_git
from checkowners.busfactor import qualified_owner_count_fields
from checkowners.github import (
    RECONCILE_MARKER,
    TeamDirectory,
    external_evidence_payload,
)
from checkowners.models import (
    DEFAULT_SEVERITY_THRESHOLDS,
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
    TeamComparison,
    TeamComparisonJson,
    repository_label,
    stamp_json,
)
from checkowners.patterns import (
    CodeownersRule,
    match_path,
    matching_rules,
    pattern_matches,
    rules_with_source,
)

_DEFAULT_CODEOWNERS_PATH = ".github/CODEOWNERS"

_EVIDENCE_STATEMENT = "Inferred ownership is evidence, not accountability."
_NO_OWNER_DISAGREEMENT = "The generated file changed without an owner-set disagreement."
_DIRECTION_LABEL: dict[DriftKind, str] = {
    "missing_observed_expert": "missing observed expert",
    "stale_rule": "stale rule",
    "complete_ownership_replacement": "complete ownership replacement",
    "stale_declared_owner": "stale declared owner",
    "owner_mismatch": "owner mismatch",
    "organizational_mismatch": "organizational mismatch",
}

_IDENTITY_NOTE = (
    "inferred owners are commit emails but CODEOWNERS uses @handles; "
    "owner comparison skipped. Set GITHUB_TOKEN (github.resolve_handles) "
    "to compare owner sets."
)
_POLICY_NOTE = "declared ownership without observed expertise"
_TEAM_UNVERIFIABLE = "team_membership_requires_github_api"
_SEVERITY_ORDER: tuple[Severity, ...] = ("info", "low", "medium", "high", "critical")


def evidence_gaps(result: DriftResult) -> tuple[AnalysisGap, ...]:
    """Return completeness gaps for unverifiable comparisons in `result`."""
    gaps: list[AnalysisGap] = []
    if _IDENTITY_NOTE in result.notes:
        gaps.append(AnalysisGap("ambiguous_identity", IDENTITY_COMPARISON_REASON))
    if any(row.status == "unverifiable" for row in result.teams):
        gaps.append(AnalysisGap("team_membership", TEAM_MEMBERSHIP_REASON))
    return tuple(gaps)


def detect_drift(
    repo_root: Path,
    ownership: OwnershipMap,
    config: Config,
    *,
    codeowners_path: Path | None = None,
    teams: TeamDirectory | None = None,
) -> DriftResult:
    """Compare inferred ownership against current CODEOWNERS.

    `teams` is the org directory for this run. None leaves team rules unverifiable.
    """
    target = codeowners_path or (repo_root / _DEFAULT_CODEOWNERS_PATH)
    rules = _load_rules(target)
    tracked = _tracked_files(repo_root)
    return _compare(
        rules,
        ownership.paths,
        tracked,
        config.drift.mode,
        config.drift.min_confidence_delta,
        teams,
    )


def _load_rules(codeowners_path: Path) -> tuple[CodeownersRule, ...]:
    if not codeowners_path.exists():
        return ()
    return rules_with_source(codeowners_path.read_text(encoding="utf-8"))


def _tracked_files(repo_root: Path) -> tuple[str, ...]:
    """List tracked files; used to tell dead rules from merely quiet ones."""
    result = run_git(repo_root, ["git", "ls-files"], check=True, paths=True)
    return tuple(line for line in result.stdout.splitlines() if line)


def _compare(
    rules: tuple[CodeownersRule, ...],
    inferred: dict[str, PathOwnership],
    tracked: tuple[str, ...],
    mode: DriftMode,
    min_delta: float,
    teams: TeamDirectory | None,
) -> DriftResult:
    stale: list[DriftEntry] = []
    missing: list[DriftEntry] = []
    changed: list[DriftEntry] = []
    notes: list[str] = []
    reviews: tuple[DriftEntry, ...] = ()
    team_rows: tuple[TeamComparison, ...] = ()

    coverage = {path: match_path(rules, path.lstrip("/")) for path in inferred}

    if mode in ("commit", "both"):
        missing = _find_missing(inferred, coverage)
        changed, notes, reviews, team_rows = _find_changed(
            rules, inferred, coverage, min_delta, teams
        )

    if mode in ("repo", "both"):
        stale = _find_stale(rules, tracked)
        if mode == "repo":
            changed, notes, reviews, team_rows = _find_changed(
                rules, inferred, coverage, min_delta, teams
            )

    stale_sorted = _sort_by_delta(stale)
    missing_sorted = _sort_by_delta(missing)
    changed_sorted = _sort_by_delta(changed)
    return DriftResult(
        stale=stale_sorted,
        missing=missing_sorted,
        changed=changed_sorted,
        drift_detected=bool(stale_sorted or missing_sorted or changed_sorted),
        notes=tuple(notes),
        reviews=reviews,
        teams=team_rows,
    )


def _former_owner_handles(owners: tuple[OwnerEntry, ...]) -> tuple[str, ...]:
    return tuple(owner.handle for owner in owners if owner.contributor_status == "former")


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
                former_owners=_former_owner_handles(po.owners),
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
                line_number=rule.line_number,
            )
        )
    return entries


@dataclass(frozen=True)
class _RuleDivergence:
    worst_delta: float
    diverging: int
    qualified_owner_count: int | None
    decay: bool
    observed_owners: tuple[str, ...]
    former_owners: tuple[str, ...] = ()


def _find_changed(
    rules: tuple[CodeownersRule, ...],
    inferred: dict[str, PathOwnership],
    coverage: dict[str, CodeownersRule | None],
    min_delta: float,
    teams: TeamDirectory | None,
) -> tuple[list[DriftEntry], list[str], tuple[DriftEntry, ...], tuple[TeamComparison, ...]]:
    """Per-rule owner disagreement, aggregated over the files the rule covers."""
    notes: list[str] = []
    if identities_incomparable(rules, inferred):
        return [], [_IDENTITY_NOTE], (), ()

    per_rule: dict[CodeownersRule, list[tuple[str, PathOwnership]]] = {}
    for path, rule in coverage.items():
        if rule is not None and rule.owners:
            per_rule.setdefault(rule, []).append((path, inferred[path]))

    entries: list[DriftEntry] = []
    reviews: list[DriftEntry] = []
    team_rows: list[TeamComparison] = []
    for rule, covered in per_rule.items():
        if any("/" in owner for owner in rule.owners):
            compared = _compare_team_rule(rule, covered, teams)
            team_rows.extend(compared)
            partial = _worst_partial(compared)
            if partial is None:
                continue
            entry = _team_drift_entry(rule, covered, partial)
            if rule.source == "manual":
                reviews.append(entry)
            else:
                entries.append(entry)
            continue
        measured = _measure_divergence(rule, covered)
        if measured is None or measured.worst_delta < min_delta:
            continue
        if rule.source == "manual":
            reviews.append(_manual_review(rule, measured, _earlier_generated(rules, rule, covered)))
            continue
        entries.append(
            DriftEntry(
                path=rule.pattern,
                confidence_delta=measured.worst_delta,
                reason=(
                    f"owners diverge on {measured.diverging} of {len(covered)} covered path(s) "
                    f"(line {rule.line_number})"
                ),
                qualified_owner_count=measured.qualified_owner_count,
                decay=measured.decay,
                owners=rule.owners,
                observed_owners=measured.observed_owners,
                drift_type=_changed_drift_type(
                    rule.owners, measured.observed_owners, measured.decay
                ),
                line_number=rule.line_number,
                former_owners=measured.former_owners,
            )
        )
    sorted_reviews = _sort_by_delta(reviews)
    notes.extend(entry.reason for entry in sorted_reviews)
    return entries, notes, sorted_reviews, tuple(team_rows)


def _compare_team_rule(
    rule: CodeownersRule,
    covered: list[tuple[str, PathOwnership]],
    teams: TeamDirectory | None,
) -> tuple[TeamComparison, ...]:
    observed = _observed_handles(covered)
    inferred = {_login(handle) for handle in observed}
    rows: list[TeamComparison] = []
    for owner in rule.owners:
        if "/" not in owner:
            continue
        rows.append(_one_team(owner, rule, observed, inferred, teams))
    return tuple(rows)


def _one_team(
    owner: str,
    rule: CodeownersRule,
    observed: tuple[str, ...],
    inferred: set[str],
    teams: TeamDirectory | None,
) -> TeamComparison:
    team = owner if owner.startswith("@") else f"@{owner}"
    if teams is None:
        return TeamComparison(
            team=team,
            pattern=rule.pattern,
            line_number=rule.line_number,
            status="unverifiable",
            reason=_TEAM_UNVERIFIABLE,
        )
    membership = teams.membership_for(owner)
    snapshot = teams.fetched_at
    if membership is None or not membership.found or not membership.visible:
        return TeamComparison(
            team=team,
            pattern=rule.pattern,
            line_number=rule.line_number,
            status="invisible",
            team_snapshot=snapshot,
        )
    individuals = {_login(item) for item in rule.owners if "/" not in item}
    member_names = {login.casefold() for login in membership.members}
    declared = member_names | individuals
    coverage, inactive, outside = _team_sets(observed, inferred, membership.members, declared)
    base = TeamComparison(
        team=team,
        pattern=rule.pattern,
        line_number=rule.line_number,
        status="aligned",
        team_coverage=coverage,
        inactive_members=inactive,
        observed_outside_team=outside,
        team_snapshot=snapshot,
    )
    if membership.nested:
        return replace(base, status="nested")
    if not membership.members:
        return replace(base, status="empty")
    if outside:
        return replace(base, status="partial-drift")
    return base


def _team_sets(
    observed: tuple[str, ...],
    inferred: set[str],
    members: frozenset[str],
    declared: set[str],
) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    outside = tuple(handle for handle in observed if _login(handle) not in declared)
    inactive = tuple(
        _at(login)
        for login in sorted(members, key=str.casefold)
        if login.casefold() not in inferred
    )
    coverage = 1.0 if not inferred else round(len(inferred & declared) / len(inferred), 4)
    return coverage, inactive, outside


def _worst_partial(rows: tuple[TeamComparison, ...]) -> TeamComparison | None:
    partials = [row for row in rows if row.status == "partial-drift"]
    if not partials:
        return None
    partials.sort(
        key=lambda row: (
            row.team_coverage if row.team_coverage is not None else 1.0,
            row.team,
        )
    )
    return partials[0]


def _team_drift_entry(
    rule: CodeownersRule,
    covered: list[tuple[str, PathOwnership]],
    row: TeamComparison,
) -> DriftEntry:
    qualified, decay, observed, former = _path_signals(covered)
    outside = set(row.observed_outside_team)
    inside = tuple(handle for handle in observed if handle not in outside)
    declared = (*row.inactive_members, *inside)
    observed_entries = _merged_owners(covered)
    delta = _confidence_delta(declared, observed_entries)
    return DriftEntry(
        path=rule.pattern,
        confidence_delta=delta,
        reason=f"observed owners outside {row.team} on line {rule.line_number}",
        qualified_owner_count=qualified,
        decay=decay,
        owners=rule.owners,
        observed_owners=observed,
        drift_type="organizational_mismatch",
        line_number=rule.line_number,
        former_owners=former,
        team_status=row.status,
        team_coverage=row.team_coverage,
        inactive_members=row.inactive_members,
        observed_outside_team=row.observed_outside_team,
        team_snapshot=row.team_snapshot,
    )


def _path_signals(
    covered: list[tuple[str, PathOwnership]],
) -> tuple[int | None, bool, tuple[str, ...], tuple[str, ...]]:
    qualified: int | None = None
    decay = False
    observed: list[str] = []
    former: list[str] = []
    seen: set[str] = set()
    for _path, path_ownership in covered:
        count = path_ownership.qualified_owner_count
        qualified = count if qualified is None else min(qualified, count)
        decay = decay or bool(path_ownership.decay_warnings)
        for owner in path_ownership.owners:
            key = owner.handle.casefold()
            if key in seen:
                continue
            seen.add(key)
            observed.append(owner.handle)
            if owner.contributor_status == "former":
                former.append(owner.handle)
    return qualified, decay, tuple(observed), tuple(former)


def _merged_owners(covered: list[tuple[str, PathOwnership]]) -> tuple[OwnerEntry, ...]:
    best: dict[str, OwnerEntry] = {}
    for _path, path_ownership in covered:
        for owner in path_ownership.owners:
            key = owner.handle.casefold()
            current = best.get(key)
            if current is None or owner.confidence > current.confidence:
                best[key] = owner
    return tuple(best.values())


def _observed_handles(covered: list[tuple[str, PathOwnership]]) -> tuple[str, ...]:
    _qualified, _decay, observed, _former = _path_signals(covered)
    return observed


def _login(handle: str) -> str:
    token = handle[1:] if handle.startswith("@") else handle
    return token.casefold()


def _at(login: str) -> str:
    if login.startswith("@"):
        return login
    return f"@{login}"


def _measure_divergence(
    rule: CodeownersRule,
    covered: list[tuple[str, PathOwnership]],
) -> _RuleDivergence | None:
    current = _normalize_owners(rule.owners)
    worst_delta = 0.0
    diverging = 0
    qualified_owner_count: int | None = None
    decay = False
    observed_owners: tuple[str, ...] = ()
    former_owners: tuple[str, ...] = ()
    for _path, path_ownership in covered:
        inferred_handles = _normalize_owners(owner.handle for owner in path_ownership.owners)
        if current == inferred_handles:
            continue
        delta = _confidence_delta(rule.owners, path_ownership.owners)
        diverging += 1
        if delta > worst_delta:
            worst_delta = delta
            qualified_owner_count = path_ownership.qualified_owner_count
            decay = bool(path_ownership.decay_warnings)
            observed_owners = tuple(owner.handle for owner in path_ownership.owners)
            former_owners = _former_owner_handles(path_ownership.owners)
    if diverging == 0:
        return None
    return _RuleDivergence(
        worst_delta=worst_delta,
        diverging=diverging,
        qualified_owner_count=qualified_owner_count,
        decay=decay,
        observed_owners=observed_owners,
        former_owners=former_owners,
    )


def _earlier_generated(
    rules: tuple[CodeownersRule, ...],
    winner: CodeownersRule,
    covered: list[tuple[str, PathOwnership]],
) -> CodeownersRule | None:
    shadow: CodeownersRule | None = None
    for path, _path_ownership in covered:
        for match in matching_rules(rules, path):
            if match.source == "generated" and match.line_number < winner.line_number:
                shadow = match
    return shadow


def _manual_review(
    rule: CodeownersRule,
    measured: _RuleDivergence,
    shadow: CodeownersRule | None,
) -> DriftEntry:
    return DriftEntry(
        path=rule.pattern,
        confidence_delta=measured.worst_delta,
        reason=_manual_note(rule, shadow),
        qualified_owner_count=measured.qualified_owner_count,
        decay=measured.decay,
        owners=rule.owners,
        observed_owners=measured.observed_owners,
        drift_type=_changed_drift_type(rule.owners, measured.observed_owners, measured.decay),
        line_number=rule.line_number,
        former_owners=measured.former_owners,
    )


def _manual_note(rule: CodeownersRule, shadow: CodeownersRule | None) -> str:
    subject = _rule_line(rule.pattern, rule.owners)
    if shadow is None:
        return f"{_POLICY_NOTE}: {subject} (line {rule.line_number}). Review this manual rule."
    other = _rule_line(shadow.pattern, shadow.owners)
    return (
        f"{_POLICY_NOTE}: {subject} wins over {other} (line {rule.line_number}). "
        "Review this manual rule."
    )


def _rule_line(pattern: str, owners: tuple[str, ...]) -> str:
    if not owners:
        return pattern
    return f"{pattern} {' '.join(owners)}"


def identities_incomparable(
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
                "teams": [team_comparison_payload(row) for row in result.teams],
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
    entries = (*result.stale, *result.missing, *result.changed)
    if not entries:
        return "info"
    cuts = _thresholds(config)
    worst: Severity = "info"
    for entry in entries:
        tier = _tier(_entry_score(entry, config), cuts)
        if _SEVERITY_ORDER.index(tier) > _SEVERITY_ORDER.index(worst):
            worst = tier
    return worst


def _thresholds(config: Config | None) -> tuple[float, float, float, float]:
    if config is None:
        return DEFAULT_SEVERITY_THRESHOLDS
    return config.drift.severity_thresholds


def _entry_score(entry: DriftEntry, config: Config | None) -> float:
    overlap = entry.team_coverage if entry.team_coverage is not None else _plain_overlap(entry)
    delta = min(1.0, abs(entry.confidence_delta))
    activity = 0.15 if entry.decay else 0.0
    concentration = 0.05 if entry.qualified_owner_count == 1 else 0.0
    return (
        0.40 * (1.0 - overlap)
        + 0.25 * delta
        + 0.15 * _criticality(entry, config)
        + activity
        + concentration
    )


def _plain_overlap(entry: DriftEntry) -> float:
    if not entry.owners and not entry.observed_owners:
        return 1.0
    return owner_overlap(entry.owners, entry.observed_owners)


def _criticality(entry: DriftEntry, config: Config | None) -> float:
    if config is None or not config.criticality:
        return 0.0
    best = 0.0
    for pattern, weight in config.criticality:
        if pattern_matches(pattern, entry.path):
            best = max(best, weight)
    return min(1.0, best)


def _tier(score: float, cuts: tuple[float, float, float, float]) -> Severity:
    low, medium, high, critical = cuts
    if score < low:
        return "info"
    if score < medium:
        return "low"
    if score < high:
        return "medium"
    if score < critical:
        return "high"
    return "critical"


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
        payload["qualified_owner_count_cap"] = counts["qualified_owner_count_cap"]
    if entry.decay:
        payload["decay"] = True
    if entry.former_owners:
        payload["former_owners"] = list(entry.former_owners)
    if entry.team_status is not None:
        payload["team_status"] = entry.team_status
    if entry.team_coverage is not None:
        payload["team_coverage"] = entry.team_coverage
    if entry.inactive_members:
        payload["inactive_members"] = list(entry.inactive_members)
    if entry.observed_outside_team:
        payload["observed_outside_team"] = list(entry.observed_outside_team)
    if entry.team_snapshot:
        payload["team_snapshot"] = entry.team_snapshot
    return payload


def team_comparison_payload(row: TeamComparison) -> TeamComparisonJson:
    return {
        "team": row.team,
        "pattern": row.pattern,
        "line_number": row.line_number,
        "status": row.status,
        "reason": row.reason,
        "team_coverage": row.team_coverage,
        "inactive_members": list(row.inactive_members),
        "observed_outside_team": list(row.observed_outside_team),
        "team_snapshot": row.team_snapshot,
    }


def format_team_line(row: TeamComparison) -> str:
    parts = [row.team, row.status]
    if row.team_coverage is not None:
        parts.append(f"coverage {row.team_coverage:.2f}")
    if row.inactive_members:
        parts.append("inactive " + " ".join(row.inactive_members))
    if row.observed_outside_team:
        parts.append("outside " + " ".join(row.observed_outside_team))
    if row.reason:
        parts.append(row.reason)
    if row.team_snapshot:
        parts.append(row.team_snapshot)
    return " ".join(parts)


def reconciliation_body(result: DriftResult, ownership: OwnershipMap, config: Config) -> str:
    """Return Markdown for `result` using owner scores from `ownership`.

    `config` selects severity. The text states that inferred ownership is evidence,
    not accountability, and describes each stale, missing, and changed entry.
    """
    lines = [
        RECONCILE_MARKER,
        "",
        _EVIDENCE_STATEMENT,
        "",
        f"Severity: {compute_severity(result, config)}",
        "",
    ]
    entries = (*result.stale, *result.missing, *result.changed)
    if not entries:
        lines.append(_NO_OWNER_DISAGREEMENT)
        lines.append("")
    else:
        for entry in entries:
            lines.extend(_reconciliation_entry(entry, ownership, config))
    return "\n".join(lines).rstrip() + "\n"


def _reconciliation_entry(
    entry: DriftEntry,
    ownership: OwnershipMap,
    config: Config,
) -> list[str]:
    lone = DriftResult(stale=(), missing=(), changed=(entry,), drift_detected=True)
    lines = [
        f"## {entry.path}",
        "",
        f"Direction: {_DIRECTION_LABEL[entry.drift_type]}",
        f"Severity: {compute_severity(lone, config)}",
        f"Why: {entry.reason}",
    ]
    if entry.owners:
        lines.append(f"Declared: {' '.join(entry.owners)}")
    if entry.former_owners:
        lines.append(f"Former contributors: {' '.join(entry.former_owners)}")
    lines.extend(_observed_owner_lines(entry, ownership))
    lines.append("")
    return lines


def _observed_owner_lines(entry: DriftEntry, ownership: OwnershipMap) -> list[str]:
    lines: list[str] = []
    for handle in entry.observed_owners:
        owner = _best_owner(ownership, handle)
        if owner is None:
            lines.append(f"Observed: {handle}")
            continue
        last = owner.last_commit.isoformat() if owner.last_commit is not None else "none"
        lines.append(
            f"Observed: {handle} ownership score {owner.ownership_score:.2f}, "
            f"evidence quality {owner.evidence_quality:.2f}, "
            f"commits {owner.commits}, last commit {last}"
        )
        evidence = _signal_evidence(owner)
        if evidence:
            lines.append(f"Evidence: {evidence}")
    return lines


def _best_owner(ownership: OwnershipMap, handle: str) -> OwnerEntry | None:
    folded = handle.casefold()
    best: OwnerEntry | None = None
    for path_ownership in ownership.paths.values():
        for owner in path_ownership.owners:
            if owner.handle.casefold() != folded:
                continue
            if best is None or owner.ownership_score > best.ownership_score:
                best = owner
    return best


def _signal_evidence(owner: OwnerEntry) -> str:
    breakdown = owner.score_breakdown
    if breakdown is None:
        return ""
    parts: list[str] = []
    for name, signal in (
        ("recency", breakdown.recency),
        ("frequency", breakdown.frequency),
        ("blame", breakdown.blame),
        ("review", breakdown.review),
    ):
        if signal.available:
            parts.append(f"{name} {signal.score:.2f}")
    return ", ".join(parts)


def format_fix_plan(result: DriftResult, ownership: OwnershipMap) -> str:
    """Return suggested edits for `result` using scores from `ownership`."""
    blocks: list[str] = []
    for entry in result.changed:
        blocks.append("\n".join(_change_lines(entry, ownership)))
    for entry in result.reviews:
        blocks.append(entry.reason)
    if not blocks:
        return "No suggested changes.\n"
    return "\n\n".join(blocks) + "\n"


def _change_lines(entry: DriftEntry, ownership: OwnershipMap) -> list[str]:
    declared = {owner.casefold() for owner in entry.owners}
    observed = {owner.casefold() for owner in entry.observed_owners}
    added = tuple(owner for owner in entry.observed_owners if owner.casefold() not in declared)
    if entry.owners and added and all(owner.casefold() in observed for owner in entry.owners):
        retained = " ".join(entry.owners)
        lines: list[str] = []
        for handle in added:
            owner = _best_owner(ownership, handle)
            score = 0.0 if owner is None else owner.ownership_score
            lines.append(
                f"Consider adding {handle} while retaining {retained}. "
                f"{handle} has {score:.2f} observed expertise "
                "and is not currently represented."
            )
        return lines
    return [
        f"-{_rule_line(entry.path, entry.owners)}",
        f"+{_rule_line(entry.path, entry.observed_owners)}",
    ]

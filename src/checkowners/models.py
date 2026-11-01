"""Data models for checkOwners."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeGuard, get_args

Severity = Literal["info", "low", "medium", "high", "critical"]
DriftMode = Literal["commit", "repo", "both"]
BlockOrder = Literal["manual-first", "generated-first"]
QualificationStrategy = Literal["adaptive", "threshold"]
RecencyStrategy = Literal["fixed", "adaptive"]
LookbackDays = int | Literal["adaptive"]
FreshnessStatus = Literal["inactive", "superseded", "stable", "departed"]
FindingRule = Literal[
    "missing",
    "stale",
    "changed",
    "single-expert",
    "knowledge-vacuum",
    "phantom-ownership",
    "shadow-maintainer",
    "ownership-review-divergence",
    "invalid-owner",
    "owner-permission",
    "invisible-team",
    "empty-team",
    "file-size",
    "unowned-file",
    "observed-unowned",
    "duplicate-rule",
    "shadowed-rule",
    "broad-rule",
    "self-protection",
    "ruleset",
    "decayed-owner",
    "orphaned-owner",
]
PolicyLevel = Literal["error", "warning"]
PolicyCheck = Literal[
    "drift",
    "single_expert",
    "incomplete_analysis",
    "unowned",
    "invalid_owner",
]
IdentityMode = Literal["handle", "email", "hashed"]
ContributorStatus = Literal["active", "inactive", "unknown", "former", "bot", "external"]
_CONTRIBUTOR_STATUSES = frozenset(get_args(ContributorStatus))
ConfiguredMergeStrategy = Literal["auto", "squash", "merge", "rebase"]
ReportedMergeStrategy = Literal["squash", "merge", "rebase"]

DEFAULT_TRUCK_FACTOR_THRESHOLDS: tuple[float, float, float] = (0.5, 0.75, 0.9)
_MAJOR_CONTRIBUTOR_SHARE = 0.05


DriftKind = Literal[
    "missing_observed_expert",
    "stale_rule",
    "complete_ownership_replacement",
    "stale_declared_owner",
    "owner_mismatch",
    "organizational_mismatch",
]
TeamStatus = Literal[
    "aligned",
    "partial-drift",
    "unverifiable",
    "invisible",
    "empty",
    "nested",
]
DEFAULT_SEVERITY_THRESHOLDS: tuple[float, float, float, float] = (0.15, 0.35, 0.55, 0.75)
RecommendationAction = Literal["review_codeowners_rule", "remove_stale_rule"]
OwnershipSignal = Literal["recency", "frequency", "blame", "review", "change_survival"]
EvidenceType = Literal["commit", "blame", "review"]
OWNERSHIP_SIGNALS: tuple[OwnershipSignal, ...] = (
    "recency",
    "frequency",
    "blame",
    "review",
    "change_survival",
)
_SIGNAL_NAMES = OWNERSHIP_SIGNALS


@dataclass(frozen=True)
class Finding:
    rule: FindingRule
    path: str
    owners: tuple[str, ...] = ()

    def identity(self) -> tuple[str, str, tuple[str, ...]]:
        if self.rule == "single-expert":
            return (self.rule, self.path, ())
        return (self.rule, self.path, tuple(sorted(owner.casefold() for owner in self.owners)))


@dataclass(frozen=True)
class RatchetCounts:
    baselined: int = 0
    suppressed: int = 0
    stale_baseline: int = 0


@dataclass(frozen=True)
class RunOptions:
    """The global flags the CLI callback collects once for the whole run."""

    as_of: str | None = None
    deterministic: bool = False
    exit_zero: bool = False
    fail_on_incomplete: bool = False
    allow_stale: bool = False
    max_age: int | None = None
    no_cache: bool = False
    verbose: bool = False
    no_incremental: bool = False
    offline: bool = False
    redact_emails: bool = False
    diff_only: str | None = None


def is_contributor_status(value: str) -> TypeGuard[ContributorStatus]:
    """Return whether `value` is one of the contributor status names."""
    return value in _CONTRIBUTOR_STATUSES


GapCode = Literal[
    "shallow_history",
    "insufficient_history",
    "absent_token",
    "review_history",
    "api_rate_limit",
    "api_budget",
    "runtime_budget",
    "team_membership",
    "ambiguous_identity",
    "excluded_files",
    "unresolved_renames",
    "missing_mailmap",
    "missing_ignore_revs",
]

GAP_CATALOG: tuple[GapCode, ...] = (
    "shallow_history",
    "insufficient_history",
    "absent_token",
    "review_history",
    "api_rate_limit",
    "api_budget",
    "runtime_budget",
    "team_membership",
    "ambiguous_identity",
    "excluded_files",
    "unresolved_renames",
    "missing_mailmap",
    "missing_ignore_revs",
)

SHALLOW_HISTORY_REASON = "Shallow history: the clone does not contain full git history."
INSUFFICIENT_HISTORY_REASON = "Insufficient history: no commits in the lookback window."
ABSENT_TOKEN_REASON = "Absent token: GitHub API evidence was not collected."  # noqa: S105
REVIEW_HISTORY_REASON = "Review history unavailable."
API_RATE_LIMIT_REASON = "Review evidence omitted: GitHub API budget insufficient."
SEARCH_RATE_LIMIT_REASON = "Identity lookup omitted: GitHub API budget insufficient."
API_BUDGET_REASON = "Analysis incomplete: API request budget exhausted."
RUNTIME_BUDGET_REASON = "Analysis incomplete: runtime budget exhausted."


def review_window_truncated_reason(days: int, configured: int | None) -> str:
    """Return the review-window gap reason for `days` covered of `configured`."""
    if configured is None:
        return f"Review window truncated: sample covers {days} days."
    return f"Review window truncated: sample covers {days} of {configured} days."


TEAM_MEMBERSHIP_REASON = "Team membership unavailable: team rules were not compared."
UNRESOLVED_RENAMES_REASON = "Unresolved renames: path history is not followed across renames."
MISSING_MAILMAP_REASON = "Missing .mailmap."
MISSING_IGNORE_REVS_REASON = "Missing .git-blame-ignore-revs."
IDENTITY_COMPARISON_REASON = (
    "Ambiguous identities: inferred owners are commit emails and were not resolved "
    "to GitHub accounts."
)


def excluded_files_reason(gitattributes: int, static: int) -> str:
    """Return the excluded-files gap reason for `gitattributes` and `static` counts."""
    return f"Excluded files: {gitattributes} gitattributes, {static} static."


def ambiguous_identity_reason(count: int) -> str:
    """Return the unresolved-email gap reason for `count` emails."""
    return f"Ambiguous identities: {count} emails were not resolved to a single GitHub account."


@dataclass(frozen=True)
class AnalysisGap:
    code: GapCode
    reason: str


def history_evidence_gaps(
    *,
    shallow: bool,
    insufficient: bool,
    renamed: bool,
    mailmap_missing: bool,
    ignore_revs_missing: bool,
    excluded_gitattributes: int,
    excluded_static: int,
    review_missing: bool,
    runtime_truncated: bool,
) -> tuple[AnalysisGap, ...]:
    """Return git-side gaps for the flags that are true."""
    gaps: list[AnalysisGap] = []
    if shallow:
        gaps.append(AnalysisGap("shallow_history", SHALLOW_HISTORY_REASON))
    if insufficient:
        gaps.append(AnalysisGap("insufficient_history", INSUFFICIENT_HISTORY_REASON))
    if renamed:
        gaps.append(AnalysisGap("unresolved_renames", UNRESOLVED_RENAMES_REASON))
    if mailmap_missing:
        gaps.append(AnalysisGap("missing_mailmap", MISSING_MAILMAP_REASON))
    if ignore_revs_missing:
        gaps.append(AnalysisGap("missing_ignore_revs", MISSING_IGNORE_REVS_REASON))
    if excluded_gitattributes or excluded_static:
        gaps.append(
            AnalysisGap(
                "excluded_files",
                excluded_files_reason(excluded_gitattributes, excluded_static),
            )
        )
    if review_missing:
        gaps.append(AnalysisGap("review_history", REVIEW_HISTORY_REASON))
    if runtime_truncated:
        gaps.append(AnalysisGap("runtime_budget", RUNTIME_BUDGET_REASON))
    return tuple(gaps)


def completeness_score(gaps: tuple[AnalysisGap, ...]) -> float:
    """Return the fraction of the evidence catalog that is not listed in `gaps`."""
    missing = len({gap.code for gap in gaps})
    return round((len(GAP_CATALOG) - missing) / len(GAP_CATALOG), 4)


def merge_gaps(*groups: tuple[AnalysisGap, ...]) -> tuple[AnalysisGap, ...]:
    """Return `groups` deduped by code, in catalog order. The first reason wins."""
    chosen: dict[GapCode, AnalysisGap] = {}
    for group in groups:
        for gap in group:
            chosen.setdefault(gap.code, gap)
    return tuple(chosen[code] for code in GAP_CATALOG if code in chosen)


def completeness_label(score: float) -> str:
    """Return the human summary line for a completeness `score` in ``[0, 1]``."""
    return f"analysis completeness: {score * 100:.0f}%"


@dataclass(frozen=True)
class AnalysisCompleteness:
    ignore_revs_applied: bool = False
    ignore_revs_file: str = ""
    mailmap_applied: bool = False
    mailmap_file: str = ""
    excluded_gitattributes: int = 0
    excluded_static: int = 0
    merge_strategy: ReportedMergeStrategy = "rebase"
    co_author_count: int = 0
    score: float | None = None
    gaps: tuple[AnalysisGap, ...] = ()


def with_gaps(
    base: AnalysisCompleteness,
    extra: tuple[AnalysisGap, ...],
) -> AnalysisCompleteness:
    """Return `base` with `extra` merged in and `score` recomputed."""
    merged = merge_gaps(base.gaps, extra)
    return replace(base, gaps=merged, score=completeness_score(merged))


@dataclass(frozen=True)
class SignalScore:
    available: bool
    score: float = 0.0

    def as_payload(self) -> dict[str, bool | float]:
        if not self.available:
            return {"available": False}
        return {"score": round(self.score, 4), "available": True}


@dataclass(frozen=True)
class ConfidenceScore:
    """Per-signal breakdown of a single owner's ownership score for a path."""

    total: float
    recency: SignalScore
    frequency: SignalScore
    blame: SignalScore
    review: SignalScore
    change_survival: SignalScore = field(default=SignalScore(available=False))

    def signals_payload(self) -> dict[str, dict[str, bool | float]]:
        return {
            "recency": self.recency.as_payload(),
            "frequency": self.frequency.as_payload(),
            "blame": self.blame.as_payload(),
            "review": self.review.as_payload(),
            "change_survival": self.change_survival.as_payload(),
        }


@dataclass(frozen=True)
class OwnershipFreshness:
    active_expertise: float
    historical_expertise: float
    maintenance_recency: float
    status: FreshnessStatus
    half_life_days: float


@dataclass(frozen=True)
class OwnerEntry:
    """A single owner of a path with score, evidence quality, and provenance."""

    handle: str
    ownership_score: float
    last_commit: datetime | None
    commits: int
    evidence_quality: float = 1.0
    score_breakdown: ConfidenceScore | None = None
    freshness: OwnershipFreshness | None = None
    contributor_status: ContributorStatus = "unknown"

    @property
    def confidence(self) -> float:
        return self.ownership_score

    @property
    def score_label(self) -> str:
        return f"{self.ownership_score:.2f}/{self.evidence_quality:.2f}"


@dataclass(frozen=True)
class DecayWarning:
    handle: str
    path: str
    last_commit: datetime
    days_since_last_commit: int
    historical_confidence: float
    status: FreshnessStatus = "inactive"


@dataclass(frozen=True)
class OwnershipEvidence:
    subject: str
    path: str
    type: EvidenceType
    timestamp: datetime | None = None
    weight: float | None = None
    share: float | None = None
    commits: int | None = None
    credit: float | None = None
    cadence_days: float | None = None
    later_commits: int | None = None
    survival: float | None = None


@dataclass(frozen=True)
class PathOwnership:
    """All information known about a single path's ownership."""

    owners: tuple[OwnerEntry, ...]
    qualified_owner_count: int
    decay_warnings: tuple[DecayWarning, ...] = ()
    candidates: tuple[OwnerEntry, ...] = ()
    scored_owners: tuple[OwnerEntry, ...] = ()
    change_events: int | None = None
    cadence_days: float | None = None
    cochange_degree: float | None = None
    evidence: tuple[OwnershipEvidence, ...] = ()


@dataclass(frozen=True)
class OwnershipMap:
    """Confidence-scored ownership across all paths."""

    paths: dict[str, PathOwnership]
    last_analyzed: datetime
    analysis_ref: str = ""
    analysis_completeness: AnalysisCompleteness = field(default_factory=AnalysisCompleteness)
    reviews_sampled: int = 0
    reviews_window_days: int = 0


@dataclass(frozen=True)
class ExpertiseRank:
    """Per-path ranking entry for the expertise command."""

    handle: str
    confidence: float
    commits: int
    last_commit: datetime | None


@dataclass(frozen=True)
class TeamCluster:
    name: str
    members: tuple[str, ...]
    primary_paths: tuple[str, ...]
    declared: bool = False
    affiliated_members: tuple[str, ...] = ()
    parent: str = ""


@dataclass(frozen=True)
class BusFactor:
    path: str
    qualified_owner_count: int
    contributors_above_threshold: tuple[str, ...]
    recommended_backups: tuple[str, ...]


@dataclass(frozen=True)
class DriftEntry:
    path: str
    confidence_delta: float
    reason: str = ""
    qualified_owner_count: int | None = None
    decay: bool = False
    owners: tuple[str, ...] = ()
    observed_owners: tuple[str, ...] = ()
    observed_teams: tuple[str, ...] = ()
    drift_type: DriftKind = "owner_mismatch"
    line_number: int | None = None
    former_owners: tuple[str, ...] = ()
    team_status: TeamStatus | None = None
    team_coverage: float | None = None
    inactive_members: tuple[str, ...] = ()
    observed_outside_team: tuple[str, ...] = ()
    team_snapshot: str = ""


@dataclass(frozen=True)
class TeamComparison:
    team: str
    pattern: str
    line_number: int
    status: TeamStatus
    reason: str = ""
    team_coverage: float | None = None
    inactive_members: tuple[str, ...] = ()
    observed_outside_team: tuple[str, ...] = ()
    team_snapshot: str = ""


@dataclass(frozen=True)
class DriftResult:
    stale: tuple[DriftEntry, ...]
    missing: tuple[DriftEntry, ...]
    changed: tuple[DriftEntry, ...]
    drift_detected: bool
    #: Human-readable caveats about the comparison (e.g. inferred owners are
    #: raw emails while CODEOWNERS uses @handles, making owner-set comparison
    #: impossible without handle resolution).
    notes: tuple[str, ...] = ()
    reviews: tuple[DriftEntry, ...] = ()
    teams: tuple[TeamComparison, ...] = ()

    @property
    def max_confidence_delta(self) -> float:
        entries = (*self.stale, *self.missing, *self.changed)
        if not entries:
            return 0.0
        return max(abs(e.confidence_delta) for e in entries)


@dataclass(frozen=True)
class KnowledgeConcentration:
    top_owner_share: float
    effective_owners: float
    truck_factor_50: int
    truck_factor_75: int
    truck_factor_90: int
    shannon_entropy: float
    hhi: float
    minor_contributor_share: float
    major_contributor_count: int
    truck_factor_thresholds: tuple[float, float, float] = DEFAULT_TRUCK_FACTOR_THRESHOLDS


@dataclass(frozen=True)
class OwnerDistribution:
    minimum: float
    p10: float
    median: float
    p90: float
    critical_path_risk: float
    knowledge_at_risk: float
    criticality_incomplete: bool
    former_knowledge_share: float = 0.0


def repository_label(repo_root: Path) -> str:
    """Return `repo_root`'s `owner/name` slug, or the directory name."""
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if slug:
        return slug
    return repo_root.name


def signal_pairs(breakdown: ConfidenceScore) -> tuple[tuple[str, SignalScore], ...]:
    return (
        ("recency", breakdown.recency),
        ("frequency", breakdown.frequency),
        ("blame", breakdown.blame),
        ("review", breakdown.review),
        ("change_survival", breakdown.change_survival),
    )


def signal_completeness(owners: tuple[OwnerEntry, ...]) -> tuple[float, list[str]]:
    """Return `(fraction available, signal names)` across `owners` and the ownership signals."""
    if not owners:
        return 0.0, []
    available: list[str] = []
    seen: set[str] = set()
    hits = 0
    total = len(owners) * len(_SIGNAL_NAMES)
    for owner in owners:
        breakdown = owner.score_breakdown
        if breakdown is None:
            continue
        for name, signal in signal_pairs(breakdown):
            if not signal.available:
                continue
            hits += 1
            if name not in seen:
                seen.add(name)
                available.append(name)
    return round(hits / total, 4), available


def ownership_signal_completeness(ownership: OwnershipMap) -> tuple[float, list[str]]:
    """Return `signal_completeness` for every owner in `ownership`."""
    owners = tuple(
        owner for path_ownership in ownership.paths.values() for owner in path_ownership.owners
    )
    return signal_completeness(owners)


def run_completeness(ownership: OwnershipMap) -> float:
    """Return the run score, or the signal fraction when no score was stored."""
    score = ownership.analysis_completeness.score
    if score is not None:
        return score
    return ownership_signal_completeness(ownership)[0]


def _coverage_count(shares: tuple[float, ...], threshold: float) -> int:
    cumulative = 0.0
    for index, share in enumerate(shares, start=1):
        cumulative += share
        if cumulative >= threshold:
            return index
    return len(shares)


def knowledge_concentration(
    scores: tuple[float, ...],
    thresholds: tuple[float, float, float] = DEFAULT_TRUCK_FACTOR_THRESHOLDS,
) -> KnowledgeConcentration:
    """Return concentration of positive `scores` at the three `thresholds`."""
    low, mid, high = thresholds
    positive = tuple(score for score in scores if score > 0)
    total = sum(positive)
    if total <= 0:
        return KnowledgeConcentration(
            top_owner_share=0.0,
            effective_owners=0.0,
            truck_factor_50=0,
            truck_factor_75=0,
            truck_factor_90=0,
            shannon_entropy=0.0,
            hhi=0.0,
            minor_contributor_share=0.0,
            major_contributor_count=0,
            truck_factor_thresholds=thresholds,
        )
    shares = tuple(sorted((score / total for score in positive), reverse=True))
    hhi = sum(share * share for share in shares)
    entropy = -sum(share * math.log(share) for share in shares)
    minor = sum(share for share in shares if share < _MAJOR_CONTRIBUTOR_SHARE)
    major = sum(1 for share in shares if share >= _MAJOR_CONTRIBUTOR_SHARE)
    return KnowledgeConcentration(
        top_owner_share=round(shares[0], 4),
        effective_owners=round(1.0 / hhi, 4),
        truck_factor_50=_coverage_count(shares, low),
        truck_factor_75=_coverage_count(shares, mid),
        truck_factor_90=_coverage_count(shares, high),
        shannon_entropy=round(entropy, 4),
        hhi=round(hhi, 4),
        minor_contributor_share=round(minor, 4),
        major_contributor_count=major,
        truck_factor_thresholds=thresholds,
    )

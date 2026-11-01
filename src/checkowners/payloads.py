"""The JSON shapes every command emits, and the builders that fill them.

Each TypedDict here is one object in the published command schema
(``schemas/commands.schema.json``). Keeping them together means the schema has
one place in the code to match.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict

from checkowners import __version__
from checkowners.models import (
    DEFAULT_TRUCK_FACTOR_THRESHOLDS,
    AnalysisCompleteness,
    AnalysisGap,
    ContributorStatus,
    DriftKind,
    FreshnessStatus,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    RecommendationAction,
    ReportedMergeStrategy,
    Severity,
    TeamStatus,
    knowledge_concentration,
    ownership_signal_completeness,
    signal_completeness,
    signal_pairs,
)


class SignalScores(TypedDict, total=False):
    recency: float
    frequency: float
    blame: float
    review: float
    change_survival: float


class FreshnessJson(TypedDict):
    active_expertise: float
    historical_expertise: float
    maintenance_recency: float
    status: FreshnessStatus
    half_life_days: float


class OwnerJson(TypedDict):
    identity: str
    ownership_score: float
    evidence_quality: float
    commits: int
    last_commit: str | None
    signals: NotRequired[SignalScores]
    freshness: NotRequired[FreshnessJson]
    contributor_status: ContributorStatus


class RiskJson(TypedDict):
    top_owner_share: float
    effective_owners: float
    truck_factor_50: int
    truck_factor_75: int
    truck_factor_90: int
    shannon_entropy: float
    hhi: float
    minor_contributor_share: float
    major_contributor_count: int
    truck_factor_thresholds: list[float]


class OwnerDistributionJson(TypedDict):
    minimum: float
    p10: float
    median: float
    p90: float
    critical_path_risk: float
    knowledge_at_risk: float
    criticality_incomplete: bool
    former_knowledge_share: float


class PathAnalysisJson(TypedDict):
    completeness: float
    signals_available: list[str]


class AnalysisFlagsJson(TypedDict):
    ignore_revs_applied: bool
    ignore_revs_file: str
    mailmap_applied: bool
    mailmap_file: str
    excluded_gitattributes: int
    excluded_static: int
    merge_strategy: ReportedMergeStrategy
    co_author_count: int


class AnalyzeAnalysisJson(PathAnalysisJson, AnalysisFlagsJson):
    pass


class DecayWarningJson(TypedDict):
    handle: str
    days_since_last_commit: int
    last_commit: str
    historical_confidence: float
    status: NotRequired[FreshnessStatus]
    contributor_status: NotRequired[ContributorStatus]


class PathOwnershipJson(TypedDict):
    owners: list[OwnerJson]
    analysis: PathAnalysisJson
    risk: RiskJson
    qualified_owner_count: int
    qualified_owner_count_cap: int
    decay_warnings: list[DecayWarningJson]


class DeclaredOwnersJson(TypedDict):
    owners: list[str]


class ObservedOwnersJson(TypedDict):
    owners: list[str]
    teams: list[str]


class DriftBodyJson(TypedDict):
    severity: Severity
    type: DriftKind
    declared_observed_overlap: float


class RecommendationJson(TypedDict):
    action: RecommendationAction
    suggested_team: NotRequired[str]


class DriftEntryJson(TypedDict):
    path: str
    declared: DeclaredOwnersJson
    observed: ObservedOwnersJson
    drift: DriftBodyJson
    recommendation: RecommendationJson
    confidence_delta: float
    reason: str
    qualified_owner_count: NotRequired[int]
    qualified_owner_count_cap: NotRequired[int]
    decay: NotRequired[bool]
    former_owners: NotRequired[list[str]]
    team_status: NotRequired[TeamStatus]
    team_coverage: NotRequired[float]
    inactive_members: NotRequired[list[str]]
    observed_outside_team: NotRequired[list[str]]
    team_snapshot: NotRequired[str]


class TeamComparisonJson(TypedDict):
    team: str
    pattern: str
    line_number: int
    status: TeamStatus
    reason: str
    team_coverage: float | None
    inactive_members: list[str]
    observed_outside_team: list[str]
    team_snapshot: str


class AnalysisGapJson(TypedDict):
    code: str
    reason: str


class ProvenanceEnvelope(TypedDict):
    checkowners_version: str
    repository: str
    head_sha: str
    generated_at: str
    analysis_completeness: float | None
    analysis_gaps: NotRequired[list[AnalysisGapJson]]


def provenance_envelope(
    *,
    repository: str,
    head_sha: str,
    generated_at: str,
    analysis_completeness: float | None,
) -> ProvenanceEnvelope:
    """Return the shared machine-readable envelope for `repository` and `head_sha`."""
    return {
        "checkowners_version": __version__,
        "repository": repository,
        "head_sha": head_sha,
        "generated_at": generated_at,
        "analysis_completeness": analysis_completeness,
    }


def analysis_gaps_json(gaps: tuple[AnalysisGap, ...]) -> list[AnalysisGapJson]:
    """Return the machine-readable gap list for `gaps`."""
    return [{"code": gap.code, "reason": gap.reason} for gap in gaps]


def stamp_json(
    data: dict[str, object],
    *,
    repository: str,
    head_sha: str,
    generated_at: str,
    analysis_completeness: float | None,
    evidence: dict[str, str] | None = None,
    analysis_gaps: list[AnalysisGapJson] | None = None,
) -> dict[str, object]:
    """Return `data` merged with the provenance envelope and optional `evidence`."""
    stamped: dict[str, object] = {
        **data,
        **provenance_envelope(
            repository=repository,
            head_sha=head_sha,
            generated_at=generated_at,
            analysis_completeness=analysis_completeness,
        ),
    }
    if analysis_gaps is not None:
        stamped["analysis_gaps"] = analysis_gaps
    if evidence:
        stamped.update(evidence)
    return stamped


def flat_signal_scores(owner: OwnerEntry) -> SignalScores:
    """Return available signal scores for `owner`, omitting unavailable signals."""
    breakdown = owner.score_breakdown
    if breakdown is None:
        return {}
    scores: SignalScores = {}
    for name, signal in signal_pairs(breakdown):
        if not signal.available:
            continue
        if name == "recency":
            scores["recency"] = round(signal.score, 4)
        elif name == "frequency":
            scores["frequency"] = round(signal.score, 4)
        elif name == "blame":
            scores["blame"] = round(signal.score, 4)
        elif name == "review":
            scores["review"] = round(signal.score, 4)
        else:
            scores["change_survival"] = round(signal.score, 4)
    return scores


def envelope_completeness(
    ownership: OwnershipMap | None,
) -> tuple[float | None, list[AnalysisGapJson] | None]:
    """Return `(score, gaps)` for the envelope. Gaps are omitted when `score` was never set."""
    if ownership is None:
        return None, None
    if ownership.analysis_completeness.score is None:
        return ownership_signal_completeness(ownership)[0], None
    return (
        ownership.analysis_completeness.score,
        analysis_gaps_json(ownership.analysis_completeness.gaps),
    )


def path_analysis_json(owners: tuple[OwnerEntry, ...]) -> PathAnalysisJson:
    """Return per-path completeness and available signal names for `owners`."""
    completeness, available = signal_completeness(owners)
    return {"completeness": completeness, "signals_available": available}


def analysis_flags_json(completeness: AnalysisCompleteness) -> AnalysisFlagsJson:
    """Return ignore-revs, mailmap, exclusion, and git-history fields from `completeness`."""
    return {
        "ignore_revs_applied": completeness.ignore_revs_applied,
        "ignore_revs_file": completeness.ignore_revs_file,
        "mailmap_applied": completeness.mailmap_applied,
        "mailmap_file": completeness.mailmap_file,
        "excluded_gitattributes": completeness.excluded_gitattributes,
        "excluded_static": completeness.excluded_static,
        "merge_strategy": completeness.merge_strategy,
        "co_author_count": completeness.co_author_count,
    }


def owner_json(owner: OwnerEntry) -> OwnerJson:
    """Return the machine-readable owner object for `owner`."""
    payload: OwnerJson = {
        "identity": owner.handle,
        "ownership_score": round(owner.ownership_score, 4),
        "evidence_quality": round(owner.evidence_quality, 4),
        "commits": owner.commits,
        "last_commit": owner.last_commit.isoformat() if owner.last_commit else None,
        "contributor_status": owner.contributor_status,
    }
    signals = flat_signal_scores(owner)
    if signals:
        payload["signals"] = signals
    if owner.freshness is not None:
        payload["freshness"] = _freshness_json(owner.freshness)
    return payload


def _freshness_json(freshness: OwnershipFreshness) -> FreshnessJson:
    """Return the machine-readable freshness object for `freshness`."""
    return {
        "active_expertise": round(freshness.active_expertise, 4),
        "historical_expertise": round(freshness.historical_expertise, 4),
        "maintenance_recency": round(freshness.maintenance_recency, 4),
        "status": freshness.status,
        "half_life_days": round(freshness.half_life_days, 4),
    }


def risk_from_scores(
    scores: tuple[float, ...],
    thresholds: tuple[float, float, float] = DEFAULT_TRUCK_FACTOR_THRESHOLDS,
) -> RiskJson:
    """Return the JSON concentration of `scores` at `thresholds`."""
    concentration = knowledge_concentration(scores, thresholds)
    return {
        "top_owner_share": concentration.top_owner_share,
        "effective_owners": concentration.effective_owners,
        "truck_factor_50": concentration.truck_factor_50,
        "truck_factor_75": concentration.truck_factor_75,
        "truck_factor_90": concentration.truck_factor_90,
        "shannon_entropy": concentration.shannon_entropy,
        "hhi": concentration.hhi,
        "minor_contributor_share": concentration.minor_contributor_share,
        "major_contributor_count": concentration.major_contributor_count,
        "truck_factor_thresholds": list(concentration.truck_factor_thresholds),
    }

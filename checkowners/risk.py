"""Composite knowledge risk from concentration, churn, criticality, and freshness."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

from checkowners.busfactor import compute_qualified_owners
from checkowners.drift import identities_incomparable
from checkowners.expertise import path_matches_glob
from checkowners.models import (
    Config,
    DriftResult,
    Finding,
    FreshnessStatus,
    KnowledgeConcentration,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    knowledge_concentration,
)
from checkowners.patterns import CodeownersRule, match_path

RiskTier = Literal["critical", "high", "medium", "low"]
FrequencyLabel = Literal["very high", "high", "medium", "low", "very low"]
PathologyKind = Literal[
    "knowledge-vacuum",
    "phantom-ownership",
    "shadow-maintainer",
    "ownership-review-divergence",
]

_INFERRED_CRITICALITY = 0.8
_SINGLE_CHANGE_FREQUENCY = 0.05
_DECAY_FLOOR = 0.05
_REASON_FLOOR = 0.6
_LOW_CRITICALITY = 0.2
_TIER_CRITICAL = 75
_TIER_HIGH = 50
_TIER_MEDIUM = 25
_HIGH_DRIFT_DELTA = 0.7
_STALE: frozenset[FreshnessStatus] = frozenset({"inactive", "superseded", "departed"})
_MANIFEST_NAMES = frozenset({"Dockerfile", "Procfile", "Chart.yaml", "fly.toml", "serverless.yml"})


@dataclass(frozen=True)
class RiskFactors:
    ownership_concentration: float
    change_frequency: float | None
    dependency_criticality: float | None
    code_criticality: float | None
    expertise_decay: float | None

    def present(self) -> tuple[float, ...]:
        values = (
            self.ownership_concentration,
            self.change_frequency,
            self.dependency_criticality,
            self.code_criticality,
            self.expertise_decay,
        )
        return tuple(value for value in values if value is not None)


@dataclass(frozen=True)
class RiskEntry:
    path: str
    risk: int
    tier: RiskTier
    reason: str
    factors: RiskFactors
    truck_factor_50: int
    effective_owners: float
    dominant_expert: str | None
    dominant_share: float | None
    change_frequency_label: FrequencyLabel | None


@dataclass(frozen=True)
class Pathology:
    kind: PathologyKind
    path: str
    detail: str
    owners: tuple[str, ...] = ()
    declared_owners: tuple[str, ...] = ()
    writer: str | None = None
    reviewer: str | None = None
    declared: str | None = None

    def finding(self) -> Finding:
        return Finding(rule=self.kind, path=self.path, owners=self.owners)


@dataclass(frozen=True)
class RiskSummary:
    observed_ownership_coverage: float
    knowledge_at_risk: float
    critical_components: int
    ownership_vacuums: int
    high_codeowners_drift: int


@dataclass(frozen=True)
class RiskReport:
    criticality_unavailable: bool
    entries: tuple[RiskEntry, ...]
    pathologies: tuple[Pathology, ...]
    summary: RiskSummary


def assess_risk(
    ownership: OwnershipMap,
    config: Config,
    rules: tuple[CodeownersRule, ...],
    drift: DriftResult,
) -> RiskReport:
    """Return composite risk for `ownership` under `config`, `rules`, and `drift`."""
    thresholds = config.risk.truck_factor_thresholds
    comparable = not identities_incomparable(rules, ownership.paths)
    entries: list[RiskEntry] = []
    pathologies: list[Pathology] = []
    for path, po in ownership.paths.items():
        observed = _observed(po)
        concentration = knowledge_concentration(
            tuple(owner.ownership_score for owner in observed),
            thresholds,
        )
        factors = _factors(path, po, observed, concentration, config)
        score = _geometric_mean(factors.present())
        dominant = _dominant(observed)
        entries.append(
            RiskEntry(
                path=path,
                risk=score,
                tier=_tier(score),
                reason=_reason(factors, concentration.truck_factor_50),
                factors=factors,
                truck_factor_50=concentration.truck_factor_50,
                effective_owners=concentration.effective_owners,
                dominant_expert=None if dominant is None else dominant.handle,
                dominant_share=None if dominant is None else concentration.top_owner_share,
                change_frequency_label=_frequency_label(factors.change_frequency),
            )
        )
        pathologies.extend(_path_pathologies(path, po, rules, ownership, config, comparable))
    entries.sort(key=lambda item: (-item.risk, item.path))
    pathologies.sort(key=lambda item: (item.kind, item.path))
    qualified = compute_qualified_owners(ownership, config)
    vacuums = sum(1 for item in pathologies if item.kind == "knowledge-vacuum")
    summary = RiskSummary(
        observed_ownership_coverage=_coverage(ownership),
        knowledge_at_risk=qualified.distribution.knowledge_at_risk,
        critical_components=sum(1 for item in entries if item.tier == "critical"),
        ownership_vacuums=vacuums,
        high_codeowners_drift=_high_drift_count(drift, config.bus_factor.critical_threshold),
    )
    return RiskReport(
        criticality_unavailable=_criticality_unavailable(ownership, config),
        entries=tuple(entries),
        pathologies=tuple(pathologies),
        summary=summary,
    )


def select_pathologies(
    report: RiskReport,
    visible: frozenset[tuple[str, str, tuple[str, ...]]],
) -> RiskReport:
    """Return `report` keeping pathologies whose finding identity is in `visible`."""
    kept = tuple(item for item in report.pathologies if item.finding().identity() in visible)
    vacuums = sum(1 for item in kept if item.kind == "knowledge-vacuum")
    summary = replace(report.summary, ownership_vacuums=vacuums)
    return replace(report, pathologies=kept, summary=summary)


def risk_payload(report: RiskReport) -> dict[str, object]:
    """Return the machine-readable body of `report`, before the provenance envelope."""
    return {
        "criticality_unavailable": report.criticality_unavailable,
        "entries": [_entry_payload(entry) for entry in report.entries],
        "pathologies": [_pathology_payload(item) for item in report.pathologies],
        "summary": {
            "observed_ownership_coverage": report.summary.observed_ownership_coverage,
            "knowledge_at_risk": report.summary.knowledge_at_risk,
            "critical_components": report.summary.critical_components,
            "ownership_vacuums": report.summary.ownership_vacuums,
            "high_codeowners_drift": report.summary.high_codeowners_drift,
        },
    }


def _entry_payload(entry: RiskEntry) -> dict[str, object]:
    factors = entry.factors
    return {
        "path": entry.path,
        "risk": entry.risk,
        "tier": entry.tier,
        "reason": entry.reason,
        "factors": {
            "ownership_concentration": factors.ownership_concentration,
            "change_frequency": factors.change_frequency,
            "dependency_criticality": factors.dependency_criticality,
            "code_criticality": factors.code_criticality,
            "expertise_decay": factors.expertise_decay,
        },
        "truck_factor_50": entry.truck_factor_50,
        "effective_owners": entry.effective_owners,
        "dominant_expert": entry.dominant_expert,
        "dominant_share": entry.dominant_share,
        "change_frequency_label": entry.change_frequency_label,
    }


def _pathology_payload(item: Pathology) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": item.kind,
        "path": item.path,
        "detail": item.detail,
    }
    if item.writer is not None:
        payload["writer"] = item.writer
    if item.reviewer is not None:
        payload["reviewer"] = item.reviewer
    if item.declared is not None:
        payload["declared"] = item.declared
    return payload


def _factors(
    path: str,
    po: PathOwnership,
    observed: tuple[OwnerEntry, ...],
    concentration: KnowledgeConcentration,
    config: Config,
) -> RiskFactors:
    share = concentration.top_owner_share
    ownership = 1.0 if share <= 0.0 else share
    return RiskFactors(
        ownership_concentration=_publish(ownership),
        change_frequency=_optional(_change_frequency(po.change_events, po.cadence_days)),
        dependency_criticality=_optional(_dependency(po)),
        code_criticality=_optional(_code_criticality(path, config)),
        expertise_decay=_optional(_decay_factor(_dominant(observed))),
    )


def _optional(value: float | None) -> float | None:
    if value is None:
        return None
    return _publish(value)


def _publish(value: float) -> float:
    rounded = round(value, 4)
    if rounded <= 0.0:
        return 0.0001
    if rounded > 1.0:
        return 1.0
    return rounded


def _change_frequency(events: int | None, cadence: float | None) -> float | None:
    if events is None:
        return None
    if events <= 1:
        return _SINGLE_CHANGE_FREQUENCY
    days = 0.0 if cadence is None else max(cadence, 0.0)
    return 1.0 / (1.0 + days / 7.0)


def _dependency(po: PathOwnership) -> float | None:
    degree = po.cochange_degree
    if degree is None or degree <= 0.0:
        return None
    if degree > 1.0:
        return 1.0
    return degree


def _code_criticality(path: str, config: Config) -> float | None:
    for pattern, weight in config.criticality:
        if path_matches_glob(path, pattern):
            return weight
    if _is_manifest(path):
        return _INFERRED_CRITICALITY
    return None


def _decay_factor(owner: OwnerEntry | None) -> float | None:
    if owner is None or owner.freshness is None:
        return None
    stale = 1.0 - owner.freshness.active_expertise
    if stale < _DECAY_FLOOR:
        return _DECAY_FLOOR
    if stale > 1.0:
        return 1.0
    return stale


def _geometric_mean(values: tuple[float, ...]) -> int:
    if not values:
        return 0
    mean = math.exp(sum(math.log(value) for value in values) / len(values))
    score = round(100 * mean)
    if score < 0:
        return 0
    if score > 100:
        return 100
    return score


def _tier(score: int) -> RiskTier:
    if score >= _TIER_CRITICAL:
        return "critical"
    if score >= _TIER_HIGH:
        return "high"
    if score >= _TIER_MEDIUM:
        return "medium"
    return "low"


def _frequency_label(frequency: float | None) -> FrequencyLabel | None:
    if frequency is None:
        return None
    if frequency >= 0.85:
        return "very high"
    if frequency >= 0.6:
        return "high"
    if frequency >= 0.3:
        return "medium"
    if frequency >= 0.1:
        return "low"
    return "very low"


def _reason(factors: RiskFactors, truck_factor_50: int) -> str:
    ranked: list[tuple[float, str]] = []
    if truck_factor_50 <= 1:
        ranked.append((factors.ownership_concentration, "single expert"))
    if factors.change_frequency is not None:
        ranked.append((factors.change_frequency, "high churn"))
    if factors.dependency_criticality is not None:
        ranked.append((factors.dependency_criticality, "dependency criticality"))
    if factors.expertise_decay is not None:
        ranked.append((factors.expertise_decay, "stale expertise"))
    code = factors.code_criticality
    if code is not None and code <= _LOW_CRITICALITY:
        ranked.append((1.0 - code, "low criticality"))
    strong = sorted(
        ((value, label) for value, label in ranked if value >= _REASON_FLOOR),
        key=lambda item: (-item[0], item[1]),
    )
    if strong:
        return " + ".join(label for _, label in strong)
    if not ranked:
        return "low criticality"
    best = max(ranked, key=lambda item: (item[0], item[1]))
    return best[1]


def _criticality_unavailable(ownership: OwnershipMap, config: Config) -> bool:
    if config.criticality:
        return False
    for path, po in ownership.paths.items():
        if _is_manifest(path) or po.cochange_degree is not None:
            return False
    return True


def _is_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in _MANIFEST_NAMES or name.endswith(".tf"):
        return True
    if _compose_name(name):
        return True
    parts = set(path.split("/"))
    return "k8s" in parts or "deploy" in parts


def _compose_name(name: str) -> bool:
    for prefix in ("docker-compose", "compose"):
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        if rest in {".yml", ".yaml"}:
            return True
        if rest.startswith(".") and rest.endswith((".yml", ".yaml")):
            return True
    return False


def _coverage(ownership: OwnershipMap) -> float:
    paths = ownership.paths
    if not paths:
        return 0.0
    covered = sum(1 for po in paths.values() if po.qualified_owner_count >= 1)
    return round(covered / len(paths), 4)


def _high_drift_count(result: DriftResult, critical_threshold: int) -> int:
    count = 0
    for entry in (*result.stale, *result.missing, *result.changed):
        few = (
            entry.qualified_owner_count is not None
            and entry.qualified_owner_count <= critical_threshold
        )
        if entry.confidence_delta >= _HIGH_DRIFT_DELTA or few or entry.decay:
            count += 1
    return count


def _path_pathologies(
    path: str,
    po: PathOwnership,
    rules: tuple[CodeownersRule, ...],
    ownership: OwnershipMap,
    config: Config,
    comparable: bool,
) -> tuple[Pathology, ...]:
    found: list[Pathology] = []
    vacuum = _vacuum(path, po, ownership, config.analysis.confidence_threshold)
    if vacuum is not None:
        found.append(vacuum)
    if not comparable:
        return tuple(found)
    rule = match_path(rules, path)
    declared = () if rule is None else rule.owners
    if not declared:
        return tuple(found)
    phantoms = _phantoms(po, declared)
    if phantoms:
        found.append(
            Pathology(
                kind="phantom-ownership",
                path=path,
                detail="declared ownership without observed expertise",
                owners=phantoms,
                declared_owners=phantoms,
            )
        )
    shadow = _shadow(po, declared)
    if shadow is not None:
        joined = ", ".join(declared)
        found.append(
            Pathology(
                kind="shadow-maintainer",
                path=path,
                detail=f"declared {joined}, dominant observed expert {shadow}",
                owners=(shadow,),
                declared_owners=declared,
            )
        )
    roles = _divergence(po, declared)
    if roles is not None:
        writer, reviewer, declared_owner = roles
        found.append(
            Pathology(
                kind="ownership-review-divergence",
                path=path,
                detail=f"writer {writer}, reviewer {reviewer}, declared {declared_owner}",
                owners=(writer, reviewer, declared_owner),
                declared_owners=(declared_owner,),
                writer=writer,
                reviewer=reviewer,
                declared=declared_owner,
            )
        )
    return tuple(found)


def _vacuum(
    path: str,
    po: PathOwnership,
    ownership: OwnershipMap,
    threshold: float,
) -> Pathology | None:
    observed = _observed(po)
    if not _is_vacuum(observed, threshold):
        return None
    best = max((owner.ownership_score for owner in observed), default=0.0)
    days = _latest_days(observed, ownership)
    score = f"{best:.2f}"
    if days is None:
        detail = f"latest activity unknown, highest score {score}"
    else:
        detail = f"latest activity {days} days ago, highest score {score}"
    return Pathology(kind="knowledge-vacuum", path=path, detail=detail)


def _is_vacuum(owners: tuple[OwnerEntry, ...], threshold: float) -> bool:
    if not owners:
        return True
    best = max(owner.ownership_score for owner in owners)
    if best < threshold:
        return True
    qualified = tuple(owner for owner in owners if owner.ownership_score >= threshold)
    return all(
        owner.freshness is not None and owner.freshness.status in _STALE for owner in qualified
    )


def _latest_days(owners: tuple[OwnerEntry, ...], ownership: OwnershipMap) -> int | None:
    stamps = tuple(owner.last_commit for owner in owners if owner.last_commit is not None)
    if not stamps:
        return None
    elapsed = ownership.last_analyzed - max(stamps)
    return max(0, elapsed.days)


def _phantoms(po: PathOwnership, declared: tuple[str, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for name in declared:
        matches = tuple(owner for owner in _everyone(po) if _same(owner.handle, name))
        if any(_touched(owner) for owner in matches):
            continue
        found.append(name)
    return tuple(found)


def _shadow(po: PathOwnership, declared: tuple[str, ...]) -> str | None:
    expert = _dominant(_observed(po))
    if expert is None:
        return None
    if any(_same(expert.handle, name) for name in declared):
        return None
    return expert.handle


def _divergence(
    po: PathOwnership,
    declared: tuple[str, ...],
) -> tuple[str, str, str] | None:
    people = _everyone(po)
    if not any(_review_score(owner) is not None for owner in people):
        return None
    writer = _writer(people)
    reviewer = _reviewer(people)
    if writer is None or reviewer is None or _same(writer.handle, reviewer.handle):
        return None
    declared_owner = _other_declared(declared, writer.handle, reviewer.handle)
    if declared_owner is None:
        return None
    return writer.handle, reviewer.handle, declared_owner


def _other_declared(declared: tuple[str, ...], writer: str, reviewer: str) -> str | None:
    for name in declared:
        if not _same(name, writer) and not _same(name, reviewer):
            return name
    return None


def _touched(owner: OwnerEntry) -> bool:
    if owner.commits > 0:
        return True
    score = _review_score(owner)
    return score is not None and score > 0


def _writer(owners: tuple[OwnerEntry, ...]) -> OwnerEntry | None:
    if not owners:
        return None
    blamed = tuple((owner, score) for owner in owners if (score := _blame_score(owner)) is not None)
    if blamed:
        return max(blamed, key=lambda item: (item[1], item[0].ownership_score, item[0].handle))[0]
    return max(owners, key=lambda owner: (owner.commits, owner.ownership_score, owner.handle))


def _reviewer(owners: tuple[OwnerEntry, ...]) -> OwnerEntry | None:
    reviewed = tuple(
        (owner, score)
        for owner in owners
        if (score := _review_score(owner)) is not None and score > 0
    )
    if not reviewed:
        return None
    return max(reviewed, key=lambda item: (item[1], item[0].handle))[0]


def _dominant(owners: tuple[OwnerEntry, ...]) -> OwnerEntry | None:
    if not owners:
        return None
    return max(owners, key=lambda owner: (owner.ownership_score, owner.commits, owner.handle))


def _observed(po: PathOwnership) -> tuple[OwnerEntry, ...]:
    if po.scored_owners:
        return po.scored_owners
    return po.owners


def _everyone(po: PathOwnership) -> tuple[OwnerEntry, ...]:
    return (*_observed(po), *po.candidates)


def _blame_score(owner: OwnerEntry) -> float | None:
    breakdown = owner.score_breakdown
    if breakdown is None or not breakdown.blame.available:
        return None
    return breakdown.blame.score


def _review_score(owner: OwnerEntry) -> float | None:
    breakdown = owner.score_breakdown
    if breakdown is None or not breakdown.review.available:
        return None
    return breakdown.review.score


def _same(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()

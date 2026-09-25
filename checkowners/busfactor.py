"""Qualified owner count per path with backup-reviewer recommendations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from checkowners.expertise import common_prefix_depth, path_matches_glob
from checkowners.models import (
    BusFactor,
    BusFactorConfig,
    Config,
    OwnerDistribution,
    OwnerDistributionJson,
    OwnershipMap,
    PathOwnership,
    knowledge_concentration,
)

Tier = Literal["critical", "warning", "ok"]


EMPTY_DISTRIBUTION = OwnerDistribution(
    minimum=0.0,
    p10=0.0,
    median=0.0,
    p90=0.0,
    critical_path_risk=0.0,
    knowledge_at_risk=0.0,
    criticality_incomplete=True,
)


@dataclass(frozen=True)
class BusFactorReport:
    entries: tuple[BusFactor, ...]
    qualified_owner_count_cap: int
    distribution: OwnerDistribution = EMPTY_DISTRIBUTION
    config: BusFactorConfig = field(default_factory=BusFactorConfig)

    @property
    def critical_paths(self) -> tuple[str, ...]:
        return tuple(
            e.path for e in self.entries if self.tier_for(e.qualified_owner_count) == "critical"
        )

    def tier_for(self, qualified_owner_count: int) -> Tier:
        """Tier a qualified owner count against this report's configured thresholds."""
        return classify(qualified_owner_count, self.config)


def format_qualified_owner_count(count: int, cap: int) -> str:
    return f"{count} (capped by top_n_owners={cap})"


def qualified_owner_count_fields(count: int, cap: int) -> dict[str, int]:
    return {
        "qualified_owner_count": count,
        "qualified_owner_count_cap": cap,
    }


def compute_qualified_owners(
    ownership: OwnershipMap,
    config: Config,
    *,
    target: str | None = None,
) -> BusFactorReport:
    """Compute qualified-owner entries for every path matching `target` (or all paths)."""
    threshold = config.analysis.confidence_threshold
    cap = config.analysis.top_n_owners
    entries: list[BusFactor] = []
    for path, po in ownership.paths.items():
        if target is not None and not path_matches_glob(path, target):
            continue
        qualified = tuple(o.handle for o in po.owners if o.confidence >= threshold)
        backups = _recommend_backups(ownership, path, qualified, threshold)
        entries.append(
            BusFactor(
                path=path,
                qualified_owner_count=po.qualified_owner_count,
                contributors_above_threshold=qualified,
                recommended_backups=backups,
            )
        )
    entries.sort(key=lambda e: (e.qualified_owner_count, e.path))
    return BusFactorReport(
        entries=tuple(entries),
        qualified_owner_count_cap=cap,
        distribution=owner_distribution(tuple(entries), ownership, config),
        config=config.bus_factor,
    )


def distribution_json(distribution: OwnerDistribution) -> OwnerDistributionJson:
    """Return the JSON object for `distribution`."""
    return {
        "minimum": distribution.minimum,
        "p10": distribution.p10,
        "median": distribution.median,
        "p90": distribution.p90,
        "critical_path_risk": distribution.critical_path_risk,
        "knowledge_at_risk": distribution.knowledge_at_risk,
        "criticality_incomplete": distribution.criticality_incomplete,
    }


def owner_distribution(
    entries: tuple[BusFactor, ...],
    ownership: OwnershipMap,
    config: Config,
) -> OwnerDistribution:
    """Return the criticality-weighted share distribution of `entries`."""
    incomplete = not config.criticality
    if not entries:
        return OwnerDistribution(
            minimum=0.0,
            p10=0.0,
            median=0.0,
            p90=0.0,
            critical_path_risk=0.0,
            knowledge_at_risk=0.0,
            criticality_incomplete=incomplete,
        )
    weighted: list[tuple[float, float]] = []
    risk_weight = 0.0
    total_weight = 0.0
    weighted_share = 0.0
    threshold = config.bus_factor.critical_threshold
    for entry in entries:
        path_ownership = ownership.paths.get(entry.path)
        share = _top_owner_share(path_ownership)
        weight = _criticality_weight(entry.path, config.criticality)
        weighted.append((share, weight))
        total_weight += weight
        weighted_share += weight * share
        if entry.qualified_owner_count <= threshold:
            risk_weight += weight
    ordered = sorted(weighted, key=lambda item: item[0])
    return OwnerDistribution(
        minimum=_weighted_percentile(ordered, total_weight, 0.0),
        p10=_weighted_percentile(ordered, total_weight, 0.10),
        median=_weighted_percentile(ordered, total_weight, 0.50),
        p90=_weighted_percentile(ordered, total_weight, 0.90),
        critical_path_risk=round(weighted_share / total_weight, 4),
        knowledge_at_risk=round(risk_weight / total_weight, 4),
        criticality_incomplete=incomplete,
    )


def _top_owner_share(path_ownership: PathOwnership | None) -> float:
    if path_ownership is None:
        return 0.0
    scored = path_ownership.scored_owners or path_ownership.owners
    scores = tuple(owner.ownership_score for owner in scored)
    return knowledge_concentration(scores).top_owner_share


def _criticality_weight(path: str, rules: tuple[tuple[str, float], ...]) -> float:
    for pattern, weight in rules:
        if path_matches_glob(path, pattern):
            return weight
    return 1.0


def _weighted_percentile(
    ordered: list[tuple[float, float]],
    total_weight: float,
    quantile: float,
) -> float:
    target = quantile * total_weight
    cumulative = 0.0
    share = ordered[-1][0]
    for item_share, weight in ordered:
        cumulative += weight
        share = item_share
        if cumulative >= target:
            break
    return share


def classify(qualified_owner_count: int, config: BusFactorConfig) -> Tier:
    """Map a qualified owner count to a severity tier."""
    return _classify(
        qualified_owner_count,
        critical_threshold=config.critical_threshold,
        warn_threshold=config.warn_threshold,
    )


def _classify(
    qualified_owner_count: int,
    *,
    critical_threshold: int,
    warn_threshold: int,
) -> Tier:
    if qualified_owner_count <= critical_threshold:
        return "critical"
    if qualified_owner_count <= warn_threshold:
        return "warning"
    return "ok"


def _recommend_backups(
    ownership: OwnershipMap,
    path: str,
    qualified: tuple[str, ...],
    threshold: float,
) -> tuple[str, ...]:
    """Suggest contributors who could build up backup expertise.

    Prefers contributors working in adjacent paths (shared leading
    directory); when that heuristic yields nothing (e.g. root-level files),
    falls back to the repo-wide top owners by confidence, excluding the
    path's own owners.
    """
    qualified_set = set(qualified)
    candidates = _adjacent_candidates(ownership, path, qualified_set, threshold)
    if not candidates:
        candidates = _repo_wide_candidates(ownership, path)
    ordered = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(handle for handle, _ in ordered[:3])


def _adjacent_candidates(
    ownership: OwnershipMap,
    path: str,
    qualified_set: set[str],
    threshold: float,
) -> dict[str, float]:
    """Backup candidates from paths sharing a leading directory with `path`."""
    candidates: dict[str, float] = {}
    for other_path, po in ownership.paths.items():
        if other_path == path:
            continue
        if common_prefix_depth(path, other_path) < 1:
            continue
        for owner in po.owners:
            if owner.handle in qualified_set:
                continue
            if owner.confidence < threshold:
                continue
            current = candidates.get(owner.handle, 0.0)
            if owner.confidence > current:
                candidates[owner.handle] = owner.confidence
    return candidates


def _repo_wide_candidates(
    ownership: OwnershipMap,
    path: str,
) -> dict[str, float]:
    """Repo-wide top owners by confidence, excluding the path's own owners."""
    own_handles = {owner.handle for owner in ownership.paths[path].owners}
    candidates: dict[str, float] = {}
    for other_path, po in ownership.paths.items():
        if other_path == path:
            continue
        for owner in po.owners:
            if owner.handle in own_handles:
                continue
            current = candidates.get(owner.handle, 0.0)
            if owner.confidence > current:
                candidates[owner.handle] = owner.confidence
    return candidates

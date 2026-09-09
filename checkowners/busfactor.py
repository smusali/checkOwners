"""Qualified owner count per path with backup-reviewer recommendations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from checkowners.expertise import common_prefix_depth, path_matches_glob
from checkowners.models import BusFactor, BusFactorConfig, Config, OwnershipMap

DEPRECATED_COUNT_KEY = "bus_factor"
DEPRECATED_AVG_COUNT_KEY = "avg_bus_factor"

Tier = Literal["critical", "warning", "ok"]


@dataclass(frozen=True)
class BusFactorReport:
    entries: tuple[BusFactor, ...]
    repo_average: float
    qualified_owner_count_cap: int
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
        "bus_factor": count,
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
    repo_average = (
        round(sum(e.qualified_owner_count for e in entries) / len(entries), 2) if entries else 0.0
    )
    return BusFactorReport(
        entries=tuple(entries),
        repo_average=repo_average,
        qualified_owner_count_cap=cap,
        config=config.bus_factor,
    )


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

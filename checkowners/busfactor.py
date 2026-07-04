"""Bus factor analysis per path with backup-reviewer recommendations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from checkowners.expertise import path_matches_glob
from checkowners.models import BusFactor, BusFactorConfig, Config, OwnershipMap

Tier = Literal["critical", "warning", "ok"]


@dataclass(frozen=True)
class BusFactorReport:
    entries: tuple[BusFactor, ...]
    repo_average: float
    config: BusFactorConfig = field(default_factory=BusFactorConfig)

    @property
    def critical_paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries if self.tier_for(e.bus_factor) == "critical")

    def tier_for(self, bus_factor: int) -> Tier:
        """Tier a bus factor against this report's configured thresholds."""
        return classify(bus_factor, self.config)


def compute_bus_factor(
    ownership: OwnershipMap,
    config: Config,
    *,
    target: str | None = None,
) -> BusFactorReport:
    """Compute bus factor entries for every path matching `target` (or all paths)."""
    threshold = config.analysis.confidence_threshold
    entries: list[BusFactor] = []
    for path, po in ownership.paths.items():
        if target is not None and not path_matches_glob(path, target):
            continue
        qualified = tuple(o.handle for o in po.owners if o.confidence >= threshold)
        backups = _recommend_backups(ownership, path, qualified, threshold)
        entries.append(
            BusFactor(
                path=path,
                bus_factor=po.bus_factor,
                contributors_above_threshold=qualified,
                recommended_backups=backups,
            )
        )
    entries.sort(key=lambda e: (e.bus_factor, e.path))
    repo_average = round(sum(e.bus_factor for e in entries) / len(entries), 2) if entries else 0.0
    return BusFactorReport(
        entries=tuple(entries),
        repo_average=repo_average,
        config=config.bus_factor,
    )


def classify(bus_factor: int, config: BusFactorConfig) -> Tier:
    """Map a bus factor value to a severity tier."""
    return _classify(
        bus_factor,
        critical_threshold=config.critical_threshold,
        warn_threshold=config.warn_threshold,
    )


def _classify(
    bus_factor: int,
    *,
    critical_threshold: int,
    warn_threshold: int,
) -> Tier:
    if bus_factor <= critical_threshold:
        return "critical"
    if bus_factor <= warn_threshold:
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
        if _common_prefix_depth(path, other_path) < 1:
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


def _common_prefix_depth(a: str, b: str) -> int:
    a_parts = a.lstrip("/").split("/")[:-1]
    b_parts = b.lstrip("/").split("/")[:-1]
    common = 0
    for x, y in zip(a_parts, b_parts, strict=False):
        if x != y:
            break
        common += 1
    return common

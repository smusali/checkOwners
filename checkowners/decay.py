"""Continuity-risk reports for ownership freshness."""

from __future__ import annotations

from dataclasses import dataclass, replace

from checkowners.expertise import common_prefix_depth
from checkowners.models import Config, DecayWarning, FreshnessStatus, OwnershipMap, PathOwnership


@dataclass(frozen=True)
class DecayReport:
    warning: DecayWarning
    recommended_transfer: str | None


def detect_decay(
    ownership: OwnershipMap,
    config: Config,
) -> tuple[DecayReport, ...]:
    """Return continuity-risk reports for `ownership` under `config`."""
    if not config.decay.alert_on_decay:
        return ()
    reports: list[DecayReport] = []
    active_handles = _active_handles(ownership, config.analysis.confidence_threshold)
    for path, po in ownership.paths.items():
        for warning in po.decay_warnings:
            recommended = _recommend_transfer(
                ownership=ownership,
                path=path,
                decaying_handle=warning.handle,
                confidence_threshold=config.analysis.confidence_threshold,
            )
            status = _report_status(warning, active_handles)
            reports.append(
                DecayReport(
                    warning=replace(warning, status=status),
                    recommended_transfer=recommended,
                )
            )
    reports.sort(key=lambda r: (-r.warning.days_since_last_commit, r.warning.path))
    return tuple(reports)


def _report_status(warning: DecayWarning, active_handles: frozenset[str]) -> FreshnessStatus:
    if warning.status == "inactive" and warning.handle not in active_handles:
        return "departed"
    return warning.status


def _active_handles(
    ownership: OwnershipMap,
    confidence_threshold: float,
) -> frozenset[str]:
    """Handles that still appear as an owner anywhere with high enough confidence."""
    handles: set[str] = set()
    for po in ownership.paths.values():
        for owner in po.owners:
            if owner.confidence >= confidence_threshold and not _is_decaying(po, owner.handle):
                handles.add(owner.handle)
    return frozenset(handles)


def _is_decaying(po: PathOwnership, handle: str) -> bool:
    return any(w.handle == handle for w in po.decay_warnings)


def _recommend_transfer(
    ownership: OwnershipMap,
    path: str,
    decaying_handle: str,
    confidence_threshold: float,
) -> str | None:
    """Pick the highest-confidence active owner of `path` other than the decaying one."""
    po = ownership.paths.get(path)
    if po is None:
        return None
    decaying_handles = {w.handle for w in po.decay_warnings}
    candidates = [
        o
        for o in po.owners
        if o.handle != decaying_handle
        and o.handle not in decaying_handles
        and o.confidence >= confidence_threshold
    ]
    if candidates:
        return candidates[0].handle
    # Fallback: walk other paths and rank by adjacency depth, then confidence.
    cross_path_candidates: dict[str, tuple[int, float]] = {}
    for other_path, other_po in ownership.paths.items():
        if other_path == path:
            continue
        adjacency = common_prefix_depth(path, other_path)
        if adjacency < 1:
            continue
        other_decaying = {w.handle for w in other_po.decay_warnings}
        for owner in other_po.owners:
            if owner.handle == decaying_handle:
                continue
            if owner.handle in other_decaying:
                continue
            if owner.confidence < confidence_threshold:
                continue
            current = cross_path_candidates.get(owner.handle)
            score = (adjacency, owner.confidence)
            if current is None or score > current:
                cross_path_candidates[owner.handle] = score
    if not cross_path_candidates:
        return None
    best = max(cross_path_candidates.items(), key=lambda kv: kv[1])
    return best[0]

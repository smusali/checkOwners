"""Team topology inference from commit co-occurrence patterns.

Clusters contributors who repeatedly co-own the same paths. Each cluster
becomes an inferred TeamCluster with the paths the cluster owns and the
contributors it contains. When GitHub API access is enabled and an org is
configured, the inferred clusters are also reconciled with declared
GitHub teams so the report can flag mismatches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from checkowners.models import Config, GithubConfig, OwnershipMap, TeamCluster

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class TopologyReport:
    clusters: tuple[TeamCluster, ...]
    mismatches: tuple[str, ...]


def infer_topology(
    ownership: OwnershipMap,
    config: Config,
    *,
    declared_teams: Mapping[str, frozenset[str]] | None = None,
) -> TopologyReport:
    """Infer implicit teams via simple co-occurrence clustering."""
    co_occurrence = _build_co_occurrence(ownership, config.analysis.confidence_threshold)
    clusters = _cluster(co_occurrence)
    primary_paths = _assign_primary_paths(ownership, clusters, config.analysis.confidence_threshold)
    declared = declared_teams or {}
    annotated: list[TeamCluster] = []
    mismatches: list[str] = []
    for idx, members in enumerate(clusters):
        name, is_declared, cluster_mismatches = _name_cluster(members, declared, idx)
        annotated.append(
            TeamCluster(
                name=name,
                members=tuple(sorted(members)),
                primary_paths=primary_paths.get(frozenset(members), ()),
                declared=is_declared,
            )
        )
        mismatches.extend(cluster_mismatches)
    return TopologyReport(clusters=tuple(annotated), mismatches=tuple(mismatches))


def _build_co_occurrence(
    ownership: OwnershipMap,
    confidence_threshold: float,
) -> dict[str, set[str]]:
    """For each contributor, the set of contributors who co-own paths with them."""
    adjacency: dict[str, set[str]] = {}
    for po in ownership.paths.values():
        qualified = [o.handle for o in po.owners if o.confidence >= confidence_threshold]
        for handle in qualified:
            adjacency.setdefault(handle, set())
        for i, handle_a in enumerate(qualified):
            for handle_b in qualified[i + 1 :]:
                adjacency[handle_a].add(handle_b)
                adjacency.setdefault(handle_b, set()).add(handle_a)
    return adjacency


def _cluster(adjacency: dict[str, set[str]]) -> list[set[str]]:
    """Greedy connected-component clustering over the adjacency graph."""
    visited: set[str] = set()
    clusters: list[set[str]] = []
    for node in sorted(adjacency):
        if node in visited:
            continue
        component: set[str] = set()
        queue: list[str] = [node]
        while queue:
            current = queue.pop()
            if current in component:
                continue
            component.add(current)
            for neighbor in adjacency.get(current, set()):
                if neighbor not in component:
                    queue.append(neighbor)
        visited.update(component)
        clusters.append(component)
    return clusters


def _assign_primary_paths(
    ownership: OwnershipMap,
    clusters: list[set[str]],
    confidence_threshold: float,
) -> dict[frozenset[str], tuple[str, ...]]:
    """Map each cluster to the paths whose qualified owners are wholly inside it."""
    result: dict[frozenset[str], list[str]] = {}
    for cluster in clusters:
        result[frozenset(cluster)] = []
    for path, po in ownership.paths.items():
        qualified = {o.handle for o in po.owners if o.confidence >= confidence_threshold}
        if not qualified:
            continue
        for cluster in clusters:
            if qualified <= cluster:
                result[frozenset(cluster)].append(path)
                break
    return {key: tuple(sorted(paths)) for key, paths in result.items()}


def _name_cluster(
    members: set[str],
    declared: Mapping[str, frozenset[str]],
    fallback_index: int,
) -> tuple[str, bool, tuple[str, ...]]:
    """Find the declared team matching `members` exactly, or note every mismatch.

    A cluster overlapping several declared teams yields one mismatch line per
    overlapping team.
    """
    member_handles = {m.lstrip("@") for m in members}
    for team, team_members in declared.items():
        normalized = {m.lstrip("@") for m in team_members}
        if normalized == member_handles:
            return team, True, ()
    name = f"inferred-{fallback_index}"
    mismatches = tuple(
        f"{name} overlaps declared team '{team}' but membership differs"
        for team in sorted(declared)
        if member_handles & {m.lstrip("@") for m in declared[team]}
    )
    return name, False, mismatches


def declared_teams_from_github(
    config: Config,
) -> dict[str, frozenset[str]] | None:
    """Fetch declared GitHub team -> members map; None when API is disabled."""
    if not config.github.api_enabled:
        return None
    return _fetch_teams(config.github)


def _fetch_teams(github: GithubConfig) -> dict[str, frozenset[str]] | None:
    if not github.org:
        return None
    try:
        from checkowners.github import (  # noqa: PLC0415
            _get_org_teams,
            get_github_client,
            get_github_token,
        )
    except ImportError:
        return None
    token = get_github_token()
    if not token:
        return None
    client = get_github_client(token)
    if client is None:
        return None
    raw = _get_org_teams(client, github.org)
    return {team: frozenset(members) for team, members in raw.items()}

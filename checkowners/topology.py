"""Exploratory repository topology from weighted collaboration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import checkowners.graph as graph_module
from checkowners.models import Config, OwnerEntry, OwnershipMap, TeamCluster

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

    import networkx as nx

TOPOLOGY_SEED = 0
STABILITY_SEEDS = (0, 1, 2, 3, 4)
DEFAULT_RESOLUTION = 1.0
AFFILIATION_FRACTION = 0.5
MIN_AFFILIATION_NEIGHBORS = 2
SHARED_MODULE_WEIGHT = 0.25
_UNAVAILABLE = ("co_change", "temporal_collaboration")

TOPOLOGY_HYPOTHESIS = (
    "Experimental. Exploratory repository topology is a hypothesis "
    "for a person to confirm, not an org chart."
)


@dataclass(frozen=True)
class TeamComparison:
    team: str
    cluster: str
    jaccard: float
    precision: float
    recall: float
    coverage: float
    cross_team: tuple[str, ...] = ()


@dataclass
class _PairWeight:
    co_ownership: float = 0.0
    co_review: float = 0.0
    shared_module: float = 0.0

    @property
    def total(self) -> float:
        return self.co_ownership + self.co_review + self.shared_module


@dataclass(frozen=True)
class TopologyReport:
    clusters: tuple[TeamCluster, ...]
    mismatches: tuple[str, ...]
    algorithm: str = "louvain"
    resolution: float = DEFAULT_RESOLUTION
    seed: int = TOPOLOGY_SEED
    modularity: float = 0.0
    stability_mean: float = 0.0
    stability_variance: float = 0.0
    stability_available: bool = False
    signals_used: tuple[str, ...] = ()
    signals_unavailable: tuple[str, ...] = _UNAVAILABLE
    experimental: bool = True
    comparisons: tuple[TeamComparison, ...] = field(default_factory=tuple)


def infer_topology(
    ownership: OwnershipMap,
    config: Config,
    *,
    declared_teams: Mapping[str, frozenset[str]] | None = None,
    resolution: float = DEFAULT_RESOLUTION,
) -> TopologyReport:
    threshold = config.analysis.confidence_threshold
    weights, people, review_used = _pair_weights(ownership, threshold)
    used, unavailable = _signal_lists(review_used)
    if not people:
        return TopologyReport(
            clusters=(),
            mismatches=(),
            resolution=resolution,
            signals_used=used,
            signals_unavailable=unavailable,
        )
    graph = _person_graph(weights, people)
    published = _cover(graph, _levels(graph, resolution, TOPOLOGY_SEED))
    affiliated = _affiliate(graph, published)
    parents = _parent_names(graph, resolution, published)
    modularity = _modularity(graph, published, resolution)
    stability_mean, stability_variance, stability_available = _stability(graph, resolution)
    clusters = _annotate(
        ownership,
        published,
        affiliated,
        parents,
        declared_teams or {},
        threshold,
    )
    comparisons = _comparisons(clusters, declared_teams or {})
    mismatches = tuple(
        line for line in (_mismatch_line(item) for item in comparisons) if line is not None
    )
    return TopologyReport(
        clusters=clusters,
        mismatches=mismatches,
        resolution=resolution,
        modularity=modularity,
        stability_mean=stability_mean,
        stability_variance=stability_variance,
        stability_available=stability_available,
        signals_used=used,
        signals_unavailable=unavailable,
        comparisons=comparisons,
    )


def _signal_lists(review_used: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    used = ["co_ownership", "shared_module"]
    unavailable = list(_UNAVAILABLE)
    if review_used:
        used.append("co_review")
    else:
        unavailable.append("co_review")
    return tuple(used), tuple(unavailable)


def _pair_weights(
    ownership: OwnershipMap,
    threshold: float,
) -> tuple[dict[tuple[str, str], _PairWeight], set[str], bool]:
    weights: dict[tuple[str, str], _PairWeight] = {}
    people: set[str] = set()
    by_directory: dict[str, set[str]] = {}
    review_used = False
    for path, path_ownership in ownership.paths.items():
        qualified = [owner for owner in path_ownership.owners if owner.confidence >= threshold]
        handles = [owner.handle for owner in qualified]
        people.update(handles)
        for index, left in enumerate(qualified):
            for right in qualified[index + 1 :]:
                key = _pair_key(left.handle, right.handle)
                slot = weights.setdefault(key, _PairWeight())
                slot.co_ownership += min(left.confidence, right.confidence)
                review = _pair_review(left, right)
                if review is None:
                    continue
                slot.co_review += review
                review_used = True
        directory = _module_dir(path)
        if directory:
            by_directory.setdefault(directory, set()).update(handles)
    for directory_handles in by_directory.values():
        ordered = sorted(directory_handles)
        for index, left_handle in enumerate(ordered):
            for right_handle in ordered[index + 1 :]:
                slot = weights.setdefault(_pair_key(left_handle, right_handle), _PairWeight())
                slot.shared_module += SHARED_MODULE_WEIGHT
    return weights, people, review_used


def _pair_review(left: OwnerEntry, right: OwnerEntry) -> float | None:
    left_score = left.score_breakdown
    right_score = right.score_breakdown
    if left_score is None or right_score is None:
        return None
    if not left_score.review.available or not right_score.review.available:
        return None
    return min(left_score.review.score, right_score.review.score)


def _module_dir(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    if parent == ".":
        return ""
    return parent


def _pair_key(left: str, right: str) -> tuple[str, str]:
    if left <= right:
        return (left, right)
    return (right, left)


def _networkx() -> ModuleType:
    return graph_module._require_networkx()


def _person_graph(
    weights: dict[tuple[str, str], _PairWeight],
    people: set[str],
) -> nx.Graph:
    nx = _networkx()
    graph = nx.Graph()
    graph.add_nodes_from(sorted(people))
    for (left, right), slot in weights.items():
        total = slot.total
        if total <= 0:
            continue
        graph.add_edge(left, right, weight=total)
    return graph


def _levels(graph: nx.Graph, resolution: float, seed: int) -> list[frozenset[str]]:
    nx = _networkx()
    if graph.number_of_nodes() == 0:
        return []
    partitions = list(
        nx.community.louvain_partitions(graph, weight="weight", resolution=resolution, seed=seed)
    )
    if not partitions:
        return []
    chosen = partitions[-2] if len(partitions) >= 2 else partitions[-1]
    return [frozenset(community) for community in chosen]


def _cover(graph: nx.Graph, communities: Sequence[frozenset[str]]) -> tuple[frozenset[str], ...]:
    covered: set[str] = set()
    groups = list(communities)
    for community in groups:
        covered.update(community)
    for node in graph.nodes:
        if isinstance(node, str) and node not in covered:
            groups.append(frozenset({node}))
    return tuple(sorted(groups, key=lambda item: tuple(sorted(item))))


def _parent_names(
    graph: nx.Graph,
    resolution: float,
    published: tuple[frozenset[str], ...],
) -> dict[frozenset[str], str]:
    nx = _networkx()
    if graph.number_of_nodes() == 0:
        return {}
    raw_levels = list(
        nx.community.louvain_partitions(
            graph,
            weight="weight",
            resolution=resolution,
            seed=TOPOLOGY_SEED,
        )
    )
    if len(raw_levels) < 2:
        return {}
    coarse = [frozenset(community) for community in raw_levels[-1]]
    parent_of: dict[frozenset[str], frozenset[str]] = {}
    for community in published:
        for container in coarse:
            if community < container:
                parent_of[community] = container
                break
    ordered = sorted(set(parent_of.values()), key=lambda item: tuple(sorted(item)))
    names = {item: f"parent-{index}" for index, item in enumerate(ordered)}
    return {child: names[parent] for child, parent in parent_of.items()}


def _modularity(graph: nx.Graph, communities: Sequence[frozenset[str]], resolution: float) -> float:
    if graph.number_of_edges() == 0 or not communities:
        return 0.0
    nx = _networkx()
    score = nx.community.modularity(
        graph,
        [set(community) for community in communities],
        weight="weight",
        resolution=resolution,
    )
    measured = graph_module._measured(score)
    if measured is None:
        return 0.0
    return round(measured, 4)


def _stability(graph: nx.Graph, resolution: float) -> tuple[float, float, bool]:
    if graph.number_of_edges() == 0:
        return 0.0, 0.0, False
    partitions = [_cover(graph, _levels(graph, resolution, seed)) for seed in STABILITY_SEEDS]
    scores: list[float] = []
    for index, left in enumerate(partitions):
        for right in partitions[index + 1 :]:
            scores.append(_mean_match(left, right))
    if not scores:
        return 1.0, 0.0, True
    mean = sum(scores) / len(scores)
    variance = sum((score - mean) ** 2 for score in scores) / len(scores)
    return round(mean, 4), round(variance, 4), True


def _mean_match(
    left: tuple[frozenset[str], ...],
    right: tuple[frozenset[str], ...],
) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    used: set[int] = set()
    scores: list[float] = []
    for community in left:
        best_score = -1.0
        best_index = -1
        for index, other in enumerate(right):
            if index in used:
                continue
            score = _jaccard(set(community), set(other))
            if score > best_score:
                best_score = score
                best_index = index
        if best_index >= 0:
            used.add(best_index)
            scores.append(best_score)
    return sum(scores) / len(scores)


def _affiliate(
    graph: nx.Graph,
    communities: tuple[frozenset[str], ...],
) -> dict[int, set[str]]:
    index_of = {node: index for index, community in enumerate(communities) for node in community}
    extra: dict[int, set[str]] = {index: set() for index in range(len(communities))}
    for node in graph.nodes:
        if not isinstance(node, str) or node not in index_of:
            continue
        primary = index_of[node]
        weights: dict[int, float] = {}
        neighbors: dict[int, int] = {}
        for neighbor in graph.neighbors(node):
            if not isinstance(neighbor, str) or neighbor not in index_of:
                continue
            other = index_of[neighbor]
            raw = graph[node][neighbor].get("weight", 1.0)
            if not isinstance(raw, int | float):
                continue
            weights[other] = weights.get(other, 0.0) + raw
            neighbors[other] = neighbors.get(other, 0) + 1
        primary_weight = weights.get(primary, 0.0)
        if primary_weight <= 0:
            continue
        for other, weight in weights.items():
            if other == primary:
                continue
            if neighbors.get(other, 0) < MIN_AFFILIATION_NEIGHBORS:
                continue
            if weight >= AFFILIATION_FRACTION * primary_weight:
                extra[other].add(node)
    return extra


def _annotate(
    ownership: OwnershipMap,
    communities: tuple[frozenset[str], ...],
    affiliated: dict[int, set[str]],
    parents: dict[frozenset[str], str],
    declared: Mapping[str, frozenset[str]],
    threshold: float,
) -> tuple[TeamCluster, ...]:
    full_sets = [
        set(community) | affiliated.get(index, set()) for index, community in enumerate(communities)
    ]
    paths = _primary_paths(ownership, full_sets, threshold)
    clusters: list[TeamCluster] = []
    for index, community in enumerate(communities):
        full = full_sets[index]
        name, is_declared = _name_cluster(full, declared, index)
        clusters.append(
            TeamCluster(
                name=name,
                members=tuple(sorted(community)),
                primary_paths=paths[index],
                declared=is_declared,
                affiliated_members=tuple(sorted(affiliated.get(index, set()))),
                parent=parents.get(community, ""),
            )
        )
    return tuple(clusters)


def _primary_paths(
    ownership: OwnershipMap,
    clusters: list[set[str]],
    threshold: float,
) -> list[tuple[str, ...]]:
    buckets: list[list[str]] = [[] for _ in clusters]
    for path, path_ownership in ownership.paths.items():
        qualified = {
            owner.handle for owner in path_ownership.owners if owner.confidence >= threshold
        }
        if not qualified:
            continue
        for index, members in enumerate(clusters):
            if qualified <= members:
                buckets[index].append(path)
    return [tuple(sorted(paths)) for paths in buckets]


def _name_cluster(
    members: set[str],
    declared: Mapping[str, frozenset[str]],
    fallback_index: int,
) -> tuple[str, bool]:
    bare_members = {_bare(member) for member in members}
    exact = sorted(
        team
        for team, team_members in declared.items()
        if {_bare(member) for member in team_members} == bare_members
    )
    if exact:
        return exact[0], True
    return f"inferred-{fallback_index}", False


def _comparisons(
    clusters: tuple[TeamCluster, ...],
    declared: Mapping[str, frozenset[str]],
) -> tuple[TeamComparison, ...]:
    clustered = {
        _bare(member)
        for cluster in clusters
        for member in (*cluster.members, *cluster.affiliated_members)
    }
    comparisons: list[TeamComparison] = []
    for team in sorted(declared):
        declared_bare = {_bare(member) for member in declared[team]}
        best = _closest(clusters, declared_bare)
        if best is None:
            comparisons.append(
                TeamComparison(
                    team=team,
                    cluster="",
                    jaccard=0.0,
                    precision=0.0,
                    recall=0.0,
                    coverage=_ratio(len(declared_bare & clustered), len(declared_bare)),
                )
            )
            continue
        full = {_bare(member) for member in (*best.members, *best.affiliated_members)}
        shared = full & declared_bare
        cross = tuple(
            sorted(
                member
                for member in (*best.members, *best.affiliated_members)
                if _bare(member) not in declared_bare
            )
        )
        comparisons.append(
            TeamComparison(
                team=team,
                cluster=best.name,
                jaccard=_jaccard(full, declared_bare),
                precision=_ratio(len(shared), len(full)),
                recall=_ratio(len(shared), len(declared_bare)),
                coverage=_ratio(len(declared_bare & clustered), len(declared_bare)),
                cross_team=cross,
            )
        )
    return tuple(comparisons)


def _closest(
    clusters: tuple[TeamCluster, ...],
    declared_bare: set[str],
) -> TeamCluster | None:
    best: TeamCluster | None = None
    best_score = -1.0
    for cluster in clusters:
        full = {_bare(member) for member in (*cluster.members, *cluster.affiliated_members)}
        score = _jaccard(full, declared_bare)
        if best is None or score > best_score or (score == best_score and cluster.name < best.name):
            best = cluster
            best_score = score
    return best


def _mismatch_line(comparison: TeamComparison) -> str | None:
    if comparison.jaccard <= 0 or comparison.jaccard >= 1:
        return None
    line = (
        f"{comparison.cluster} overlaps declared team '{comparison.team}' "
        f"but membership differs: Jaccard {comparison.jaccard:.2f}, "
        f"precision {comparison.precision:.2f}, recall {comparison.recall:.2f}, "
        f"coverage {comparison.coverage:.2f}"
    )
    if comparison.cross_team:
        experts = ", ".join(comparison.cross_team)
        return f"{line}. Potential cross-team expert: {experts}"
    return line


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return _ratio(len(left & right), len(union))


def _ratio(part: int, whole: int) -> float:
    if whole == 0:
        return 0.0
    return round(part / whole, 4)


def _bare(handle: str) -> str:
    text = handle.strip()
    if text.startswith("@"):
        return text[1:]
    return text

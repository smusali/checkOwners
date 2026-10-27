"""Ownership graph backed by an optional networkx extra."""

from __future__ import annotations

import ast
import io
import json
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import TYPE_CHECKING

from checkowners.models import Config, OwnerEntry, OwnershipMap, PathOwnership, TeamCluster
from checkowners.patterns import CodeownersRule, matching_rules
from checkowners.privacy import label

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    import networkx as nx

EXPORT_FORMATS: tuple[str, ...] = ("dot", "graphml", "gexf", "json")
_BETWEENNESS_LIMIT = 10
_INACTIVE_FRESHNESS = frozenset({"inactive", "departed"})
_INACTIVE_STATUS = frozenset({"former", "inactive"})
_HEADINGS = {
    "repository": "repositories",
    "team": "teams",
    "rule": "rules",
    "directory": "directories",
    "contributor": "contributors",
    "path": "paths",
}
_KIND_ORDER = ("repository", "team", "rule", "directory", "contributor", "path")
_ATTR = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')
_PREFIXES = ("contrib::", "path::", "dir::", "team::", "rule::", "repo::")
GraphValue = str | int | float | bool


class GraphExtraMissingError(ImportError):
    """Raised when networkx is not installed but a graph operation was requested."""


@dataclass(frozen=True)
class ArticulationPoint:
    person: str
    directories: tuple[str, ...]


@dataclass(frozen=True)
class BetweennessScore:
    person: str
    score: float


@dataclass(frozen=True)
class OrphanSubgraph:
    directory: str
    paths: tuple[str, ...]
    dependents: tuple[str, ...]


@dataclass(frozen=True)
class GraphAnalytics:
    articulation_points: tuple[ArticulationPoint, ...] = ()
    betweenness: tuple[BetweennessScore, ...] = ()
    orphans: tuple[OrphanSubgraph, ...] = ()
    dependency_graph: str = "unavailable"
    transfer_path: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "articulation_points": [
                {"person": item.person, "directories": list(item.directories)}
                for item in self.articulation_points
            ],
            "betweenness": [
                {"person": item.person, "score": item.score} for item in self.betweenness
            ],
            "orphans": [
                {
                    "directory": item.directory,
                    "paths": list(item.paths),
                    "dependents": list(item.dependents),
                }
                for item in self.orphans
            ],
            "dependency_graph": self.dependency_graph,
        }
        if self.transfer_path:
            payload["transfer_path"] = list(self.transfer_path)
        return payload


@dataclass(frozen=True)
class TransferResult:
    nodes: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def _require_networkx() -> ModuleType:
    try:
        import networkx  # noqa: PLC0415
    except ImportError as exc:
        msg = "networkx is required for graph commands; install checkowners[graph]"
        raise GraphExtraMissingError(msg) from exc
    module: ModuleType = networkx
    return module


def build_graph(
    ownership: OwnershipMap,
    *,
    clusters: tuple[TeamCluster, ...] = (),
    rules: tuple[CodeownersRule, ...] = (),
    repo_root: Path | None = None,
    repository: str = "",
) -> nx.Graph:
    nx = _require_networkx()
    graph = nx.Graph()
    if repository:
        graph.add_node(_repo_node(repository), kind="repository")
    for path, path_ownership in ownership.paths.items():
        _add_path(graph, path, path_ownership, repository)
    for cluster in clusters:
        _add_cluster(graph, cluster)
    if rules:
        _add_rules(graph, ownership, rules)
    if repo_root is not None:
        for source, target in python_dependencies(repo_root, tuple(ownership.paths)):
            _add_relation(
                graph,
                _path_node(source),
                _path_node(target),
                "DEPENDS_ON",
                dependent=source,
                dependency=target,
            )
    return graph


def python_dependencies(
    repo_root: Path,
    paths: Collection[str],
) -> tuple[tuple[str, str], ...]:
    known = _python_modules(paths)
    edges: set[tuple[str, str]] = set()
    path_set = set(paths)
    for path in sorted(path_set):
        if not path.endswith(".py"):
            continue
        source = repo_root / path
        if not source.is_file():
            continue
        modules = _imported_modules(source, path)
        for module in modules:
            target = known.get(module)
            if target is None or target == path:
                continue
            edges.add((path, target))
    return tuple(sorted(edges))


def for_display(graph: nx.Graph, config: Config, repo_id: str) -> nx.Graph:
    nx = _require_networkx()
    if config.output.aggregate_only:
        kept = [
            node for node, attrs in graph.nodes(data=True) if attrs.get("kind") != "contributor"
        ]
        return graph.subgraph(kept).copy()
    mapping: dict[str, str] = {}
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "contributor" or not isinstance(node, str):
            continue
        if not node.startswith("contrib::"):
            continue
        handle = node.removeprefix("contrib::")
        mapping[node] = f"contrib::{label(handle, config, repo_id)}"
    if not mapping:
        return graph
    return nx.relabel_nodes(graph, mapping, copy=True)


def graph_analytics(
    graph: nx.Graph,
    ownership: OwnershipMap,
    config: Config,
) -> GraphAnalytics:
    knowledge = _knowledge_view(graph)
    return GraphAnalytics(
        articulation_points=_articulation(knowledge),
        betweenness=_betweenness(knowledge),
        orphans=_orphans(graph, ownership, config.analysis.confidence_threshold),
        dependency_graph=_dependency_status(graph),
    )


def transfer_path(graph: nx.Graph, source: str, target: str) -> TransferResult:
    start = resolve_node(graph, source)
    end = resolve_node(graph, target)
    missing = tuple(item for item, node in ((source, start), (target, end)) if node is None)
    if start is None or end is None:
        return TransferResult(missing=missing)
    nx = _require_networkx()
    view = _knowledge_view(graph)
    if start not in view or end not in view:
        view = graph
    try:
        found = nx.shortest_path(view, start, end)
    except nx.NetworkXNoPath:
        return TransferResult()
    nodes = tuple(node for node in found if isinstance(node, str))
    return TransferResult(nodes=nodes)


def endpoint_is_contributor(graph: nx.Graph, text: str) -> bool:
    node = resolve_node(graph, text)
    if node is None:
        return False
    kind = graph.nodes[node].get("kind")
    return isinstance(kind, str) and kind == "contributor"


def resolve_node(graph: nx.Graph, text: str) -> str | None:
    if text in graph and isinstance(text, str):
        return text
    bare = text[1:] if text.startswith("@") else text
    path = text if text.startswith("/") else f"/{text}"
    candidates = (
        f"contrib::{text}",
        f"contrib::@{bare}",
        f"path::{path}",
        f"dir::{text.strip('/')}",
        f"team::{text}",
        f"repo::{text}",
    )
    for candidate in candidates:
        if candidate in graph:
            return candidate
    return None


def to_serializable(graph: nx.Graph) -> dict[str, list[dict[str, object]]]:
    nodes = [{"id": node, **attrs} for node, attrs in graph.nodes(data=True)]
    edges = [{"source": u, "target": v, **attrs} for u, v, attrs in graph.edges(data=True)]
    return {"nodes": nodes, "edges": edges}


def from_serializable(data: Mapping[str, object]) -> nx.Graph:
    nx = _require_networkx()
    graph = nx.Graph()
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            if not isinstance(node_id, str):
                continue
            attrs = {key: value for key, value in node.items() if key != "id"}
            graph.add_node(node_id, **attrs)
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = edge.get("source")
            target = edge.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            attrs = {key: value for key, value in edge.items() if key not in ("source", "target")}
            graph.add_edge(source, target, **attrs)
    return graph


def to_json(graph: nx.Graph, analytics: GraphAnalytics | None = None) -> str:
    payload: dict[str, object] = dict(to_serializable(graph))
    if analytics is not None:
        payload["analytics"] = analytics.as_payload()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def to_graphml(graph: nx.Graph) -> str:
    nx = _require_networkx()
    buffer = io.BytesIO()
    nx.write_graphml(graph, buffer, infer_numeric_types=True)
    return buffer.getvalue().decode("utf-8")


def to_gexf(graph: nx.Graph) -> str:
    nx = _require_networkx()
    buffer = io.BytesIO()
    nx.write_gexf(graph, buffer)
    return buffer.getvalue().decode("utf-8")


def to_dot(graph: nx.Graph) -> str:
    _require_networkx()
    lines = ["graph checkowners {"]
    for node, attrs in graph.nodes(data=True):
        attr_pairs = ", ".join(
            f'{key}="{_dot_quote(value)}"' for key, value in sorted(attrs.items())
        )
        attr_str = f" [{attr_pairs}]" if attr_pairs else ""
        lines.append(f'  "{_dot_quote(node)}"{attr_str};')
    for source, target, attrs in graph.edges(data=True):
        attr_pairs = ", ".join(
            f'{key}="{_dot_quote(value)}"' for key, value in sorted(attrs.items())
        )
        attr_str = f" [{attr_pairs}]" if attr_pairs else ""
        lines.append(f'  "{_dot_quote(source)}" -- "{_dot_quote(target)}"{attr_str};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def from_dot(text: str) -> nx.Graph:
    nx = _require_networkx()
    graph = nx.Graph()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("graph ") or line == "}":
            continue
        if line.endswith(";"):
            line = line[:-1].strip()
        if " -- " in line:
            left, right = line.split(" -- ", 1)
            source, _ = _dot_endpoint(left)
            target, attrs = _dot_endpoint(right)
            graph.add_edge(source, target, **attrs)
            continue
        node, attrs = _dot_endpoint(line)
        graph.add_node(node, **attrs)
    return graph


def format_analytics(analytics: GraphAnalytics) -> str:
    lines = ["# analytics"]
    lines.append(f"dependency_graph: {analytics.dependency_graph}")
    lines.append("# articulation points")
    if analytics.articulation_points:
        for point in analytics.articulation_points:
            directories = ", ".join(point.directories) if point.directories else "-"
            lines.append(f"  {_display_node(point.person)}: {directories}")
    else:
        lines.append("  -")
    lines.append("# betweenness")
    if analytics.betweenness:
        for score in analytics.betweenness:
            lines.append(f"  {_display_node(score.person)}: {score.score:.4f}")
    else:
        lines.append("  -")
    lines.append("# orphan subgraphs")
    if analytics.orphans:
        for orphan in analytics.orphans:
            directory = orphan.directory or "."
            lines.append(f"  {directory}: {', '.join(orphan.paths)}")
            if orphan.dependents:
                lines.append(f"    dependents: {', '.join(orphan.dependents)}")
    else:
        lines.append("  -")
    return "\n".join(lines) + "\n"


def format_transfer(result: TransferResult) -> str:
    if not result.nodes:
        return ""
    return " -> ".join(_display_node(node) for node in result.nodes)


def label_nodes(nodes: tuple[str, ...], config: Config, repo_id: str) -> tuple[str, ...]:
    labeled: list[str] = []
    for node in nodes:
        if node.startswith("contrib::"):
            handle = label(node.removeprefix("contrib::"), config, repo_id)
            labeled.append(f"contrib::{handle}")
        else:
            labeled.append(node)
    return tuple(labeled)


def to_text(graph: nx.Graph) -> str:
    lines: list[str] = []
    nodes_by_kind: dict[str, list[str]] = {kind: [] for kind in _KIND_ORDER}
    for node, attrs in graph.nodes(data=True):
        kind = attrs.get("kind", "node")
        if not isinstance(kind, str):
            kind = "node"
        nodes_by_kind.setdefault(kind, []).append(str(node))
    for kind in (*_KIND_ORDER, *(key for key in nodes_by_kind if key not in _KIND_ORDER)):
        nodes = sorted(nodes_by_kind.get(kind, []))
        if not nodes:
            continue
        heading = _HEADINGS.get(kind, f"{kind}s")
        lines.append(f"# {heading} ({len(nodes)})")
        for node in nodes:
            neighbors = sorted(str(item) for item in graph.neighbors(node))
            weights = [
                f"{neighbor}({_edge_weight(graph, node, neighbor):.2f})"
                if _is_observed(graph[node][neighbor])
                else neighbor
                for neighbor in neighbors
            ]
            lines.append(f"  {node}: {', '.join(weights) if weights else '-'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _add_path(
    graph: nx.Graph,
    path: str,
    path_ownership: PathOwnership,
    repository: str,
) -> None:
    graph.add_node(
        _path_node(path),
        kind="path",
        qualified_owner_count=path_ownership.qualified_owner_count,
    )
    _add_directories(graph, path, repository)
    for owner in path_ownership.owners:
        _add_owner_edge(graph, path, owner)


def _add_owner_edge(graph: nx.Graph, path: str, owner: OwnerEntry) -> None:
    person = _contrib_node(owner.handle)
    _ensure_node(graph, person, kind="contributor")
    _add_relation(
        graph,
        person,
        _path_node(path),
        "OWNS_OBSERVED",
        weight=round(owner.confidence, 4),
        commits=owner.commits,
    )
    if owner.commits > 0:
        _add_relation(graph, person, _path_node(path), "AUTHORED")
    review = _review_score(owner)
    if review is not None:
        _add_relation(graph, person, _path_node(path), "REVIEWS", review_score=round(review, 4))
    if _expertise_decaying(owner):
        _add_relation(graph, person, _path_node(path), "DECAYING_EXPERTISE")


def _add_cluster(graph: nx.Graph, cluster: TeamCluster) -> None:
    team = _team_node(cluster.name)
    _ensure_node(graph, team, kind="team", declared=cluster.declared)
    for member in (*cluster.members, *cluster.affiliated_members):
        person = _contrib_node(member)
        _ensure_node(graph, person, kind="contributor")
        _add_relation(graph, person, team, "MEMBER_OF")
    for path in cluster.primary_paths:
        _ensure_node(graph, _path_node(path), kind="path")
        relation = "OWNS_DECLARED" if cluster.declared else "OWNS_OBSERVED"
        _add_relation(graph, team, _path_node(path), relation)


def _add_rules(
    graph: nx.Graph,
    ownership: OwnershipMap,
    rules: tuple[CodeownersRule, ...],
) -> None:
    for path in ownership.paths:
        matches = matching_rules(rules, path)
        if not matches:
            continue
        rule = matches[-1]
        if not rule.owners:
            continue
        rule_node = _rule_node(rule.line_number)
        _ensure_node(
            graph,
            rule_node,
            kind="rule",
            pattern=rule.pattern,
            line_number=rule.line_number,
        )
        _add_relation(graph, rule_node, _path_node(path), "OWNS_DECLARED")
        for owner in rule.owners:
            node, kind = _owner_node(owner)
            _ensure_node(graph, node, kind=kind, declared=True)
            _add_relation(graph, node, rule_node, "OWNS_DECLARED")


def _add_directories(graph: nx.Graph, path: str, repository: str) -> None:
    parents = [
        parent.as_posix() for parent in PurePosixPath(path).parents if parent.as_posix() != "."
    ]
    file_node = _path_node(path)
    if not parents:
        if repository:
            _add_relation(graph, _repo_node(repository), file_node, "CONTAINS")
        return
    for directory in parents:
        _ensure_node(graph, _dir_node(directory), kind="directory")
    _add_relation(graph, _dir_node(parents[0]), file_node, "CONTAINS")
    for child, parent in pairwise(parents):
        _add_relation(graph, _dir_node(parent), _dir_node(child), "CONTAINS")
    if repository:
        _add_relation(graph, _repo_node(repository), _dir_node(parents[-1]), "CONTAINS")


def _add_relation(
    graph: nx.Graph,
    source: str,
    target: str,
    relation: str,
    **attrs: GraphValue,
) -> None:
    if graph.has_edge(source, target):
        data = graph[source][target]
        parts = set(_relation_names(data))
        parts.add(relation)
        data["relations"] = " ".join(sorted(parts))
        for key, value in attrs.items():
            if key not in data:
                data[key] = value
        return
    graph.add_edge(source, target, kind=relation, relations=relation, **attrs)


def _ensure_node(graph: nx.Graph, node: str, **attrs: GraphValue) -> None:
    if node not in graph:
        graph.add_node(node, **attrs)
        return
    current = graph.nodes[node]
    for key, value in attrs.items():
        if key not in current:
            current[key] = value
    if attrs.get("declared") is True:
        current["declared"] = True


def _owner_node(owner: str) -> tuple[str, str]:
    if "/" in owner:
        return _team_node(owner), "team"
    return _contrib_node(owner), "contributor"


def _review_score(owner: OwnerEntry) -> float | None:
    breakdown = owner.score_breakdown
    if breakdown is None or not breakdown.review.available:
        return None
    return breakdown.review.score


def _expertise_decaying(owner: OwnerEntry) -> bool:
    if owner.contributor_status == "former":
        return True
    freshness = owner.freshness
    return freshness is not None and freshness.status in _INACTIVE_FRESHNESS


def _active_expert(owner: OwnerEntry, threshold: float) -> bool:
    if owner.confidence < threshold:
        return False
    if owner.contributor_status in _INACTIVE_STATUS:
        return False
    freshness = owner.freshness
    return freshness is None or freshness.status not in _INACTIVE_FRESHNESS


def _knowledge_view(graph: nx.Graph) -> nx.Graph:
    kept = [
        node
        for node, attrs in graph.nodes(data=True)
        if attrs.get("kind") in {"contributor", "path"}
    ]
    view = graph.subgraph(kept).copy()
    drop = [
        (source, target) for source, target, data in view.edges(data=True) if not _is_observed(data)
    ]
    view.remove_edges_from(drop)
    return view


def _articulation(graph: nx.Graph) -> tuple[ArticulationPoint, ...]:
    if graph.number_of_nodes() == 0:
        return ()
    nx = _require_networkx()
    found: list[ArticulationPoint] = []
    for node in nx.articulation_points(graph):
        if not isinstance(node, str) or graph.nodes[node].get("kind") != "contributor":
            continue
        found.append(ArticulationPoint(person=node, directories=_split_directories(graph, node)))
    return tuple(sorted(found, key=lambda item: item.person))


def _split_directories(graph: nx.Graph, person: str) -> tuple[str, ...]:
    nx = _require_networkx()
    neighbors = [node for node in graph.neighbors(person) if isinstance(node, str)]
    view = graph.copy()
    view.remove_node(person)
    component_of: dict[str, int] = {}
    for index, component in enumerate(nx.connected_components(view)):
        for node in component:
            if isinstance(node, str):
                component_of[node] = index
    directories: set[str] = set()
    seen: set[int] = set()
    for neighbor in neighbors:
        if graph.nodes[neighbor].get("kind") != "path":
            continue
        component = component_of.get(neighbor)
        if component is None or component in seen:
            continue
        seen.add(component)
        directory = _path_directory(neighbor)
        if directory:
            directories.add(directory)
    return tuple(sorted(directories))


def _betweenness(graph: nx.Graph) -> tuple[BetweennessScore, ...]:
    if graph.number_of_nodes() == 0:
        return ()
    nx = _require_networkx()
    scores = nx.betweenness_centrality(graph)
    ranked: list[BetweennessScore] = []
    for node, score in scores.items():
        if not isinstance(node, str) or graph.nodes[node].get("kind") != "contributor":
            continue
        measured = _measured(score)
        if measured is None or measured <= 0:
            continue
        ranked.append(BetweennessScore(person=node, score=round(measured, 4)))
    ranked.sort(key=lambda item: (-item.score, item.person))
    return tuple(ranked[:_BETWEENNESS_LIMIT])


def _orphans(
    graph: nx.Graph,
    ownership: OwnershipMap,
    threshold: float,
) -> tuple[OrphanSubgraph, ...]:
    orphan_paths = [
        path
        for path, path_ownership in ownership.paths.items()
        if not any(_active_expert(owner, threshold) for owner in path_ownership.owners)
    ]
    dependents = _dependents(graph, set(orphan_paths))
    grouped: dict[str, list[str]] = {}
    for path in orphan_paths:
        parent = PurePosixPath(path).parent.as_posix()
        directory = "" if parent == "." else parent
        grouped.setdefault(directory, []).append(path)
    groups = [
        OrphanSubgraph(
            directory=directory,
            paths=tuple(sorted(paths)),
            dependents=tuple(sorted({item for path in paths for item in dependents.get(path, ())})),
        )
        for directory, paths in sorted(grouped.items())
    ]
    return tuple(groups)


def _dependents(graph: nx.Graph, orphan_paths: set[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {path: set() for path in orphan_paths}
    for _source, _target, data in graph.edges(data=True):
        if "DEPENDS_ON" not in _relation_names(data):
            continue
        dependency = data.get("dependency")
        dependent = data.get("dependent")
        if isinstance(dependency, str) and isinstance(dependent, str) and dependency in found:
            found[dependency].add(dependent)
    return found


def _dependency_status(graph: nx.Graph) -> str:
    for _source, _target, data in graph.edges(data=True):
        if "DEPENDS_ON" in _relation_names(data):
            return "available"
    return "unavailable"


def _relation_names(data: Mapping[str, object]) -> tuple[str, ...]:
    raw = data.get("relations", data.get("kind", ""))
    if not isinstance(raw, str):
        return ()
    return tuple(part for part in raw.split() if part)


def _is_observed(data: Mapping[str, object]) -> bool:
    return "OWNS_OBSERVED" in _relation_names(data) or data.get("kind") == "ownership"


def _edge_weight(graph: nx.Graph, source: str, target: str) -> float:
    measured = _measured(graph[source][target].get("weight", 1.0))
    if measured is None:
        return 1.0
    return measured


def _measured(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return value * 1.0
    return None


def _path_directory(node: str) -> str:
    text = node.removeprefix("path::").lstrip("/")
    parent = PurePosixPath(text).parent.as_posix()
    if parent == ".":
        return ""
    return parent


def _display_node(node: str) -> str:
    for prefix in _PREFIXES:
        if node.startswith(prefix):
            return node.removeprefix(prefix)
    return node


def _python_modules(paths: Collection[str]) -> dict[str, str]:
    known: dict[str, str] = {}
    for path in sorted(paths):
        pure = PurePosixPath(path)
        if pure.suffix != ".py":
            continue
        if pure.name == "__init__.py":
            name = ".".join(pure.parent.parts)
        else:
            name = ".".join(pure.with_suffix("").parts)
        if name:
            known.setdefault(name, path)
    return known


def _imported_modules(source: Path, path: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=path)
    except (OSError, SyntaxError, UnicodeError):
        return ()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _from_base(path, node.level or 0)
        module = node.module or ""
        qualified = f"{base}.{module}" if base and module else base or module
        if qualified:
            found.append(qualified)
        prefix = qualified or base
        if not prefix:
            continue
        found.extend(
            f"{prefix}.{alias.name}" for alias in node.names if alias.name and alias.name != "*"
        )
    return tuple(found)


def _from_base(path: str, level: int) -> str:
    if level <= 0:
        return ""
    parts = list(PurePosixPath(path).parent.parts)
    drop = level - 1
    if drop > len(parts):
        return ""
    kept = parts if drop == 0 else parts[: len(parts) - drop]
    return ".".join(kept)


def _dot_quote(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _dot_endpoint(text: str) -> tuple[str, dict[str, GraphValue]]:
    stripped = text.strip()
    if not stripped.startswith('"'):
        msg = "DOT node id must be quoted"
        raise ValueError(msg)
    node, rest = _read_quoted(stripped)
    attrs: dict[str, GraphValue] = {}
    bracket = rest.find("[")
    if bracket == -1:
        return node, attrs
    body = rest[bracket + 1 :]
    end = body.rfind("]")
    if end != -1:
        body = body[:end]
    for match in _ATTR.finditer(body):
        attrs[match.group(1)] = _dot_value(_unescape(match.group(2)))
    return node, attrs


def _read_quoted(text: str) -> tuple[str, str]:
    chars: list[str] = []
    escaped = False
    for index, char in enumerate(text[1:], start=1):
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return "".join(chars), text[index + 1 :]
        chars.append(char)
    msg = "unterminated DOT string"
    raise ValueError(msg)


def _unescape(value: str) -> str:
    chars: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        chars.append(char)
    return "".join(chars)


def _dot_value(text: str) -> GraphValue:
    if text == "True":
        return True
    if text == "False":
        return False
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    try:
        number = float(text)
    except ValueError:
        return text
    return number


def _contrib_node(handle: str) -> str:
    return f"contrib::{handle}"


def _path_node(path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return f"path::{normalized}"


def _team_node(name: str) -> str:
    return f"team::{name}"


def _dir_node(path: str) -> str:
    return f"dir::{path.strip('/')}"


def _repo_node(repository: str) -> str:
    return f"repo::{repository}"


def _rule_node(line_number: int) -> str:
    return f"rule::{line_number}"

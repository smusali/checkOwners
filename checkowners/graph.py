"""Knowledge graph builder backed by an optional networkx extra.

The graph is a bipartite multi-edge structure: contributors on one side,
file paths on the other, with edge weights equal to the inferred
confidence score. Cluster nodes from TeamCluster (when topology has been
inferred) are attached as a third partition.

networkx is loaded lazily so the rest of checkOwners stays usable when
the [graph] extra has not been installed.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING

from checkowners.models import OwnershipMap, TeamCluster

if TYPE_CHECKING:
    import networkx as nx


class GraphExtraMissingError(ImportError):
    """Raised when networkx is not installed but a graph operation was requested."""


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
) -> nx.Graph:
    """Build a multipartite knowledge graph from an ownership map."""
    nx = _require_networkx()
    graph = nx.Graph()
    for path, po in ownership.paths.items():
        graph.add_node(_path_node(path), kind="path", bus_factor=po.bus_factor)
        for owner in po.owners:
            graph.add_node(_contrib_node(owner.handle), kind="contributor")
            graph.add_edge(
                _contrib_node(owner.handle),
                _path_node(path),
                weight=round(owner.confidence, 4),
                commits=owner.commits,
                kind="ownership",
            )
    for cluster in clusters:
        graph.add_node(_team_node(cluster.name), kind="team", declared=cluster.declared)
        for member in cluster.members:
            graph.add_node(_contrib_node(member), kind="contributor")
            graph.add_edge(_contrib_node(member), _team_node(cluster.name), kind="membership")
        for path in cluster.primary_paths:
            graph.add_node(_path_node(path), kind="path")
            graph.add_edge(_team_node(cluster.name), _path_node(path), kind="responsibility")
    return graph


def to_serializable(graph: nx.Graph) -> dict[str, list[dict[str, object]]]:
    """Serialize a graph to a plain node/edge dict (JSON-friendly, version-stable)."""
    nodes = [{"id": node, **attrs} for node, attrs in graph.nodes(data=True)]
    edges = [{"source": u, "target": v, **attrs} for u, v, attrs in graph.edges(data=True)]
    return {"nodes": nodes, "edges": edges}


def from_serializable(data: dict[str, list[dict[str, object]]]) -> nx.Graph:
    """Rebuild a graph from the dict produced by :func:`to_serializable`."""
    nx = _require_networkx()
    graph = nx.Graph()
    for node in data.get("nodes", []):
        node_id = node["id"]
        attrs = {k: v for k, v in node.items() if k != "id"}
        graph.add_node(node_id, **attrs)
    for edge in data.get("edges", []):
        attrs = {k: v for k, v in edge.items() if k not in ("source", "target")}
        graph.add_edge(edge["source"], edge["target"], **attrs)
    return graph


def to_dot(graph: nx.Graph) -> str:
    """Serialize a knowledge graph to Graphviz DOT format."""
    _require_networkx()
    lines = ["graph checkowners {"]
    for node, attrs in graph.nodes(data=True):
        attr_pairs = ", ".join(f'{k}="{_dot_quote(v)}"' for k, v in sorted(attrs.items()))
        attr_str = f" [{attr_pairs}]" if attr_pairs else ""
        lines.append(f'  "{_dot_quote(node)}"{attr_str};')
    seen: set[frozenset[str]] = set()
    for u, v, attrs in graph.edges(data=True):
        edge_id = frozenset({u, v})
        if edge_id in seen:
            continue
        seen.add(edge_id)
        attr_pairs = ", ".join(f'{k}="{_dot_quote(v)}"' for k, v in sorted(attrs.items()))
        attr_str = f" [{attr_pairs}]" if attr_pairs else ""
        lines.append(f'  "{_dot_quote(u)}" -- "{_dot_quote(v)}"{attr_str};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _dot_quote(value: object) -> str:
    """Escape a value for use inside a double-quoted DOT string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def to_text(graph: nx.Graph) -> str:
    """Render the graph as a simple adjacency listing suitable for the terminal."""
    lines: list[str] = []
    nodes_by_kind: dict[str, list[str]] = {"path": [], "contributor": [], "team": []}
    for node, attrs in graph.nodes(data=True):
        kind = attrs.get("kind", "node")
        nodes_by_kind.setdefault(kind, []).append(node)
    for kind in ("team", "contributor", "path"):
        nodes = sorted(nodes_by_kind.get(kind, []))
        if not nodes:
            continue
        lines.append(f"# {kind}s ({len(nodes)})")
        for node in nodes:
            neighbors = sorted(graph.neighbors(node))
            weights = [
                f"{n}({graph[node][n].get('weight', 1.0):.2f})"
                if graph[node][n].get("kind") == "ownership"
                else n
                for n in neighbors
            ]
            lines.append(f"  {node}: {', '.join(weights) if weights else '-'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _contrib_node(handle: str) -> str:
    return f"contrib::{handle}"


def _path_node(path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return f"path::{normalized}"


def _team_node(name: str) -> str:
    return f"team::{name}"

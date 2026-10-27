"""Tests for checkowners.graph module."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import networkx as nx
import pytest

import checkowners.analyze as analyze_module
import checkowners.graph as graph_module
from checkowners.graph import (
    GraphExtraMissingError,
    _dot_quote,
    build_graph,
    from_dot,
    from_serializable,
    graph_analytics,
    python_dependencies,
    to_dot,
    to_gexf,
    to_graphml,
    to_json,
    to_serializable,
    to_text,
    transfer_path,
)
from checkowners.models import (
    Config,
    ContributorStatus,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    TeamCluster,
)
from checkowners.patterns import CodeownersRule
from checkowners.topology import infer_topology

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(
    handle: str,
    confidence: float,
    commits: int = 5,
    *,
    contributor_status: ContributorStatus = "unknown",
) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=confidence,
        last_commit=_NOW,
        commits=commits,
        contributor_status=contributor_status,
    )


def _ownership() -> OwnershipMap:
    return OwnershipMap(
        paths={
            "src/api.py": PathOwnership(
                owners=(_entry("@alice", 0.9), _entry("@bob", 0.6)),
                qualified_owner_count=2,
            ),
            "src/db.py": PathOwnership(
                owners=(_entry("@carol", 0.85),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
    )


def test_build_graph_creates_contributor_and_path_nodes() -> None:
    graph = build_graph(_ownership())
    assert graph.has_node("contrib::@alice")
    assert graph.has_node("contrib::@bob")
    assert graph.has_node("contrib::@carol")
    assert graph.has_node("path::/src/api.py")
    assert graph.has_node("path::/src/db.py")


def test_build_graph_edge_weight_matches_confidence() -> None:
    graph = build_graph(_ownership())
    data = graph["contrib::@alice"]["path::/src/api.py"]
    assert data["weight"] == 0.9
    assert data["kind"] == "OWNS_OBSERVED"


def test_build_graph_includes_clusters() -> None:
    clusters = (
        TeamCluster(
            name="backend",
            members=("@alice", "@bob"),
            primary_paths=("src/api.py",),
            declared=True,
        ),
    )
    graph = build_graph(_ownership(), clusters=clusters)
    assert graph.has_node("team::backend")
    assert graph.has_edge("contrib::@alice", "team::backend")
    assert graph.has_edge("team::backend", "path::/src/api.py")


def test_serializable_roundtrip_preserves_nodes_and_edges() -> None:
    original = build_graph(_ownership())
    data = to_serializable(original)
    restored = from_serializable(data)
    assert set(restored.nodes) == set(original.nodes)
    assert set(map(frozenset, restored.edges)) == set(map(frozenset, original.edges))
    # Edge attributes survive the round trip.
    assert restored["contrib::@alice"]["path::/src/api.py"]["weight"] == 0.9


def test_to_text_lists_partitions() -> None:
    text = to_text(build_graph(_ownership()))
    assert "# contributors (3)" in text
    assert "# paths (2)" in text
    assert "@alice" in text


def test_to_graphml_round_trip() -> None:
    original = build_graph(_ownership())
    xml = to_graphml(original)
    assert xml.lstrip().startswith("<?xml")
    restored = nx.read_graphml(io.BytesIO(xml.encode("utf-8")))
    assert set(restored.nodes) == set(original.nodes)
    assert set(map(frozenset, restored.edges)) == set(map(frozenset, original.edges))
    weight = restored["contrib::@alice"]["path::/src/api.py"]["weight"]
    assert isinstance(weight, int | float)
    assert weight == 0.9


def test_to_dot_emits_graphviz_format() -> None:
    dot = to_dot(build_graph(_ownership()))
    assert "graph" in dot or "digraph" in dot
    assert "contrib::@alice" in dot


def test_dot_quote_escapes_backslash_and_quote() -> None:
    assert _dot_quote('say "hi"') == 'say \\"hi\\"'
    assert _dot_quote("a\\b") == "a\\\\b"
    assert _dot_quote('a\\"b') == 'a\\\\\\"b'
    assert _dot_quote("plain") == "plain"
    assert _dot_quote(0.9) == "0.9"


def test_to_dot_escapes_node_ids_and_attribute_values() -> None:
    ownership = OwnershipMap(
        paths={
            'src/we"ird\\file.py': PathOwnership(
                owners=(_entry('@ali"ce', 0.9),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
    )
    clusters = (
        TeamCluster(
            name='team "core"',
            members=('@ali"ce',),
            primary_paths=('src/we"ird\\file.py',),
            declared=False,
        ),
    )
    dot = to_dot(build_graph(ownership, clusters=clusters))
    # No raw (unescaped) double quote or backslash may survive inside quotes.
    assert '"path::/src/we\\"ird\\\\file.py"' in dot
    assert '"contrib::@ali\\"ce"' in dot
    assert '"team::team \\"core\\""' in dot
    assert 'path::/src/we"ird' not in dot
    restored = from_dot(dot)
    assert 'path::/src/we"ird\\file.py' in restored
    assert 'contrib::@ali"ce' in restored
    assert 'team::team "core"' in restored


def test_missing_extra_raises_clear_error() -> None:
    missing = GraphExtraMissingError("missing")
    with (
        patch.object(graph_module, "_require_networkx", side_effect=missing),
        pytest.raises(GraphExtraMissingError),
    ):
        build_graph(_ownership())
    assert analyze_module.run_git
    with (
        patch.object(graph_module, "_require_networkx", side_effect=missing),
        pytest.raises(GraphExtraMissingError),
    ):
        infer_topology(_ownership(), Config())


def test_exports_round_trip_nodes_edges_and_weight() -> None:
    original = build_graph(_ownership())
    document = json.loads(to_json(original, graph_analytics(original, _ownership(), Config())))
    assert "analytics" in document
    restored_json = from_serializable(document)
    restored_dot = from_dot(to_dot(original))
    restored_graphml = nx.read_graphml(io.BytesIO(to_graphml(original).encode("utf-8")))
    restored_gexf = nx.read_gexf(io.BytesIO(to_gexf(original).encode("utf-8")))
    for restored in (restored_json, restored_dot, restored_graphml, restored_gexf):
        assert set(restored.nodes) == set(original.nodes)
        assert set(map(frozenset, restored.edges)) == set(map(frozenset, original.edges))
        weight = restored["contrib::@alice"]["path::/src/api.py"]["weight"]
        assert isinstance(weight, int | float)
        assert weight == 0.9


def test_articulation_point_splits_directories() -> None:
    ownership = OwnershipMap(
        paths={
            "api/a.py": PathOwnership(
                owners=(_entry("@alice", 0.9), _entry("@bob", 0.8)),
                qualified_owner_count=2,
            ),
            "db/b.py": PathOwnership(
                owners=(_entry("@alice", 0.9), _entry("@carol", 0.8)),
                qualified_owner_count=2,
            ),
        },
        last_analyzed=_NOW,
    )
    graph = build_graph(ownership)
    report = graph_analytics(graph, ownership, Config())
    points = {item.person: item.directories for item in report.articulation_points}
    assert points["contrib::@alice"] == ("api", "db")
    result = transfer_path(graph, "@bob", "@carol")
    assert result.missing == ()
    assert "contrib::@alice" in result.nodes
    assert "path::/api/a.py" in result.nodes
    assert "path::/db/b.py" in result.nodes
    assert result.nodes[0] == "contrib::@bob"
    assert result.nodes[-1] == "contrib::@carol"


def test_former_owner_orphan_lists_python_dependent(tmp_path: Path) -> None:
    package = tmp_path / "src"
    package.mkdir()
    (package / "legacy.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "app.py").write_text("from src.legacy import VALUE\n", encoding="utf-8")
    ownership = OwnershipMap(
        paths={
            "src/legacy.py": PathOwnership(
                owners=(_entry("@carol", 0.9, contributor_status="former"),),
                qualified_owner_count=1,
            ),
            "src/app.py": PathOwnership(
                owners=(_entry("@alice", 0.9),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
    )
    edges = python_dependencies(tmp_path, tuple(ownership.paths))
    assert edges == (("src/app.py", "src/legacy.py"),)
    graph = build_graph(ownership, repo_root=tmp_path)
    report = graph_analytics(graph, ownership, Config())
    assert report.dependency_graph == "available"
    assert len(report.orphans) == 1
    assert report.orphans[0].paths == ("src/legacy.py",)
    assert report.orphans[0].dependents == ("src/app.py",)
    data = graph["contrib::@carol"]["path::/src/legacy.py"]
    assert "DECAYING_EXPERTISE" in data["relations"]


def test_declared_rule_adds_team_and_rule_nodes() -> None:
    rules = (
        CodeownersRule(
            pattern="src/api.py",
            owners=("@acme/backend", "@alice"),
            line_number=4,
        ),
    )
    graph = build_graph(_ownership(), rules=rules)
    assert graph.has_node("rule::4")
    assert graph.has_node("team::@acme/backend")
    assert graph.has_edge("team::@acme/backend", "rule::4")
    assert "OWNS_DECLARED" in graph["rule::4"]["path::/src/api.py"]["relations"]

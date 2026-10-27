"""Tests for checkowners.graph module."""

from __future__ import annotations

import builtins
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
    ArticulationPoint,
    BetweennessScore,
    GraphAnalytics,
    GraphExtraMissingError,
    OrphanSubgraph,
    TransferResult,
    _add_relation,
    _dot_quote,
    _split_directories,
    build_graph,
    endpoint_is_contributor,
    format_analytics,
    format_transfer,
    from_dot,
    from_serializable,
    graph_analytics,
    python_dependencies,
    resolve_node,
    to_dot,
    to_gexf,
    to_graphml,
    to_json,
    to_serializable,
    to_text,
    transfer_path,
)
from checkowners.models import (
    ConfidenceScore,
    Config,
    ContributorStatus,
    FreshnessStatus,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    SignalScore,
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
    review: float | None = None,
    freshness: FreshnessStatus | None = None,
) -> OwnerEntry:
    breakdown = None
    if review is not None:
        breakdown = ConfidenceScore(
            total=confidence,
            recency=SignalScore(available=True, score=1.0),
            frequency=SignalScore(available=True, score=1.0),
            blame=SignalScore(available=True, score=1.0),
            review=SignalScore(available=True, score=review),
        )
    recorded = None
    if freshness is not None:
        recorded = OwnershipFreshness(
            active_expertise=0.0,
            historical_expertise=confidence,
            maintenance_recency=0.0,
            status=freshness,
            half_life_days=180.0,
        )
    return OwnerEntry(
        handle=handle,
        ownership_score=confidence,
        last_commit=_NOW,
        commits=commits,
        contributor_status=contributor_status,
        score_breakdown=breakdown,
        freshness=recorded,
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


def test_require_networkx_names_the_install_extra() -> None:
    real_import = builtins.__import__

    def guarded(name: str, *args: object, **kwargs: object) -> object:
        if name == "networkx" or name.startswith("networkx."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    with (
        patch("builtins.__import__", side_effect=guarded),
        pytest.raises(GraphExtraMissingError, match=r"checkowners\[graph\]"),
    ):
        graph_module._require_networkx()


def test_python_dependencies_skip_unresolved_and_broken_files(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    nested = tmp_path / "deep" / "nest"
    nested.mkdir(parents=True)
    (tmp_path / "top.py").write_text(
        "import pkg.mod\nfrom pkg import sub\nfrom . import ghost\n",
        encoding="utf-8",
    )
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from .mod import thing\nfrom . import sub\n",
        encoding="utf-8",
    )
    (package / "mod.py").write_text("import json\n", encoding="utf-8")
    (package / "sub.py").write_text("from pkg.mod import *\n", encoding="utf-8")
    (package / "bad.py").write_text("def (\n", encoding="utf-8")
    (nested / "rel.py").write_text("from .... import missing\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x\n", encoding="utf-8")
    edges = python_dependencies(
        tmp_path,
        (
            "top.py",
            "__init__.py",
            "pkg/__init__.py",
            "pkg/mod.py",
            "pkg/sub.py",
            "pkg/bad.py",
            "pkg/missing.py",
            "deep/nest/rel.py",
            "notes.txt",
        ),
    )
    assert ("top.py", "pkg/mod.py") in edges
    assert ("top.py", "pkg/sub.py") in edges
    assert ("top.py", "pkg/__init__.py") in edges
    assert ("pkg/__init__.py", "pkg/mod.py") in edges
    assert ("pkg/__init__.py", "pkg/sub.py") in edges
    assert ("pkg/sub.py", "pkg/mod.py") in edges
    assert all("bad.py" not in item for edge in edges for item in edge)
    assert all("notes.txt" not in item for edge in edges for item in edge)


def test_graph_records_repository_reviews_and_nested_directories() -> None:
    ownership = OwnershipMap(
        paths={
            "README": PathOwnership(
                owners=(_entry("@ann", 0.4, commits=0, freshness="departed"),),
                qualified_owner_count=1,
            ),
            "a/b/c.py": PathOwnership(
                owners=(_entry("@bob", 0.1), _entry("@alice", 0.9, review=0.4)),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
    )
    rules = (
        CodeownersRule(pattern="README", owners=(), line_number=1),
        CodeownersRule(pattern="a/b/c.py", owners=("@alice",), line_number=3),
    )
    bare = build_graph(
        OwnershipMap(
            paths={
                "README": PathOwnership(
                    owners=(_entry("@ann", 0.4),),
                    qualified_owner_count=1,
                ),
            },
            last_analyzed=_NOW,
        )
    )
    assert all(not str(node).startswith("repo::") for node in bare.nodes)
    graph = build_graph(ownership, rules=rules, repository="acme/app")
    assert graph.has_node("repo::acme/app")
    assert graph.has_edge("repo::acme/app", "path::/README")
    assert graph.has_edge("dir::a", "dir::a/b")
    assert graph.has_edge("repo::acme/app", "dir::a")
    readme = graph["contrib::@ann"]["path::/README"]
    assert "AUTHORED" not in readme["relations"]
    assert "DECAYING_EXPERTISE" in readme["relations"]
    reviewed = graph["contrib::@alice"]["path::/a/b/c.py"]
    assert reviewed["review_score"] == 0.4
    assert "REVIEWS" in reviewed["relations"]
    _add_relation(
        graph,
        "contrib::@alice",
        "path::/a/b/c.py",
        "REVIEWS",
        weight=0.2,
        review_score=0.4,
    )
    assert graph["contrib::@alice"]["path::/a/b/c.py"]["weight"] == 0.9
    assert not graph.has_node("rule::1")
    assert graph.has_node("rule::3")
    report = graph_analytics(graph, ownership, Config())
    assert report.orphans[0].paths == ("README",)


def test_analytics_and_parsers_cover_edge_inputs() -> None:
    ownership = OwnershipMap(
        paths={
            "api/a.py": PathOwnership(
                owners=(_entry("@alice", 0.9), _entry("@bob", 0.8)),
                qualified_owner_count=2,
            ),
            "api/b.py": PathOwnership(
                owners=(_entry("@alice", 0.9), _entry("@bob", 0.8)),
                qualified_owner_count=2,
            ),
            "db/c.py": PathOwnership(
                owners=(_entry("@alice", 0.9), _entry("@carol", 0.8)),
                qualified_owner_count=2,
            ),
        },
        last_analyzed=_NOW,
    )
    graph = build_graph(
        ownership,
        clusters=(
            TeamCluster(
                name="owners",
                members=("@alice",),
                primary_paths=("api/a.py",),
                declared=True,
            ),
        ),
    )
    assert from_dot(to_dot(graph)).nodes["team::owners"]["declared"] is True
    graph.add_node(5, kind="path")
    graph.add_edge(
        "contrib::@alice",
        "contrib::@bob",
        kind="OWNS_OBSERVED",
        relations="OWNS_OBSERVED",
    )
    graph.add_edge(
        "path::/api/a.py",
        "path::/db/c.py",
        kind="DEPENDS_ON",
        relations="DEPENDS_ON",
        dependent="db/c.py",
        dependency=1,
    )
    report = graph_analytics(graph, ownership, Config())
    assert any(point.person == "contrib::@alice" for point in report.articulation_points)
    rooted = nx.Graph()
    rooted.add_node("contrib::@alice", kind="contributor")
    rooted.add_node("path::/top.py", kind="path")
    rooted.add_node("path::/db/c.py", kind="path")
    rooted.add_edge("contrib::@alice", "path::/top.py", relations="OWNS_OBSERVED")
    rooted.add_edge("contrib::@alice", "path::/db/c.py", relations="OWNS_OBSERVED")
    assert _split_directories(rooted, "contrib::@alice") == ("db",)
    graph.remove_node(5)
    text = format_analytics(
        GraphAnalytics(
            articulation_points=(
                ArticulationPoint("contrib::@alice", ("api",)),
                ArticulationPoint("solo", ()),
            ),
            betweenness=(BetweennessScore("contrib::@alice", 0.5),),
            orphans=(OrphanSubgraph("", ("README",), ("app.py",)),),
        )
    )
    assert "alice: api" in text
    assert "solo: -" in text
    assert "0.5000" in text
    assert "dependents: app.py" in text
    assert format_transfer(TransferResult()) == ""
    assert format_transfer(TransferResult(nodes=("plain",))) == "plain"
    assert endpoint_is_contributor(graph, "@missing") is False
    assert resolve_node(graph, "contrib::@alice") == "contrib::@alice"
    moved = transfer_path(graph, "team::owners", "api/a.py")
    assert moved.missing == ()
    assert moved.nodes[0] == "team::owners"
    assert "analytics" not in json.loads(to_json(graph))
    restored = from_serializable(
        {
            "nodes": ["nope", {"id": 1}, {"id": "kept", "kind": "path"}],
            "edges": [
                "nope",
                {"source": 1, "target": "kept"},
                {"source": "kept", "target": "other"},
            ],
        }
    )
    assert "kept" in restored
    assert from_serializable({"nodes": None, "edges": None}).number_of_nodes() == 0
    plain = from_dot('graph checkowners {\n  "solo"\n}\n')
    assert "solo" in plain
    open_bracket = from_dot('graph checkowners {\n  "solo" [declared="True"\n}\n')
    assert open_bracket.nodes["solo"]["declared"] is True
    escaped = from_dot('graph checkowners {\n  "solo" [pattern="a\\\\b"];\n}\n')
    assert escaped.nodes["solo"]["pattern"] == "a\\b"
    with pytest.raises(ValueError, match="quoted"):
        from_dot("graph checkowners {\n  bare;\n}\n")
    with pytest.raises(ValueError, match="unterminated"):
        from_dot('graph checkowners {\n  "abc;\n}\n')
    observed = graph["contrib::@alice"]["path::/api/a.py"]
    observed["weight"] = True
    assert "(1.00)" in to_text(graph)
    observed["weight"] = 2
    assert "(2.00)" in to_text(graph)
    observed["weight"] = "n/a"
    assert "(1.00)" in to_text(graph)
    observed["relations"] = 1
    graph.nodes["path::/api/a.py"]["kind"] = 1
    listing = to_text(graph)
    assert "path::/api/a.py" in listing

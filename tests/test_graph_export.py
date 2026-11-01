"""Exporting the graph, and the analytics it reports."""

from __future__ import annotations

import io
import json

import networkx as nx
import pytest

from checkowners.config import (
    Config,
)
from checkowners.graph import (
    ArticulationPoint,
    BetweennessScore,
    GraphAnalytics,
    OrphanSubgraph,
    TransferResult,
    build_graph,
    endpoint_is_contributor,
    format_analytics,
    format_transfer,
    from_dot,
    from_serializable,
    graph_analytics,
    resolve_node,
    to_dot,
    to_gexf,
    to_graphml,
    to_json,
    to_text,
    transfer_path,
)
from checkowners.models import (
    OwnershipMap,
    PathOwnership,
    TeamCluster,
)
from tests.test_graph import (
    _NOW,
    _entry,
    _ownership,
)


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

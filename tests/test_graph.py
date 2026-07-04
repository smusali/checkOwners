"""Tests for checkowners.graph module."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

import checkowners.graph as graph_module
from checkowners.graph import (
    GraphExtraMissingError,
    _dot_quote,
    build_graph,
    from_serializable,
    to_dot,
    to_serializable,
    to_text,
)
from checkowners.models import (
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    TeamCluster,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float, commits: int = 5) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=_NOW, commits=commits)


def _ownership() -> OwnershipMap:
    return OwnershipMap(
        paths={
            "src/api.py": PathOwnership(
                owners=(_entry("@alice", 0.9), _entry("@bob", 0.6)),
                bus_factor=2,
            ),
            "src/db.py": PathOwnership(
                owners=(_entry("@carol", 0.85),),
                bus_factor=1,
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
    assert data["kind"] == "ownership"


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
                bus_factor=1,
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


def test_missing_extra_raises_clear_error() -> None:
    missing = GraphExtraMissingError("missing")
    with (
        patch.object(graph_module, "_require_networkx", side_effect=missing),
        pytest.raises(GraphExtraMissingError),
    ):
        build_graph(_ownership())

"""Tests for checkowners.topology module."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import networkx as nx

from checkowners.github import declared_teams_from_github
from checkowners.models import (
    AnalysisConfig,
    ConfidenceScore,
    Config,
    GithubConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    SignalScore,
)
from checkowners.topology import (
    _affiliate,
    _comparisons,
    _cover,
    _jaccard,
    _levels,
    _mean_match,
    _parent_names,
    infer_topology,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(
    handle: str,
    confidence: float,
    commits: int = 5,
    *,
    review: float | None = None,
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
    return OwnerEntry(
        handle=handle,
        ownership_score=confidence,
        last_commit=_NOW,
        commits=commits,
        score_breakdown=breakdown,
    )


def _ownership(raw: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    return OwnershipMap(
        paths={
            p: PathOwnership(owners=owners, qualified_owner_count=len(owners))
            for p, owners in raw.items()
        },
        last_analyzed=_NOW,
    )


def _config() -> Config:
    return Config(analysis=AnalysisConfig(confidence_threshold=0.0))


def test_infer_topology_no_owners_returns_empty() -> None:
    report = infer_topology(_ownership({}), _config())
    assert report.clusters == ()
    assert report.mismatches == ()
    assert report.experimental is True
    assert report.algorithm == "louvain"
    assert report.seed == 0
    assert report.stability_available is False
    assert "co_change" in report.signals_unavailable
    assert "temporal_collaboration" in report.signals_unavailable


def test_infer_topology_two_independent_clusters() -> None:
    ownership = _ownership(
        {
            "api/one.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
            "api/two.py": (_entry("@alice", 0.9), _entry("@bob", 0.7)),
            "db/one.py": (_entry("@carol", 0.85), _entry("@dave", 0.7)),
        }
    )
    report = infer_topology(ownership, _config())
    cluster_sets = [set(cluster.members) for cluster in report.clusters]
    assert {"@alice", "@bob"} in cluster_sets
    assert {"@carol", "@dave"} in cluster_sets


def test_infer_topology_primary_paths_assigned() -> None:
    ownership = _ownership(
        {
            "api/one.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
            "db/one.py": (_entry("@carol", 0.85),),
        }
    )
    report = infer_topology(ownership, _config())
    paths_by_cluster = {tuple(c.members): c.primary_paths for c in report.clusters}
    assert paths_by_cluster[("@alice", "@bob")] == ("api/one.py",)
    assert paths_by_cluster[("@carol",)] == ("db/one.py",)


def test_infer_topology_matches_declared_team() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
        }
    )
    declared = {"backend": frozenset({"alice", "bob"})}
    report = infer_topology(ownership, _config(), declared_teams=declared)
    assert report.clusters[0].name == "backend"
    assert report.clusters[0].declared is True


def test_infer_topology_flags_membership_mismatch() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
        }
    )
    declared = {"backend": frozenset({"alice", "carol"})}
    report = infer_topology(ownership, _config(), declared_teams=declared)
    assert report.clusters[0].declared is False
    assert report.comparisons[0].team == "backend"
    assert report.comparisons[0].jaccard == 0.3333
    assert report.comparisons[0].precision == 0.5
    assert report.comparisons[0].recall == 0.5
    assert report.comparisons[0].coverage == 0.5
    assert report.comparisons[0].cross_team == ("@bob",)
    assert any("backend" in line for line in report.mismatches)


def test_infer_topology_reports_mismatch_per_overlapping_team() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
        }
    )
    declared = {
        "backend": frozenset({"alice", "carol"}),
        "frontend": frozenset({"bob", "dave"}),
        "infra": frozenset({"erin"}),
    }
    report = infer_topology(ownership, _config(), declared_teams=declared)
    by_team = {item.team: item for item in report.comparisons}
    assert by_team["backend"].jaccard == 0.3333
    assert by_team["frontend"].recall == 0.5
    assert by_team["infra"].jaccard == 0.0
    assert by_team["infra"].coverage == 0.0
    assert len(report.mismatches) == 2
    assert any("'backend'" in line for line in report.mismatches)
    assert any("'frontend'" in line for line in report.mismatches)
    assert not any("'infra'" in line for line in report.mismatches)


def test_chain_does_not_form_one_cluster() -> None:
    ownership = _ownership(
        {
            "a/file.py": (_entry("@alice", 0.9), _entry("@bob", 0.9)),
            "b/file.py": (_entry("@bob", 0.9), _entry("@carol", 0.9)),
            "c/file.py": (_entry("@carol", 0.9), _entry("@dave", 0.9)),
        }
    )
    report = infer_topology(ownership, _config())
    assert len(report.clusters) > 1
    for cluster in report.clusters:
        union = set(cluster.members) | set(cluster.affiliated_members)
        assert not {"@alice", "@dave"} <= union


def test_dense_community_is_recovered() -> None:
    owners = tuple(_entry(handle, 0.9) for handle in ("@alice", "@bob", "@carol", "@dave"))
    ownership = _ownership(
        {
            "pay/a.py": owners,
            "pay/b.py": owners,
            "pay/c.py": owners,
            "other/x.py": (_entry("@erin", 0.9), _entry("@frank", 0.9)),
        }
    )
    report = infer_topology(ownership, _config())
    dense = {"@alice", "@bob", "@carol", "@dave"}
    assert any(dense <= set(cluster.members) for cluster in report.clusters)


def test_topology_is_deterministic_for_a_fixed_seed() -> None:
    ownership = _ownership(
        {
            "a/file.py": (_entry("@alice", 0.9), _entry("@bob", 0.9)),
            "b/file.py": (_entry("@bob", 0.9), _entry("@carol", 0.9)),
            "c/file.py": (_entry("@carol", 0.9), _entry("@dave", 0.9)),
        }
    )
    assert infer_topology(ownership, _config()) == infer_topology(ownership, _config())


def test_topology_reports_stability_across_seeds() -> None:
    ownership = _ownership(
        {
            "a/file.py": (_entry("@alice", 0.9), _entry("@bob", 0.9)),
            "b/file.py": (_entry("@bob", 0.9), _entry("@carol", 0.9)),
            "c/file.py": (_entry("@carol", 0.9), _entry("@dave", 0.9)),
        }
    )
    report = infer_topology(ownership, _config())
    assert report.stability_available is True
    assert 0.0 <= report.stability_mean <= 1.0
    assert report.stability_variance >= 0.0
    assert report.resolution == 1.0
    assert "co_ownership" in report.signals_used
    assert "shared_module" in report.signals_used
    assert "co_review" in report.signals_unavailable
    assert "co_change" in report.signals_unavailable
    assert "temporal_collaboration" in report.signals_unavailable


def test_declared_teams_from_github_disabled_returns_none() -> None:
    config = Config(github=GithubConfig(api_enabled=False, org="myorg"))
    with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test"}):
        assert declared_teams_from_github(config) is None


def test_declared_teams_from_github_no_token_returns_none() -> None:
    config = Config(github=GithubConfig(api_enabled=True, org="myorg"))
    with patch.dict("os.environ", {}, clear=True):
        assert declared_teams_from_github(config) is None


def test_declared_teams_from_github_success() -> None:
    config = Config(github=GithubConfig(api_enabled=True, org="myorg"))
    mock_client = MagicMock()
    with (
        patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test"}),
        patch("checkowners.github.get_github_client", return_value=mock_client),
        patch(
            "checkowners.github._get_org_teams",
            return_value={"backend": {"alice", "bob"}},
        ),
    ):
        result = declared_teams_from_github(config)
    assert result == {"backend": frozenset({"alice", "bob"})}


def test_co_review_zero_weight_and_superset_metrics() -> None:
    reviewed = _ownership(
        {
            "src/api.py": (
                _entry("@bob", 0.9, review=0.4),
                _entry("@alice", 0.8, review=0.2),
            ),
        }
    )
    report = infer_topology(reviewed, _config())
    assert "co_review" in report.signals_used
    assert "co_review" not in report.signals_unavailable
    isolated = _ownership({"README": (_entry("@bob", 0.0), _entry("@alice", 0.0))})
    separate = infer_topology(isolated, _config())
    assert len(separate.clusters) == 2
    paired = _ownership(
        {
            "api/one.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
            "db/one.py": (_entry("@carol", 0.9), _entry("@dave", 0.8)),
        }
    )
    declared = {
        "aaa": frozenset({"carol", "dave"}),
        "all": frozenset({"alice", "bob", "frank"}),
        "ghost": frozenset(),
        "zzz": frozenset({"erin"}),
    }
    compared = infer_topology(paired, _config(), declared_teams=declared)
    by_team = {item.team: item for item in compared.comparisons}
    assert by_team["all"].cross_team == ()
    assert by_team["all"].jaccard == 0.6667
    assert "Potential" not in next(line for line in compared.mismatches if "'all'" in line)
    assert by_team["ghost"].coverage == 0.0
    assert by_team["zzz"].jaccard == 0.0
    assert by_team["aaa"].cluster == "aaa"
    assert _comparisons((), {"solo": frozenset({"alice"})})[0].cluster == ""
    with patch("checkowners.topology.graph_module._measured", return_value=None):
        unmeasured = infer_topology(reviewed, _config())
    assert unmeasured.modularity == 0.0
    with patch("checkowners.topology.STABILITY_SEEDS", (0,)):
        stable = infer_topology(reviewed, _config())
    assert stable.stability_mean == 1.0
    assert stable.stability_variance == 0.0


def test_affiliation_lists_a_person_on_the_other_clique() -> None:
    pairs = (
        ("@alice", "@bob", 10.0),
        ("@bob", "@carol", 10.0),
        ("@alice", "@carol", 10.0),
        ("@dave", "@erin", 10.0),
        ("@erin", "@frank", 10.0),
        ("@dave", "@frank", 10.0),
        ("@alice", "@dave", 6.0),
        ("@alice", "@erin", 6.0),
    )
    ownership = _ownership(
        {
            f"m{index}/file.py": (_entry(left, weight), _entry(right, weight))
            for index, (left, right, weight) in enumerate(pairs)
        }
    )
    report = infer_topology(ownership, _config())
    affiliated = {member for cluster in report.clusters for member in cluster.affiliated_members}
    assert "@alice" in affiliated


def test_finer_level_records_the_coarser_parent() -> None:
    def two_levels(*_args: object, **_kwargs: object) -> object:
        yield [{"@alice"}, {"@bob"}]
        yield [{"@alice", "@carol"}, {"@bob"}]

    ownership = _ownership(
        {"src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.8), _entry("@carol", 0.7))}
    )
    with patch("networkx.community.louvain_partitions", side_effect=two_levels):
        report = infer_topology(ownership, _config())
    parents = {tuple(cluster.members): cluster.parent for cluster in report.clusters}
    assert parents[("@alice",)] == "parent-0"
    assert parents[("@bob",)] == ""
    assert _levels(nx.Graph(), 1.0, 0) == []
    assert _parent_names(nx.Graph(), 1.0, ()) == {}
    person = nx.Graph()
    person.add_edge("@alice", "@bob", weight=1)
    with patch("networkx.community.louvain_partitions", return_value=iter(())):
        assert _levels(person, 1.0, 0) == []
    mixed = nx.Graph()
    mixed.add_nodes_from(["@alice", "@erin", 1])
    assert frozenset({"@erin"}) in _cover(mixed, [frozenset({"@alice"})])
    bridge = nx.Graph()
    bridge.add_edge("@alice", "@bob", weight=4)
    bridge.add_edge("@alice", "@carol", weight=4)
    bridge.add_edge("@alice", "@dave", weight=3)
    bridge.add_edge("@alice", "@erin", weight=3)
    bridge.add_edge("@alice", "@frank", weight="nope")
    bridge.add_edge("@alice", 1, weight=1)
    bridge.add_node(2)
    bridge.add_edge("@gina", "@hal", weight=10)
    bridge.add_edge("@gina", "@ira", weight=1)
    bridge.add_edge("@gina", "@joe", weight=1)
    extra = _affiliate(
        bridge,
        (
            frozenset({"@alice", "@bob", "@carol", "@gina", "@hal"}),
            frozenset({"@dave", "@erin", "@frank", "@ira", "@joe"}),
        ),
    )
    assert "@alice" in extra[1]
    assert _jaccard(set(), set()) == 1.0
    assert _mean_match((), ()) == 1.0
    assert _mean_match((frozenset({"@alice"}),), ()) == 0.0
    assert (
        _mean_match(
            (frozenset({"@alice"}), frozenset({"@bob"}), frozenset({"@carol"})),
            (frozenset({"@alice", "@bob", "@carol"}),),
        )
        > 0
    )


def test_declared_teams_from_github_skips_unavailable_clients() -> None:
    blank = Config(github=GithubConfig(api_enabled=True, org=""))
    with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test"}):
        assert declared_teams_from_github(blank) is None
    enabled = Config(github=GithubConfig(api_enabled=True, org="acme"))
    with (
        patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test"}),
        patch("checkowners.github.get_github_client", return_value=None),
    ):
        assert declared_teams_from_github(enabled) is None

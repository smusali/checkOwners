"""Tests for checkowners.topology module."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from checkowners.models import (
    AnalysisConfig,
    Config,
    GithubConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.topology import (
    _cluster,
    declared_teams_from_github,
    infer_topology,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float, commits: int = 5) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=_NOW, commits=commits)


def _ownership(raw: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    return OwnershipMap(
        paths={
            p: PathOwnership(owners=owners, bus_factor=len(owners)) for p, owners in raw.items()
        },
        last_analyzed=_NOW,
    )


def _config() -> Config:
    return Config(analysis=AnalysisConfig(confidence_threshold=0.0))


def test_infer_topology_no_owners_returns_empty() -> None:
    report = infer_topology(_ownership({}), _config())
    assert report.clusters == ()
    assert report.mismatches == ()


def test_infer_topology_two_independent_clusters() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
            "src/auth.py": (_entry("@alice", 0.9), _entry("@bob", 0.7)),
            "src/db.py": (_entry("@carol", 0.85), _entry("@dave", 0.7)),
        }
    )
    report = infer_topology(ownership, _config())
    cluster_sets = [set(cluster.members) for cluster in report.clusters]
    assert {"@alice", "@bob"} in cluster_sets
    assert {"@carol", "@dave"} in cluster_sets


def test_infer_topology_primary_paths_assigned() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.8)),
            "src/db.py": (_entry("@carol", 0.85),),
        }
    )
    report = infer_topology(ownership, _config())
    paths_by_cluster = {tuple(c.members): c.primary_paths for c in report.clusters}
    assert paths_by_cluster[("@alice", "@bob")] == ("src/api.py",)
    assert paths_by_cluster[("@carol",)] == ("src/db.py",)


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
    assert any("backend" in m for m in report.mismatches)


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
    # The cluster {alice, bob} overlaps both backend and frontend: one line each.
    assert len(report.mismatches) == 2
    assert any("'backend'" in m for m in report.mismatches)
    assert any("'frontend'" in m for m in report.mismatches)
    assert not any("'infra'" in m for m in report.mismatches)


def test_cluster_disconnected_subgraphs() -> None:
    adjacency = {
        "a": {"b"},
        "b": {"a"},
        "c": {"d"},
        "d": {"c"},
        "e": set(),
    }
    clusters = _cluster(adjacency)
    cluster_sets = [c for c in clusters]
    assert {"a", "b"} in cluster_sets
    assert {"c", "d"} in cluster_sets
    assert {"e"} in cluster_sets


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

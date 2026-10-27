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
    declared_teams_from_github,
    infer_topology,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float, commits: int = 5) -> OwnerEntry:
    return OwnerEntry(handle=handle, ownership_score=confidence, last_commit=_NOW, commits=commits)


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

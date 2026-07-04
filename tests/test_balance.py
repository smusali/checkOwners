"""Tests for checkowners.balance module."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from checkowners.balance import _MAX_CLOSED_PRS, _qualified_pairs, analyze_balance
from checkowners.models import (
    AnalysisConfig,
    Config,
    GithubConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
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


def _config(confidence_threshold: float = 0.0) -> Config:
    return Config(
        analysis=AnalysisConfig(confidence_threshold=confidence_threshold),
        github=GithubConfig(api_enabled=False),
    )


def test_analyze_balance_empty_returns_empty() -> None:
    ownership = OwnershipMap(paths={}, last_analyzed=_NOW)
    report = analyze_balance(ownership, _config())
    assert report.loads == ()
    assert report.average == 0.0
    assert report.overloaded == ()
    assert report.suggestions == ()


def test_analyze_balance_authorship_fallback() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9, commits=20), _entry("@bob", 0.8, commits=5)),
            "src/db.py": (_entry("@alice", 0.85, commits=30), _entry("@carol", 0.7, commits=5)),
        }
    )
    report = analyze_balance(ownership, _config())
    handles = {load.handle: load.reviews for load in report.loads}
    assert handles["@alice"] == 50
    assert handles["@bob"] == 5
    assert handles["@carol"] == 5
    assert report.source == "git_authorship"


def test_analyze_balance_detects_overloaded_reviewer() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, commits=80),
                _entry("@bob", 0.8, commits=10),
                _entry("@carol", 0.7, commits=10),
            ),
        }
    )
    report = analyze_balance(ownership, _config())
    overloaded_handles = {load.handle for load in report.overloaded}
    assert "@alice" in overloaded_handles
    assert "@bob" not in overloaded_handles


def test_analyze_balance_external_review_counts() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, commits=1),
                _entry("@bob", 0.8, commits=1),
            ),
        }
    )
    report = analyze_balance(
        ownership,
        _config(),
        review_counts={"@alice": 50, "@bob": 5},
    )
    assert report.source == "external"
    handles = {load.handle: load.reviews for load in report.loads}
    assert handles == {"@alice": 50, "@bob": 5}


def test_analyze_balance_suggests_routing_to_qualified_co_owner() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, commits=100),
                _entry("@bob", 0.7, commits=10),
                _entry("@carol", 0.6, commits=10),
            ),
        }
    )
    report = analyze_balance(ownership, _config())
    overloaded = {load.handle for load in report.overloaded}
    assert "@alice" in overloaded
    candidates = {s.candidate for s in report.suggestions if s.overloaded == "@alice"}
    assert candidates == {"@bob", "@carol"}


def test_analyze_balance_filters_low_confidence_candidates() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, commits=100),
                _entry("@bob", 0.1, commits=10),
            ),
        }
    )
    report = analyze_balance(ownership, _config(confidence_threshold=0.5))
    assert report.suggestions == ()


def test_qualified_pairs_orders_by_confidence() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9),
                _entry("@bob", 0.7),
                _entry("@carol", 0.85),
            ),
        }
    )
    pairs = _qualified_pairs(ownership, 0.5)
    alice_neighbors = [handle for handle, _ in pairs["@alice"]]
    assert alice_neighbors == ["@carol", "@bob"]


def test_suggestions_never_target_overloaded_reviewers() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9),
                _entry("@bob", 0.85),
                _entry("@carol", 0.7),
            ),
        }
    )
    # Both alice and bob are overloaded; carol is the only valid candidate.
    report = analyze_balance(
        ownership,
        _config(),
        review_counts={"@alice": 100, "@bob": 100, "@carol": 5, "@dave": 5, "@eve": 5},
    )
    overloaded_handles = {load.handle for load in report.overloaded}
    assert overloaded_handles == {"@alice", "@bob"}
    assert report.suggestions
    for suggestion in report.suggestions:
        assert suggestion.candidate not in overloaded_handles
    assert {s.candidate for s in report.suggestions} == {"@carol"}


def _github_config() -> Config:
    return Config(
        analysis=AnalysisConfig(confidence_threshold=0.0),
        github=GithubConfig(api_enabled=True, org="myorg"),
    )


def _mock_pull(reviewer_logins: tuple[str, ...]) -> MagicMock:
    pull = MagicMock()
    reviews = []
    for login in reviewer_logins:
        review = MagicMock()
        review.user.login = login
        reviews.append(review)
    pull.get_reviews.return_value = reviews
    return pull


def test_gather_from_github_scopes_to_current_repo() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    mock_repo = MagicMock()
    mock_repo.get_pulls.return_value = [_mock_pull(("alice",)), _mock_pull(("bob", "alice"))]
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo
    with (
        patch.dict(
            "os.environ",
            {"GITHUB_TOKEN": "ghp_test", "GITHUB_REPOSITORY": "myorg/myrepo"},
        ),
        patch("checkowners.github.get_github_client", return_value=mock_client),
    ):
        report = analyze_balance(ownership, _github_config())
    mock_client.get_repo.assert_called_once_with("myorg/myrepo")
    mock_client.get_organization.assert_not_called()
    assert report.source == "github_api"
    assert report.fallback_reason == ""
    handles = {load.handle: load.reviews for load in report.loads}
    assert handles == {"@alice": 2, "@bob": 1}


def test_gather_from_github_bounds_pr_scan() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    pulls = [_mock_pull(("alice",)) for _ in range(_MAX_CLOSED_PRS + 50)]
    mock_repo = MagicMock()
    mock_repo.get_pulls.return_value = pulls
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo
    with (
        patch.dict(
            "os.environ",
            {"GITHUB_TOKEN": "ghp_test", "GITHUB_REPOSITORY": "myorg/myrepo"},
        ),
        patch("checkowners.github.get_github_client", return_value=mock_client),
    ):
        report = analyze_balance(ownership, _github_config())
    handles = {load.handle: load.reviews for load in report.loads}
    assert handles == {"@alice": _MAX_CLOSED_PRS}


def test_gather_from_github_falls_back_without_repository_env() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test"}, clear=True):
        report = analyze_balance(ownership, _github_config())
    assert report.source == "git_authorship"
    assert report.fallback_reason == "GITHUB_REPOSITORY is not set"


def test_gather_from_github_falls_back_without_token() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    with patch.dict("os.environ", {}, clear=True):
        report = analyze_balance(ownership, _github_config())
    assert report.source == "git_authorship"
    assert report.fallback_reason == "GITHUB_TOKEN is not set"


def test_gather_from_github_falls_back_on_api_error() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    mock_client = MagicMock()
    mock_client.get_repo.side_effect = RuntimeError("rate limited")
    with (
        patch.dict(
            "os.environ",
            {"GITHUB_TOKEN": "ghp_test", "GITHUB_REPOSITORY": "myorg/myrepo"},
        ),
        patch("checkowners.github.get_github_client", return_value=mock_client),
    ):
        report = analyze_balance(ownership, _github_config())
    assert report.source == "git_authorship"
    assert report.fallback_reason.startswith("GitHub API error:")
    assert "rate limited" in report.fallback_reason


def test_fallback_reason_empty_on_normal_paths() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    authorship = analyze_balance(ownership, _config())
    assert authorship.fallback_reason == ""
    external = analyze_balance(ownership, _config(), review_counts={"@alice": 3})
    assert external.fallback_reason == ""

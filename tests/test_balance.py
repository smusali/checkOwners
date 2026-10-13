"""Tests for checkowners.balance module."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, call, patch

from checkowners.balance import _MAX_CLOSED_PRS, _qualified_pairs, analyze_balance
from checkowners.models import (
    AnalysisConfig,
    Config,
    GithubConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.patterns import CodeownersRule

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
    assert report.load_signal == "git_authorship_proxy"


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
    assert report.load_signal == "git_authorship_proxy"
    assert report.suggestions == ()
    assert report.overloaded == ()
    assert report.review_concentration == ()
    assert report.divergence == ()
    assert all(load.reviews_completed is None for load in report.loads)
    assert all(load.load_band is None for load in report.loads)


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
    report = analyze_balance(
        ownership,
        _config(),
        review_counts={"@alice": 80, "@bob": 10, "@carol": 10},
    )
    overloaded_handles = {load.handle for load in report.overloaded}
    assert "@alice" in overloaded_handles
    assert "@bob" not in overloaded_handles
    assert report.load_signal == "external"
    proxy = analyze_balance(ownership, _config())
    assert proxy.overloaded == ()
    assert proxy.suggestions == ()
    assert proxy.load_signal == "git_authorship_proxy"


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
    assert report.load_signal == "external"
    handles = {load.handle: load.reviews for load in report.loads}
    completed = {load.handle: load.reviews_completed for load in report.loads}
    assert handles == {"@alice": 50, "@bob": 5}
    assert completed == {"@alice": 50, "@bob": 5}


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
    report = analyze_balance(
        ownership,
        _config(),
        review_counts={"@alice": 100, "@bob": 10, "@carol": 10},
    )
    overloaded = {load.handle for load in report.overloaded}
    assert "@alice" in overloaded
    candidates = [s.candidate for s in report.suggestions if s.overloaded == "@alice"]
    assert candidates == ["@bob"]


def test_analyze_balance_filters_low_confidence_candidates() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, commits=100),
                _entry("@bob", 0.1, commits=10),
            ),
        }
    )
    report = analyze_balance(
        ownership,
        _config(confidence_threshold=0.5),
        review_counts={"@alice": 100, "@bob": 0},
    )
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
    created = datetime(2026, 5, 1, tzinfo=UTC)
    submitted = datetime(2026, 5, 1, 5, tzinfo=UTC)
    pulls = [_mock_pull(("alice",)), _mock_pull(("bob", "alice"))]
    for pull in pulls:
        pull.additions = 10
        pull.deletions = 5
        pull.created_at = created
        pull.requested_reviewers = []
        pull.get_files.return_value = [MagicMock(filename="src/api.py")]
        for review in pull.get_reviews.return_value:
            review.submitted_at = submitted
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
    assert mock_client.get_repo.call_args_list == [call("myorg/myrepo"), call("myorg/myrepo")]
    mock_client.get_organization.assert_not_called()
    assert report.source == "github_api"
    assert report.load_signal == "github_completed_reviews"
    assert report.fallback_reason == ""
    by_handle = {load.handle: load for load in report.loads}
    assert {handle: load.reviews for handle, load in by_handle.items()} == {"@alice": 2, "@bob": 1}
    assert by_handle["@alice"].reviews_completed == 2
    assert by_handle["@alice"].reviews_requested == 2
    assert by_handle["@alice"].prs_waiting == 0
    assert by_handle["@alice"].reviewed_lines == 30
    assert by_handle["@alice"].pr_complexity == 15
    assert by_handle["@alice"].median_response_time_hours == 5
    assert by_handle["@bob"].reviews_completed == 1
    assert by_handle["@bob"].reviewed_lines == 15
    assert report.review_concentration == ()


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
    assert report.load_signal == "git_authorship_proxy"
    assert report.fallback_reason == "GITHUB_REPOSITORY is not set"


def test_gather_from_github_falls_back_without_token() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    with patch.dict("os.environ", {}, clear=True):
        report = analyze_balance(ownership, _github_config())
    assert report.source == "git_authorship"
    assert report.load_signal == "git_authorship_proxy"
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


def test_top_committer_is_not_the_top_reviewer() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.94, commits=50),
                _entry("@bob", 0.81, commits=4),
            ),
        }
    )
    report = analyze_balance(
        ownership,
        _config(),
        review_counts={"@alice": 2, "@bob": 30},
    )
    by_handle = {load.handle: load for load in report.loads}
    assert by_handle["@alice"].reviews_completed == 2
    assert by_handle["@bob"].reviews_completed == 30
    commits = {owner.handle: owner.commits for owner in ownership.paths["src/api.py"].owners}
    assert commits["@alice"] > commits["@bob"]


def test_review_concentration_flags_a_monopoly() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9),
                _entry("@bob", 0.8),
                _entry("@carol", 0.7),
            ),
            "src/solo.py": (_entry("@bob", 0.9),),
        }
    )
    report = analyze_balance(
        ownership,
        _config(confidence_threshold=0.5),
        review_counts={"@bob": 72, "@alice": 28},
        path_reviews={
            "src/api.py": {"@bob": 72, "@alice": 28},
            "src/solo.py": {"@bob": 100},
        },
    )
    flagged = {item.path: item for item in report.review_concentration}
    assert set(flagged) == {"src/api.py"}
    assert flagged["src/api.py"].share == 0.72
    assert flagged["src/api.py"].reviewer == "@bob"
    assert flagged["src/api.py"].knowledgeable_owners == 3


def test_divergence_reports_three_roles() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, commits=40),
                _entry("@bob", 0.8, commits=3),
                _entry("@carol", 0.2, commits=1),
            ),
        }
    )
    report = analyze_balance(
        ownership,
        _config(),
        review_counts={"@alice": 1, "@bob": 9},
        path_reviews={"src/api.py": {"@bob": 9, "@alice": 1}},
        rules=(CodeownersRule(pattern="src/api.py", owners=("@carol",), line_number=1),),
    )
    assert len(report.divergence) == 1
    split = report.divergence[0]
    assert split.writer == "@alice"
    assert split.reviewer == "@bob"
    assert split.declared == "@carol"
    proxy = analyze_balance(ownership, _config())
    assert proxy.divergence == ()


def test_fallback_reason_empty_on_normal_paths() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    authorship = analyze_balance(ownership, _config())
    assert authorship.fallback_reason == ""
    external = analyze_balance(ownership, _config(), review_counts={"@alice": 3})
    assert external.fallback_reason == ""

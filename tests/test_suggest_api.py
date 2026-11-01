"""Review load read from the GitHub API."""

from __future__ import annotations

import pytest

from checkowners.config import (
    AnalysisConfig,
    Config,
    GithubConfig,
)
from checkowners.github import set_offline
from checkowners.models import (
    OwnershipMap,
)
from checkowners.providers import begin_api_budget, clear_api_budget
from checkowners.suggest import BalanceReport, analyze_balance
from tests.conftest import Route, fake_api
from tests.test_suggest import (
    _config,
    _entry,
    _github_config,
    _ownership,
)


def test_gather_from_github_falls_back_without_repository_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    report = analyze_balance(ownership, _github_config())
    assert report.source == "git_authorship"
    assert report.load_signal == "git_authorship_proxy"
    assert report.fallback_reason == "GITHUB_REPOSITORY is not set"


def test_gather_from_github_falls_back_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    report = analyze_balance(ownership, _github_config())
    assert report.source == "git_authorship"
    assert report.load_signal == "git_authorship_proxy"
    assert report.fallback_reason == "GITHUB_TOKEN is not set"


def _pull(
    number: int,
    reviewers: tuple[str, ...],
    *,
    requested: tuple[str, ...] = (),
    created: str = "2026-05-01T00:00:00Z",
    submitted: str = "2026-05-01T05:00:00Z",
    additions: int = 10,
    deletions: int = 5,
    files: tuple[str, ...] = ("src/api.py",),
) -> tuple[Route, ...]:
    """Return the routes that serve one pull request to the review-load scan."""
    return (
        Route(
            "GET",
            f"/repos/myorg/myrepo/pulls/{number}/reviews",
            body=[{"user": {"login": login}, "submitted_at": submitted} for login in reviewers],
        ),
        Route(
            "GET",
            f"/repos/myorg/myrepo/pulls/{number}/files",
            body=[{"filename": name} for name in files],
        ),
        Route(
            "GET",
            "/repos/myorg/myrepo/pulls?state=open",
            body=[
                {
                    "number": number,
                    "created_at": created,
                    "additions": additions,
                    "deletions": deletions,
                    "requested_reviewers": [{"login": login} for login in requested],
                }
            ]
            if requested
            else [],
        ),
    )


def _closed_route(pulls: tuple[tuple[int, str, int, int], ...]) -> Route:
    return Route(
        "GET",
        "/repos/myorg/myrepo/pulls?state=closed",
        body=[
            {
                "number": number,
                "created_at": created,
                "additions": additions,
                "deletions": deletions,
                "requested_reviewers": [],
            }
            for number, created, additions, deletions in pulls
        ],
    )


@pytest.fixture
def _github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "myorg/myrepo")


def _run_github(
    *routes: Route,
    ownership: OwnershipMap,
    config: Config | None = None,
) -> BalanceReport:
    with fake_api(*routes):
        return analyze_balance(ownership, config or _github_config())


@pytest.mark.usefixtures("_github_env")
def test_review_load_comes_from_closed_pulls_and_their_reviews() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    routes = (
        _closed_route(((1, "2026-05-01T00:00:00Z", 10, 5), (2, "2026-05-01T00:00:00Z", 10, 5))),
        *_pull(1, ("alice",)),
        *_pull(2, ("bob", "alice")),
        Route("GET", "/repos/myorg/myrepo/pulls?state=open", body=[]),
    )
    report = _run_github(*routes, ownership=ownership)
    by_handle = {load.handle: load for load in report.loads}
    assert report.source == "github_api"
    assert report.load_signal == "github_completed_reviews"
    assert report.fallback_reason == ""
    assert {handle: load.reviews for handle, load in by_handle.items()} == {"@alice": 2, "@bob": 1}
    assert by_handle["@alice"].reviewed_lines == 30
    assert by_handle["@alice"].pr_complexity == 15
    assert by_handle["@alice"].median_response_time_hours == 5


@pytest.mark.usefixtures("_github_env")
def test_an_open_pull_counts_a_requested_reviewer_as_waiting() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    routes = (
        _closed_route(((1, "2026-05-01T00:00:00Z", 10, 5),)),
        Route("GET", "/repos/myorg/myrepo/pulls/1/reviews", body=[{"user": {"login": "alice"}}]),
        Route("GET", "/repos/myorg/myrepo/pulls/1/files", body=[{"filename": "src/api.py"}]),
        Route(
            "GET",
            "/repos/myorg/myrepo/pulls?state=open",
            body=[{"number": 2, "requested_reviewers": [{"login": "carol"}]}],
        ),
        Route("GET", "/repos/myorg/myrepo/pulls/2/reviews", body=[]),
    )
    report = _run_github(*routes, ownership=ownership)
    by_handle = {load.handle: load for load in report.loads}
    assert by_handle["@carol"].prs_waiting == 1
    assert by_handle["@carol"].reviews == 0


@pytest.mark.usefixtures("_github_env")
def test_ownership_criticality_follows_the_configured_globs() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=10),)})
    config = Config(
        analysis=AnalysisConfig(confidence_threshold=0.5),
        github=GithubConfig(api_enabled=True, org="myorg"),
        criticality=(("docs/**", 0.2), ("src/api.py", 0.4), ("src/**", 1.0)),
    )
    routes = (
        _closed_route(((1, "2026-05-01T00:00:00Z", 3, 2),)),
        *_pull(1, ("alice",)),
        Route("GET", "/repos/myorg/myrepo/pulls?state=open", body=[]),
    )
    report = _run_github(*routes, ownership=ownership, config=config)
    by_handle = {load.handle: load for load in report.loads}
    assert by_handle["@alice"].ownership_criticality == 1.0
    assert by_handle["@alice"].reviewed_lines == 5


@pytest.mark.parametrize(
    ("routes", "reason"),
    [
        ((), "GitHub API error: could not list closed pull requests"),
        (
            (
                _closed_route(((1, "2026-05-01T00:00:00Z", 10, 5),)),
                Route("GET", "/repos/myorg/myrepo/pulls/1/reviews", body=[]),
                Route("GET", "/repos/myorg/myrepo/pulls/1/files", body=[]),
                Route("GET", "/repos/myorg/myrepo/pulls?state=open", body=[]),
            ),
            "no PR reviews found in the last 200 closed PRs",
        ),
    ],
    ids=["list-fails", "no-reviews"],
)
@pytest.mark.usefixtures("_github_env")
def test_a_failed_scan_keeps_the_authorship_proxy(routes: tuple[Route, ...], reason: str) -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    report = _run_github(*routes, ownership=ownership)
    assert report.source == "git_authorship"
    assert report.load_signal == "git_authorship_proxy"
    assert report.fallback_reason == reason


@pytest.mark.usefixtures("_github_env")
def test_a_spent_request_budget_stops_the_scan() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    routes = (_closed_route(((1, "2026-05-01T00:00:00Z", 10, 5),)), *_pull(1, ("alice",)))
    begin_api_budget(0)
    try:
        report = _run_github(*routes, ownership=ownership)
    finally:
        clear_api_budget()
    assert report.fallback_reason == "GitHub API budget insufficient"


@pytest.mark.usefixtures("_github_env")
def test_an_offline_run_keeps_the_authorship_proxy() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    set_offline(True)
    try:
        report = _run_github(ownership=ownership)
    finally:
        set_offline(False)
    assert report.fallback_reason == "GitHub API access is disabled"


def test_fallback_reason_empty_on_normal_paths() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9, commits=3),)})
    authorship = analyze_balance(ownership, _config())
    assert authorship.fallback_reason == ""
    external = analyze_balance(ownership, _config(), review_counts={"@alice": 3})
    assert external.fallback_reason == ""

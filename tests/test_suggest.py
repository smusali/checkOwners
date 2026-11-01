"""Tests for checkowners.suggest module."""

from __future__ import annotations

from datetime import UTC, datetime

from checkowners.config import (
    AnalysisConfig,
    Config,
    GithubConfig,
)
from checkowners.models import (
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.patterns import CodeownersRule
from checkowners.suggest import analyze_balance

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


def test_recommendation_prefers_low_load_and_skips_incomplete_roles() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, commits=40),
                _entry("@frank", 0.99, commits=4),
                _entry("@dave", 0.85, commits=6),
                _entry("@bob", 0.8, commits=3),
                _entry("@gina", 0.2, commits=2),
            ),
            "src/again.py": (_entry("@alice", 0.4, commits=1), _entry("@dave", 0.1, commits=1)),
            "src/same.py": (_entry("@alice", 0.5, commits=10), _entry("@erin", 0.4, commits=2)),
            "src/zero.py": (_entry("@carol", 0.9, commits=0),),
            "src/solo.py": (_entry("@alice", 0.6, commits=8), _entry("@bob", 0.4, commits=1)),
            "src/norule.py": (_entry("@alice", 0.7, commits=4),),
            "src/flat.py": (_entry("@alice", 0.5, commits=2), _entry("@bob", 0.5, commits=2)),
        }
    )
    report = analyze_balance(
        ownership,
        _config(),
        review_counts={"@alice": 40, "@frank": 20, "@dave": 8, "@eve": 2},
        path_reviews={
            "missing.py": {"@alice": 10},
            "src/flat.py": {"@alice": 0},
            "src/api.py": {"@bob": 80, "@alice": 10},
            "src/same.py": {"@alice": 9},
            "src/zero.py": {"@bob": 5},
            "src/solo.py": {"@bob": 9, "@alice": 1},
            "src/norule.py": {"@bob": 3, "@alice": 1},
        },
        rules=(
            CodeownersRule(pattern="src/api.py", owners=("@erin",), line_number=1),
            CodeownersRule(pattern="src/same.py", owners=("@erin",), line_number=2),
            CodeownersRule(pattern="src/solo.py", owners=("@alice",), line_number=3),
        ),
    )
    bands = {load.handle: load.load_band for load in report.loads}
    assert bands["@alice"] == "high"
    assert bands["@frank"] == "typical"
    assert bands["@dave"] == "low"
    assert report.suggestions[0].candidate == "@dave"
    assert "@gina" not in {load.handle for load in report.loads}
    assert {item.path for item in report.review_concentration} == {
        "src/api.py",
        "src/same.py",
        "src/solo.py",
    }
    assert [item.path for item in report.divergence] == ["src/api.py"]
    assert report.divergence[0].declared == "@erin"

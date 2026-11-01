"""Qualification: the adaptive strategy, the commit-count gate, and bot filtering."""

from __future__ import annotations

import pytest

from checkowners.analyze import (
    analyze_ownership,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    QualificationConfig,
    ScoringConfig,
)
from checkowners.scoring import (
    Contribution,
    score_owners,
)
from tests.conftest import (
    NOW,
    OLD,
    RECENT,
    GitRepo,
)

_NO_BLAME = ScoringConfig(blame_weight=0.0, change_survival_weight=0.0)


def test_analyze_min_commits_filter(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("bob@example.com", RECENT, ["src/main.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=2, top_n_owners=5, confidence_threshold=0.0),
        qualification=QualificationConfig(strategy="threshold", min_commits=2),
        scoring=_NO_BLAME,
    )
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    handles = [owner.handle for owner in result.paths["src/main.py"].owners]
    assert handles == ["alice@example.com"]


def test_analyze_adaptive_keeps_high_blame_author(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/main.py": "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n"},
        "alice seed",
        author="Alice",
        email="alice@example.com",
        date=RECENT.isoformat(),
    )
    git_repo.commit_files(
        {"src/main.py": "line1\nline2\nline3\n"},
        "alice trim",
        author="Alice",
        email="alice@example.com",
        date=RECENT.isoformat(),
    )
    git_repo.commit_files(
        {"src/main.py": "line1\n"},
        "alice again",
        author="Alice",
        email="alice@example.com",
        date=RECENT.isoformat(),
    )
    git_repo.commit_files(
        {
            "src/main.py": (
                "bob1\nbob2\nbob3\nbob4\nbob5\nbob6\nbob7\nbob8\nbob9\nbob10\nbob11\nbob12\n"
            )
        },
        "bob rewrite",
        author="Bob",
        email="bob@example.com",
        date=RECENT.isoformat(),
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=2, top_n_owners=5, confidence_threshold=0.0),
        qualification=QualificationConfig(strategy="adaptive", min_commits=2),
    )
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    handles = {owner.handle for owner in result.paths["src/main.py"].owners}
    assert handles == {"alice@example.com", "bob@example.com"}


def test_bot_authors_excluded_by_default(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("49699333+dependabot[bot]@users.noreply.github.com", RECENT, ["deps.txt"]),
            ("49699333+dependabot[bot]@users.noreply.github.com", RECENT, ["deps.txt"]),
            ("49699333+dependabot[bot]@users.noreply.github.com", RECENT, ["deps.txt"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
        scoring=_NO_BLAME,
    )
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    assert set(result.paths) == {"src/main.py"}


def test_bot_authors_kept_when_disabled(git_repo: GitRepo) -> None:
    git_repo.seed_history([("github-actions[bot]@users.noreply.github.com", RECENT, ["gen.md"])])
    config = Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0, exclude_bots=False),
        scoring=_NO_BLAME,
    )
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    assert set(result.paths) == {"gen.md"}


def test_adaptive_single_commit_full_blame_is_owner(git_repo: GitRepo) -> None:
    git_repo.seed_history([("alice@example.com", RECENT, ["new.py"])])
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.3))
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    owner = result.paths["new.py"].owners[0]
    assert owner.handle == "alice@example.com"
    assert owner.ownership_score > 0.3
    reasons = {gap.code: gap.reason for gap in result.analysis_completeness.gaps}
    assert reasons["missing_mailmap"] == "Missing .mailmap."
    assert reasons["missing_ignore_revs"] == "Missing .git-blame-ignore-revs."
    assert reasons["review_history"] == "Review history unavailable."


def test_adaptive_squash_merge_produces_owners(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"svc.py": "alice\nbob-line\n"},
        "alice",
        author="Alice",
        email="alice@example.com",
        date=RECENT.isoformat(),
    )
    git_repo.commit_files(
        {"svc.py": "alice\nbob\nbob2\nbob3\n"},
        "bob",
        author="Bob",
        email="bob@example.com",
        date=RECENT.isoformat(),
    )
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.0))
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    assert "svc.py" in result.paths
    assert {owner.handle for owner in result.paths["svc.py"].owners} == {
        "alice@example.com",
        "bob@example.com",
    }


def test_adaptive_low_blame_scores_below_high_blame() -> None:
    scoring = ScoringConfig()
    low = score_owners(
        {"alice@example.com": Contribution(commits=1, last_commit=RECENT, credit=1.0)},
        {"alice@example.com": 0.03},
        {},
        max_commits=1,
        scoring=scoring,
        now=NOW,
        blame_available=True,
        review_available=False,
        frequency_prior=3.0,
    )
    high = score_owners(
        {"alice@example.com": Contribution(commits=1, last_commit=RECENT, credit=1.0)},
        {"alice@example.com": 0.95},
        {},
        max_commits=1,
        scoring=scoring,
        now=NOW,
        blame_available=True,
        review_available=False,
        frequency_prior=3.0,
    )
    assert low[0].ownership_score < high[0].ownership_score
    assert high[0].ownership_score > 0.3
    assert high[0].evidence_quality == pytest.approx(0.70)


def test_retain_all_keeps_below_threshold_candidates(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("bob@example.com", OLD, ["src/main.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=3, confidence_threshold=0.5),
        scoring=_NO_BLAME,
    )
    kept = analyze_ownership(git_repo.path, config, as_of=NOW, retain_all=True)
    dropped = analyze_ownership(git_repo.path, config, as_of=NOW)
    path_ownership = kept.paths["src/main.py"]
    assert all(owner.confidence >= 0.5 for owner in path_ownership.owners)
    assert any(candidate.confidence < 0.5 for candidate in path_ownership.candidates)
    assert "bob@example.com" not in {owner.handle for owner in path_ownership.owners}
    assert "bob@example.com" in {candidate.handle for candidate in path_ownership.candidates}
    assert dropped.paths["src/main.py"].candidates == ()

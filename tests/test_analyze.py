"""Tests for checkowners.analyze module."""

from __future__ import annotations

from datetime import timedelta

import pytest

from checkowners.analyze import (
    analyze_ownership,
    apply_completeness,
)
from checkowners.blame import (
    BlamePass,
    gather_blame_coverage,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    DecayConfig,
    ScoringConfig,
)
from checkowners.drift import detect_drift
from checkowners.explain import signal_tuples
from checkowners.models import (
    AnalysisCompleteness,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.scoring import (
    combine_available_signals,
    signal_reliabilities,
    signal_weights,
)
from tests.conftest import (
    NOW,
    OLD,
    RECENT,
    GitRepo,
    write_codeowners,
)

_NO_BLAME = ScoringConfig(blame_weight=0.0, change_survival_weight=0.0)


def _config(*, top_n_owners: int = 3, lookback_days: int = 365) -> Config:
    return Config(
        analysis=AnalysisConfig(
            min_commits=1,
            confidence_threshold=0.0,
            top_n_owners=top_n_owners,
            lookback_days=lookback_days,
        ),
        scoring=_NO_BLAME,
    )


def test_analyze_basic_confidence_scoring(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["src/main.py", "src/utils.py"]),
            ("bob@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py", "src/utils.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
        ]
    )
    result = analyze_ownership(git_repo.path, _config(top_n_owners=2), as_of=NOW)
    main_owners = result.paths["src/main.py"].owners
    assert main_owners[0].handle == "alice@example.com"
    assert main_owners[0].confidence > main_owners[1].confidence
    assert {owner.handle for owner in main_owners} == {"alice@example.com", "bob@example.com"}
    assert result.paths["src/utils.py"].owners[0].handle == "alice@example.com"


def test_analyze_review_provider_feeds_review_factor(git_repo: GitRepo) -> None:
    git_repo.seed_history([("alice@example.com", RECENT, ["src/main.py"])] * 3)
    captured: dict[str, set[str]] = {}

    def provider(emails: set[str]) -> dict[str, dict[str, float]]:
        captured["emails"] = emails
        return {"src/main.py": {"alice@example.com": 1.0}}

    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=2, confidence_threshold=0.0),
        scoring=ScoringConfig(review_weight=0.5, blame_weight=0.0, change_survival_weight=0.0),
    )
    result = analyze_ownership(git_repo.path, config, review_provider=provider, as_of=NOW)
    alice = result.paths["src/main.py"].owners[0]
    assert alice.score_breakdown is not None
    assert alice.score_breakdown.review.available is True
    assert alice.score_breakdown.review.score == 1.0
    assert captured["emails"] == {"alice@example.com"}


def test_analyze_review_factor_zero_without_provider(git_repo: GitRepo) -> None:
    git_repo.seed_history([("alice@example.com", RECENT, ["src/main.py"])])
    result = analyze_ownership(git_repo.path, _config(), as_of=NOW)
    breakdown = result.paths["src/main.py"].owners[0].score_breakdown
    assert breakdown is not None
    assert breakdown.review.available is False
    assert "score" not in breakdown.review.as_payload()


def test_analyze_lookback_days(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", OLD, ["src/old.py"]),
            ("alice@example.com", RECENT, ["src/new.py"]),
        ]
    )
    result = analyze_ownership(git_repo.path, _config(lookback_days=90), as_of=NOW)
    assert "src/new.py" in result.paths
    assert "src/old.py" not in result.paths


def test_analyze_top_n_owners(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [("alice@example.com", RECENT, ["f.py"])] * 10
        + [("bob@example.com", RECENT, ["f.py"])] * 7
        + [("carol@example.com", RECENT, ["f.py"])] * 3
    )
    result = analyze_ownership(git_repo.path, _config(top_n_owners=2), as_of=NOW)
    assert [owner.handle for owner in result.paths["f.py"].owners] == [
        "alice@example.com",
        "bob@example.com",
    ]


def test_concentration_is_stable_when_top_n_owners_changes(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [("alice@example.com", RECENT, ["f.py"])] * 10
        + [("bob@example.com", RECENT, ["f.py"])] * 7
        + [("carol@example.com", RECENT, ["f.py"])] * 3
        + [("dan@example.com", RECENT, ["f.py"])] * 1
    )
    narrow = analyze_ownership(git_repo.path, _config(top_n_owners=1), as_of=NOW).paths["f.py"]
    wide = analyze_ownership(git_repo.path, _config(top_n_owners=4), as_of=NOW).paths["f.py"]
    assert len(narrow.owners) < len(wide.owners)
    assert [owner.handle for owner in narrow.scored_owners] == [
        owner.handle for owner in wide.scored_owners
    ]


def test_analyze_path_exclusions(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            (
                "alice@example.com",
                RECENT,
                ["src/main.py", "yarn.lock", "dist/bundle.js", "vendor/lib/u.go", "node_modules/x"],
            )
        ]
        * 3
    )
    result = analyze_ownership(git_repo.path, _config(), as_of=NOW)
    assert "src/main.py" in result.paths
    assert "yarn.lock" not in result.paths
    assert "dist/bundle.js" not in result.paths
    assert "vendor/lib/u.go" not in result.paths
    assert "node_modules/x" not in result.paths
    assert result.analysis_completeness.excluded_static == 4


def test_analyze_empty_repo(git_repo: GitRepo) -> None:
    git_repo.seed_history([("alice@example.com", RECENT, ["yarn.lock"])])
    result = analyze_ownership(git_repo.path, _config(), as_of=NOW)
    assert result.paths == {}


def test_analyze_confidence_threshold_filters(git_repo: GitRepo) -> None:
    git_repo.seed_history([("alice@example.com", OLD, ["f.py"])] * 2)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=5, confidence_threshold=0.95),
        scoring=ScoringConfig(
            recency_half_life_days=10,
            blame_weight=0.0,
            change_survival_weight=0.0,
        ),
    )
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    assert "f.py" not in result.paths


def test_analyze_decay_warning_flagged(git_repo: GitRepo) -> None:
    git_repo.seed_history([("alice@example.com", OLD, ["src/auth.py"])] * 3)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=2, confidence_threshold=0.0),
        scoring=_NO_BLAME,
        decay=DecayConfig(threshold_days=100),
    )
    path_ownership = analyze_ownership(git_repo.path, config, as_of=NOW).paths["src/auth.py"]
    warnings = path_ownership.decay_warnings
    assert len(warnings) == 1
    assert warnings[0].handle == "alice@example.com"
    assert warnings[0].days_since_last_commit > 100


def test_analyze_bus_factor_counts_qualified_owners(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [("alice@example.com", RECENT, ["src/main.py"])] * 10
        + [("bob@example.com", RECENT, ["src/main.py"])] * 8
        + [("carol@example.com", RECENT, ["src/main.py"])] * 5
    )
    result = analyze_ownership(git_repo.path, _config(top_n_owners=3), as_of=NOW)
    count = result.paths["src/main.py"].qualified_owner_count
    assert count == 3
    assert count <= 3


def test_adaptive_freshness_keeps_stable_owner(git_repo: GitRepo) -> None:
    stable = [
        ("alice@example.com", NOW - timedelta(days=days), ["crypto.py"])
        for days in (1200, 800, 400)
    ]
    churn = [
        ("alice@example.com", NOW - timedelta(days=days), ["hot.py"]) for days in (86, 84, 82, 80)
    ]
    churn.extend(
        ("bob@example.com", NOW - timedelta(days=days), ["hot.py"])
        for days in (16, 14, 12, 10, 8, 6, 4, 2)
    )
    git_repo.seed_history([*stable, *churn])
    config = Config(
        analysis=AnalysisConfig(
            lookback_days="adaptive",
            min_commits=1,
            top_n_owners=5,
            confidence_threshold=0.0,
        ),
        scoring=_NO_BLAME,
    )
    write_codeowners(git_repo.path, "crypto.py alice@example.com\n")
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    drift = detect_drift(git_repo.path, result, config)
    stable_owner = result.paths["crypto.py"].owners[0]
    assert stable_owner.handle == "alice@example.com"
    assert stable_owner.freshness is not None
    assert stable_owner.freshness.status == "stable"
    assert result.paths["crypto.py"].decay_warnings == ()
    assert all(entry.path != "crypto.py" for entry in drift.missing)
    previous = next(
        owner for owner in result.paths["hot.py"].owners if owner.handle.startswith("alice")
    )
    assert previous.freshness is not None
    assert previous.freshness.status == "inactive"


def test_gather_blame_coverage_aggregates(git_repo: GitRepo) -> None:
    git_repo.commit_files({"x.py": "print(1)\n"}, "add", email="alice@example.com")
    result = gather_blame_coverage(["x.py"], git_repo.path)
    assert result.coverage["x.py"]["alice@example.com"] == 1.0
    assert gather_blame_coverage([], git_repo.path) == BlamePass()


def test_apply_completeness_leaves_unscored_owners_unchanged() -> None:
    bare = OwnerEntry(
        handle="alice@example.com",
        ownership_score=0.4,
        last_commit=NOW,
        commits=1,
        evidence_quality=0.2,
    )
    ownership = OwnershipMap(
        paths={"a.py": PathOwnership(owners=(bare,), qualified_owner_count=1)},
        last_analyzed=NOW,
        analysis_ref="abc",
    )
    scaled = apply_completeness(ownership, AnalysisCompleteness(score=0.5), ScoringConfig())
    assert scaled.paths["a.py"].owners[0].evidence_quality == 0.2
    assert scaled.paths["a.py"].owners[0].ownership_score == 0.4


def test_owner_total_matches_combine_available_signals(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("bob@example.com", OLD, ["src/main.py"]),
        ]
    )
    config = _config()
    result = analyze_ownership(git_repo.path, config, as_of=NOW, retain_all=True)
    scoring = config.scoring
    for entry in result.paths["src/main.py"].candidates or result.paths["src/main.py"].owners:
        total, quality = combine_available_signals(
            signal_tuples(entry),
            signal_weights(scoring),
            signal_reliabilities(scoring),
        )
        assert total == pytest.approx(entry.ownership_score)
        factor = result.analysis_completeness.score
        scaled = quality * (factor if factor is not None else 1.0)
        assert scaled == pytest.approx(entry.evidence_quality)

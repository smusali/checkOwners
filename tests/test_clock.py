"""Resolving the analysis instant, the HEAD commit, and the git version."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from checkowners.analyze import (
    analyze_ownership,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    ScoringConfig,
)
from checkowners.git import (
    MIN_GIT_VERSION,
    SOURCE_DATE_EPOCH_ENV,
    analysis_epoch,
    head_commit_datetime,
    head_commit_sha,
    parse_as_of,
    parse_git_version,
    parse_source_date_epoch,
    resolve_as_of,
)
from checkowners.models import (
    OwnershipMap,
)
from tests.conftest import (
    NOW,
    RECENT,
    GitRepo,
)

_NO_BLAME = ScoringConfig(blame_weight=0.0, change_survival_weight=0.0)


def _ownership_json(ownership: OwnershipMap) -> str:
    return json.dumps(
        {
            "analysis_epoch": analysis_epoch(ownership.last_analyzed),
            "analysis_ref": ownership.analysis_ref,
            "inferred": {
                path: {
                    "owners": [
                        {
                            "handle": owner.handle,
                            "ownership_score": round(owner.ownership_score, 4),
                        }
                        for owner in path_ownership.owners
                    ]
                }
                for path, path_ownership in sorted(ownership.paths.items())
            },
        },
        indent=2,
        sort_keys=True,
    )


def _analyzed(git_repo: GitRepo, as_of: datetime | None = NOW) -> OwnershipMap:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
        scoring=_NO_BLAME,
    )
    if as_of is not None:
        return analyze_ownership(
            git_repo.path,
            config,
            as_of=as_of,
            analysis_ref=git_repo.head_sha(),
        )
    return analyze_ownership(git_repo.path, config)


def test_parse_as_of_naive_is_utc() -> None:
    assert parse_as_of("2026-05-28T12:00:00") == NOW
    assert parse_as_of("2026-05-28T12:00:00+00:00") == NOW
    assert parse_as_of("2026-05-28T16:00:00+04:00") == NOW
    with pytest.raises(ValueError, match="Invalid as-of value"):
        parse_as_of("not-a-date")
    assert analysis_epoch(datetime(2026, 5, 28, 12, 0, 0)) == NOW.isoformat()


def test_parse_source_date_epoch_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        parse_source_date_epoch("not-an-int")


def test_resolve_as_of_cli_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, "1000000000")
    assert resolve_as_of("2026-05-28T12:00:00+00:00", tmp_path) == NOW


def test_resolve_as_of_source_date_epoch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, "1000000000")
    got = resolve_as_of(None, tmp_path)
    assert got == datetime.fromtimestamp(1_000_000_000, tz=UTC)


def test_resolve_as_of_falls_back_to_head(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SOURCE_DATE_EPOCH_ENV, raising=False)
    git_repo.commit_files({"src/main.py": "x\n"}, "seed", date=NOW.isoformat())
    assert resolve_as_of(None, git_repo.path) == NOW


def test_head_commit_datetime_parses_committer_time(git_repo: GitRepo) -> None:
    git_repo.commit_files({"src/main.py": "x\n"}, "seed", date="2026-05-28T12:00:00+00:00")
    assert head_commit_datetime(git_repo.path) == NOW


def test_head_commit_sha_reads_head(git_repo: GitRepo) -> None:
    git_repo.commit_files({"src/main.py": "x\n"}, "seed")
    assert head_commit_sha(git_repo.path) == git_repo.head_sha()


def test_analyze_resolves_clock_when_omitted(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SOURCE_DATE_EPOCH_ENV, raising=False)
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
        scoring=_NO_BLAME,
    )
    result = analyze_ownership(git_repo.path, config)
    assert result.last_analyzed == head_commit_datetime(git_repo.path)
    assert result.analysis_ref == git_repo.head_sha()


def test_same_as_of_is_byte_identical(git_repo: GitRepo) -> None:
    first = _analyzed(git_repo)
    second = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
            scoring=_NO_BLAME,
        ),
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
        max_workers=1,
    )
    third = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
            scoring=_NO_BLAME,
        ),
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
        max_workers=16,
    )
    assert _ownership_json(first) == _ownership_json(second) == _ownership_json(third)
    later = NOW + timedelta(days=30)
    shifted = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
            scoring=_NO_BLAME,
        ),
        as_of=later,
        analysis_ref=git_repo.head_sha(),
    )
    assert _ownership_json(first) != _ownership_json(shifted)


def test_max_workers_does_not_change_output(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["src/main.py"]),
            ("alice@example.com", RECENT, ["src/main.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
        scoring=_NO_BLAME,
    )
    one = analyze_ownership(
        git_repo.path, config, as_of=NOW, analysis_ref=git_repo.head_sha(), max_workers=1
    )
    many = analyze_ownership(
        git_repo.path, config, as_of=NOW, analysis_ref=git_repo.head_sha(), max_workers=16
    )
    assert _ownership_json(one) == _ownership_json(many)


def test_parse_git_version_variants() -> None:
    assert parse_git_version("git version 2.40.0") == (2, 40, 0)
    assert parse_git_version("git version 2.39.3 (Apple Git-146)") == (2, 39, 3)
    assert parse_git_version("git version 2.43.0.windows.1") == (2, 43, 0)
    assert MIN_GIT_VERSION == (2, 23, 0)
    with pytest.raises(ValueError, match="Could not parse"):
        parse_git_version("not a version")

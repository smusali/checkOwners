"""Tests for checkowners.trends module."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from checkowners.analyze import _RawCommit
from checkowners.models import AnalysisConfig, Config, ScoringConfig
from checkowners.trends import analyze_trends, build_trends

_NOW = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


def _commit(author: str, days_ago: int, files: tuple[str, ...]) -> _RawCommit:
    return _RawCommit(author=author, timestamp=_NOW - timedelta(days=days_ago), files=files)


def _config() -> Config:
    return Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=3, confidence_threshold=0.0),
        scoring=ScoringConfig(),
    )


def test_build_trends_emits_one_point_per_period() -> None:
    commits = [_commit("alice@example.com", 5, ("src/main.py",))]
    report = build_trends(commits, _config(), periods=3, period_days=30, now=_NOW)
    assert report.periods == 3
    assert len(report.points) == 3
    # Points are ordered oldest -> newest.
    ends = [p.period_end for p in report.points]
    assert ends == sorted(ends)
    assert report.points[-1].period_end == _NOW


def test_build_trends_window_is_cumulative() -> None:
    # alice commits 80 days ago, bob 5 days ago.
    commits = [
        _commit("alice@example.com", 80, ("src/main.py",)),
        _commit("bob@example.com", 5, ("src/main.py",)),
    ]
    report = build_trends(commits, _config(), periods=3, period_days=30, now=_NOW)
    # Oldest period ends 60 days before now: only alice's commit is in scope.
    oldest = report.points[0]
    assert oldest.active_contributors == 1
    # Newest period includes both contributors.
    newest = report.points[-1]
    assert newest.active_contributors == 2
    assert newest.commits == 2


def test_build_trends_counts_distinct_commits_not_touched_files() -> None:
    # One commit touching many files must count as a single commit.
    many_files = tuple(f"src/module_{i}.py" for i in range(12))
    commits = [
        _commit("alice@example.com", 5, many_files),
        _commit("bob@example.com", 3, ("src/other.py",)),
    ]
    report = build_trends(commits, _config(), periods=1, period_days=30, now=_NOW)
    assert report.points[0].commits == 2


def test_build_trends_excludes_configured_paths() -> None:
    commits = [
        _commit("alice@example.com", 1, ("src/main.py",)),
        _commit("alice@example.com", 1, ("pkg.lock",)),
    ]
    report = build_trends(commits, _config(), periods=1, period_days=30, now=_NOW)
    point = report.points[0]
    # *.lock is excluded by default, so only src/main.py is tracked.
    assert point.tracked_paths == 1


def test_build_trends_empty_history() -> None:
    report = build_trends([], _config(), periods=2, period_days=30, now=_NOW)
    assert len(report.points) == 2
    assert all(p.tracked_paths == 0 for p in report.points)
    assert all(p.avg_top_confidence == 0.0 for p in report.points)


def test_build_trends_confidence_in_unit_range() -> None:
    commits = [_commit("alice@example.com", 2, ("src/main.py",)) for _ in range(5)]
    report = build_trends(commits, _config(), periods=2, period_days=30, now=_NOW)
    for point in report.points:
        assert 0.0 <= point.avg_top_confidence <= 1.0


def test_analyze_trends_fetches_history_span() -> None:
    config = _config()
    empty = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("checkowners.trends._get_commit_history", return_value=[]) as mock_hist:
        report = analyze_trends(Path("/fake"), config, periods=4, period_days=15)
    # span = periods * period_days
    assert mock_hist.call_args[0][1] == 60
    assert report.periods == 4
    assert empty.returncode == 0

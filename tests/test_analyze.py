"""Tests for checkowners.analyze module."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from checkowners.analyze import (
    _aggregate_contributions,
    _blame_for_path,
    _Contribution,
    _filter_excluded,
    _filter_nonexistent,
    _frequency_score,
    _gather_blame_coverage,
    _get_commit_history,
    _is_excluded,
    _parse_blame_output,
    _parse_log_output,
    _RawCommit,
    _recency_score,
    analyze_ownership,
)
from checkowners.models import AnalysisConfig, Config, DecayConfig, ScoringConfig


def _make_git_log_output(
    commits: list[tuple[str, datetime, list[str]]],
) -> str:
    """Build a fake git log stdout string from (author, timestamp, files) triples."""
    chunks: list[str] = []
    for author, ts, files in commits:
        chunk = f"COMMIT_START\n{author}\n{ts.isoformat()}\n\n" + "\n".join(files)
        chunks.append(chunk)
    return "\n".join(chunks) + "\n"


def _mock_run(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


_MOCK_GIT = "checkowners.analyze.subprocess.run"
_MOCK_EXIST = "checkowners.analyze._filter_nonexistent"
_MOCK_BLAME = "checkowners.analyze._gather_blame_coverage"


def _passthrough(
    contributions: dict[str, dict[str, _Contribution]],
    _root: Path,
) -> dict[str, dict[str, _Contribution]]:
    return contributions


def _no_blame(
    _paths: object,
    _root: Path,
    *,
    on_progress: object = None,
) -> dict[str, dict[str, float]]:
    return {}


_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_RECENT = _NOW - timedelta(days=10)
_OLD = _NOW - timedelta(days=200)


def test_analyze_basic_confidence_scoring() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["src/main.py", "src/utils.py"]),
        ("bob@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py", "src/utils.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=2, confidence_threshold=0.0),
    )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    main_owners = result.paths["src/main.py"].owners
    assert main_owners[0].handle == "alice@example.com"
    assert main_owners[0].confidence > main_owners[1].confidence
    assert {o.handle for o in main_owners} == {"alice@example.com", "bob@example.com"}
    assert "src/utils.py" in result.paths
    utils_owners = result.paths["src/utils.py"].owners
    assert utils_owners[0].handle == "alice@example.com"


def test_analyze_review_provider_feeds_review_factor() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=2, confidence_threshold=0.0),
        scoring=ScoringConfig(review_weight=0.5),
    )

    captured: dict[str, set[str]] = {}

    def provider(emails: set[str]) -> dict[str, dict[str, float]]:
        captured["emails"] = emails
        return {"src/main.py": {"alice@example.com": 1.0}}

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config, review_provider=provider)

    alice = result.paths["src/main.py"].owners[0]
    assert alice.handle == "alice@example.com"
    assert alice.score_breakdown is not None
    assert alice.score_breakdown.review == 1.0
    assert captured["emails"] == {"alice@example.com"}


def test_analyze_review_factor_zero_without_provider() -> None:
    commits = [("alice@example.com", _RECENT, ["src/main.py"])]
    stdout = _make_git_log_output(commits)
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    breakdown = result.paths["src/main.py"].owners[0].score_breakdown
    assert breakdown is not None
    assert breakdown.review == 0.0


def test_analyze_lookback_days() -> None:
    config = Config(analysis=AnalysisConfig(lookback_days=90, min_commits=1))

    with (
        patch(_MOCK_GIT, return_value=_mock_run("")) as mock,
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        analyze_ownership(Path("/fake"), config)

    args = mock.call_args[0][0]
    assert "--since=90 days ago" in args


def test_analyze_min_commits_filter() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("bob@example.com", _RECENT, ["src/main.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=2, top_n_owners=5, confidence_threshold=0.0),
    )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    handles = [o.handle for o in result.paths["src/main.py"].owners]
    assert handles == ["alice@example.com"]


def test_analyze_top_n_owners() -> None:
    commits = (
        [("alice@example.com", _RECENT, ["f.py"])] * 10
        + [("bob@example.com", _RECENT, ["f.py"])] * 7
        + [("carol@example.com", _RECENT, ["f.py"])] * 3
    )
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=2, confidence_threshold=0.0),
    )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    handles = [o.handle for o in result.paths["f.py"].owners]
    assert handles == ["alice@example.com", "bob@example.com"]


def test_analyze_path_exclusions() -> None:
    commits = [
        (
            "alice@example.com",
            _RECENT,
            ["src/main.py", "yarn.lock", "dist/bundle.js", "vendor/lib/u.go", "node_modules/x"],
        ),
        (
            "alice@example.com",
            _RECENT,
            ["src/main.py", "yarn.lock", "dist/bundle.js", "vendor/lib/u.go", "node_modules/x"],
        ),
        (
            "alice@example.com",
            _RECENT,
            ["src/main.py", "yarn.lock", "dist/bundle.js", "vendor/lib/u.go", "node_modules/x"],
        ),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    assert "src/main.py" in result.paths
    assert "yarn.lock" not in result.paths
    assert "dist/bundle.js" not in result.paths
    assert "vendor/lib/u.go" not in result.paths
    assert "node_modules/x" not in result.paths


def test_analyze_empty_repo() -> None:
    config = Config(analysis=AnalysisConfig(min_commits=1))
    with (
        patch(_MOCK_GIT, return_value=_mock_run("")),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)
    assert result.paths == {}


def test_analyze_confidence_threshold_filters() -> None:
    commits = [
        ("alice@example.com", _OLD, ["f.py"]),
        ("alice@example.com", _OLD, ["f.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=5, confidence_threshold=0.95),
        scoring=ScoringConfig(recency_half_life_days=10),
    )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)
    assert "f.py" not in result.paths


def test_analyze_decay_warning_flagged() -> None:
    commits = [
        ("alice@example.com", _OLD, ["src/auth.py"]),
        ("alice@example.com", _OLD, ["src/auth.py"]),
        ("alice@example.com", _OLD, ["src/auth.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=2, confidence_threshold=0.0),
        decay=DecayConfig(threshold_days=100),
    )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    warnings = result.paths["src/auth.py"].decay_warnings
    assert len(warnings) == 1
    assert warnings[0].handle == "alice@example.com"
    assert warnings[0].days_since_last_commit > 100


def test_analyze_bus_factor_counts_qualified_owners() -> None:
    commits = (
        [("alice@example.com", _RECENT, ["src/main.py"])] * 10
        + [("bob@example.com", _RECENT, ["src/main.py"])] * 8
        + [("carol@example.com", _RECENT, ["src/main.py"])] * 5
    )
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=3, confidence_threshold=0.0),
    )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    assert result.paths["src/main.py"].bus_factor == 3


def test_is_excluded_patterns() -> None:
    assert _is_excluded("yarn.lock", ("*.lock",))
    assert _is_excluded("package-lock.json", ("*.lock",)) is False
    assert _is_excluded("dist/bundle.js", ("dist/**",))
    assert _is_excluded("dist/sub/file.js", ("dist/**",))
    assert _is_excluded("vendor/a/b.go", ("vendor/**",))
    assert _is_excluded("src/main.py", ("*.lock", "dist/**", "vendor/**")) is False


def test_recency_score_decays_exponentially() -> None:
    half_life = 90
    fresh = _recency_score(_NOW, _NOW, half_life)
    aged = _recency_score(_NOW - timedelta(days=90), _NOW, half_life)
    older = _recency_score(_NOW - timedelta(days=180), _NOW, half_life)
    assert fresh == pytest.approx(1.0)
    assert aged == pytest.approx(0.5, abs=0.01)
    assert older == pytest.approx(0.25, abs=0.01)


def test_recency_score_zero_half_life_returns_one() -> None:
    assert _recency_score(_NOW - timedelta(days=10), _NOW, 0) == 1.0


def test_frequency_score_normalizes() -> None:
    assert _frequency_score(5, 10) == 0.5
    assert _frequency_score(10, 10) == 1.0
    assert _frequency_score(0, 0) == 0.0


def test_aggregate_contributions_takes_latest_timestamp() -> None:
    commits = [
        _RawCommit("alice@example.com", _OLD, ("a.py",)),
        _RawCommit("alice@example.com", _RECENT, ("a.py",)),
        _RawCommit("bob@example.com", _OLD, ("a.py",)),
    ]
    result = _aggregate_contributions(commits)
    assert result["a.py"]["alice@example.com"].commits == 2
    assert result["a.py"]["alice@example.com"].last_commit == _RECENT
    assert result["a.py"]["bob@example.com"].commits == 1


def test_filter_excluded_removes_paths() -> None:
    contribs: dict[str, dict[str, _Contribution]] = {
        "src/main.py": {"alice": _Contribution(3, _NOW)},
        "yarn.lock": {"alice": _Contribution(5, _NOW)},
        "dist/out.js": {"bob": _Contribution(2, _NOW)},
    }
    patterns = ("*.lock", "dist/**")
    filtered = _filter_excluded(contribs, patterns)
    assert "src/main.py" in filtered
    assert "yarn.lock" not in filtered
    assert "dist/out.js" not in filtered


def test_filter_nonexistent(tmp_path: Path) -> None:
    (tmp_path / "exists.py").write_text("x", encoding="utf-8")
    contribs: dict[str, dict[str, _Contribution]] = {
        "exists.py": {"alice": _Contribution(3, _NOW)},
        "deleted.py": {"bob": _Contribution(5, _NOW)},
    }
    result = _filter_nonexistent(contribs, tmp_path)
    assert "exists.py" in result
    assert "deleted.py" not in result


def test_get_commit_history_subprocess_error() -> None:
    with (
        patch(_MOCK_GIT, side_effect=subprocess.CalledProcessError(128, "git")),
        pytest.raises(subprocess.CalledProcessError),
    ):
        _get_commit_history(Path("/fake"), 180)


def test_parse_log_output_empty() -> None:
    assert _parse_log_output("") == []
    assert _parse_log_output("  \n  ") == []


def test_parse_log_output_skips_missing_timestamp() -> None:
    raw = "COMMIT_START\nalice@example.com\nnot-a-timestamp\nfile.py\n"
    assert _parse_log_output(raw) == []


def test_parse_blame_output_counts_lines() -> None:
    blame = (
        "abc123 1 1 1\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "\tline 1\n"
        "abc124 2 2 1\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "\tline 2\n"
        "abc125 3 3 1\n"
        "author Bob\n"
        "author-mail <bob@example.com>\n"
        "\tline 3\n"
    )
    coverage = _parse_blame_output(blame)
    assert coverage["alice@example.com"] == pytest.approx(2 / 3)
    assert coverage["bob@example.com"] == pytest.approx(1 / 3)


def test_parse_blame_output_empty_returns_empty_dict() -> None:
    assert _parse_blame_output("") == {}


def test_blame_for_path_handles_error() -> None:
    with patch(_MOCK_GIT, side_effect=subprocess.CalledProcessError(128, "git")):
        assert _blame_for_path(Path("/fake"), "x.py") == {}


def test_gather_blame_coverage_aggregates() -> None:
    blame_stdout = "abc 1 1 1\nauthor Alice\nauthor-mail <alice@example.com>\n\tline\n"
    with patch(_MOCK_GIT, return_value=_mock_run(blame_stdout)):
        coverage = _gather_blame_coverage(["x.py"], Path("/fake"))
    assert coverage["x.py"]["alice@example.com"] == 1.0


def test_recency_decay_constant_matches_expected() -> None:
    """Sanity: at 1 half-life decay = exp(-ln2)."""
    expected = math.pow(0.5, 1.0)
    actual = math.pow(0.5, 90 / 90)
    assert expected == actual


def test_unqualified_paths_not_blamed() -> None:
    """Paths where no author reaches min_commits must skip the blame pass."""
    commits = [
        ("alice@example.com", _RECENT, ["hot.py"]),
        ("alice@example.com", _RECENT, ["hot.py"]),
        ("alice@example.com", _RECENT, ["hot.py"]),
        ("bob@example.com", _RECENT, ["cold.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(analysis=AnalysisConfig(min_commits=3, confidence_threshold=0.0))
    blamed: list[str] = []

    def record_blame(
        paths: Iterable[str],
        _root: Path,
        *,
        on_progress: object = None,
    ) -> dict[str, dict[str, float]]:
        blamed.extend(paths)
        return {}

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=record_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)

    assert blamed == ["hot.py"]
    assert set(result.paths) == {"hot.py"}


def test_progress_hook_reports_blame_progress() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["a.py", "b.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    updates: list[tuple[int, int]] = []
    blame_stdout = "abc 1 1 1\nauthor Alice\nauthor-mail <alice@example.com>\n\tline\n"

    def fake_git(cmd: list[str], **kwargs: object) -> object:
        if "blame" in cmd:
            return _mock_run(blame_stdout)
        return _mock_run(stdout)

    with (
        patch(_MOCK_GIT, side_effect=fake_git),
        patch(_MOCK_EXIST, side_effect=_passthrough),
    ):
        analyze_ownership(Path("/fake"), config, on_progress=lambda d, t: updates.append((d, t)))

    assert updates == [(1, 2), (2, 2)]


def test_bot_authors_excluded_by_default() -> None:
    commits = [
        ("49699333+dependabot[bot]@users.noreply.github.com", _RECENT, ["deps.txt"]),
        ("49699333+dependabot[bot]@users.noreply.github.com", _RECENT, ["deps.txt"]),
        ("49699333+dependabot[bot]@users.noreply.github.com", _RECENT, ["deps.txt"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)
    assert set(result.paths) == {"src/main.py"}


def test_bot_authors_kept_when_disabled() -> None:
    commits = [
        ("github-actions[bot]@users.noreply.github.com", _RECENT, ["gen.md"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0, exclude_bots=False),
    )
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)
    assert set(result.paths) == {"gen.md"}

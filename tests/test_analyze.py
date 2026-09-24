"""Tests for checkowners.analyze module."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from checkowners.analyze import (
    _BLAME_DEADLINE,
    MIN_GIT_VERSION,
    SOURCE_DATE_EPOCH_ENV,
    Contribution,
    GitRequirementError,
    _aggregate_contributions,
    _blame_for_path,
    _BlamePass,
    _detect_decay,
    _display_ignore_revs_path,
    _existing_file,
    _filter_excluded,
    _filter_nonexistent,
    _frequency_score,
    _gather_review_coverage,
    _get_commit_history,
    _git_stdout,
    _gitattributes_pattern_matches,
    _insufficient_history,
    _is_excluded,
    _is_shallow_repository,
    _linguist_excluded_paths,
    _parse_blame_output,
    _parse_gitattributes,
    _parse_log_output,
    _RawCommit,
    _recency_score,
    _stdout_has_rename,
    _window_has_renames,
    analysis_epoch,
    analyze_ownership,
    apply_completeness,
    combine_available_signals,
    gather_blame_coverage,
    head_commit_datetime,
    head_commit_sha,
    parse_as_of,
    parse_git_version,
    parse_source_date_epoch,
    resolve_as_of,
    score_owners,
    signal_reliabilities,
    signal_weights,
)
from checkowners.explain import signal_tuples
from checkowners.models import (
    AnalysisCompleteness,
    AnalysisConfig,
    Config,
    DecayConfig,
    GitConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    QualificationConfig,
    ScoringConfig,
)
from tests.conftest import git_commit, init_git_repo


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
_MOCK_BLAME = "checkowners.analyze.gather_blame_coverage"


def _passthrough(
    contributions: dict[str, dict[str, Contribution]],
    _root: Path,
) -> dict[str, dict[str, Contribution]]:
    return contributions


def _no_blame(
    _paths: object,
    _root: Path,
    *,
    git: object = None,  # noqa: ARG001
    on_progress: object = None,  # noqa: ARG001
    max_workers: object = None,  # noqa: ARG001
) -> _BlamePass:
    return _BlamePass()


_DEFAULT_BLAME = "abc 1 1 1\nauthor Alice\nauthor-mail <alice@example.com>\n\tline\n"


def _dispatch_git(
    *,
    log_stdout: str = "",
    blame_stdout: str = _DEFAULT_BLAME,
    version: str = "git version 2.40.0",
    ls_files: str = "",
    config_value: str | None = None,
    on_blame: object | None = None,
    reject_mailmap: bool = False,
) -> object:
    def fake(cmd: list[str], **_kwargs: object) -> object:
        if cmd[1] == "version":
            return _mock_run(version)
        if cmd[1] == "config":
            if config_value is None:
                raise subprocess.CalledProcessError(1, cmd)
            return _mock_run(config_value)
        if cmd[1] == "ls-files":
            return _mock_run(ls_files)
        if cmd[1] == "log":
            return _mock_run(log_stdout)
        if cmd[1] == "blame":
            if reject_mailmap and "--line-porcelain" not in cmd:
                return _mock_run("error: unknown option `--use-mailmap'\n")
            if on_blame is not None and "--line-porcelain" in cmd:
                on_blame(cmd)
            return _mock_run(blame_stdout)
        return _mock_run("")

    return fake


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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

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
        result = analyze_ownership(
            Path("/fake"), config, review_provider=provider, as_of=_NOW, analysis_ref="deadbeef"
        )

    alice = result.paths["src/main.py"].owners[0]
    assert alice.handle == "alice@example.com"
    assert alice.score_breakdown is not None
    assert alice.score_breakdown.review.available is True
    assert alice.score_breakdown.review.score == 1.0
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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    owner = result.paths["src/main.py"].owners[0]
    breakdown = owner.score_breakdown
    assert breakdown is not None
    assert breakdown.review.available is False
    assert "score" not in breakdown.review.as_payload()
    assert 0.0 <= owner.ownership_score <= 1.0


def test_analyze_lookback_days() -> None:
    config = Config(analysis=AnalysisConfig(lookback_days=90, min_commits=1))

    with (
        patch(_MOCK_GIT, return_value=_mock_run("")) as mock,
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    args = next(call.args[0] for call in mock.call_args_list if "--name-only" in call.args[0])
    since = _NOW - timedelta(days=90)
    assert f"--since={since.isoformat()}" in args
    assert f"--until={_NOW.isoformat()}" in args


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
        qualification=QualificationConfig(strategy="threshold", min_commits=2),
    )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    handles = [o.handle for o in result.paths["src/main.py"].owners]
    assert handles == ["alice@example.com"]


def test_analyze_adaptive_keeps_high_blame_author() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("bob@example.com", _RECENT, ["src/main.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(
        analysis=AnalysisConfig(min_commits=2, top_n_owners=5, confidence_threshold=0.0),
        qualification=QualificationConfig(strategy="adaptive", min_commits=2),
    )

    def blame_bob(
        _paths: object,
        _root: Path,
        *,
        git: object = None,  # noqa: ARG001
        on_progress: object = None,  # noqa: ARG001
        max_workers: object = None,  # noqa: ARG001
    ) -> _BlamePass:
        return _BlamePass(
            coverage={"src/main.py": {"alice@example.com": 0.4, "bob@example.com": 0.6}}
        )

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=blame_bob),
    ):
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    handles = {o.handle for o in result.paths["src/main.py"].owners}
    assert handles == {"alice@example.com", "bob@example.com"}


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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    assert "src/main.py" in result.paths
    assert "yarn.lock" not in result.paths
    assert "dist/bundle.js" not in result.paths
    assert "vendor/lib/u.go" not in result.paths
    assert "node_modules/x" not in result.paths
    assert result.analysis_completeness.excluded_gitattributes == 0
    assert result.analysis_completeness.excluded_static == 4


def test_analyze_empty_repo() -> None:
    config = Config(analysis=AnalysisConfig(min_commits=1))

    def _unused_reviews(emails: set[str]) -> dict[str, dict[str, float]]:
        raise AssertionError(f"review provider should not run for empty history: {emails}")

    with (
        patch(_MOCK_GIT, return_value=_mock_run("")),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(
            Path("/fake"),
            config,
            as_of=_NOW,
            analysis_ref="deadbeef",
            review_provider=_unused_reviews,
        )
    assert result.paths == {}
    assert result.analysis_completeness.excluded_gitattributes == 0
    assert result.analysis_completeness.excluded_static == 0
    assert _gather_review_coverage({}, _unused_reviews) == {}


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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")
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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    count = result.paths["src/main.py"].qualified_owner_count
    assert count == 3
    assert count <= config.analysis.top_n_owners


def test_is_excluded_patterns() -> None:
    assert _is_excluded("yarn.lock", ("*.lock",))
    assert _is_excluded("package-lock.json", ("*.lock",)) is False
    assert _is_excluded("dist/bundle.js", ("dist/**",))
    assert _is_excluded("dist/sub/file.js", ("dist/**",))
    assert _is_excluded("vendor/a/b.go", ("vendor/**",))
    assert _is_excluded("src/main.py", ("*.lock", "dist/**", "vendor/**")) is False


def test_gitattributes_parsing_comments_negation_and_scoping() -> None:
    rules = _parse_gitattributes(
        "# comment\n"
        "\n"
        "generated/** linguist-generated\n"
        "generated/keep.py -linguist-generated\n"
        "!skip.py linguist-generated\n"
        "vendor/** linguist-vendored linguist-generated\n"
        "pattern-only\n"
        "other.py text -diff eol=lf\n"
        "hist.py linguist-generated=true\n"
        "off.py linguist-generated=false -text\n"
        "mix.py -text linguist-vendored\n",
        "",
    )
    by_pattern = {rule.pattern: rule.values for rule in rules}
    assert set(by_pattern) == {
        "generated/**",
        "generated/keep.py",
        "vendor/**",
        "hist.py",
        "off.py",
        "mix.py",
    }
    assert by_pattern["generated/keep.py"] == (("linguist-generated", False),)
    assert by_pattern["hist.py"] == (("linguist-generated", True),)
    assert by_pattern["off.py"] == (("linguist-generated", False),)
    assert by_pattern["mix.py"] == (("linguist-vendored", True),)
    assert _gitattributes_pattern_matches("generated/*", "generated/foo.py", "")
    assert _gitattributes_pattern_matches("generated/*", "generated/nested/foo.py", "") is False
    assert _gitattributes_pattern_matches("generated/**", "generated/nested/foo.py", "")
    assert _gitattributes_pattern_matches("*.pb.go", "src/foo.pb.go", "src")
    assert _gitattributes_pattern_matches("*.pb.go", "other/foo.pb.go", "src") is False
    assert _gitattributes_pattern_matches("f?.py", "fa.py", "")
    assert _gitattributes_pattern_matches("f?.py", "fab.py", "") is False
    assert _gitattributes_pattern_matches("a/**/b.py", "a/x/b.py", "")
    assert _gitattributes_pattern_matches("/rooted.py", "rooted.py", "")
    assert _gitattributes_pattern_matches("/rooted.py", "sub/rooted.py", "") is False
    assert _gitattributes_pattern_matches("generated/", "generated/foo.py", "") is False
    assert _gitattributes_pattern_matches("/", "anything.py", "") is False
    assert _gitattributes_pattern_matches("*", "src/", "src") is False


def test_linguist_nested_gitattributes_and_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("*.pb.go linguist-generated\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gitattributes").write_text("keep.pb.go -linguist-generated\n", encoding="utf-8")
    excluded = _linguist_excluded_paths(
        tmp_path,
        ("src/foo.pb.go", "src/keep.pb.go", "src/main.py"),
    )
    assert excluded == frozenset({"src/foo.pb.go"})
    assert _linguist_excluded_paths(tmp_path, ()) == frozenset()
    with patch.object(Path, "read_text", side_effect=OSError("unreadable")):
        assert _linguist_excluded_paths(tmp_path, ("src/foo.pb.go",)) == frozenset()


def test_analyze_score_scale_with_and_without_review_provider() -> None:
    contrib = Contribution(commits=3, last_commit=_NOW)
    qualified = {"alice@example.com": contrib}
    scoring = ScoringConfig()
    path_blame = {"alice@example.com": 1.0}

    offline = score_owners(
        qualified,
        path_blame,
        {},
        max_commits=3,
        scoring=scoring,
        now=_NOW,
        blame_available=True,
        review_available=False,
        frequency_prior=0.0,
    )
    online = score_owners(
        qualified,
        path_blame,
        {"alice@example.com": 1.0},
        max_commits=3,
        scoring=scoring,
        now=_NOW,
        blame_available=True,
        review_available=True,
        frequency_prior=0.0,
    )
    assert offline[0].ownership_score == pytest.approx(1.0)
    assert online[0].ownership_score == pytest.approx(1.0)
    assert offline[0].evidence_quality == pytest.approx(0.85)
    assert online[0].evidence_quality == pytest.approx(1.0)
    assert offline[0].evidence_quality != online[0].evidence_quality


def test_combine_available_signals_recency_only_is_unit_scale() -> None:
    scoring = ScoringConfig()
    score, quality = combine_available_signals(
        {
            "recency": (1.0, True),
            "frequency": (0.0, False),
            "blame": (0.0, False),
            "review": (0.0, False),
        },
        signal_weights(scoring),
        signal_reliabilities(scoring),
    )
    assert score == pytest.approx(1.0)
    assert 0.0 <= score <= 1.0
    assert quality == pytest.approx(0.35)


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
    assert _frequency_score(3, 3, 3) == 0.5
    assert _frequency_score(3, 3, 3) < 1.0
    assert _frequency_score(1, 1, 3) == 0.25


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
    contribs: dict[str, dict[str, Contribution]] = {
        "src/main.py": {"alice": Contribution(3, _NOW)},
        "yarn.lock": {"alice": Contribution(5, _NOW)},
        "dist/out.js": {"bob": Contribution(2, _NOW)},
    }
    patterns = ("*.lock", "dist/**")
    filtered = _filter_excluded(contribs, patterns)
    assert "src/main.py" in filtered
    assert "yarn.lock" not in filtered
    assert "dist/out.js" not in filtered


def test_filter_nonexistent(tmp_path: Path) -> None:
    (tmp_path / "exists.py").write_text("x", encoding="utf-8")
    contribs: dict[str, dict[str, Contribution]] = {
        "exists.py": {"alice": Contribution(3, _NOW)},
        "deleted.py": {"bob": Contribution(5, _NOW)},
    }
    result = _filter_nonexistent(contribs, tmp_path)
    assert "exists.py" in result
    assert "deleted.py" not in result


def test_get_commit_history_subprocess_error() -> None:
    with (
        patch(_MOCK_GIT, side_effect=subprocess.CalledProcessError(128, "git")),
        pytest.raises(subprocess.CalledProcessError),
    ):
        _get_commit_history(Path("/fake"), 180, _NOW)


def test_parse_log_output_empty() -> None:
    assert _parse_log_output("") == []
    assert _parse_log_output("  \n  ") == []


def test_parse_log_output_skips_missing_timestamp() -> None:
    raw = "COMMIT_START\nalice@example.com\nnot-a-timestamp\nfile.py\n"
    assert _parse_log_output(raw) == []
    short = "COMMIT_START\nalice@example.com\n"
    assert _parse_log_output(short) == []
    no_files = "COMMIT_START\nalice@example.com\n2026-05-28T12:00:00+00:00\n"
    assert _parse_log_output(no_files) == []


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
        assert _blame_for_path(Path("/fake"), "x.py", ["git", "blame"]) == {}


def testgather_blame_coverage_aggregates() -> None:
    with patch(_MOCK_GIT, side_effect=_dispatch_git()):
        result = gather_blame_coverage(["x.py"], Path("/fake"))
    assert result.coverage["x.py"]["alice@example.com"] == 1.0
    assert gather_blame_coverage([], Path("/fake")) == _BlamePass()
    with (
        patch(_MOCK_GIT, side_effect=_dispatch_git()),
        patch("checkowners.analyze._blame_for_path", return_value={}),
    ):
        empty = gather_blame_coverage(["empty.py"], Path("/fake"))
        assert empty.coverage == {}


def _hot_cold_commits() -> str:
    return _make_git_log_output(
        [
            ("alice@example.com", _RECENT, ["hot.py"]),
            ("alice@example.com", _RECENT, ["hot.py"]),
            ("alice@example.com", _RECENT, ["hot.py"]),
            ("bob@example.com", _RECENT, ["cold.py"]),
        ]
    )


def _record_blame(blamed: list[str]) -> object:
    def record_blame(
        paths: Iterable[str],
        _root: Path,
        *,
        git: object = None,  # noqa: ARG001
        on_progress: object = None,  # noqa: ARG001
        max_workers: object = None,  # noqa: ARG001
    ) -> _BlamePass:
        blamed.extend(paths)
        return _BlamePass()

    return record_blame


def test_unqualified_paths_not_blamed() -> None:
    config = Config(
        analysis=AnalysisConfig(min_commits=3, confidence_threshold=0.0),
        qualification=QualificationConfig(strategy="threshold", min_commits=3),
    )
    blamed: list[str] = []

    with (
        patch(_MOCK_GIT, return_value=_mock_run(_hot_cold_commits())),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_record_blame(blamed)),
    ):
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    assert blamed == ["hot.py"]
    assert set(result.paths) == {"hot.py"}


def test_adaptive_blames_all_contributed_paths() -> None:
    config = Config(
        analysis=AnalysisConfig(min_commits=3, confidence_threshold=0.0),
        qualification=QualificationConfig(strategy="adaptive", min_commits=3),
    )
    blamed: list[str] = []

    with (
        patch(_MOCK_GIT, return_value=_mock_run(_hot_cold_commits())),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_record_blame(blamed)),
    ):
        analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    assert set(blamed) == {"hot.py", "cold.py"}


def test_progress_hook_reports_blame_progress() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["a.py", "b.py"]),
    ]
    stdout = _make_git_log_output(commits)
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    updates: list[tuple[int, int]] = []
    blame_stdout = "abc 1 1 1\nauthor Alice\nauthor-mail <alice@example.com>\n\tline\n"

    def fake_git(cmd: list[str], **_kwargs: object) -> object:
        if cmd[1] == "blame":
            return _mock_run(blame_stdout)
        if cmd[1] == "version":
            return _mock_run("git version 2.40.0")
        if cmd[1] == "config":
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[1] == "ls-files":
            return _mock_run("a.py\nb.py\n")
        return _mock_run(stdout)

    with (
        patch(_MOCK_GIT, side_effect=fake_git),
        patch(_MOCK_EXIST, side_effect=_passthrough),
    ):
        analyze_ownership(
            Path("/fake"),
            config,
            on_progress=lambda d, t: updates.append((d, t)),
            as_of=_NOW,
            analysis_ref="deadbeef",
        )

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
    blamed: list[str] = []
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_record_blame(blamed)),
    ):
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")
    assert set(result.paths) == {"src/main.py"}
    assert blamed == ["src/main.py"]


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
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")
    assert set(result.paths) == {"gen.md"}


def test_adaptive_single_commit_full_blame_is_owner() -> None:
    stdout = _make_git_log_output([("alice@example.com", _RECENT, ["new.py"])])
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.3))

    def full_blame(
        _paths: object,
        _root: Path,
        *,
        git: object = None,  # noqa: ARG001
        on_progress: object = None,  # noqa: ARG001
        max_workers: object = None,  # noqa: ARG001
    ) -> _BlamePass:
        return _BlamePass(coverage={"new.py": {"alice@example.com": 1.0}})

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=full_blame),
    ):
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    owner = result.paths["new.py"].owners[0]
    assert owner.handle == "alice@example.com"
    assert owner.ownership_score > 0.3
    assert owner.evidence_quality == pytest.approx(
        0.85 * (result.analysis_completeness.score or 1.0)
    )
    reasons = {gap.code: gap.reason for gap in result.analysis_completeness.gaps}
    assert reasons["missing_mailmap"] == "Missing .mailmap."
    assert reasons["missing_ignore_revs"] == "Missing .git-blame-ignore-revs."
    assert reasons["review_history"] == "Review history unavailable."
    assert result.analysis_completeness.score == round(
        (13 - len(reasons)) / 13,
        4,
    )


def test_runtime_budget_marks_unblamed_paths_incomplete() -> None:
    stdout = _make_git_log_output([("alice@example.com", _RECENT, ["src/main.py"])])
    config = Config(
        analysis=AnalysisConfig(max_runtime_seconds=0, confidence_threshold=0.0, min_commits=1),
    )
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME) as blame,
    ):
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")
    blame.assert_not_called()
    owner = result.paths["src/main.py"].owners[0]
    assert owner.score_breakdown is not None
    assert owner.score_breakdown.blame.available is False
    assert owner.ownership_score > 0
    assert any(gap.code == "runtime_budget" for gap in result.analysis_completeness.gaps)
    assert result.analysis_completeness.score is not None
    assert result.analysis_completeness.score < 1


def test_git_probes_ignore_unexpected_output(tmp_path: Path) -> None:
    with patch(_MOCK_GIT, side_effect=OSError("down")):
        assert _git_stdout(tmp_path, ["git", "rev-parse"]) is None
        assert _is_shallow_repository(tmp_path) is False
        assert _insufficient_history(tmp_path, []) is False
        assert _window_has_renames(tmp_path, _RECENT, _NOW) is False
    failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="true\n", stderr="")
    with patch(_MOCK_GIT, return_value=failed):
        assert _git_stdout(tmp_path, ["git", "status"]) is None
    shallow = subprocess.CompletedProcess(args=[], returncode=0, stdout="true\n", stderr="")
    with patch(_MOCK_GIT, return_value=shallow):
        assert _is_shallow_repository(tmp_path) is True
    head = subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{'a' * 40}\n", stderr="")
    commit = _RawCommit(author="alice@example.com", timestamp=_RECENT, files=("a.py",))
    with patch(_MOCK_GIT, return_value=head):
        assert _insufficient_history(tmp_path, []) is True
        assert _insufficient_history(tmp_path, [commit]) is False
        assert _window_has_renames(tmp_path, _RECENT, _NOW) is False
    renamed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="R100\told.py\tnew.py\n",
        stderr="",
    )
    with patch(_MOCK_GIT, return_value=renamed):
        assert _window_has_renames(tmp_path, _RECENT, _NOW) is True
    assert _stdout_has_rename("R\told.py\tnew.py\n") is True
    assert _stdout_has_rename("R100\told.py\tnew.py\n") is True
    assert _stdout_has_rename("RX\told.py\tnew.py\n") is False
    assert _stdout_has_rename("R\n") is False
    assert _stdout_has_rename("M\told.py\n") is False


def test_ignore_revs_path_rejects_unexpected_names(tmp_path: Path) -> None:
    assert _existing_file(tmp_path, "") is None
    assert _existing_file(tmp_path, "a\nb") is None
    assert _existing_file(tmp_path, "a\x00b") is None
    present = tmp_path / "ignore"
    present.write_text("abc\n", encoding="utf-8")
    assert _existing_file(tmp_path, "ignore") == present
    assert _existing_file(tmp_path, str(present)) == present
    with patch.object(Path, "is_file", side_effect=OSError("too long")):
        assert _existing_file(tmp_path, "ignore") is None
    assert _display_ignore_revs_path(tmp_path, Path("/etc/hosts")) == "/etc/hosts"


def test_apply_completeness_leaves_unscored_owners_unchanged() -> None:
    bare = OwnerEntry(
        handle="alice@example.com",
        ownership_score=0.4,
        last_commit=_NOW,
        commits=1,
        evidence_quality=0.2,
    )
    ownership = OwnershipMap(
        paths={"a.py": PathOwnership(owners=(bare,), qualified_owner_count=1)},
        last_analyzed=_NOW,
        analysis_ref="abc",
    )
    scaled = apply_completeness(ownership, AnalysisCompleteness(score=0.5), ScoringConfig())
    assert scaled.paths["a.py"].owners[0].evidence_quality == 0.2
    assert scaled.paths["a.py"].owners[0].ownership_score == 0.4


def test_blame_stops_when_the_deadline_has_passed(tmp_path: Path) -> None:
    token = _BLAME_DEADLINE.set(0.0)
    try:
        with (
            patch("checkowners.analyze._ensure_git_version"),
            patch("checkowners.analyze._resolve_ignore_revs_file", return_value=None),
            patch("checkowners.analyze._mass_refactor_revs", return_value=()),
            patch("checkowners.analyze._blame_accepts_mailmap_flags", return_value=True),
            patch("checkowners.analyze._blame_for_path") as blame,
        ):
            result = gather_blame_coverage(["a.py"], tmp_path)
    finally:
        _BLAME_DEADLINE.reset(token)
    blame.assert_not_called()
    assert result.truncated is True
    assert result.coverage == {}


def test_adaptive_squash_merge_produces_owners() -> None:
    stdout = _make_git_log_output(
        [
            ("alice@example.com", _RECENT, ["svc.py"]),
            ("bob@example.com", _RECENT, ["svc.py"]),
        ]
    )
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.0))

    def split_blame(
        _paths: object,
        _root: Path,
        *,
        git: object = None,  # noqa: ARG001
        on_progress: object = None,  # noqa: ARG001
        max_workers: object = None,  # noqa: ARG001
    ) -> _BlamePass:
        return _BlamePass(coverage={"svc.py": {"alice@example.com": 0.7, "bob@example.com": 0.3}})

    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=split_blame),
    ):
        result = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    assert "svc.py" in result.paths
    assert {o.handle for o in result.paths["svc.py"].owners} == {
        "alice@example.com",
        "bob@example.com",
    }


def test_adaptive_low_blame_scores_below_high_blame() -> None:
    scoring = ScoringConfig()
    low = score_owners(
        {"alice@example.com": Contribution(commits=1, last_commit=_RECENT)},
        {"alice@example.com": 0.03},
        {},
        max_commits=1,
        scoring=scoring,
        now=_NOW,
        blame_available=True,
        review_available=False,
        frequency_prior=3.0,
    )
    high = score_owners(
        {"alice@example.com": Contribution(commits=1, last_commit=_RECENT)},
        {"alice@example.com": 0.95},
        {},
        max_commits=1,
        scoring=scoring,
        now=_NOW,
        blame_available=True,
        review_available=False,
        frequency_prior=3.0,
    )
    assert low[0].ownership_score < high[0].ownership_score
    assert high[0].ownership_score > 0.3
    assert high[0].evidence_quality == pytest.approx(0.85)


@pytest.mark.parametrize("commits", [1, 2, 3, 4, 5])
def test_frequency_score_monotonic_and_damped(commits: int) -> None:
    max_commits = 5
    prior = 3.0
    score = _frequency_score(commits, max_commits, prior)
    if commits > 1:
        assert score > _frequency_score(commits - 1, max_commits, prior)
    if commits <= 3:
        assert score < 1.0


def test_threshold_frequency_undamped() -> None:
    assert _frequency_score(3, 3) == 1.0
    assert _frequency_score(3, 3, 0.0) == 1.0


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
                        for owner in po.owners
                    ]
                }
                for path, po in sorted(ownership.paths.items())
            },
        },
        indent=2,
        sort_keys=True,
    )


def _sample_commits() -> str:
    return _make_git_log_output(
        [
            ("alice@example.com", _RECENT, ["src/main.py"]),
            ("alice@example.com", _RECENT, ["src/main.py"]),
        ]
    )


def test_parse_as_of_naive_is_utc() -> None:
    assert parse_as_of("2026-05-28T12:00:00") == _NOW
    assert parse_as_of("2026-05-28T12:00:00+00:00") == _NOW
    assert parse_as_of("2026-05-28T16:00:00+04:00") == _NOW
    with pytest.raises(ValueError, match="Invalid as-of value"):
        parse_as_of("not-a-date")
    assert analysis_epoch(datetime(2026, 5, 28, 12, 0, 0)) == _NOW.isoformat()


def test_parse_source_date_epoch_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        parse_source_date_epoch("not-an-int")


def test_resolve_as_of_cli_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, "1000000000")
    assert resolve_as_of("2026-05-28T12:00:00+00:00", tmp_path) == _NOW


def test_resolve_as_of_source_date_epoch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, "1000000000")
    with patch("checkowners.analyze.head_commit_datetime", side_effect=AssertionError):
        got = resolve_as_of(None, tmp_path)
    assert got == datetime.fromtimestamp(1_000_000_000, tz=UTC)


def test_resolve_as_of_falls_back_to_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOURCE_DATE_EPOCH_ENV, raising=False)
    with patch("checkowners.analyze.head_commit_datetime", return_value=_NOW) as mock_head:
        assert resolve_as_of(None, tmp_path) == _NOW
    mock_head.assert_called_once_with(tmp_path)


def test_head_commit_datetime_parses_committer_time(tmp_path: Path) -> None:
    with patch(_MOCK_GIT, return_value=_mock_run("2026-05-28T12:00:00+00:00\n")):
        assert head_commit_datetime(tmp_path) == _NOW
    with patch(_MOCK_GIT, return_value=_mock_run("2026-05-28T12:00:00\n")):
        assert head_commit_datetime(tmp_path) == _NOW
    with (
        patch(_MOCK_GIT, return_value=_mock_run("not-a-date\n")),
        pytest.raises(ValueError, match="committer timestamp"),
    ):
        head_commit_datetime(tmp_path)


def test_head_commit_sha_reads_head(tmp_path: Path) -> None:
    with patch(_MOCK_GIT, return_value=_mock_run("abc123\n")):
        assert head_commit_sha(tmp_path) == "abc123"
    with (
        patch(_MOCK_GIT, return_value=_mock_run("\n")),
        pytest.raises(ValueError, match="HEAD commit SHA"),
    ):
        head_commit_sha(tmp_path)


def test_analyze_resolves_clock_when_omitted() -> None:
    stdout = _sample_commits()
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    with (
        patch("checkowners.analyze.resolve_as_of", return_value=_NOW) as mock_as_of,
        patch("checkowners.analyze.head_commit_sha", return_value="abc123") as mock_sha,
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(Path("/fake"), config)
    assert result.last_analyzed == _NOW
    assert result.analysis_ref == "abc123"
    mock_as_of.assert_called_once_with(None, Path("/fake"))
    mock_sha.assert_called_once_with(Path("/fake"))


def test_detect_decay_skips_owner_without_contribution() -> None:
    orphan = OwnerEntry(
        handle="ghost@example.com",
        ownership_score=0.9,
        last_commit=_NOW,
        commits=3,
    )
    assert _detect_decay("src/main.py", {}, (orphan,), 100, _NOW) == ()


def test_same_as_of_is_byte_identical() -> None:
    stdout = _sample_commits()
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        first = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")
        second = analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")
    assert _ownership_json(first) == _ownership_json(second)
    later = _NOW + timedelta(days=30)
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        shifted = analyze_ownership(Path("/fake"), config, as_of=later, analysis_ref="deadbeef")
    assert _ownership_json(first) != _ownership_json(shifted)


def test_max_workers_does_not_change_output() -> None:
    stdout = _sample_commits()
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        one = analyze_ownership(
            Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef", max_workers=1
        )
        many = analyze_ownership(
            Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef", max_workers=16
        )
    assert _ownership_json(one) == _ownership_json(many)


def test_parse_git_version_variants() -> None:
    assert parse_git_version("git version 2.40.0") == (2, 40, 0)
    assert parse_git_version("git version 2.39.3 (Apple Git-146)") == (2, 39, 3)
    assert parse_git_version("git version 2.43.0.windows.1") == (2, 43, 0)
    assert MIN_GIT_VERSION == (2, 23, 0)
    with pytest.raises(ValueError, match="Could not parse"):
        parse_git_version("not a version")


def test_gather_includes_fidelity_flags_by_default() -> None:
    seen: list[list[str]] = []
    with patch(_MOCK_GIT, side_effect=_dispatch_git(on_blame=seen.append)):
        gather_blame_coverage(["x.py"], Path("/fake"))
    assert "-w" in seen[0]
    assert "-M" in seen[0]
    assert "-C" in seen[0]
    assert "--use-mailmap" in seen[0]


def test_gather_omits_move_flags_when_disabled() -> None:
    seen: list[list[str]] = []
    with patch(
        _MOCK_GIT,
        side_effect=_dispatch_git(on_blame=seen.append, reject_mailmap=True),
    ):
        gather_blame_coverage(
            ["x.py"],
            Path("/fake"),
            git=GitConfig(detect_moves=False, use_mailmap=False),
        )
    assert "-w" in seen[0]
    assert "-M" not in seen[0]
    assert "-C" not in seen[0]
    assert "--use-mailmap" not in seen[0]
    assert "--no-use-mailmap" not in seen[0]


def test_gather_rejects_old_git() -> None:
    with (
        patch(_MOCK_GIT, side_effect=_dispatch_git(version="git version 2.19.0")),
        pytest.raises(ValueError, match=r"requires Git 2\.23"),
    ):
        gather_blame_coverage(["x.py"], Path("/fake"))


def test_gather_wraps_unparseable_git_version() -> None:
    with (
        patch(_MOCK_GIT, side_effect=_dispatch_git(version="not a version")),
        pytest.raises(GitRequirementError, match="Could not parse"),
    ):
        gather_blame_coverage(["x.py"], Path("/fake"))


def test_gather_honors_git_config_ignore_revs(tmp_path: Path) -> None:
    (tmp_path / "custom-revs").write_text("a" * 40 + "\n", encoding="utf-8")
    seen: list[list[str]] = []
    with patch(
        _MOCK_GIT,
        side_effect=_dispatch_git(on_blame=seen.append, config_value="custom-revs"),
    ):
        result = gather_blame_coverage(
            ["x.py"],
            tmp_path,
            git=GitConfig(blame_ignore_revs_file="missing-revs"),
        )
    ignore_args = [arg for arg in seen[0] if arg.startswith("--ignore-revs-file=")]
    assert any(arg.endswith("custom-revs") for arg in ignore_args)
    assert result.ignore_revs_applied is True
    assert result.ignore_revs_file == "custom-revs"


def test_gather_passes_ignore_revs_file(tmp_path: Path) -> None:
    (tmp_path / ".git-blame-ignore-revs").write_text("a" * 40 + "\n", encoding="utf-8")
    seen: list[list[str]] = []
    with patch(_MOCK_GIT, side_effect=_dispatch_git(on_blame=seen.append)):
        result = gather_blame_coverage(["x.py"], tmp_path)
    assert any(
        arg.startswith("--ignore-revs-file=") and arg.endswith(".git-blame-ignore-revs")
        for arg in seen[0]
    )
    assert result.ignore_revs_applied is True
    assert result.ignore_revs_file == ".git-blame-ignore-revs"


_PINNED = "2026-05-01T12:00:00+00:00"
_BOB_DATE = "2026-05-02T12:00:00+00:00"
_THIRD_DATE = "2026-05-03T12:00:00+00:00"
_AS_OF = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _owner_handles(result: OwnershipMap, path: str) -> list[str]:
    return [owner.handle for owner in result.paths[path].owners]


def test_formatter_does_not_steal_blame_via_mass_refactor(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f'x = "{value}"\n', encoding="utf-8")
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=_PINNED)
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f"x = '{value}'\n", encoding="utf-8")
    git_commit(repo, "bob formats", author="Bob", email="bob@example.com", date=_BOB_DATE)
    result = analyze_ownership(
        repo,
        Config(analysis=AnalysisConfig(confidence_threshold=0.0)),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    assert _owner_handles(result, "a.py")[0] == "alice@example.com"
    assert result.analysis_completeness.ignore_revs_applied is False


def test_formatter_ignored_via_ignore_revs_file(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f'x = "{value}"\n', encoding="utf-8")
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=_PINNED)
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f"x = '{value}'\n", encoding="utf-8")
    bob_sha = git_commit(repo, "bob formats", author="Bob", email="bob@example.com", date=_BOB_DATE)
    (repo / ".git-blame-ignore-revs").write_text(f"{bob_sha}\n", encoding="utf-8")
    result = analyze_ownership(
        repo,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    assert _owner_handles(result, "a.py")[0] == "alice@example.com"
    assert result.analysis_completeness.ignore_revs_applied is True
    assert result.analysis_completeness.ignore_revs_file == ".git-blame-ignore-revs"


def test_moved_file_keeps_original_author(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "a.py").write_text(
        "def compute_total(items):\n    return sum(item.amount for item in items)\n",
        encoding="utf-8",
    )
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=_PINNED)
    (repo / "b.py").write_text(
        "def compute_total(items):\n    return sum(item.amount for item in items)\n",
        encoding="utf-8",
    )
    (repo / "a.py").unlink()
    git_commit(repo, "bob moves", author="Bob", email="bob@example.com", date=_BOB_DATE)
    blame = gather_blame_coverage(["b.py"], repo)
    assert blame.coverage["b.py"].get("alice@example.com", 0.0) > 0.5


def test_whitespace_reindent_does_not_transfer_ownership(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "indent.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=_PINNED)
    (repo / "indent.py").write_text("def foo():\n        return 1\n", encoding="utf-8")
    git_commit(repo, "bob reindents", author="Bob", email="bob@example.com", date=_BOB_DATE)
    result = analyze_ownership(
        repo,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    assert _owner_handles(result, "indent.py")[0] == "alice@example.com"


def test_mailmap_collapses_three_addresses(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    target = repo / "owned.py"
    target.write_text("v1\n", encoding="utf-8")
    git_commit(repo, "work", author="Alice", email="alice@work.com", date=_PINNED)
    target.write_text("v2\n", encoding="utf-8")
    git_commit(repo, "home", author="Alice", email="alice@home.com", date=_BOB_DATE)
    target.write_text("v3\n", encoding="utf-8")
    git_commit(repo, "old", author="Alice", email="alice@old.com", date=_THIRD_DATE)
    (repo / ".mailmap").write_text(
        "Alice Example <alice@example.com> <alice@work.com>\n"
        "Alice Example <alice@example.com> <alice@home.com>\n"
        "Alice Example <alice@example.com> <alice@old.com>\n",
        encoding="utf-8",
    )
    base = Config(
        analysis=AnalysisConfig(confidence_threshold=0.0),
        git=GitConfig(mass_refactor_file_fraction=0.0),
    )
    merged = analyze_ownership(repo, base, as_of=_AS_OF, analysis_ref="test")
    assert _owner_handles(merged, "owned.py") == ["alice@example.com"]
    assert merged.paths["owned.py"].qualified_owner_count == 1
    assert merged.analysis_completeness.mailmap_applied is True
    assert merged.analysis_completeness.mailmap_file == ".mailmap"

    raw = analyze_ownership(
        repo,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0),
            git=GitConfig(mass_refactor_file_fraction=0.0, use_mailmap=False),
        ),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    assert set(_owner_handles(raw, "owned.py")) == {
        "alice@work.com",
        "alice@home.com",
        "alice@old.com",
    }
    assert raw.paths["owned.py"].qualified_owner_count == 3
    assert raw.analysis_completeness.mailmap_applied is False


def test_gitattributes_generated_directory_excluded_before_blame(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "generated").mkdir()
    (repo / "generated" / "out.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("y = 2\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("generated/** linguist-generated\n", encoding="utf-8")
    git_commit(repo, "init", author="Alice", email="alice@example.com", date=_PINNED)
    blamed: list[str] = []
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    with patch(_MOCK_BLAME, side_effect=_record_blame(blamed)):
        result = analyze_ownership(repo, config, as_of=_AS_OF, analysis_ref="test")
    assert "generated/out.py" not in result.paths
    assert "src/main.py" in result.paths
    assert "generated/out.py" not in blamed
    assert "src/main.py" in blamed
    assert result.analysis_completeness.excluded_gitattributes == 1
    assert result.analysis_completeness.excluded_static == 0

    blamed_off: list[str] = []
    disabled = Config(
        analysis=AnalysisConfig(
            min_commits=1,
            confidence_threshold=0.0,
            respect_gitattributes=False,
        )
    )
    with patch(_MOCK_BLAME, side_effect=_record_blame(blamed_off)):
        off = analyze_ownership(repo, disabled, as_of=_AS_OF, analysis_ref="test")
    assert "generated/out.py" in off.paths
    assert "generated/out.py" in blamed_off
    assert off.analysis_completeness.excluded_gitattributes == 0


def test_gitattributes_and_static_exclusions_are_not_double_counted(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "lib.go").write_text("package v\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("ok\n", encoding="utf-8")
    (repo / "generated").mkdir()
    (repo / "generated" / "out.py").write_text("x = 2\n", encoding="utf-8")
    (repo / ".gitattributes").write_text(
        "vendor/** linguist-vendored\ngenerated/** linguist-generated\n",
        encoding="utf-8",
    )
    git_commit(repo, "init", author="Alice", email="alice@example.com", date=_PINNED)
    result = analyze_ownership(
        repo,
        Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0)),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    completeness = result.analysis_completeness
    assert "src/main.py" in result.paths
    assert "vendor/lib.go" not in result.paths
    assert "dist/bundle.js" not in result.paths
    assert "generated/out.py" not in result.paths
    assert completeness.excluded_gitattributes == 2
    assert completeness.excluded_static == 1
    assert completeness.excluded_gitattributes + completeness.excluded_static == 3
    assert any(
        gap.reason == "Excluded files: 2 gitattributes, 1 static." for gap in completeness.gaps
    )


def test_pathspec_limits_blame_to_requested_path() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["keep.py", "other.py"]),
        ("bob@example.com", _RECENT, ["other.py"]),
    ]
    blamed: list[str] = []
    with (
        patch(_MOCK_GIT, return_value=_mock_run(_make_git_log_output(commits))) as mock,
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_record_blame(blamed)),
    ):
        result = analyze_ownership(
            Path("/fake"),
            Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0)),
            as_of=_NOW,
            analysis_ref="deadbeef",
            pathspec=("keep.py",),
        )
    assert blamed == ["keep.py"]
    assert set(result.paths) == {"keep.py"}
    log_calls = [call.args[0] for call in mock.call_args_list if call.args[0][1] == "log"]
    assert log_calls
    assert "--" in log_calls[0]
    assert "keep.py" in log_calls[0]


def test_pathspec_directory_blames_only_matching_files() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["src/a.py", "src/b.py", "docs/readme.md"]),
    ]
    blamed: list[str] = []
    with (
        patch(_MOCK_GIT, return_value=_mock_run(_make_git_log_output(commits))),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_record_blame(blamed)),
    ):
        result = analyze_ownership(
            Path("/fake"),
            Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0)),
            as_of=_NOW,
            analysis_ref="deadbeef",
            pathspec=("src/",),
        )
    assert set(blamed) == {"src/a.py", "src/b.py"}
    assert set(result.paths) == {"src/a.py", "src/b.py"}


def test_retain_all_keeps_below_threshold_candidates() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("bob@example.com", _OLD, ["src/main.py"]),
    ]
    config = Config(
        analysis=AnalysisConfig(min_commits=1, top_n_owners=3, confidence_threshold=0.5),
    )
    with (
        patch(_MOCK_GIT, return_value=_mock_run(_make_git_log_output(commits))),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        kept = analyze_ownership(
            Path("/fake"),
            config,
            as_of=_NOW,
            analysis_ref="deadbeef",
            retain_all=True,
        )
        dropped = analyze_ownership(
            Path("/fake"),
            config,
            as_of=_NOW,
            analysis_ref="deadbeef",
        )
    po = kept.paths["src/main.py"]
    assert all(owner.confidence >= 0.5 for owner in po.owners)
    assert any(candidate.confidence < 0.5 for candidate in po.candidates)
    assert "bob@example.com" not in {owner.handle for owner in po.owners}
    assert "bob@example.com" in {candidate.handle for candidate in po.candidates}
    assert dropped.paths["src/main.py"].candidates == ()


def test_owner_total_matches_combine_available_signals() -> None:
    commits = [
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("alice@example.com", _RECENT, ["src/main.py"]),
        ("bob@example.com", _OLD, ["src/main.py"]),
    ]
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    with (
        patch(_MOCK_GIT, return_value=_mock_run(_make_git_log_output(commits))),
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(
            Path("/fake"),
            config,
            as_of=_NOW,
            analysis_ref="deadbeef",
            retain_all=True,
        )
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

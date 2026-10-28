"""Tests for checkowners.analyze module."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from checkowners.analyze import (
    _BLAME_DEADLINE,
    _BODY_END,
    _BODY_START,
    _COMMIT_SENTINEL,
    FREQUENCY_SHRINKAGE_PRIOR,
    MIN_GIT_VERSION,
    SOURCE_DATE_EPOCH_ENV,
    Contribution,
    GitRequirementError,
    _aggregate_contributions,
    _apply_copy,
    _apply_renames,
    _author_counts,
    _author_emails,
    _author_index,
    _blame_for_path,
    _blame_inputs_token,
    _BlamePass,
    _blob_shas,
    _blobs_at_revision,
    _bounded_priors,
    _cap_mechanical_authors,
    _clamp,
    _classify_freshness,
    _collapse_directory_moves,
    _CommitHistory,
    _common_directory,
    _current_commits,
    _detect_decay,
    _display_ignore_revs_path,
    _elapsed_days,
    _existing_file,
    _filter_excluded,
    _filter_nonexistent,
    _follow_path,
    _frequency_score,
    _gather_review_coverage,
    _get_commit_history,
    _git_baseline_reason,
    _git_stdout,
    _gitattributes_pattern_matches,
    _historical_expertise,
    _insufficient_history,
    _is_ancestor,
    _is_excluded,
    _is_shallow_repository,
    _linguist_excluded_paths,
    _logical_id,
    _mass_shas,
    _parent_dir,
    _parse_blame_detail,
    _parse_blame_output,
    _parse_gitattributes,
    _parse_log_output,
    _parse_status_paths,
    _qualify_authors,
    _RawCommit,
    _recency_score,
    _remap_contributions,
    _rename_commit_shas,
    _reused_fraction,
    _scale_lineage_confidence,
    _status_score,
    _stdout_has_rename,
    _survival_by_path,
    _window_has_renames,
    _with_lineage,
    analysis_epoch,
    analyze_ownership,
    apply_completeness,
    assign_coauthors,
    canonical_emails,
    collect_analysis,
    combine_available_signals,
    consume_analysis_outcome,
    effective_half_life,
    frequency_prior_for,
    gather_blame_coverage,
    head_commit_datetime,
    head_commit_sha,
    parse_as_of,
    parse_git_version,
    parse_source_date_epoch,
    resolve_as_of,
    run_git,
    score_owners,
    set_blame_cache_enabled,
    set_blame_verbose,
    signal_reliabilities,
    signal_weights,
)
from checkowners.drift import detect_drift
from checkowners.explain import signal_tuples
from checkowners.models import (
    AnalysisCompleteness,
    AnalysisConfig,
    Config,
    ConfiguredMergeStrategy,
    DecayConfig,
    GitConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    QualificationConfig,
    ScoringConfig,
)
from checkowners.state import BlameLines, HistoryBaseline, PathMove, _state_path, write_state
from tests.conftest import GitRepo, git_commit, init_git_repo


def _make_git_log_output(
    commits: list[tuple[str, datetime, list[str]]],
) -> str:
    """Build a fake git log stdout string from (author, timestamp, files) triples."""
    chunks: list[str] = []
    for index, (author, ts, files) in enumerate(commits, start=1):
        sha = f"{index:040x}"
        chunk = (
            f"COMMIT_START\n{sha}\n{author}\n{ts.isoformat()}\n"
            f"\nsubject\nBODY_START\n\nBODY_END\n" + "\n".join(files)
        )
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


def _git_subcommand(cmd: list[str]) -> str:
    index = 1
    while index < len(cmd) and cmd[index] == "-c":
        index += 2
    if index >= len(cmd):
        return ""
    return cmd[index]


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
        command = _git_subcommand(cmd)
        if command == "version":
            return _mock_run(version)
        if command == "config":
            if config_value is None:
                raise subprocess.CalledProcessError(1, cmd)
            return _mock_run(config_value)
        if command == "ls-files":
            return _mock_run(ls_files)
        if command == "log":
            return _mock_run(log_stdout)
        if command == "blame":
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

    args = next(call.args[0] for call in mock.call_args_list if "--name-status" in call.args[0])
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


def test_concentration_is_stable_when_top_n_owners_changes() -> None:
    commits = (
        [("alice@example.com", _RECENT, ["f.py"])] * 10
        + [("bob@example.com", _RECENT, ["f.py"])] * 7
        + [("carol@example.com", _RECENT, ["f.py"])] * 3
        + [("dan@example.com", _RECENT, ["f.py"])] * 1
    )
    stdout = _make_git_log_output(commits)

    def run(cap: int) -> OwnershipMap:
        config = Config(
            analysis=AnalysisConfig(min_commits=1, top_n_owners=cap, confidence_threshold=0.0),
        )
        with (
            patch(_MOCK_GIT, return_value=_mock_run(stdout)),
            patch(_MOCK_EXIST, side_effect=_passthrough),
            patch(_MOCK_BLAME, side_effect=_no_blame),
        ):
            return analyze_ownership(Path("/fake"), config, as_of=_NOW, analysis_ref="deadbeef")

    narrow = run(1).paths["f.py"]
    wide = run(4).paths["f.py"]
    assert len(narrow.owners) < len(wide.owners)
    assert [owner.handle for owner in narrow.scored_owners] == [
        owner.handle for owner in wide.scored_owners
    ]
    assert [owner.ownership_score for owner in narrow.scored_owners] == [
        owner.ownership_score for owner in wide.scored_owners
    ]


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
    assert warnings[0].status == "inactive"


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


def test_score_owners_empty_qualified_returns_nothing() -> None:
    assert (
        score_owners(
            {},
            {},
            {},
            max_commits=1,
            scoring=ScoringConfig(),
            now=_NOW,
            blame_available=False,
            review_available=False,
        )
        == ()
    )


def test_decay_without_freshness_uses_the_day_threshold() -> None:
    stale_at = _NOW - timedelta(days=200)
    recent = OwnerEntry(handle="alice", ownership_score=0.5, last_commit=_NOW, commits=1)
    stale = OwnerEntry(handle="bob", ownership_score=0.4, last_commit=stale_at, commits=1)
    missing = OwnerEntry(handle="carol", ownership_score=0.3, last_commit=stale_at, commits=1)
    qualified = {
        "alice": Contribution(commits=1, last_commit=_NOW, credit=1.0),
        "bob": Contribution(commits=1, last_commit=stale_at, credit=1.0),
    }
    warnings = _detect_decay("src/a.py", qualified, (recent, stale, missing), 180, _NOW)
    assert [warning.handle for warning in warnings] == ["bob"]
    assert warnings[0].status == "inactive"


def test_analyze_score_scale_with_and_without_review_provider() -> None:
    contrib = Contribution(commits=3, last_commit=_NOW, credit=3.0)
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
    assert offline[0].evidence_quality == pytest.approx(0.70)
    assert online[0].evidence_quality == pytest.approx(0.85)
    assert offline[0].evidence_quality != online[0].evidence_quality


def test_combine_available_signals_recency_only_is_unit_scale() -> None:
    scoring = ScoringConfig()
    score, quality = combine_available_signals(
        {
            "recency": (1.0, True),
            "frequency": (0.0, False),
            "blame": (0.0, False),
            "review": (0.0, False),
            "change_survival": (0.0, False),
        },
        signal_weights(scoring),
        signal_reliabilities(scoring),
    )
    assert score == pytest.approx(1.0)
    assert 0.0 <= score <= 1.0
    assert quality == pytest.approx(0.30)


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


def test_recency_score_one_day_half_life_is_one_half() -> None:
    aged = _recency_score(_NOW - timedelta(seconds=86400), _NOW, 1)
    assert aged == pytest.approx(0.5)


def test_clamp_and_frequency_stay_inside_the_unit_interval() -> None:
    assert _clamp(1.5) == 1.0
    assert _clamp(-0.2) == 0.0
    assert _frequency_score(3, 2) == 1.0
    assert _frequency_score(1, 4) == pytest.approx(0.25)


def test_elapsed_days_counts_whole_utc_days() -> None:
    assert _elapsed_days(_NOW, _NOW) == 0
    assert _elapsed_days(_NOW + timedelta(seconds=30), _NOW) == 0
    assert _elapsed_days(_NOW - timedelta(seconds=86400), _NOW) == 1


def test_historical_expertise_is_the_share_of_commits() -> None:
    assert _historical_expertise(0, 0) == 0.0
    assert _historical_expertise(1, 0) == 1.0
    assert _historical_expertise(3, 1) == pytest.approx(0.75)


def test_freshness_uses_exact_boundaries_and_status_names() -> None:
    tied = Contribution(commits=2, last_commit=_NOW, credit=2.0, later_commits=2)
    superseded = Contribution(commits=2, last_commit=_NOW, credit=2.0, later_commits=3)
    quiet = Contribution(commits=2, last_commit=_NOW, credit=2.0)
    recent = Contribution(commits=2, last_commit=_NOW, credit=2.0, cadence_days=10.0)

    def status(
        contrib: Contribution,
        *,
        days: int,
        path_last: datetime = _NOW,
        survival: float | None = None,
    ) -> str:
        return _classify_freshness(
            contrib,
            days=days,
            half_life_days=10.0,
            path_last=path_last,
            ceiling_days=30,
            threshold_days=180,
            survival=survival,
        )

    assert status(tied, days=0) == "stable"
    assert status(superseded, days=0) == "superseded"
    assert status(superseded, days=0, survival=0.0) == "superseded"
    assert status(superseded, days=0, survival=0.19) == "superseded"
    assert status(superseded, days=0, survival=0.2) == "stable"
    assert status(quiet, days=0, survival=1.0) == "stable"
    assert status(quiet, days=180) == "stable"
    assert status(quiet, days=181) == "inactive"
    assert status(recent, days=10) == "stable"
    assert status(recent, days=10, path_last=_NOW - timedelta(days=1)) == "stable"
    assert status(recent, days=11, path_last=_NOW - timedelta(days=1)) == "inactive"
    assert status(recent, days=30) == "stable"


def test_qualification_and_frequency_prior_follow_the_strategy() -> None:
    author = Contribution(commits=0, last_commit=_NOW, credit=0.0)
    authors = {"ada": author}
    assert (
        _qualify_authors(
            authors,
            min_commits=1,
            strategy="threshold",
            path_blame={"ada": 1.0},
            blame_override=0.5,
        )
        == {}
    )
    assert "ada" in _qualify_authors(
        authors,
        min_commits=1,
        strategy="adaptive",
        path_blame={"ada": 0.5},
        blame_override=0.5,
    )
    assert (
        _qualify_authors(
            authors,
            min_commits=1,
            strategy="adaptive",
            path_blame={},
            blame_override=0.5,
        )
        == {}
    )
    assert frequency_prior_for("adaptive") == FREQUENCY_SHRINKAGE_PRIOR
    assert frequency_prior_for("threshold") == 0.0


def test_fixed_half_life_ignores_cadence() -> None:
    scoring = ScoringConfig(recency_strategy="fixed", recency_half_life_days=90)
    assert effective_half_life(3.0, scoring) == 90.0


def test_missing_signal_weights_do_not_invent_a_score() -> None:
    empty_score, empty_quality = combine_available_signals({}, {}, {})
    assert empty_score == 0.0
    assert empty_quality == 0.0
    unknown_score, unknown_quality = combine_available_signals({"mystery": (1.0, True)}, {}, {})
    assert unknown_score == 0.0
    assert unknown_quality == 0.0
    score, quality = combine_available_signals(
        {"known": (0.0, False), "mystery": (1.0, True)},
        {"known": 1.0, "mystery": 1.0},
        {"known": 1.0},
    )
    assert score == pytest.approx(1.0)
    assert quality == pytest.approx(0.5)
    scaled, scaled_quality = combine_available_signals(
        {"recency": (1.0, True)},
        {"recency": 1.0},
        {"recency": 0.5},
    )
    assert scaled == pytest.approx(1.0)
    assert scaled_quality == pytest.approx(0.5)


def test_score_owners_locks_defaults_and_unavailable_signals() -> None:
    scoring = ScoringConfig(
        recency_strategy="fixed",
        recency_half_life_days=90,
        recency_weight=0.25,
        frequency_weight=0.25,
        blame_weight=0.25,
        review_weight=0.25,
        change_survival_weight=0.0,
        recency_reliability=0.5,
        frequency_reliability=0.5,
        blame_reliability=0.5,
        review_reliability=0.5,
    )
    current = Contribution(commits=2, last_commit=_NOW, credit=2.0)
    scored = score_owners(
        {"ada": current},
        {},
        {},
        max_commits=2,
        scoring=scoring,
        now=_NOW,
        blame_available=True,
        review_available=True,
    )
    entry = scored[0]
    assert entry.commits == 2
    assert entry.last_commit == _NOW
    assert entry.evidence_quality == pytest.approx(0.5)
    assert entry.freshness is not None
    assert entry.freshness.status == "stable"
    assert entry.freshness.half_life_days == 90.0
    assert entry.freshness.historical_expertise == 1.0
    assert entry.freshness.maintenance_recency == pytest.approx(1.0)
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.total == pytest.approx(entry.ownership_score)
    assert entry.score_breakdown.blame.score == 0.0
    assert entry.score_breakdown.review.score == 0.0
    assert entry.score_breakdown.frequency.score == pytest.approx(1.0)

    hidden = score_owners(
        {"ada": current},
        {"ada": 1.0},
        {"ada": 1.0},
        max_commits=2,
        scoring=scoring,
        now=_NOW,
        blame_available=False,
        review_available=False,
    )
    assert hidden[0].score_breakdown is not None
    assert hidden[0].score_breakdown.blame.score == 0.0
    assert hidden[0].score_breakdown.review.score == 0.0

    aged = Contribution(commits=2, last_commit=_NOW - timedelta(days=181), credit=2.0)
    aged_score = score_owners(
        {"ada": aged},
        {},
        {},
        max_commits=2,
        scoring=scoring,
        now=_NOW,
        blame_available=False,
        review_available=False,
    )
    assert aged_score[0].freshness is not None
    assert aged_score[0].freshness.status == "inactive"

    shrunk = score_owners(
        {"ada": current},
        {},
        {},
        max_commits=2,
        scoring=scoring,
        now=_NOW,
        blame_available=False,
        review_available=False,
        frequency_prior=3.0,
    )
    assert shrunk[0].score_breakdown is not None
    assert shrunk[0].score_breakdown.frequency.score == pytest.approx(2 / 5)

    maintained = Contribution(
        commits=2,
        last_commit=_NOW - timedelta(days=20),
        credit=2.0,
        cadence_days=10.0,
    )
    maintained_score = score_owners(
        {"ada": maintained},
        {},
        {},
        max_commits=2,
        scoring=ScoringConfig(recency_strategy="adaptive"),
        now=_NOW,
        blame_available=False,
        review_available=False,
    )
    assert maintained_score[0].freshness is not None
    assert maintained_score[0].freshness.status == "stable"


@given(
    cadence=st.floats(min_value=0.01, max_value=100_000, allow_nan=False, allow_infinity=False),
    floor=st.integers(min_value=1, max_value=100),
    ceiling=st.integers(min_value=101, max_value=5_000),
)
def test_effective_half_life_is_bounded(cadence: float, floor: int, ceiling: int) -> None:
    scoring = ScoringConfig(
        recency_strategy="adaptive",
        recency_half_life_floor_days=floor,
        recency_half_life_ceiling_days=ceiling,
    )
    half_life = effective_half_life(cadence, scoring)
    assert floor <= half_life <= ceiling


def test_adaptive_freshness_keeps_stable_owner(tmp_path: Path) -> None:
    stable = [
        ("alice@example.com", _NOW - timedelta(days=days), ["crypto.py"])
        for days in (1200, 800, 400)
    ]
    churn = [
        ("alice@example.com", _NOW - timedelta(days=days), ["hot.py"]) for days in (86, 84, 82, 80)
    ]
    churn.extend(
        ("bob@example.com", _NOW - timedelta(days=days), ["hot.py"])
        for days in (16, 14, 12, 10, 8, 6, 4, 2)
    )
    stdout = _make_git_log_output([*stable, *churn])
    config = Config(
        analysis=AnalysisConfig(
            lookback_days="adaptive",
            min_commits=1,
            top_n_owners=5,
            confidence_threshold=0.0,
        ),
    )
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text("crypto.py alice@example.com\n", encoding="utf-8")
    with (
        patch(_MOCK_GIT, return_value=_mock_run(stdout)) as mock,
        patch(_MOCK_EXIST, side_effect=_passthrough),
        patch(_MOCK_BLAME, side_effect=_no_blame),
    ):
        result = analyze_ownership(tmp_path, config, as_of=_NOW, analysis_ref="deadbeef")
        drift = detect_drift(tmp_path, result, config, codeowners_path=codeowners)
    log_args = next(call.args[0] for call in mock.call_args_list if "--name-status" in call.args[0])
    assert not any(arg.startswith("--since=") for arg in log_args)
    stable_owner = result.paths["crypto.py"].owners[0]
    assert stable_owner.handle == "alice@example.com"
    assert stable_owner.ownership_score >= 0.3
    assert stable_owner.freshness is not None
    assert stable_owner.freshness.status == "stable"
    assert result.paths["crypto.py"].decay_warnings == ()
    assert all(entry.path != "crypto.py" for entry in drift.missing)
    previous = next(
        owner for owner in result.paths["hot.py"].owners if owner.handle.startswith("alice")
    )
    assert previous.freshness is not None
    assert previous.freshness.status == "superseded"
    assert previous.freshness.active_expertise < 0.25


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


def test_rename_keeps_history_on_the_current_path(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/api/billing.py": "amount = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("src/api/billing.py", "amount = 2\n")
    git_repo.commit(
        "edit",
        author="Alice",
        email="alice@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    git_repo.rename("src/api/billing.py", "services/payments/billing.py")
    git_repo.commit(
        "rename",
        author="Bob",
        email="bob@example.com",
        date="2026-05-03T12:00:00+00:00",
    )
    git_repo.write("services/payments/billing.py", "amount = 3\n")
    git_repo.commit(
        "edit again",
        author="Bob",
        email="bob@example.com",
        date="2026-05-04T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        _score_config(),
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    handles = {owner.handle for owner in ownership.paths["services/payments/billing.py"].owners}
    assert "alice@example.com" in handles
    assert "src/api/billing.py" not in ownership.paths


def _owner(ownership: OwnershipMap, path: str, handle: str) -> OwnerEntry:
    return next(owner for owner in ownership.paths[path].owners if owner.handle == handle)


def test_full_rewrite_supersedes_the_original_author(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/pay.py": "a = 1\nb = 2\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("src/pay.py", "x = 9\ny = 8\n")
    git_repo.commit(
        "replace",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    alice = _owner(ownership, "src/pay.py", "alice@example.com")
    breakdown = alice.score_breakdown
    freshness = alice.freshness
    assert breakdown is not None
    assert freshness is not None
    assert breakdown.change_survival.available
    assert breakdown.change_survival.score == pytest.approx(0.0, abs=0.05)
    assert freshness.status == "superseded"


def test_untouched_file_keeps_full_survival(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/stay.py": "a = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    alice = _owner(ownership, "src/stay.py", "alice@example.com")
    breakdown = alice.score_breakdown
    freshness = alice.freshness
    assert breakdown is not None
    assert freshness is not None
    assert breakdown.change_survival.score == pytest.approx(1.0, abs=0.05)
    assert freshness.status == "stable"


def test_rename_keeps_change_survival(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/old.py": "a = 1\nb = 2\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    git_repo.rename("src/old.py", "src/new.py")
    git_repo.commit(
        "rename",
        author="Bob",
        email="bob@example.com",
        date="2026-05-21T12:00:00+00:00",
    )
    git_repo.write("src/new.py", "a = 1\nb = 2\nc = 3\n")
    git_repo.commit(
        "append",
        author="Bob",
        email="bob@example.com",
        date="2026-05-22T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    alice = _owner(ownership, "src/new.py", "alice@example.com")
    breakdown = alice.score_breakdown
    freshness = alice.freshness
    assert breakdown is not None
    assert freshness is not None
    assert breakdown.change_survival.score == pytest.approx(1.0, abs=0.05)
    assert freshness.status == "stable"


def test_directory_rename_keeps_every_file(git_repo: GitRepo) -> None:
    for index in range(300):
        git_repo.write(f"old/file_{index}.py", f"value = {index}\n")
    git_repo.commit(
        "seed directory",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.rename("old", "new")
    git_repo.commit(
        "rename directory",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    started = time.perf_counter()
    history = _get_commit_history(
        git_repo.path,
        None,
        _NOW,
        follow_renames=True,
        rename_similarity=50,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert any(move.kind == "D" for moves in history.moves.values() for move in moves)
    contributions = _aggregate_contributions(_current_commits(history))
    for index in range(300):
        authors = contributions[f"new/file_{index}.py"]
        assert authors["alice@example.com"].commits >= 1


def test_copy_inherits_history_only_when_the_source_changes(git_repo: GitRepo) -> None:
    body = "".join(f"line = {index}\n" for index in range(20))
    git_repo.commit_files(
        {"src.py": body},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("src.py", body + "line = extra\n")
    git_repo.copy("src.py", "copied.py")
    git_repo.commit(
        "copy and edit source",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    git_repo.copy("src.py", "plain.py")
    git_repo.commit(
        "copy untouched source",
        author="Bob",
        email="bob@example.com",
        date="2026-05-03T12:00:00+00:00",
    )
    history = _get_commit_history(git_repo.path, None, _NOW, follow_renames=True)
    contributions = _aggregate_contributions(_current_commits(history))
    assert contributions["copied.py"]["alice@example.com"].commits >= 1
    assert contributions["src.py"]["alice@example.com"].commits >= 1
    assert "alice@example.com" not in contributions["plain.py"]


def test_lineage_edges_cover_scores_copies_and_survival(tmp_path: Path) -> None:
    assert _status_score("R") == 100
    assert _status_score("R120") == 100
    assert _status_score("C0") == 0
    files, moves = _parse_status_paths(
        "\nplain.py\nR100\told.py\tnew.py\nC080\tsrc.py\tcopied.py\nM\tedited.py\nX\tweird\n"
    )
    assert "plain.py" in files
    assert "X\tweird" in files
    assert {move.kind for move in moves} == {"R", "C"}
    first = _logical_id("a.py", set(), "n")
    assert _logical_id("a.py", {first}, "n") != first
    assert _parent_dir("file.py") == ""
    assert _common_directory([]) == ""
    assert _common_directory(["file.py", "a/b.py"]) == ""
    assert _common_directory(["a/b.py", "c/d.py"]) == ""
    assert _common_directory(["/a/file.py", "/b/file.py"]) == ""
    assert _common_directory(["a/b/c.py", "a/b/d.py"]) == "a/b"
    single = (PathMove(kind="R", score=100, source="a.py", dest="b.py"),)
    assert _collapse_directory_moves(single, {}) == single
    roots = (
        PathMove(kind="R", score=100, source="a.py", dest="b.py"),
        PathMove(kind="R", score=100, source="c.py", dest="d.py"),
    )
    assert _collapse_directory_moves(roots, {}) == roots
    same_prefix = (
        PathMove(kind="R", score=90, source="old/a.py", dest="old/b.py"),
        PathMove(kind="R", score=90, source="old/c.py", dest="old/d.py"),
    )
    assert _collapse_directory_moves(same_prefix, {}) == same_prefix
    mismatch = (
        PathMove(kind="R", score=90, source="old/a.py", dest="new/a.py"),
        PathMove(kind="R", score=90, source="old/b.py", dest="new/c.py"),
    )
    living = {"old/a.py": "1", "old/b.py": "2"}
    assert _collapse_directory_moves(mismatch, living) == mismatch
    left_behind = (
        PathMove(kind="R", score=90, source="old/a.py", dest="new/a.py"),
        PathMove(kind="R", score=90, source="old/b.py", dest="new/b.py"),
    )
    assert _collapse_directory_moves(left_behind, {**living, "old/c.py": "3"}) == left_behind
    collapsed = _collapse_directory_moves(
        (
            PathMove(kind="R", score=80, source="old/a.py", dest="new/a.py"),
            PathMove(kind="C", score=70, source="old/a.py", dest="copy.py"),
            PathMove(kind="R", score=90, source="old/b.py", dest="new/b.py"),
        ),
        living,
    )
    assert {move.kind for move in collapsed} == {"C", "D"}

    names: dict[str, str] = {}
    origin: dict[str, str] = {}
    priors: dict[str, list[str]] = {}
    confidence: dict[str, int] = {}
    _apply_renames(
        names,
        origin,
        priors,
        confidence,
        (PathMove(kind="R", score=60, source="old.py", dest="new.py"),),
        "nonce",
    )
    logical = names["new.py"]
    assert priors[logical] == ["old.py"]
    assert confidence[logical] == 60
    assert (
        _apply_copy(
            names,
            origin,
            priors,
            confidence,
            PathMove(kind="C", score=70, source="absent.py", dest="copy.py"),
            "nonce",
        )
        is None
    )
    assert "copy.py" in names
    assert _bounded_priors(["old.py", "older.py", "old.py"], "new.py") == ("old.py", "older.py")
    assert _bounded_priors(["old.py", "new.py"], "new.py") == ("old.py",)

    earlier = "a" * 40
    later = "b" * 40
    history = _with_lineage(
        _CommitHistory(
            commits=[
                _RawCommit("bob@example.com", _NOW, ("src.py", "copied.py"), sha=later),
                _RawCommit(
                    "alice@example.com",
                    _NOW - timedelta(days=1),
                    ("src.py",),
                    sha=earlier,
                ),
            ],
            merge_strategy="rebase",
            co_author_count=0,
            moves={
                later: (PathMove(kind="C", score=80, source="src.py", dest="copied.py"),),
            },
        )
    )
    assert history.logical_ids["copied.py"]
    assert "src.py" in history.priors["copied.py"]
    unlinked = _with_lineage(
        _CommitHistory(
            commits=[_RawCommit("bob@example.com", _NOW, ("copied.py",), sha=later)],
            merge_strategy="rebase",
            co_author_count=0,
            moves={
                later: (PathMove(kind="C", score=80, source="missing.py", dest="copied.py"),),
            },
        )
    )
    assert unlinked.priors["copied.py"] == ("missing.py",)
    assert unlinked.confidence["copied.py"] == pytest.approx(0.8)
    seeded = _current_commits(history)
    assert any("copied.py" in commit.files and commit.sha == earlier for commit in seeded)
    orphan = {"missing": {"alice@example.com": Contribution(1, _NOW, credit=1.0)}}
    assert _remap_contributions(orphan, {}) is orphan
    assert _remap_contributions(orphan, {"other": "kept.py"}) == {}

    owner = OwnerEntry("alice@example.com", 0.8, _NOW, 1, evidence_quality=0.8)
    other = OwnerEntry("bob@example.com", 0.4, _NOW, 1)
    ownership = OwnershipMap(
        paths={
            "a.py": PathOwnership(
                owners=(owner,),
                qualified_owner_count=1,
                candidates=(owner,),
                scored_owners=(owner,),
            ),
            "b.py": PathOwnership(owners=(other,), qualified_owner_count=1),
        },
        last_analyzed=_NOW,
    )
    assert _scale_lineage_confidence(ownership, {"b.py": 1.0}) is ownership
    scaled = _scale_lineage_confidence(ownership, {"a.py": 0.5, "b.py": 1.0})
    assert scaled.paths["a.py"].owners[0].evidence_quality == pytest.approx(0.4)
    assert scaled.paths["b.py"].owners[0].evidence_quality == 1.0

    assert _author_counts({"gone": 3, "zero": 0, later: 2}, {later: "bob@example.com"}) == {
        "bob@example.com": 2
    }
    assert _blobs_at_revision(tmp_path, "", ["a.py"]) == {}
    assert _blobs_at_revision(tmp_path, later, ["a.py\n"]) == {}
    failed = subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="")
    blob = "c" * 40
    found = subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout=f"missing missing\n{blob} blob 4\n",
        stderr="",
    )
    with patch("checkowners.analyze.run_git", side_effect=[failed, found]):
        assert _blobs_at_revision(tmp_path, later, ["a.py"]) == {}
        assert _blobs_at_revision(tmp_path, later, ["gone.py", "a.py"]) == {"a.py": blob}

    empty = Contribution(1, _NOW, credit=1.0, last_sha="")
    present = Contribution(1, _NOW, credit=1.0, last_sha=later)
    ratios = _survival_by_path(
        {
            "skip.py": {"bob@example.com": present},
            "kept.py": {"gone@example.com": empty, "bob@example.com": present},
        },
        {"kept.py"},
        {"kept.py": {"bob@example.com": 1}},
        {},
        tmp_path,
        GitConfig(),
        (),
    )
    assert "skip.py" not in ratios
    assert ratios["kept.py"]["gone@example.com"] is None
    with (
        patch("checkowners.analyze._blobs_at_revision", return_value={"kept.py": blob}),
        patch(
            "checkowners.analyze.gather_blame_coverage",
            return_value=_BlamePass(lines={"kept.py": {}}),
        ),
    ):
        zero = _survival_by_path(
            {"kept.py": {"bob@example.com": present}},
            {"kept.py"},
            {"kept.py": {"bob@example.com": 1}},
            {},
            tmp_path,
            GitConfig(),
            (),
        )
    assert zero["kept.py"]["bob@example.com"] is None


def test_follow_renames_false_keeps_literal_paths(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"foo.py": "value = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.rename("foo.py", "bar.py")
    git_repo.commit(
        "rename",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    config = Config(
        analysis=AnalysisConfig(follow_renames=False, min_commits=1, confidence_threshold=0.0)
    )
    calls: list[list[str]] = []
    with patch("checkowners.analyze.run_git", _record_git(calls)):
        ownership = analyze_ownership(
            git_repo.path,
            config,
            as_of=_NOW,
            analysis_ref=git_repo.head_sha(),
        )
    assert any("--name-only" in argv for argv in calls)
    assert all(not any(arg.startswith("--find-renames=") for arg in argv) for argv in calls)
    handles = {owner.handle for owner in ownership.paths["bar.py"].owners}
    assert handles == {"bob@example.com"}


@given(
    st.lists(
        st.text(alphabet="abc", min_size=1, max_size=3),
        min_size=1,
        max_size=6,
        unique=True,
    )
)
def test_lineage_follow_stops(names: list[str]) -> None:
    forward = {names[index]: names[index + 1] for index in range(len(names) - 1)}
    assert _follow_path(names[0], forward) == names[-1]
    if len(names) > 1:
        forward[names[-1]] = names[0]
        assert _follow_path(names[0], forward) in set(names)


def _record_git(
    calls: list[list[str]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def spy(
        repo: Path,
        args: list[str],
        *,
        check: bool,
        paths: bool = False,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return run_git(repo, args, check=check, paths=paths, stdin=stdin)

    return spy


def test_blame_cache_matches_an_uncached_blame(
    git_repo: GitRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    paths = ["a.py"]
    first = gather_blame_coverage(paths, git_repo.path, git=GitConfig())
    calls: list[list[str]] = []
    set_blame_verbose(True)
    try:
        with patch("checkowners.analyze.run_git", _record_git(calls)):
            second = gather_blame_coverage(paths, git_repo.path, git=GitConfig())
        err = capsys.readouterr().err
    finally:
        set_blame_verbose(False)
    set_blame_cache_enabled(False)
    try:
        third = gather_blame_coverage(paths, git_repo.path, git=GitConfig())
    finally:
        set_blame_cache_enabled(True)
    assert first.coverage == second.coverage == third.coverage
    assert first.coverage
    assert _blamed_paths(calls) == []
    assert "blame cache: hits=1 misses=0" in err


def test_rejected_baseline_reuses_the_blame_cache(git_repo: GitRepo) -> None:
    config = _score_config()
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    _store(git_repo, config)
    narrowed = Config(
        analysis=AnalysisConfig(
            follow_renames=False,
            confidence_threshold=0.0,
            min_commits=1,
        )
    )
    calls: list[list[str]] = []
    with patch("checkowners.analyze.run_git", _record_git(calls)):
        outcome = collect_analysis(
            git_repo.path,
            narrowed,
            as_of=_NOW,
            analysis_ref=git_repo.head_sha(),
        )
    assert outcome.mode == "full"
    assert outcome.fallback
    assert _blamed_paths(calls) == []


def test_blame_cache_misses_when_the_key_changes(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    gather_blame_coverage(["a.py"], git_repo.path, git=GitConfig())

    def blamed(git: GitConfig | None = None) -> list[str]:
        calls: list[list[str]] = []
        chosen = GitConfig() if git is None else git
        with patch("checkowners.analyze.run_git", _record_git(calls)):
            gather_blame_coverage(["a.py"], git_repo.path, git=chosen)
        return _blamed_paths(calls)

    (git_repo.path / ".git-blame-ignore-revs").write_text(
        f"{git_repo.head_sha()}\n",
        encoding="utf-8",
    )
    assert blamed() == ["a.py"]
    (git_repo.path / ".mailmap").write_text(
        "Alias <alias@example.com> <alice@example.com>\n",
        encoding="utf-8",
    )
    assert blamed() == ["a.py"]
    assert blamed(GitConfig(detect_moves=False)) == ["a.py"]


def test_filter_excluded_removes_paths() -> None:
    contribs: dict[str, dict[str, Contribution]] = {
        "src/main.py": {"alice": Contribution(3, _NOW, credit=3.0)},
        "yarn.lock": {"alice": Contribution(5, _NOW, credit=5.0)},
        "dist/out.js": {"bob": Contribution(2, _NOW, credit=2.0)},
    }
    patterns = ("*.lock", "dist/**")
    filtered = _filter_excluded(contribs, patterns)
    assert "src/main.py" in filtered
    assert "yarn.lock" not in filtered
    assert "dist/out.js" not in filtered


def test_filter_nonexistent(tmp_path: Path) -> None:
    (tmp_path / "exists.py").write_text("x", encoding="utf-8")
    contribs: dict[str, dict[str, Contribution]] = {
        "exists.py": {"alice": Contribution(3, _NOW, credit=3.0)},
        "deleted.py": {"bob": Contribution(5, _NOW, credit=5.0)},
    }
    result = _filter_nonexistent(contribs, tmp_path)
    assert "exists.py" in result
    assert "deleted.py" not in result


def test_get_commit_history_subprocess_error() -> None:
    with (
        patch(_MOCK_GIT, side_effect=subprocess.CalledProcessError(128, "git")),
        pytest.raises(subprocess.CalledProcessError),
    ):
        _get_commit_history(Path("/fake"), _NOW - timedelta(days=180), _NOW)


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
        command = _git_subcommand(cmd)
        if command == "blame":
            return _mock_run(blame_stdout)
        if command == "version":
            return _mock_run("git version 2.40.0")
        if command == "config":
            raise subprocess.CalledProcessError(1, cmd)
        if command == "ls-files":
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
        0.70 * (result.analysis_completeness.score or 1.0)
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
    shallow_file = tmp_path / "shallow"
    echoed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="--is-shallow-repository\n",
        stderr="",
    )

    def fallback(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--git-path" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=f"{shallow_file.as_posix()}\n",
                stderr="",
            )
        return echoed

    with patch(_MOCK_GIT, side_effect=fallback):
        assert _is_shallow_repository(tmp_path) is False
    shallow_file.write_text("abc\n", encoding="utf-8")
    with patch(_MOCK_GIT, side_effect=fallback):
        assert _is_shallow_repository(tmp_path) is True

    def relative(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--git-path" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="shallow\n",
                stderr="",
            )
        return echoed

    with patch(_MOCK_GIT, side_effect=relative):
        assert _is_shallow_repository(tmp_path) is True
    with (
        patch(_MOCK_GIT, side_effect=relative),
        patch.object(Path, "is_file", side_effect=OSError("too long")),
    ):
        assert _is_shallow_repository(tmp_path) is False
    origin = init_git_repo(tmp_path / "origin")
    (origin / "README").write_text("full\n", encoding="utf-8")
    git_commit(
        origin,
        "init",
        author="Test",
        email="test@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    cloned = tmp_path / "cloned"
    subprocess.run(  # noqa: S603
        ["git", "clone", "--depth", "1", origin.as_uri(), cloned.as_posix()],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    assert _is_shallow_repository(cloned) is True
    assert _is_shallow_repository(origin) is False


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
        {"alice@example.com": Contribution(commits=1, last_commit=_RECENT, credit=1.0)},
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
        {"alice@example.com": Contribution(commits=1, last_commit=_RECENT, credit=1.0)},
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
    assert high[0].evidence_quality == pytest.approx(0.70)


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
        second = analyze_ownership(
            Path("/fake"),
            config,
            as_of=_NOW,
            analysis_ref="deadbeef",
            max_workers=1,
        )
        third = analyze_ownership(
            Path("/fake"),
            config,
            as_of=_NOW,
            analysis_ref="deadbeef",
            max_workers=16,
        )
    assert _ownership_json(first) == _ownership_json(second) == _ownership_json(third)
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
    assert "core.quotePath=false" in seen[0]
    assert "core.longpaths=true" in seen[0]


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

    def missing(_cmd: list[str], **_kwargs: object) -> object:
        raise OSError("missing")

    with (
        patch(_MOCK_GIT, side_effect=missing),
        pytest.raises(GitRequirementError, match="found none"),
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
    log_calls = [
        call.args[0] for call in mock.call_args_list if _git_subcommand(call.args[0]) == "log"
    ]
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


def _credit_config(
    *,
    count_co_authors: bool = True,
    co_author_weight: float = 1.0,
    merge_strategy: ConfiguredMergeStrategy = "auto",
) -> Config:
    return Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
        git=GitConfig(
            mass_refactor_file_fraction=0.0,
            count_co_authors=count_co_authors,
            co_author_weight=co_author_weight,
            merge_strategy=merge_strategy,
        ),
    )


def _git(repo: Path, args: list[str], *, date: str | None = None) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(repo),
        "XDG_CONFIG_HOME": str(repo),
    }
    if date is not None:
        env["GIT_AUTHOR_NAME"] = "Merger"
        env["GIT_AUTHOR_EMAIL"] = "merger@example.com"
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_NAME"] = "Merger"
        env["GIT_COMMITTER_EMAIL"] = "merger@example.com"
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_coauthor_trailers_credit_authors(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "zero.py").write_text("v = 0\n", encoding="utf-8")
    git_commit(repo, "zero trailers", author="Alice", email="alice@example.com", date=_PINNED)
    (repo / "one.py").write_text("v = 1\n", encoding="utf-8")
    git_commit(
        repo,
        "one trailer\n\nCo-authored-by: Bob <bob@example.com>",
        author="Alice",
        email="alice@example.com",
        date=_BOB_DATE,
    )
    (repo / "two.py").write_text("v = 2\n", encoding="utf-8")
    git_commit(
        repo,
        (
            "two trailers\n\n"
            "Co-authored-by: Bob <bob@example.com>\n"
            "Co-authored-by: Carol <carol@example.com>\n"
            "Co-authored-by: Bob <bob@example.com>"
        ),
        author="Alice",
        email="alice@example.com",
        date=_THIRD_DATE,
    )
    (repo / "bad.py").write_text("v = 3\n", encoding="utf-8")
    git_commit(
        repo,
        (
            "malformed\n\n"
            "Co-authored-by: not an email\n"
            "Co-authored-by: Dana <dana@example.com>\n"
            "Co-authored-by: <missing-at>"
        ),
        author="Alice",
        email="alice@example.com",
        date="2026-05-04T12:00:00+00:00",
    )
    result = analyze_ownership(repo, _credit_config(), as_of=_AS_OF, analysis_ref="test")
    assert result.analysis_completeness.merge_strategy == "rebase"
    assert result.analysis_completeness.co_author_count == 4
    assert _owner_handles(result, "zero.py") == ["alice@example.com"]
    assert set(_owner_handles(result, "one.py")) == {"alice@example.com", "bob@example.com"}
    assert set(_owner_handles(result, "two.py")) == {
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
    }
    assert set(_owner_handles(result, "bad.py")) == {"alice@example.com", "dana@example.com"}

    full = {owner.handle: owner for owner in result.paths["one.py"].owners}
    weighed = analyze_ownership(
        repo,
        _credit_config(co_author_weight=0.5),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    half = {owner.handle: owner for owner in weighed.paths["one.py"].owners}
    full_bob = full["bob@example.com"].score_breakdown
    half_bob = half["bob@example.com"].score_breakdown
    full_alice = full["alice@example.com"].score_breakdown
    half_alice = half["alice@example.com"].score_breakdown
    assert full_bob is not None and half_bob is not None
    assert full_alice is not None and half_alice is not None
    assert half_bob.frequency.score < full_bob.frequency.score
    assert half_alice.frequency.score == pytest.approx(full_alice.frequency.score)

    author_only = analyze_ownership(
        repo,
        _credit_config(count_co_authors=False),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    assert _owner_handles(author_only, "one.py") == ["alice@example.com"]
    assert _owner_handles(author_only, "two.py") == ["alice@example.com"]
    assert author_only.analysis_completeness.co_author_count == 0

    mapped = init_git_repo(tmp_path / "mapped")
    (mapped / "pair.py").write_text("v = 1\n", encoding="utf-8")
    git_commit(mapped, "bob writes", author="Bob", email="bob@example.com", date=_PINNED)
    (mapped / "pair.py").write_text("v = 2\n", encoding="utf-8")
    git_commit(
        mapped,
        "alice with bob home\n\nCo-authored-by: Bob Home <bob@home.example>",
        author="Alice",
        email="alice@example.com",
        date=_BOB_DATE,
    )
    (mapped / ".mailmap").write_text(
        "Bob <bob@example.com> <bob@home.example>\n",
        encoding="utf-8",
    )
    git_commit(mapped, "mailmap", author="Alice", email="alice@example.com", date=_THIRD_DATE)
    collapsed = analyze_ownership(mapped, _credit_config(), as_of=_AS_OF, analysis_ref="test")
    handles = _owner_handles(collapsed, "pair.py")
    assert "bob@example.com" in handles
    assert "bob@home.example" not in handles


def test_squash_and_merge_histories_differ(tmp_path: Path) -> None:
    merged = init_git_repo(tmp_path / "merge")
    (merged / "a.py").write_text("a = 1\n", encoding="utf-8")
    git_commit(merged, "alice adds a", author="Alice", email="alice@example.com", date=_PINNED)
    _git(merged, ["checkout", "-b", "feature"])
    (merged / "b.py").write_text("b = 1\n", encoding="utf-8")
    git_commit(merged, "bob adds b", author="Bob", email="bob@example.com", date=_BOB_DATE)
    _git(merged, ["checkout", "main"])
    _git(
        merged,
        ["merge", "--no-ff", "-m", "Merge branch feature", "feature"],
        date=_THIRD_DATE,
    )
    squashed = init_git_repo(tmp_path / "squash")
    (squashed / "a.py").write_text("a = 1\n", encoding="utf-8")
    (squashed / "b.py").write_text("b = 1\n", encoding="utf-8")
    git_commit(
        squashed,
        "Add files (#7)\n\nCo-authored-by: Bob <bob@example.com>",
        author="Alice",
        email="alice@example.com",
        date=_PINNED,
    )
    config = _credit_config()
    merge_result = analyze_ownership(merged, config, as_of=_AS_OF, analysis_ref="test")
    squash_result = analyze_ownership(squashed, config, as_of=_AS_OF, analysis_ref="test")
    assert merge_result.analysis_completeness.merge_strategy == "merge"
    assert merge_result.analysis_completeness.co_author_count == 0
    assert _owner_handles(merge_result, "a.py") == ["alice@example.com"]
    assert _owner_handles(merge_result, "b.py") == ["bob@example.com"]
    assert squash_result.analysis_completeness.merge_strategy == "squash"
    assert squash_result.analysis_completeness.co_author_count == 1
    assert set(_owner_handles(squash_result, "a.py")) == {"alice@example.com", "bob@example.com"}
    assert set(_owner_handles(squash_result, "b.py")) == {"alice@example.com", "bob@example.com"}
    squash_owners = {owner.handle: owner for owner in squash_result.paths["a.py"].owners}
    alice = squash_owners["alice@example.com"].score_breakdown
    bob = squash_owners["bob@example.com"].score_breakdown
    assert alice is not None and bob is not None
    assert alice.blame.available is True
    assert alice.blame.score == pytest.approx(1.0)
    assert bob.blame.available is True
    assert bob.blame.score == pytest.approx(0.0)
    forced = analyze_ownership(
        merged,
        _credit_config(merge_strategy="rebase"),
        as_of=_AS_OF,
        analysis_ref="test",
    )
    assert forced.analysis_completeness.merge_strategy == "rebase"


def test_coauthor_mailmap_and_log_edges(tmp_path: Path) -> None:
    contacts = (("Bob", "bob@example.com"), ("", "carol@example.com"))
    raw = ("bob@example.com", "carol@example.com")
    assert canonical_emails(tmp_path, (), enabled=True) == ()
    assert canonical_emails(tmp_path, contacts, enabled=False) == raw
    failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    with patch("checkowners.analyze.subprocess.run", return_value=failed) as mocked:
        assert canonical_emails(tmp_path, contacts, enabled=True) == raw
    argv = mocked.call_args.args[0]
    assert "Bob <bob@example.com>" in argv
    assert "<carol@example.com>" in argv
    short = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Bob <bob@example.com>\n",
        stderr="",
    )
    with patch("checkowners.analyze.subprocess.run", return_value=short):
        assert canonical_emails(tmp_path, contacts, enabled=True) == raw
    unmatched = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="not-an-email\nCarol <carol@example.com>\n",
        stderr="",
    )
    with patch("checkowners.analyze.subprocess.run", return_value=unmatched):
        assert canonical_emails(tmp_path, contacts, enabled=True) == raw
    credited = assign_coauthors(
        tmp_path,
        ("alice@example.com", "alice@example.com"),
        (
            (
                ("Alice", "alice@example.com"),
                ("Bob", "bob@example.com"),
                ("Bob Again", "bob@example.com"),
            ),
            (("Bob", "bob@example.com"),),
        ),
        use_mailmap=False,
        enabled=True,
    )
    assert credited == (("bob@example.com",), ("bob@example.com",))
    when = "2026-05-28T12:00:00+00:00"
    broken = "\n".join(
        (
            _COMMIT_SENTINEL,
            "alice@example.com",
            when,
            "",
            "subject",
            _BODY_START,
            "hello",
            "file.py",
            _COMMIT_SENTINEL,
            "alice@example.com",
            _BODY_START,
            "",
            _BODY_END,
            "file.py",
            _COMMIT_SENTINEL,
            "   ",
            when,
            "abc",
            "subject",
            _BODY_START,
            "",
            _BODY_END,
            "file.py",
            _COMMIT_SENTINEL,
            "alice@example.com",
            "not-a-time",
            "abc",
            "subject",
            _BODY_START,
            "",
            _BODY_END,
            "file.py",
            _COMMIT_SENTINEL,
            "b" * 40,
            "bob@example.com",
            when,
            "parent",
            _BODY_START,
            "",
            _BODY_END,
            "ok.py",
        )
    )
    parsed = _parse_log_output(broken)
    assert len(parsed) == 1
    assert parsed[0].author == "bob@example.com"
    assert parsed[0].files == ("ok.py",)


def _canonical_ownership(ownership: OwnershipMap) -> str:
    def convert(value: object) -> object:
        if isinstance(value, datetime):
            aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
            return aware.astimezone(UTC).isoformat()
        if isinstance(value, dict):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [convert(item) for item in value]
        return value

    return json.dumps(convert(asdict(ownership)), sort_keys=True, separators=(",", ":"))


def _assert_same_ownership(left: OwnershipMap, right: OwnershipMap) -> None:
    assert _canonical_ownership(left) == _canonical_ownership(right)


def _score_config(lookback_days: int | None = None) -> Config:
    if lookback_days is None:
        return Config(analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1))
    return Config(
        analysis=AnalysisConfig(
            confidence_threshold=0.0,
            min_commits=1,
            lookback_days=lookback_days,
        )
    )


def _store(repo: GitRepo, config: Config) -> None:
    outcome = collect_analysis(
        repo.path,
        config,
        as_of=_NOW,
        analysis_ref=repo.head_sha(),
    )
    write_state(repo.path, outcome.ownership, config=config, history=outcome.baseline)


def _blamed_paths(calls: list[list[str]]) -> list[str]:
    paths: list[str] = []
    for argv in calls:
        if not argv or argv[0] != "git" or "blame" not in argv or "--" not in argv:
            continue
        paths.append(argv[argv.index("--") + 1])
    return paths


def test_incremental_matches_full_analysis_and_blames_only_new_paths(git_repo: GitRepo) -> None:
    config = _score_config()
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add a",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("a.py", "def alpha():\n    return 2\n")
    git_repo.coauthored_commit(
        "update a",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
        coauthor="Alice",
        coemail="alice@example.com",
    )
    _store(git_repo, config)
    git_repo.commit_files(
        {"b.py": "def beta():\n    return 1\n"},
        "add b",
        author="Carol",
        email="carol@example.com",
        date="2026-05-03T12:00:00+00:00",
    )
    calls: list[list[str]] = []

    def spy(
        repo: Path,
        args: list[str],
        *,
        check: bool,
        paths: bool = False,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return run_git(repo, args, check=check, paths=paths, stdin=stdin)

    with patch("checkowners.analyze.run_git", spy):
        incremental = collect_analysis(
            git_repo.path,
            config,
            as_of=_NOW,
            analysis_ref=git_repo.head_sha(),
        )
    assert incremental.mode == "incremental"
    assert incremental.fallback == ""
    full = analyze_ownership(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    _assert_same_ownership(incremental.ownership, full)
    assert _blamed_paths(calls) == ["b.py"]
    named = [
        argv
        for argv in calls
        if any(arg == "--name-only" or arg.startswith("--find-renames=") for arg in argv)
    ]
    assert named
    assert all("--no-walk" in argv for argv in named)


def test_incremental_handles_rename_deletion_and_window_expiry(git_repo: GitRepo) -> None:
    config = _score_config()
    git_repo.commit_files(
        {"foo.py": "value = 1\n", "gone.py": "keep = 1\n"},
        "seed",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    _store(git_repo, config)
    git_repo.rename("foo.py", "bar.py")
    (git_repo.path / "gone.py").unlink()
    git_repo.commit(
        "rename and delete",
        author="Bob",
        email="bob@example.com",
        date="2026-05-04T12:00:00+00:00",
    )
    incremental = collect_analysis(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    full = analyze_ownership(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    _assert_same_ownership(incremental.ownership, full)
    assert "foo.py" not in incremental.ownership.paths
    assert "gone.py" not in incremental.ownership.paths
    assert "bar.py" in incremental.ownership.paths

    expired = GitRepo.create(git_repo.path.parent / "expired")
    short = _score_config(lookback_days=1)
    expired.commit_files(
        {"old.py": "old = 1\n"},
        "old",
        author="Alice",
        email="alice@example.com",
        date="2026-01-01T12:00:00+00:00",
    )
    early = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    first = collect_analysis(expired.path, short, as_of=early, analysis_ref=expired.head_sha())
    write_state(expired.path, first.ownership, config=short, history=first.baseline)
    expired.commit_files(
        {"new.py": "new = 1\n"},
        "new",
        author="Bob",
        email="bob@example.com",
        date="2026-03-01T12:00:00+00:00",
    )
    later = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    warmed = collect_analysis(expired.path, short, as_of=later, analysis_ref=expired.head_sha())
    cold = analyze_ownership(expired.path, short, as_of=later, analysis_ref=expired.head_sha())
    _assert_same_ownership(warmed.ownership, cold)
    assert warmed.mode == "incremental"
    assert "old.py" not in warmed.ownership.paths
    assert "new.py" in warmed.ownership.paths


def test_invalid_baseline_falls_back_to_full_analysis(git_repo: GitRepo) -> None:
    config = _score_config()
    git_repo.commit_files(
        {"a.py": "value = 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    _store(git_repo, config)
    target = _state_path(git_repo.path)
    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["history"]["files"].pop(next(iter(stored["history"]["files"])))
    target.write_text(json.dumps(stored), encoding="utf-8")
    gapped = collect_analysis(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert gapped.mode == "full"
    assert gapped.fallback == "incremental baseline does not cover this lookback window"
    _assert_same_ownership(
        gapped.ownership,
        analyze_ownership(git_repo.path, config, as_of=_NOW, analysis_ref=git_repo.head_sha()),
    )
    changed = _score_config(lookback_days=30)
    rejected = collect_analysis(
        git_repo.path,
        changed,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert rejected.mode == "full"
    assert rejected.fallback == "scoring configuration changed"
    _assert_same_ownership(
        rejected.ownership,
        analyze_ownership(
            git_repo.path,
            changed,
            as_of=_NOW,
            analysis_ref=git_repo.head_sha(),
        ),
    )
    forced = collect_analysis(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
        incremental=False,
    )
    assert forced.mode == "full"
    assert forced.fallback == "incremental analysis disabled"
    _assert_same_ownership(
        forced.ownership,
        analyze_ownership(git_repo.path, config, as_of=_NOW, analysis_ref=git_repo.head_sha()),
    )
    run_git(git_repo.path, ["git", "checkout", "--orphan", "side"], check=True)
    git_repo.commit_files(
        {"side.py": "side = 1\n"},
        "side",
        author="Carol",
        email="carol@example.com",
        date="2026-05-06T12:00:00+00:00",
    )
    diverged = collect_analysis(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert diverged.mode == "full"
    assert diverged.fallback == "cached ref is not an ancestor of HEAD"
    _assert_same_ownership(
        diverged.ownership,
        analyze_ownership(git_repo.path, config, as_of=_NOW, analysis_ref=git_repo.head_sha()),
    )


def _baseline(
    ref: str,
    *,
    truncated: bool = False,
    rename_detection: str = "true",
) -> HistoryBaseline:
    return HistoryBaseline(
        analysis_ref=ref,
        files={},
        modified={},
        renames=frozenset(),
        blame={},
        blame_inputs="",
        rename_detection=rename_detection,
        truncated=truncated,
    )


def test_incremental_edges_cover_rejects_and_reuse(git_repo: GitRepo) -> None:
    config = _score_config()
    git_repo.commit_files(
        {"a.py": "value = 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    head = git_repo.head_sha()
    assert _git_baseline_reason(git_repo.path, _baseline(head, truncated=True), head).startswith(
        "incremental baseline is incomplete"
    )
    assert "no analyzed ref" in _git_baseline_reason(git_repo.path, _baseline(""), head)
    assert "missing" in _git_baseline_reason(git_repo.path, _baseline("d" * 40), head)
    assert (
        _git_baseline_reason(
            git_repo.path,
            _baseline(head),
            head,
            Config(analysis=AnalysisConfig(rename_similarity=40)),
        )
        == "lineage settings changed"
    )
    assert not _is_ancestor(git_repo.path, "", head)
    assert _rename_commit_shas(git_repo.path, None, _NOW) == frozenset()
    assert _mass_shas({}, 0, 0.5) == ()
    assert _mass_shas({head: 1}, 4, 0.0) == ()
    assert _blob_shas(git_repo.path, ["a\nb"]) == {}
    assert _reused_fraction(None, head, {}) is None
    assert _reused_fraction(BlameLines(blob_sha=head, lines={}), head, {}) is None
    missing = BlameLines(blob_sha=head, lines={head: 2})
    assert _reused_fraction(missing, "other", {}) is None
    assert _reused_fraction(missing, head, {}) is None
    assert _reused_fraction(missing, head, {head: "alice@example.com"}) == {
        "alice@example.com": 1.0
    }
    zero_lines = BlameLines(blob_sha=head, lines={head: 0})
    assert _reused_fraction(zero_lines, head, {head: "alice@example.com"}) is None
    zero = "0" * 40
    detail = _parse_blame_detail(f"{zero} 1 1 1\nauthor-mail <zero@example.com>\n")
    assert detail.reusable is False
    assert _parse_blame_detail("author-mail <early@example.com>\n").reusable is False
    failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    short = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc\n", stderr="")
    mailed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=f"not-a-sha\n{zero}\x00\n{head}\x00alice@example.com\n",
        stderr="",
    )
    with patch("checkowners.analyze.run_git", return_value=failed):
        assert _blob_shas(git_repo.path, ["a.py"]) == {}
    with patch("checkowners.analyze.run_git", return_value=short):
        assert _blob_shas(git_repo.path, ["a.py", "b.py"]) == {}
    with patch("checkowners.analyze.run_git", return_value=mailed):
        assert _author_emails(git_repo.path, [head, "short"], use_mailmap=False) == {
            head: "alice@example.com"
        }
    renamed = f"{head}\nM\ta.py\nR100\told.py\tnew.py\n"
    with patch("checkowners.analyze._git_stdout", return_value=renamed):
        assert _rename_commit_shas(git_repo.path, _NOW, _NOW) == frozenset({head})
    with patch("checkowners.analyze._git_stdout", return_value=None):
        assert _rename_commit_shas(git_repo.path, _NOW, _NOW) == frozenset()
    ignore = git_repo.path / ".git-blame-ignore-revs"
    ignore.write_text(f"{head}\n", encoding="utf-8")
    assert _blame_inputs_token(git_repo.path, config, ())
    real_read = Path.read_text

    def deny(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == ".git-blame-ignore-revs":
            raise OSError("denied")
        return real_read(self, *args, **kwargs)

    with patch.object(Path, "read_text", deny):
        assert _blame_inputs_token(git_repo.path, config, (head,))
    _store(git_repo, config)
    seen: list[int] = []
    same = collect_analysis(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=head,
        on_progress=lambda done, _total: seen.append(done),
    )
    assert same.mode == "incremental"
    assert seen
    quiet = collect_analysis(git_repo.path, config, as_of=_NOW, analysis_ref=head)
    assert quiet.mode == "incremental"
    plain = analyze_ownership(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=head,
        incremental=True,
    )
    assert consume_analysis_outcome() is None
    assert plain.analysis_ref == head
    (git_repo.path / "a.py").write_text("value = 1\nextra = 1\n", encoding="utf-8")
    dirty = collect_analysis(git_repo.path, config, as_of=_NOW, analysis_ref=head)
    assert dirty.mode == "incremental"
    git_repo.commit(
        "stage extra",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    with patch(
        "checkowners.analyze.gather_blame_coverage",
        return_value=_BlamePass(truncated=True),
    ):
        truncated = collect_analysis(
            git_repo.path,
            config,
            as_of=_NOW,
            analysis_ref=git_repo.head_sha(),
        )
    assert truncated.baseline.truncated
    assert truncated.baseline.blame == {}
    with patch("checkowners.analyze._fetch_commit_files", return_value=({}, {})):
        unfetched = collect_analysis(
            git_repo.path,
            config,
            as_of=_NOW,
            analysis_ref=git_repo.head_sha(),
        )
    assert unfetched.fallback == "incremental baseline does not cover this lookback window"
    run_git(git_repo.path, ["git", "config", "diff.renames", "false"], check=True)
    renamed_config = collect_analysis(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert renamed_config.fallback == "rename detection changed"
    adaptive = Config(analysis=AnalysisConfig(lookback_days="adaptive", confidence_threshold=0.0))
    assert (
        collect_analysis(
            git_repo.path,
            adaptive,
            as_of=_NOW,
            analysis_ref=git_repo.head_sha(),
            incremental=False,
        ).mode
        == "full"
    )


def test_author_index_skips_blank_fields_and_caps_only_mechanical_authors() -> None:
    indexed = _author_index(
        [
            _RawCommit(author="ada@x.com", timestamp=_NOW, files=("a.py",)),
            _RawCommit(
                author="ada@x.com",
                timestamp=_NOW,
                files=("a.py",),
                sha="abc",
                author_name="Ada",
                subject="bump deps",
            ),
        ]
    )
    assert indexed.shas == {"ada@x.com": "abc"}
    assert indexed.names == {"ada@x.com": ("Ada",)}
    commits = [
        _RawCommit(author="bot@x.com", timestamp=_NOW, files=("a.py",), subject="bump deps")
        for _ in range(20)
    ]
    commits.append(_RawCommit(author="ada@x.com", timestamp=_NOW, files=("a.py",), subject="fix"))
    capped = _cap_mechanical_authors(
        {
            "a.py": {
                "bot@x.com": Contribution(commits=20, last_commit=_NOW, credit=20.0),
                "ada@x.com": Contribution(commits=1, last_commit=_NOW, credit=4.0),
            }
        },
        commits,
    )
    assert capped["a.py"]["bot@x.com"].credit == 1.0
    assert capped["a.py"]["ada@x.com"].credit == 4.0

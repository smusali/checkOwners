"""Tests for checkowners.cli module."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from checkowners import __version__
from checkowners.action_report import (
    DIAGNOSTIC,
    summarize_bus_factor,
    summarize_decay,
    summarize_drift,
)
from checkowners.analyze import GitRequirementError, resolve_as_of
from checkowners.balance import BalanceReport, RebalanceSuggestion, ReviewLoad
from checkowners.busfactor import BusFactorReport
from checkowners.cli import (
    _api_gaps,
    _days_since,
    _declared_owners,
    _merge_identities,
    _owner_payload,
    _render_explained_owner,
    _render_explanation,
    _resolve_github_owners,
    _signal_label,
    app,
)
from checkowners.decay import DecayReport
from checkowners.explain import ExplainedOwner, PathExplanation
from checkowners.generate import (
    SIZE_WARN_BYTES,
    BroadPatternRecord,
    CodeownersSizeError,
    CodeownersVerificationError,
    GenerateResult,
)
from checkowners.graph import GraphExtraMissingError
from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    OWNERSHIP_MODEL_VERSION,
    AnalysisCompleteness,
    AnalysisGap,
    BusFactor,
    ConfidenceScore,
    Config,
    DecayWarning,
    DriftEntry,
    DriftResult,
    ExpertiseRank,
    GitConfig,
    GithubConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    PolicyConfig,
    SignalScore,
    TeamCluster,
    models_payload,
)
from checkowners.onboard import OnboardingPath, OnboardingStep
from checkowners.topology import TopologyReport
from checkowners.trends import TrendPoint, TrendReport
from checkowners.validate import ValidationError

runner = CliRunner()
_REFUSED_BROAD = BroadPatternRecord(
    source="app/[id]/",
    generated_fallback="app/*/",
    also_matches=("app/static/index.tsx",),
    intended_owners=("@alice",),
    affected=(("app/static/index.tsx", ("@bob",)),),
    accepted=False,
)
_ACCEPTED_BROAD = BroadPatternRecord(
    source="app/[slug]/",
    generated_fallback="app/*/",
    also_matches=("app/[id]/page.tsx",),
    intended_owners=("@alice",),
    affected=(("app/[id]/page.tsx", ("@alice",)),),
    accepted=True,
)
_GENERATED = GenerateResult(content="content", broad_patterns=())
_GENERATED_WITH_BROAD = GenerateResult(
    content="content",
    broad_patterns=(_ACCEPTED_BROAD, _REFUSED_BROAD),
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float = 0.8, commits: int = 7) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=confidence,
        last_commit=_NOW,
        commits=commits,
        evidence_quality=1.0,
        score_breakdown=ConfidenceScore(
            total=confidence,
            recency=SignalScore(available=True, score=1.0),
            frequency=SignalScore(available=True, score=1.0),
            blame=SignalScore(available=True, score=1.0),
            review=SignalScore(available=False),
        ),
    )


_OWNERSHIP = OwnershipMap(
    paths={
        "src/main.py": PathOwnership(
            owners=(_entry("alice@example.com", 0.92), _entry("bob@example.com", 0.55)),
            qualified_owner_count=2,
        ),
        "src/auth.py": PathOwnership(
            owners=(_entry("dave@example.com", 0.34),),
            qualified_owner_count=1,
            decay_warnings=(
                DecayWarning(
                    handle="dave@example.com",
                    path="src/auth.py",
                    last_commit=_NOW,
                    days_since_last_commit=289,
                    historical_confidence=0.34,
                ),
            ),
        ),
    },
    last_analyzed=_NOW,
    analysis_ref="deadbeef",
)

_EMPTY_OWNERSHIP = OwnershipMap(paths={}, last_analyzed=_NOW, analysis_ref="deadbeef")


def _scored(
    handle: str,
    score: float,
    *,
    commits: int = 7,
    recency: float = 0.9,
    frequency: float = 0.8,
    blame: float = 0.7,
    review: float | None = None,
) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=_NOW,
        commits=commits,
        evidence_quality=0.85,
        score_breakdown=ConfidenceScore(
            total=score,
            recency=SignalScore(available=True, score=recency),
            frequency=SignalScore(available=True, score=frequency),
            blame=SignalScore(available=True, score=blame),
            review=SignalScore(available=review is not None, score=review or 0.0),
        ),
    )


_EXPLAIN_OWNERSHIP = OwnershipMap(
    paths={
        "src/main.py": PathOwnership(
            owners=(_scored("@alice", 0.86, blame=0.68), _scored("@bob", 0.54, recency=0.33)),
            qualified_owner_count=2,
            candidates=(
                _scored("@alice", 0.86, blame=0.68),
                _scored("@bob", 0.54, recency=0.33),
                _scored("@carol", 0.12, recency=0.05, frequency=0.1, blame=0.03, commits=1),
            ),
        ),
        "src/util.py": PathOwnership(
            owners=(_scored("@alice", 0.70, blame=0.40),),
            qualified_owner_count=1,
            candidates=(_scored("@alice", 0.70, blame=0.40),),
        ),
    },
    last_analyzed=_NOW,
    analysis_ref="deadbeef",
)


@contextmanager
def _explain_run(ownership: OwnershipMap = _EXPLAIN_OWNERSHIP) -> Iterator[None]:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=ownership),
        patch("checkowners.cli.load_config", return_value=Config()),
        patch("checkowners.explain.commit_shas", return_value={"@alice": ("abc1234def56",)}),
        patch("checkowners.explain.rename_lineage", return_value=("src/old_main.py",)),
        patch("checkowners.explain.last_contribution", return_value=None),
        patch("checkowners.explain.blame_shares", return_value={}),
        _MOCK_TOKEN,
    ):
        yield


_DRIFT_DETECTED = DriftResult(
    stale=(DriftEntry(path="/old.py", confidence_delta=1.0, reason="stale path"),),
    missing=(
        DriftEntry(
            path="/new.py",
            confidence_delta=0.7,
            reason="missing",
            qualified_owner_count=1,
            decay=True,
        ),
    ),
    changed=(DriftEntry(path="/changed.py", confidence_delta=0.4, reason="owner shuffle"),),
    drift_detected=True,
)

_NO_DRIFT = DriftResult(stale=(), missing=(), changed=(), drift_detected=False)

_MOCK_TOKEN = patch("checkowners.cli.get_github_token", return_value="")
_MOCK_PATH = patch(
    "checkowners.cli.find_codeowners_path",
    return_value=Path.cwd() / ".github" / "CODEOWNERS",
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {"CHECKOWNERS_STATE_DIR": str(tmp_path)}),
        patch("checkowners.cli.resolve_as_of", return_value=_NOW),
        patch("checkowners.cli.head_commit_sha", return_value="deadbeef"),
    ):
        yield


# --- analyze ---


def test_analyze_json() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["analyze", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "src/main.py" in data["inferred"]
    owners = data["inferred"]["src/main.py"]["owners"]
    assert owners[0]["identity"] == "alice@example.com"
    assert owners[0]["handle"] == "alice@example.com"
    assert owners[0]["ownership_score"] == 0.92
    assert owners[0]["confidence"] == 0.92
    assert owners[0]["evidence_quality"] == 1.0
    assert owners[0]["signals"]["recency"] == 1.0
    assert "review" not in owners[0]["signals"]
    path_data = data["inferred"]["src/main.py"]
    assert path_data["qualified_owner_count"] == 2
    assert path_data["bus_factor"] == 2
    assert path_data["qualified_owner_count_cap"] == 3
    assert path_data["analysis"]["completeness"] == 0.75
    assert path_data["risk"]["truck_factor_50"] == 1
    assert data["model_version"] == OWNERSHIP_MODEL_VERSION
    assert data["models"] == models_payload()
    assert data["schema_version"] == COMMAND_SCHEMA_VERSION
    assert data["head_sha"] == "deadbeef"
    assert data["deprecated_keys"] == ["bus_factor", "confidence"]
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert data["generated_at"] == _NOW.isoformat()
    assert data["analysis_completeness"] == 0.75
    assert data["analysis"]["ignore_revs_applied"] is False
    assert data["analysis"]["ignore_revs_file"] == ""
    assert data["analysis"]["mailmap_applied"] is False
    assert data["analysis"]["mailmap_file"] == ""
    assert data["analysis"]["excluded_gitattributes"] == 0
    assert data["analysis"]["excluded_static"] == 0


def test_analyze_invalid_as_of_exits() -> None:
    with patch(
        "checkowners.cli.resolve_as_of",
        side_effect=ValueError("Invalid as-of value: 'nope'"),
    ):
        result = runner.invoke(app, ["--as-of", "nope", "analyze"])
    assert result.exit_code == 2
    assert "Invalid as-of" in result.stdout


def test_analyze_clock_git_error() -> None:
    with patch(
        "checkowners.cli.head_commit_sha",
        side_effect=subprocess.CalledProcessError(128, "git"),
    ):
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 4
    assert "Git command failed" in result.stdout


def test_analyze_deterministic_flag() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["--deterministic", "analyze", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["analysis_ref"] == "deadbeef"


def test_analyze_as_of_overrides() -> None:
    pinned = datetime(2024, 1, 1, tzinfo=UTC)
    with (
        patch("checkowners.cli.resolve_as_of", return_value=pinned) as mock_as_of,
        patch("checkowners.cli.head_commit_sha", return_value="deadbeef"),
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["--as-of", "2024-01-01T00:00:00+00:00", "analyze", "--json"])
    assert result.exit_code == 0
    assert mock_as_of.call_args[0][0] == "2024-01-01T00:00:00+00:00"


def test_analyze_honors_source_date_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    with (
        patch("checkowners.cli.resolve_as_of", side_effect=resolve_as_of),
        patch("checkowners.cli.head_commit_sha", return_value="deadbeef"),
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP) as mock_an,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["analyze", "--json"])
    assert result.exit_code == 0
    assert mock_an.call_args.kwargs["as_of"] == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_analyze_table() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0
    assert "alice@example.com" in result.stdout
    assert "0.92/1.00" in result.stdout
    assert "Blame ignore-revs: not found" in result.stdout
    assert "Mailmap: not found" in result.stdout
    assert "Exclusions: 0 gitattributes, 0 static" in result.stdout
    assert "analysis completeness: 75%" in result.stdout

    applied = replace(
        _OWNERSHIP,
        analysis_completeness=AnalysisCompleteness(
            mailmap_applied=True,
            mailmap_file=".mailmap",
        ),
    )
    with patch("checkowners.cli.analyze_ownership", return_value=applied), _MOCK_TOKEN:
        applied_result = runner.invoke(app, ["analyze"])
    assert "Mailmap: applied (.mailmap)" in applied_result.stdout

    with (
        patch("checkowners.cli.load_config", return_value=Config(git=GitConfig(use_mailmap=False))),
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        _MOCK_TOKEN,
    ):
        disabled = runner.invoke(app, ["analyze"])
    assert "Mailmap: disabled" in disabled.stdout


def test_analyze_empty() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0
    assert "No ownership" in result.stdout


def test_analyze_git_error() -> None:
    with patch(
        "checkowners.cli.analyze_ownership",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 4


def test_invalid_config_exits_config() -> None:
    with patch("checkowners.cli.load_config", side_effect=ValueError("bad config")):
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 2
    assert "bad config" in result.stdout


def test_scored_run_prints_gaps_and_merges_later_evidence() -> None:
    scored = replace(
        _OWNERSHIP,
        analysis_completeness=AnalysisCompleteness(
            score=0.5,
            gaps=(AnalysisGap("missing_mailmap", "Missing .mailmap."),),
        ),
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=scored),
        patch("checkowners.cli.get_github_token", return_value=""),
    ):
        result = runner.invoke(app, ["analyze"])
    assert "Missing .mailmap." in result.stdout
    assert "Absent token: GitHub API evidence was not collected." in result.stdout


def test_scored_run_without_extra_gaps_stays_complete() -> None:
    scored = replace(_OWNERSHIP, analysis_completeness=AnalysisCompleteness(score=1.0))
    with (
        patch(
            "checkowners.cli.load_config",
            return_value=Config(github=GithubConfig(resolve_handles=False)),
        ),
        patch("checkowners.cli.analyze_ownership", return_value=scored),
    ):
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0
    assert "analysis completeness: 100%" in result.stdout


def test_api_gaps_name_absent_token_and_unresolved_emails() -> None:
    with patch("checkowners.cli.get_github_token", return_value=""):
        gaps = {gap.code: gap.reason for gap in _api_gaps(_OWNERSHIP, Config())}
    assert gaps["absent_token"] == "Absent token: GitHub API evidence was not collected."
    assert gaps["ambiguous_identity"] == (
        "Ambiguous identities: 3 emails were not resolved to a single GitHub account."
    )


def test_policy_incomplete_analysis_exits_findings() -> None:
    policy = Config(policy=PolicyConfig(incomplete_analysis_fail=True))
    with (
        patch("checkowners.cli.load_config", return_value=policy),
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        _MOCK_TOKEN,
    ):
        failed = runner.invoke(app, ["analyze"])
        cleared = runner.invoke(app, ["--exit-zero", "analyze"])
    assert failed.exit_code == 3
    assert cleared.exit_code == 0


def test_fail_on_incomplete_stays_zero_when_signals_are_complete() -> None:
    complete = _entry("alice@example.com")
    breakdown = complete.score_breakdown
    assert breakdown is not None
    ownership = OwnershipMap(
        paths={
            "src/main.py": PathOwnership(
                owners=(
                    replace(
                        complete,
                        score_breakdown=replace(
                            breakdown,
                            review=SignalScore(available=True, score=1.0),
                        ),
                    ),
                ),
                qualified_owner_count=1,
            )
        },
        last_analyzed=_NOW,
        analysis_ref="deadbeef",
    )
    with patch("checkowners.cli.analyze_ownership", return_value=ownership), _MOCK_TOKEN:
        result = runner.invoke(app, ["--fail-on-incomplete", "analyze"])
    assert result.exit_code == 0


def test_analyze_git_version_error() -> None:
    with patch(
        "checkowners.cli.analyze_ownership",
        side_effect=GitRequirementError("checkOwners requires Git 2.23 or newer; found 2.19.0"),
    ):
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 4
    assert "requires Git 2.23" in result.stdout


# --- generate ---


def test_generate_rich() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED_WITH_BROAD),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate", "--allow-broad-patterns"])
    assert result.exit_code == 0
    assert "Generated" in result.stdout
    combined = result.stdout + result.stderr
    assert "cannot precisely represent" in combined
    assert "app/[id]/" in combined


def test_generate_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED_WITH_BROAD),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "CODEOWNERS" in data["path"]
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert data["bytes_written"] == len(b"content")
    assert data["rules_written"] == 1
    assert data["broad_patterns"] == [_ACCEPTED_BROAD.as_json(), _REFUSED_BROAD.as_json()]


# --- print ---


def test_print_json() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["print", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "src/main.py" in data["paths"]
    assert data["paths"]["src/main.py"]["qualified_owner_count"] == 2
    assert data["paths"]["src/main.py"]["bus_factor"] == 2
    assert data["paths"]["src/main.py"]["qualified_owner_count_cap"] == 3
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    bare = OwnerEntry(handle="@bare", ownership_score=0.4, last_commit=None, commits=1)
    payload = _owner_payload(bare)
    assert payload["last_commit"] is None
    assert "signals" not in payload


def test_print_plain_shows_confidence() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["print"])
    assert result.exit_code == 0
    assert "src/main.py" in result.stdout
    assert "alice@example.com(0.92/1.00)" in result.stdout


# --- validate ---


def test_validate_valid() -> None:
    with patch("checkowners.cli.validate_codeowners", return_value=[]), _MOCK_PATH:
        result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_validate_errors() -> None:
    errors = [ValidationError(line_number=3, line="bad", message="bad line")]
    with patch("checkowners.cli.validate_codeowners", return_value=errors), _MOCK_PATH:
        result = runner.invoke(app, ["validate"])
    assert result.exit_code == 3
    assert "Line 3" in result.stdout


def test_validate_json_valid() -> None:
    with patch("checkowners.cli.validate_codeowners", return_value=[]), _MOCK_PATH:
        result = runner.invoke(app, ["validate", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["valid"] is True


def test_validate_json_errors() -> None:
    errors = [ValidationError(line_number=1, line="x", message="oops")]
    with patch("checkowners.cli.validate_codeowners", return_value=errors), _MOCK_PATH:
        result = runner.invoke(app, ["validate", "--json"])
    assert result.exit_code == 3
    data = json.loads(result.stdout)
    assert data["valid"] is False
    assert len(data["errors"]) == 1


def test_validate_json_when_git_metadata_unavailable() -> None:
    with (
        patch("checkowners.cli.validate_codeowners", return_value=[]),
        patch("checkowners.cli.head_commit_sha", side_effect=ValueError("no head")),
        patch("checkowners.cli.resolve_as_of", side_effect=ValueError("no clock")),
        _MOCK_PATH,
    ):
        result = runner.invoke(app, ["validate", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["head_sha"] == ""
    assert data["generated_at"] == ""


# --- drift ---


def test_drift_no_drift() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift"])
    assert result.exit_code == 0
    assert "No drift" in result.stdout


def test_drift_detected_shows_severity() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift"])
    assert result.exit_code == 3
    assert "CRITICAL" in result.stdout
    assert "stale" in result.stdout


def test_drift_json_includes_severity() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift", "--json"])
    assert result.exit_code == 3
    data = json.loads(result.stdout)
    assert data["drift_detected"] is True
    assert data["severity"] == "critical"
    assert data["max_confidence_delta"] == 1.0
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()


# --- notify ---


def test_notify_sent() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        patch("checkowners.cli.send_notification", return_value=True),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["notify"])
    assert result.exit_code == 0
    assert "sent" in result.stdout.lower()


def test_notify_skipped() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        patch("checkowners.cli.send_notification", return_value=False),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["notify"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout.lower()


def test_notify_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        patch("checkowners.cli.send_notification", return_value=True),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["notify", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["sent"] is True
    assert data["severity"] == "critical"
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()


# --- sync ---


def test_sync_rich() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["sync", "--allow-broad-patterns"])
    assert result.exit_code == 0
    assert "committed" in result.stdout.lower()


def test_sync_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["committed"] is True
    assert data["bytes_written"] == len(b"content")
    assert data["rules_written"] == 1
    assert data["broad_patterns"] == []


def test_sync_git_os_error() -> None:
    status = MagicMock(returncode=0, stdout=" M CODEOWNERS\n")
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED),
        patch(
            "checkowners.cli.subprocess.run",
            side_effect=[status, OSError("git not found")],
        ),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 4
    assert "Git commit failed" in result.stdout
    assert "git not found" in result.stdout


def test_sync_git_commit_error() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED),
        patch(
            "checkowners.cli.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git", stderr="nothing to commit"),
        ),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 4


# --- github-action ---


_FIXED_HEX = "0123456789abcdef0123456789abcdef"


def _gh_block(name: str, payload: str) -> str:
    delim = f"ghadelim_{_FIXED_HEX}"
    return f"{name}<<{delim}\n{payload}\n{delim}\n"


def _run_github_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    *,
    drift: DriftResult = _NO_DRIFT,
) -> tuple[int, str, Path, Path]:
    output_file = tmp_path / "gh_output"
    summary_file = tmp_path / "step_summary"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=drift),
        patch("checkowners.action_report.secrets.token_hex", return_value=_FIXED_HEX),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, args)
    return result.exit_code, result.stdout, output_file, summary_file


def test_github_action_fails_on_drift_and_writes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, _stdout, output_file, summary_file = _run_github_action(
        tmp_path, monkeypatch, ["github-action"], drift=_DRIFT_DETECTED
    )
    assert exit_code == 3
    written = output_file.read_text(encoding="utf-8")
    drift = json.loads((tmp_path / "drift.json").read_text(encoding="utf-8"))
    bus = json.loads((tmp_path / "bus_factor.json").read_text(encoding="utf-8"))
    decay = json.loads((tmp_path / "decay.json").read_text(encoding="utf-8"))
    expected = (
        _gh_block("artifact_name", "checkowners-reports")
        + _gh_block(
            "checkowners_drift",
            json.dumps(summarize_drift(drift, 50), separators=(",", ":")),
        )
        + _gh_block(
            "bus_factor_summary",
            json.dumps(summarize_bus_factor(bus, 50), separators=(",", ":")),
        )
        + _gh_block(
            "decay_summary",
            json.dumps(summarize_decay(decay, 50), separators=(",", ":")),
        )
    )
    assert written == expected
    assert '"schema_version":2' in written
    assert (tmp_path / "checkowners-report.md").read_text(encoding="utf-8") == (
        summary_file.read_text(encoding="utf-8")
    )
    assert "## CheckOwners" in summary_file.read_text(encoding="utf-8")


def test_github_action_no_fail_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, stdout, output_file, _summary_file = _run_github_action(
        tmp_path,
        monkeypatch,
        ["github-action", "--no-fail-on-drift", "--json"],
        drift=_DRIFT_DETECTED,
    )
    assert exit_code == 0
    data = json.loads(stdout)
    assert data["checkowners_drift"]["drift_detected"] is True
    assert data["checkowners_drift"]["schema_version"] == COMMAND_SCHEMA_VERSION
    assert data["schema_version"] == COMMAND_SCHEMA_VERSION
    assert "entries" in data["bus_factor_summary"]
    assert data["bus_factor_summary"]["deprecated_keys"] == ["bus_factor"]
    assert data["bus_factor_summary"]["qualified_owner_count_cap"] == 3
    assert "reports" in data["decay_summary"]
    delim = f"ghadelim_{_FIXED_HEX}"
    body = output_file.read_text(encoding="utf-8").split(f"checkowners_drift<<{delim}\n", 1)[1]
    summary = json.loads(body.split(f"\n{delim}\n", 1)[0])
    assert summary["schema_version"] == 2
    assert "counts" in summary
    assert "truncated" in summary


def test_github_action_clean_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, _stdout, _output_file, _summary_file = _run_github_action(
        tmp_path, monkeypatch, ["github-action"], drift=_NO_DRIFT
    )
    assert exit_code == 0
    assert (tmp_path / "drift.json").is_file()
    assert (tmp_path / "bus_factor.json").is_file()
    assert (tmp_path / "decay.json").is_file()


@pytest.mark.parametrize("fail_on_drift", [True, False])
@pytest.mark.parametrize("include_bus_factor", [True, False])
@pytest.mark.parametrize("include_decay", [True, False])
def test_github_action_input_combinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_drift: bool,
    include_bus_factor: bool,
    include_decay: bool,
) -> None:
    args = ["github-action"]
    if not fail_on_drift:
        args.append("--no-fail-on-drift")
    if not include_bus_factor:
        args.append("--no-include-bus-factor")
    if not include_decay:
        args.append("--no-include-decay")
    exit_code, _stdout, output_file, _summary_file = _run_github_action(
        tmp_path, monkeypatch, args, drift=_DRIFT_DETECTED
    )
    assert exit_code == (3 if fail_on_drift else 0)
    written = output_file.read_text(encoding="utf-8")
    assert (tmp_path / "drift.json").is_file()
    assert "checkowners_drift<<" in written
    assert (tmp_path / "bus_factor.json").is_file() is include_bus_factor
    assert ("bus_factor_summary<<" in written) is include_bus_factor
    assert (tmp_path / "decay.json").is_file() is include_decay
    assert ("decay_summary<<" in written) is include_decay


def test_github_action_reads_toggles_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHECKOWNERS_FAIL_ON_DRIFT", "false")
    monkeypatch.setenv("CHECKOWNERS_INCLUDE_BUS_FACTOR", "false")
    monkeypatch.setenv("CHECKOWNERS_INCLUDE_DECAY", "false")
    exit_code, stdout, output_file, _summary_file = _run_github_action(
        tmp_path, monkeypatch, ["github-action", "--json"], drift=_DRIFT_DETECTED
    )
    assert exit_code == 0
    written = output_file.read_text(encoding="utf-8")
    assert not (tmp_path / "bus_factor.json").exists()
    assert not (tmp_path / "decay.json").exists()
    assert "bus_factor_summary<<" not in written
    assert "decay_summary<<" not in written
    data = json.loads(stdout)
    assert "bus_factor_summary" not in data
    assert "decay_summary" not in data


def test_github_action_analysis_failure_writes_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "gh_output"
    summary_file = tmp_path / "step_summary"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    with (
        patch(
            "checkowners.cli.analyze_ownership",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ),
        patch("checkowners.action_report.secrets.token_hex", return_value=_FIXED_HEX),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["github-action", "--no-fail-on-drift"])
    assert result.exit_code == 4
    assert not (tmp_path / "drift.json").exists()
    assert summary_file.read_text(encoding="utf-8") == DIAGNOSTIC
    assert output_file.read_text(encoding="utf-8") == _gh_block(
        "artifact_name", "checkowners-reports"
    )


def test_github_action_rejects_non_positive_max_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, _stdout, _output_file, _summary_file = _run_github_action(
        tmp_path, monkeypatch, ["github-action", "--max-output-entries", "0"]
    )
    assert exit_code == 2


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (GitRequirementError("checkOwners requires Git 2.23 or newer"), "requires Git 2.23"),
        (subprocess.CalledProcessError(1, "git"), "Git command failed"),
        (OSError("git not found"), "Git command failed"),
    ],
)
def test_github_action_classifies_leaked_integration_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    message: str,
) -> None:
    output_file = tmp_path / "gh_output"
    summary_file = tmp_path / "step_summary"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        patch("checkowners.cli.compute_qualified_owners", side_effect=exc),
        patch("checkowners.action_report.secrets.token_hex", return_value=_FIXED_HEX),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["github-action", "--no-include-decay"])
    assert result.exit_code == 4
    assert message in result.stdout
    assert summary_file.read_text(encoding="utf-8") == DIAGNOSTIC
    assert not (tmp_path / "drift.json").exists()


def test_github_action_unexpected_error_writes_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "gh_output"
    summary_file = tmp_path / "step_summary"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", side_effect=RuntimeError("boom")),
        patch("checkowners.action_report.secrets.token_hex", return_value=_FIXED_HEX),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["github-action", "--no-fail-on-drift"])
    assert result.exit_code == 1
    assert summary_file.read_text(encoding="utf-8") == DIAGNOSTIC
    assert not (tmp_path / "drift.json").exists()


# --- trends ---


_TREND_REPORT = TrendReport(
    points=(
        TrendPoint(
            period_end=datetime(2026, 4, 1, tzinfo=UTC),
            commits=10,
            active_contributors=2,
            tracked_paths=3,
            avg_top_confidence=0.6,
            avg_qualified_owner_count=1.5,
        ),
        TrendPoint(
            period_end=datetime(2026, 5, 1, tzinfo=UTC),
            commits=18,
            active_contributors=3,
            tracked_paths=4,
            avg_top_confidence=0.72,
            avg_qualified_owner_count=1.8,
        ),
    ),
    periods=2,
    period_days=30,
)


def test_trends_table() -> None:
    with patch("checkowners.cli.analyze_trends", return_value=_TREND_REPORT):
        result = runner.invoke(app, ["trends", "--periods", "2", "--period-days", "30"])
    assert result.exit_code == 0
    assert "2026-04-01" in result.stdout
    assert "0.72" in result.stdout


def test_trends_json() -> None:
    with patch("checkowners.cli.analyze_trends", return_value=_TREND_REPORT):
        result = runner.invoke(app, ["trends", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["periods"] == 2
    assert data["points"][1]["avg_top_confidence"] == 0.72
    assert data["points"][0]["active_contributors"] == 2
    assert data["points"][1]["avg_qualified_owner_count"] == 1.8
    assert data["points"][1]["avg_bus_factor"] == 1.8
    assert data["deprecated_keys"] == ["avg_bus_factor"]
    assert data["qualified_owner_count_cap"] == 3
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()


def test_trends_git_error() -> None:
    with patch(
        "checkowners.cli.analyze_trends",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        result = runner.invoke(app, ["trends"])
    assert result.exit_code == 4
    assert "Git command failed" in result.stdout


def test_decay_json_includes_stamp() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["decay", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert "reports" in data


def test_qualified_owners_json_includes_stamp() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["qualified-owners", "--all", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert "entries" in data


def test_balance_prints_loads_and_suggestions() -> None:
    report = BalanceReport(
        loads=(ReviewLoad(handle="@alice", reviews=10),),
        average=10.0,
        overloaded=(ReviewLoad(handle="@alice", reviews=10),),
        suggestions=(
            RebalanceSuggestion(
                overloaded="@alice",
                candidate="@bob",
                confidence=0.8,
                proposed_shift=3,
            ),
        ),
        source="git_authorship",
    )
    quiet = replace(report, suggestions=())
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.analyze_balance", side_effect=[report, quiet]),
        _MOCK_TOKEN,
    ):
        listed = runner.invoke(app, ["balance"])
        plain = runner.invoke(app, ["balance"])
    assert listed.exit_code == 0
    assert "@alice" in listed.stdout
    assert "@bob" in listed.stdout
    assert plain.exit_code == 0
    assert "Rebalance suggestions" not in plain.stdout


def test_balance_json_includes_stamp() -> None:
    empty = BalanceReport(
        loads=(),
        average=0.0,
        overloaded=(),
        suggestions=(),
        source="git_authorship",
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.analyze_balance", return_value=empty),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["balance", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert data["source"] == "git_authorship"


def test_topology_json_includes_stamp() -> None:
    empty = TopologyReport(clusters=(), mismatches=())
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.infer_topology", return_value=empty),
        patch("checkowners.cli.declared_teams_from_github", return_value={}),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["topology", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert data["clusters"] == []


def test_onboard_markdown_and_table() -> None:
    report = OnboardingPath(
        target="src",
        steps=(
            OnboardingStep(
                order=1,
                path="src/main.py",
                reviewer="@alice",
                complexity="easy",
                description="start here",
            ),
        ),
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_onboarding_path", return_value=report),
        _MOCK_TOKEN,
    ):
        markdown = runner.invoke(app, ["onboard", "src", "--markdown"])
        table = runner.invoke(app, ["onboard", "src"])
    assert markdown.exit_code == 0
    assert "src/main.py" in markdown.stdout
    assert table.exit_code == 0
    assert "start here" in table.stdout


def test_onboard_json_includes_stamp() -> None:
    empty = OnboardingPath(target="src/", steps=())
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_onboarding_path", return_value=empty),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["onboard", "src/", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert data["target"] == "src/"


def test_expertise_json_includes_stamp() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.rank_expertise", return_value=()),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["expertise", "src/main.py", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()
    assert data["path"] == "src/main.py"
    assert data["ranking"] == []


def test_resolve_github_owners_keeps_analysis_ref() -> None:
    config = Config(github=GithubConfig(resolve_handles=True))
    mapping = {
        "alice@example.com": "@alice",
        "bob@example.com": "@bob",
        "dave@example.com": "@dave",
    }
    with patch("checkowners.cli.resolve_handles", return_value=mapping):
        result = _resolve_github_owners(_OWNERSHIP, config)
    assert result.analysis_ref == "deadbeef"
    assert result.last_analyzed == _NOW
    assert result.paths["src/main.py"].owners[0].handle == "@alice"
    assert result.paths["src/auth.py"].decay_warnings[0].handle == "@dave"
    skipped = _resolve_github_owners(_OWNERSHIP, Config(github=GithubConfig(resolve_handles=False)))
    assert skipped is _OWNERSHIP


class _CommandOutput(Protocol):
    stdout: str
    stderr: str


def _models_text(result: _CommandOutput) -> str:
    return result.stdout + result.stderr


def test_graph_missing_extra_and_bad_export_exit_config() -> None:
    with (
        patch("checkowners.cli._load_or_analyze", return_value=_OWNERSHIP),
        patch(
            "checkowners.cli._build_or_load_graph",
            side_effect=GraphExtraMissingError("networkx is required"),
        ),
    ):
        missing = runner.invoke(app, ["graph"])
    assert missing.exit_code == 2
    assert "networkx is required" in missing.stdout
    with (
        patch("checkowners.cli._load_or_analyze", return_value=_OWNERSHIP),
        patch("checkowners.cli._build_or_load_graph", return_value=object()),
    ):
        exported = runner.invoke(app, ["graph", "--export", "png"])
    assert exported.exit_code == 2
    assert "png" in exported.stdout


def test_graph_reports_topology_model() -> None:
    with (
        patch("checkowners.cli._load_or_analyze", return_value=_OWNERSHIP),
        patch("checkowners.cli._build_or_load_graph", return_value=object()),
        patch("checkowners.cli.to_text", return_value="graph\n"),
        patch("checkowners.cli.to_dot", return_value="graph {\n}\n"),
    ):
        plain = runner.invoke(app, ["graph"])
        exported = runner.invoke(app, ["graph", "--export", "dot"])
    assert plain.exit_code == 0
    assert "models: topology" in _models_text(plain)
    assert exported.exit_code == 0
    assert "models: topology" in _models_text(exported)


def test_decay_reports_ownership_and_risk_models() -> None:
    warning = DecayWarning(
        handle="@dave",
        path="src/auth.py",
        last_commit=_NOW,
        days_since_last_commit=40,
        historical_confidence=0.8,
    )
    report = DecayReport(warning=warning, recommended_transfer="@alice", departed=True)
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_decay", return_value=()),
        _MOCK_TOKEN,
    ):
        quiet = runner.invoke(app, ["decay"])
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_decay", return_value=(report,)),
        _MOCK_TOKEN,
    ):
        listed = runner.invoke(app, ["decay"])
    assert quiet.exit_code == 0
    assert "models: ownership" in _models_text(quiet)
    assert "risk" in _models_text(quiet)
    assert listed.exit_code == 0
    assert "@dave" in listed.stdout
    assert "models: ownership" in _models_text(listed)


def test_qualified_owners_reports_risk_model() -> None:
    empty = BusFactorReport(entries=(), repo_average=0.0, qualified_owner_count_cap=3)
    filled = BusFactorReport(
        entries=(
            BusFactor(
                path="src/main.py",
                qualified_owner_count=1,
                contributors_above_threshold=("@alice",),
                recommended_backups=("@bob",),
            ),
        ),
        repo_average=1.0,
        qualified_owner_count_cap=3,
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.compute_qualified_owners", return_value=empty),
        _MOCK_TOKEN,
    ):
        quiet = runner.invoke(app, ["qualified-owners", "--all"])
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.compute_qualified_owners", return_value=filled),
        _MOCK_TOKEN,
    ):
        listed = runner.invoke(app, ["qualified-owners", "--all"])
    assert quiet.exit_code == 0
    assert "models: risk" in _models_text(quiet)
    assert listed.exit_code == 0
    assert "src/main.py" in listed.stdout
    assert "models: risk" in _models_text(listed)


def test_topology_reports_topology_model() -> None:
    empty = TopologyReport(clusters=(), mismatches=())
    filled = TopologyReport(
        clusters=(
            TeamCluster(
                name="platform",
                members=("@alice",),
                primary_paths=("src/",),
                declared=True,
            ),
        ),
        mismatches=("platform is missing @bob",),
    )
    agreed = TopologyReport(clusters=filled.clusters, mismatches=())
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.infer_topology", return_value=empty),
        patch("checkowners.cli.declared_teams_from_github", return_value={}),
        _MOCK_TOKEN,
    ):
        quiet = runner.invoke(app, ["topology"])
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.infer_topology", return_value=filled),
        patch("checkowners.cli.declared_teams_from_github", return_value={}),
        _MOCK_TOKEN,
    ):
        listed = runner.invoke(app, ["topology"])
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.infer_topology", return_value=agreed),
        patch("checkowners.cli.declared_teams_from_github", return_value={}),
        _MOCK_TOKEN,
    ):
        matched = runner.invoke(app, ["topology"])
    assert quiet.exit_code == 0
    assert "models: topology" in _models_text(quiet)
    assert listed.exit_code == 0
    assert "platform" in listed.stdout
    assert "missing @bob" in listed.stdout
    assert "models: topology" in _models_text(listed)
    assert matched.exit_code == 0
    assert "models: topology" in _models_text(matched)


def test_expertise_reports_ownership_model() -> None:
    rank = ExpertiseRank(handle="@alice", confidence=0.8, commits=3, last_commit=_NOW)
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.rank_expertise", return_value=()),
        _MOCK_TOKEN,
    ):
        quiet = runner.invoke(app, ["expertise", "src/main.py"])
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.rank_expertise", return_value=(rank,)),
        _MOCK_TOKEN,
    ):
        listed = runner.invoke(app, ["expertise", "src/main.py"])
    assert quiet.exit_code == 0
    assert "models: ownership" in _models_text(quiet)
    assert listed.exit_code == 0
    assert "@alice" in listed.stdout
    assert "models: ownership" in _models_text(listed)


def test_trends_without_history_reports_ownership_model() -> None:
    empty = TrendReport(points=(), periods=6, period_days=30)
    with patch("checkowners.cli.analyze_trends", return_value=empty):
        result = runner.invoke(app, ["trends"])
    assert result.exit_code == 0
    assert "No history" in result.stdout
    assert "models: ownership" in _models_text(result)


def test_expertise_json_null_last_commit() -> None:
    rank = ExpertiseRank(handle="@alice", confidence=0.8, commits=3, last_commit=None)
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.rank_expertise", return_value=(rank,)),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["expertise", "src/main.py", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ranking"][0]["last_commit"] is None


# --- version / identity merge ---


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_merge_identities_dedupes_same_handle() -> None:
    noreply = "1+a@users.noreply.github.com"
    entries = (
        OwnerEntry(handle="a@x.com", ownership_score=0.9, last_commit=None, commits=4),
        OwnerEntry(handle=noreply, ownership_score=0.5, last_commit=None, commits=6),
    )
    mapping = {"a@x.com": "@a", "1+a@users.noreply.github.com": "@a"}
    merged = _merge_identities(entries, mapping)
    assert len(merged) == 1
    assert merged[0].handle == "@a"
    assert merged[0].commits == 10
    assert merged[0].confidence == 0.9


def test_sync_noop_when_already_in_sync() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED),
        patch("checkowners.cli._has_uncommitted_changes", return_value=False),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        human = runner.invoke(app, ["sync"])
        result = runner.invoke(app, ["sync", "--json"])
    assert human.exit_code == 0
    assert "already in sync" in human.stdout
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["committed"] is False
    assert data["bytes_written"] == len(b"content")
    assert data["rules_written"] == 1
    assert data["broad_patterns"] == []
    mock_run.assert_not_called()


def test_generate_refuses_handwritten_before_analyzing(tmp_path: Path) -> None:
    target = tmp_path / "CODEOWNERS"
    target.write_text("# Hand-curated\nsrc/ @human\n", encoding="utf-8")
    with (
        patch("checkowners.cli.find_codeowners_path", return_value=target),
        patch("checkowners.cli.analyze_ownership") as mock_analyze,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 2
    mock_analyze.assert_not_called()


def test_generate_size_refusal_exits_config() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch(
            "checkowners.cli.generate_codeowners",
            side_effect=CodeownersSizeError("exceeds output.max_bytes"),
        ),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 2
    assert "max_bytes" in result.stdout


def test_generate_verification_failure_exits() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch(
            "checkowners.cli.generate_codeowners",
            side_effect=CodeownersVerificationError(
                "Round-trip verification failed for src/a.py: "
                "intended @alice, resolved @bob, winning rule: * @bob"
            ),
        ),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 3
    assert "src/a.py" in result.stdout
    assert "@alice" in result.stdout
    assert "* @bob" in result.stdout


def test_sync_verification_failure_does_not_commit() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch(
            "checkowners.cli.generate_codeowners",
            side_effect=CodeownersVerificationError("round-trip failed"),
        ),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 3
    mock_run.assert_not_called()


def test_generate_warns_at_two_megabytes() -> None:
    huge = "x" * SIZE_WARN_BYTES
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch(
            "checkowners.cli.generate_codeowners",
            return_value=GenerateResult(content=huge, broad_patterns=()),
        ),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "2 MB" in combined


def test_explain_path_json(tmp_path: Path) -> None:
    target = tmp_path / "CODEOWNERS"
    target.write_text("* @global\n/src/ @alice\n", encoding="utf-8")
    with patch("checkowners.cli.find_codeowners_path", return_value=target):
        result = runner.invoke(app, ["explain-path", "src/a.py", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["path"] == "src/a.py"
    assert data["winner"]["pattern"] == "/src/"
    assert data["winner"]["owners"] == ["@alice"]
    assert data["models"] == models_payload()
    assert len(data["matches"]) == 2
    assert data["matches"][0]["wins"] is False
    assert data["matches"][-1]["wins"] is True


def test_explain_path_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "CODEOWNERS"
    with patch("checkowners.cli.find_codeowners_path", return_value=missing):
        result = runner.invoke(app, ["explain-path", "src/a.py"])
    assert result.exit_code == 2
    assert "No CODEOWNERS file found" in result.stdout


def test_explain_path_human_chain(tmp_path: Path) -> None:
    target = tmp_path / "CODEOWNERS"
    target.write_text("* @global\n/src/ @alice\ninternal/secret.py\n", encoding="utf-8")
    with patch("checkowners.cli.find_codeowners_path", return_value=target):
        owned = runner.invoke(app, ["explain-path", "src/a.py"])
        exempt = runner.invoke(app, ["explain-path", "internal/secret.py"])
    assert owned.exit_code == 0
    assert "* @global" in owned.stdout
    assert "/src/ @alice" in owned.stdout
    assert "winner" in owned.stdout
    assert exempt.exit_code == 0
    assert "(none)" in exempt.stdout


def test_explain_path_unmatched(tmp_path: Path) -> None:
    target = tmp_path / "CODEOWNERS"
    target.write_text("/src/ @alice\n", encoding="utf-8")
    with patch("checkowners.cli.find_codeowners_path", return_value=target):
        human = runner.invoke(app, ["explain-path", "docs/readme.md"])
        json_result = runner.invoke(app, ["explain-path", "docs/readme.md", "--json"])
    assert human.exit_code == 0
    assert "No CODEOWNERS rule matches" in human.stdout
    data = json.loads(json_result.stdout)
    assert data["winner"] is None
    assert data["matches"] == []


def test_validate_errors_render_brackets_verbatim() -> None:
    """Rich markup must not swallow [segments] from user paths."""
    errors = [
        ValidationError(
            line_number=7,
            line="x",
            message="bad pattern: /app/[companyId]/page.tsx",
        )
    ]
    with patch("checkowners.cli.validate_codeowners", return_value=errors), _MOCK_PATH:
        result = runner.invoke(app, ["validate"])
    assert result.exit_code == 3
    assert "[companyId]" in result.stdout


def test_explain_json_decomposes_signals() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/main.py", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["schema_version"] == COMMAND_SCHEMA_VERSION
    assert data["model_version"] == OWNERSHIP_MODEL_VERSION
    assert data["models"] == models_payload()
    assert data["path"] == "src/main.py"
    assert data["analysis_ref"] == "deadbeef"
    alice = data["owners"][0]
    assert alice["handle"] == "@alice"
    assert alice["signals"]["recency"]["available"] is True
    assert alice["signals"]["review"]["available"] is False
    assert "score" not in alice["signals"]["review"]
    assert "analysis.confidence_threshold" in " ".join(data["knobs"])
    assert data["lineage"] == ["src/old_main.py"]


def test_explain_human_shows_unavailable_not_zero() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/main.py"])
    assert result.exit_code == 0
    assert "Observed owners" in result.stdout
    assert "@alice" in result.stdout
    assert "Reviews n/a" in result.stdout
    assert "Would change the result" in result.stdout
    assert "Prior names" in result.stdout


def test_explain_owner_scopes_output() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/main.py", "--owner", "@alice"])
        missing = runner.invoke(app, ["explain", "src/main.py", "--owner", "@carol"])
    assert result.exit_code == 0
    assert "@alice" in result.stdout
    assert "@bob" not in result.stdout
    assert missing.exit_code == 0
    assert "not an inferred owner" in missing.stdout
    assert "--why-not" in missing.stdout


def test_explain_why_not_below_threshold() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/main.py", "--why-not", "@carol"])
    assert result.exit_code == 0
    assert "@carol was not inferred because" in result.stdout
    assert "0.12" in result.stdout
    assert "0.30" in result.stdout
    assert "never contributed" not in result.stdout


def test_explain_why_not_never_touched() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/main.py", "--why-not", "@dave"])
    assert result.exit_code == 0
    assert "never contributed to this path" in result.stdout
    assert "0.00" in result.stdout
    assert "0.12" not in result.stdout


def test_explain_owner_and_why_not_are_exclusive() -> None:
    result = runner.invoke(
        app, ["explain", "src/main.py", "--owner", "@alice", "--why-not", "@bob"]
    )
    assert result.exit_code == 2
    assert "--owner or --why-not" in result.stdout


def test_explain_directory_mentions_aggregation() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/"])
    assert result.exit_code == 0
    assert "2 files" in result.stdout
    assert "highest-scoring file" in result.stdout
    assert "@alice" in result.stdout


def test_owners_and_who_are_minimal() -> None:
    with _explain_run():
        owners = runner.invoke(app, ["owners", "src/main.py"])
        who = runner.invoke(app, ["who", "src/main.py"])
        payload = runner.invoke(app, ["owners", "src/main.py", "--json"])
    assert owners.exit_code == 0
    assert who.exit_code == 0
    assert "@alice" in owners.stdout
    assert "0.86" in owners.stdout
    assert "Observed owners" not in owners.stdout
    assert owners.stdout == who.stdout
    data = json.loads(payload.stdout)
    assert data["schema_version"] == COMMAND_SCHEMA_VERSION
    assert data["models"] == models_payload()
    assert data["owners"][0]["identity"] == "@alice"
    assert data["owners"][0]["handle"] == "@alice"
    assert data["owners"][0]["ownership_score"] == 0.86
    assert data["owners"][0]["signals"]["recency"] == 0.9
    assert "risk" in data
    assert "analysis" in data


def test_explain_and_owners_scope_analyze_to_the_path() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_EXPLAIN_OWNERSHIP) as mocked,
        patch("checkowners.cli.load_config", return_value=Config()),
        patch("checkowners.explain.commit_shas", return_value={}),
        patch("checkowners.explain.rename_lineage", return_value=()),
        patch("checkowners.explain.last_contribution", return_value=None),
        patch("checkowners.explain.blame_shares", return_value={}),
        _MOCK_TOKEN,
    ):
        explain = runner.invoke(app, ["explain", "src/main.py"])
        owners = runner.invoke(app, ["owners", "src/util.py"])
    assert explain.exit_code == 0
    assert owners.exit_code == 0
    first = mocked.call_args_list[0].kwargs
    second = mocked.call_args_list[1].kwargs
    assert first["pathspec"] == ("src/main.py",)
    assert first["retain_all"] is True
    assert second["pathspec"] == ("src/util.py",)
    assert second["retain_all"] is True


def test_explain_analyze_value_error() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", side_effect=ValueError("bad clock")),
        patch("checkowners.cli.load_config", return_value=Config()),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["explain", "src/main.py"])
    assert result.exit_code == 1
    assert "bad clock" in result.stdout


def test_explain_analyze_git_error() -> None:
    with (
        patch(
            "checkowners.cli.analyze_ownership",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ),
        patch("checkowners.cli.load_config", return_value=Config()),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["explain", "src/main.py"])
    assert result.exit_code == 4
    assert "Git command failed" in result.stdout


def test_explain_no_inferred_owners() -> None:
    empty = OwnershipMap(paths={}, last_analyzed=_NOW, analysis_ref="deadbeef")
    with _explain_run(empty):
        result = runner.invoke(app, ["explain", "missing.py"])
    assert result.exit_code == 0
    assert "No inferred owners" in result.stdout


def test_owners_empty_path() -> None:
    empty = OwnershipMap(paths={}, last_analyzed=_NOW, analysis_ref="deadbeef")
    with _explain_run(empty):
        result = runner.invoke(app, ["owners", "missing.py"])
    assert result.exit_code == 0
    assert "No inferred owners" in result.stdout


def test_explain_why_not_already_inferred() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/main.py", "--why-not", "@alice"])
    assert result.exit_code == 0
    assert "is an inferred owner" in result.stdout
    assert "Would change the result" not in result.stdout


def test_explain_why_not_json() -> None:
    with _explain_run():
        result = runner.invoke(app, ["explain", "src/main.py", "--why-not", "@carol", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["why_not"]["kind"] == "below_threshold"
    assert data["why_not"]["handle"] == "@carol"


def test_explain_shows_declared_codeowners_and_team(tmp_path: Path) -> None:
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text("src/main.py @org/platform\n", encoding="utf-8")
    with (
        _explain_run(),
        patch("checkowners.cli.find_codeowners_path", return_value=codeowners),
        patch(
            "checkowners.cli.declared_teams_from_github",
            return_value={"@org/platform": frozenset({"@alice"})},
        ),
    ):
        result = runner.invoke(app, ["explain", "src/main.py"])
    assert result.exit_code == 0
    assert "aligned" in result.stdout
    assert "@alice ∈ @org/platform" in result.stdout


def test_explain_unknown_last_commit() -> None:
    alice = OwnerEntry(
        handle="@alice",
        ownership_score=0.86,
        last_commit=None,
        commits=3,
        evidence_quality=0.5,
        score_breakdown=ConfidenceScore(
            total=0.86,
            recency=SignalScore(available=True, score=0.9),
            frequency=SignalScore(available=True, score=0.8),
            blame=SignalScore(available=True, score=0.7),
            review=SignalScore(available=False),
        ),
    )
    ownership = OwnershipMap(
        paths={
            "src/a.py": PathOwnership(
                owners=(alice,),
                qualified_owner_count=1,
                candidates=(alice,),
            )
        },
        last_analyzed=_NOW,
        analysis_ref="deadbeef",
    )
    with _explain_run(ownership):
        result = runner.invoke(app, ["explain", "src/a.py"])
    assert result.exit_code == 0
    assert "unknown" in result.stdout


def test_explain_human_shows_review_score() -> None:
    alice = _scored("@alice", 0.86, review=0.74)
    ownership = OwnershipMap(
        paths={
            "src/main.py": PathOwnership(
                owners=(alice,),
                qualified_owner_count=1,
                candidates=(alice,),
            )
        },
        last_analyzed=_NOW,
        analysis_ref="deadbeef",
    )
    with _explain_run(ownership):
        result = runner.invoke(app, ["explain", "src/main.py"])
    assert result.exit_code == 0
    assert "Reviews 0.74" in result.stdout


def test_declared_owners_missing_no_match_and_last_rule(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "CODEOWNERS"
    with patch("checkowners.cli.find_codeowners_path", return_value=missing):
        assert _declared_owners(tmp_path, "src/a.py") == ()

    unmatched = tmp_path / "CODEOWNERS"
    unmatched.write_text("/docs/ @docs\n", encoding="utf-8")
    with patch("checkowners.cli.find_codeowners_path", return_value=unmatched):
        assert _declared_owners(tmp_path, "src/a.py") == ()

    matched = tmp_path / "rules"
    matched.write_text("* @global\nsrc/a.py @alice @bob\n", encoding="utf-8")
    with patch("checkowners.cli.find_codeowners_path", return_value=matched):
        assert _declared_owners(tmp_path, "src/a.py") == ("@alice", "@bob")


def test_days_since_and_signal_label_helpers() -> None:
    assert _days_since(None, _NOW) == "unknown"
    assert _days_since(_NOW, _NOW) == "today"
    assert _days_since(_NOW - timedelta(days=1), _NOW) == "1 day ago"
    assert _days_since(_NOW - timedelta(days=9), _NOW) == "9 days ago"
    assert _signal_label("review", 0.5, False) == "Reviews n/a"
    assert _signal_label("review", 0.5, True) == "Reviews 0.50"
    assert _signal_label("blame", 0.7, True) == "Blame 0.70"


def test_render_explained_owner_without_optional_signals() -> None:
    item = ExplainedOwner(
        entry=_scored("@alice", 0.86),
        signals=(),
        source_path="src/a.py",
    )
    _render_explained_owner(item, _NOW)
    explanation = PathExplanation(
        target="src/a.py",
        kind="file",
        files=("src/a.py",),
        inferred=(item,),
        candidates=(),
        evidence_quality=0.85,
        declared=(),
        team_resolution=(),
        assessment="unverifiable",
        lineage=(),
        knobs=(),
        weights={},
        why_not=None,
    )
    _render_explanation(explanation, _NOW)


@pytest.mark.parametrize(
    "args",
    [
        ["analyze", "--json"],
        ["generate", "--json"],
        ["print", "--json"],
        ["validate", "--json"],
        ["drift", "--json"],
        ["notify", "--json"],
        ["sync", "--json"],
        ["decay", "--json"],
        ["qualified-owners", "--all", "--json"],
        ["bus-factor", "--all", "--json"],
        ["balance", "--json"],
        ["topology", "--json"],
        ["onboard", "src/", "--json"],
        ["expertise", "src/main.py", "--json"],
        ["trends", "--json"],
        ["explain", "src/main.py", "--json"],
        ["owners", "src/main.py", "--json"],
        ["who", "src/main.py", "--json"],
        ["baseline", "create", "--json"],
        ["github-action", "--json", "--no-fail-on-drift"],
    ],
)
def test_command_json_includes_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    explanation = PathExplanation(
        target="src/main.py",
        kind="file",
        files=("src/main.py",),
        inferred=(),
        candidates=(),
        evidence_quality=1.0,
        declared=(),
        team_resolution=(),
        assessment="aligned",
        lineage=(),
        knobs=(),
        weights={},
        why_not=None,
    )
    empty_balance = BalanceReport(
        loads=(),
        average=0.0,
        overloaded=(),
        suggestions=(),
        source="git_authorship",
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED),
        patch("checkowners.cli.send_notification", return_value=False),
        patch("checkowners.cli.validate_codeowners", return_value=[]),
        patch("checkowners.cli.analyze_trends", return_value=_TREND_REPORT),
        patch("checkowners.cli.analyze_balance", return_value=empty_balance),
        patch(
            "checkowners.cli.infer_topology",
            return_value=TopologyReport(clusters=(), mismatches=()),
        ),
        patch(
            "checkowners.cli.generate_onboarding_path",
            return_value=OnboardingPath(target="src/", steps=()),
        ),
        patch("checkowners.cli.rank_expertise", return_value=()),
        patch("checkowners.cli.declared_teams_from_github", return_value={}),
        patch("checkowners.cli.build_explanation", return_value=explanation),
        patch("checkowners.cli.subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("checkowners.cli.find_codeowners_path", return_value=tmp_path / "CODEOWNERS"),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["models"] == models_payload()


_EXIT_OK: tuple[tuple[str, list[str]], ...] = (
    ("analyze", ["analyze"]),
    ("generate", ["generate"]),
    ("print", ["print"]),
    ("validate", ["validate"]),
    ("explain-path", ["explain-path", "src/main.py"]),
    ("explain", ["explain", "src/main.py"]),
    ("owners", ["owners", "src/main.py"]),
    ("who", ["who", "src/main.py"]),
    ("drift", ["drift"]),
    ("notify", ["notify"]),
    ("sync", ["sync"]),
    ("decay", ["decay"]),
    ("graph", ["graph"]),
    ("qualified-owners", ["qualified-owners", "--all"]),
    ("bus-factor", ["bus-factor", "--all"]),
    ("balance", ["balance"]),
    ("topology", ["topology"]),
    ("onboard", ["onboard", "src"]),
    ("expertise", ["expertise", "src/main.py"]),
    ("trends", ["trends"]),
    ("baseline", ["baseline", "create"]),
    ("github-action", ["github-action"]),
)


def _exit_cases() -> list[object]:
    cases: list[object] = [
        pytest.param("ok", command, args, 0, id=f"ok-{command}") for command, args in _EXIT_OK
    ]
    cases.extend(
        [
            pytest.param("findings", "drift", ["drift"], 3, id="drift-findings"),
            pytest.param("findings", "drift", ["--exit-zero", "drift"], 0, id="drift-exit-zero"),
            pytest.param("findings", "validate", ["validate"], 3, id="validate-findings"),
            pytest.param(
                "findings",
                "validate",
                ["--exit-zero", "validate"],
                0,
                id="validate-exit-zero",
            ),
            pytest.param("findings", "generate", ["generate"], 3, id="generate-findings"),
            pytest.param(
                "findings",
                "generate",
                ["--exit-zero", "generate"],
                0,
                id="generate-exit-zero",
            ),
            pytest.param("findings", "github-action", ["github-action"], 3, id="action-findings"),
            pytest.param(
                "findings",
                "github-action",
                ["--exit-zero", "github-action"],
                0,
                id="action-exit-zero",
            ),
            pytest.param(
                "findings",
                "github-action",
                ["github-action", "--no-fail-on-drift"],
                0,
                id="action-no-fail-on-drift",
            ),
            pytest.param(
                "findings",
                "analyze",
                ["--fail-on-incomplete", "analyze"],
                3,
                id="analyze-incomplete",
            ),
            pytest.param(
                "findings",
                "analyze",
                ["--exit-zero", "--fail-on-incomplete", "analyze"],
                0,
                id="analyze-incomplete-exit-zero",
            ),
            pytest.param("config", "analyze", ["--as-of", "nope", "analyze"], 2, id="as-of"),
            pytest.param(
                "config",
                "analyze",
                ["--exit-zero", "--as-of", "nope", "analyze"],
                2,
                id="as-of-exit-zero",
            ),
            pytest.param(
                "config",
                "explain",
                ["explain", "src/main.py", "--owner", "@a", "--why-not", "@b"],
                2,
                id="explain-usage",
            ),
            pytest.param("config", "qualified-owners", ["qualified-owners"], 2, id="owners-usage"),
            pytest.param("config", "bus-factor", ["bus-factor"], 2, id="bus-usage"),
            pytest.param(
                "config",
                "github-action",
                ["github-action", "--max-output-entries", "0"],
                2,
                id="action-usage",
            ),
            pytest.param(
                "config",
                "drift",
                ["drift", "--baseline", "missing.json"],
                2,
                id="baseline-missing",
            ),
            pytest.param(
                "config",
                "explain-path",
                ["explain-path", "src/main.py"],
                2,
                id="explain-path-missing",
            ),
            pytest.param("config", "generate", ["generate"], 2, id="generate-overwrite"),
            pytest.param("git", "analyze", ["analyze"], 4, id="analyze-git"),
            pytest.param(
                "git",
                "analyze",
                ["--exit-zero", "analyze"],
                4,
                id="analyze-git-exit-zero",
            ),
            pytest.param("git", "drift", ["drift"], 4, id="drift-git"),
            pytest.param("git", "trends", ["trends"], 4, id="trends-git"),
            pytest.param("git", "explain", ["explain", "src/main.py"], 4, id="explain-git"),
            pytest.param("git", "sync", ["sync"], 4, id="sync-git"),
            pytest.param("git", "github-action", ["github-action"], 4, id="action-git"),
            pytest.param("internal", "github-action", ["github-action"], 1, id="action-internal"),
            pytest.param(
                "internal",
                "github-action",
                ["--exit-zero", "github-action"],
                1,
                id="action-internal-exit-zero",
            ),
        ]
    )
    return cases


@pytest.mark.parametrize(("kind", "command", "args", "expected"), _exit_cases())
def test_exit_code_contract(
    kind: str,
    command: str,
    args: list[str],
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    codeowners = tmp_path / "CODEOWNERS"
    if kind == "ok" and command == "explain-path":
        codeowners.write_text("* @alice\n", encoding="utf-8")
    if kind == "config" and command == "generate":
        codeowners.write_text("# Hand-curated\nsrc/ @human\n", encoding="utf-8")

    analyze_effect: object = _OWNERSHIP
    if kind == "git" and command in {"analyze", "explain", "github-action"}:
        analyze_effect = subprocess.CalledProcessError(1, "git")
    if kind == "internal":
        analyze_effect = RuntimeError("boom")

    drift_effect: object = (
        _DRIFT_DETECTED
        if kind == "findings"
        and command
        in {
            "drift",
            "github-action",
        }
        else _NO_DRIFT
    )
    if kind == "git" and command == "drift":
        drift_effect = subprocess.CalledProcessError(1, "git")

    generate_effect: object = _GENERATED
    if kind == "findings" and command == "generate":
        generate_effect = CodeownersVerificationError("round-trip failed")

    validation: object = []
    if kind == "findings" and command == "validate":
        validation = [ValidationError(line_number=1, line="x", message="oops")]

    codeowners_path = codeowners
    if kind == "config" and command == "explain-path":
        codeowners_path = tmp_path / "missing"
    git_sync = kind == "git" and command == "sync"
    trends_effect: object = _TREND_REPORT
    if kind == "git" and command == "trends":
        trends_effect = subprocess.CalledProcessError(1, "git")

    with (
        patch("checkowners.cli.analyze_ownership", side_effect=analyze_effect)
        if not isinstance(analyze_effect, OwnershipMap)
        else patch("checkowners.cli.analyze_ownership", return_value=analyze_effect),
        patch("checkowners.cli.detect_drift", side_effect=drift_effect)
        if not isinstance(drift_effect, DriftResult)
        else patch("checkowners.cli.detect_drift", return_value=drift_effect),
        patch("checkowners.cli.generate_codeowners", side_effect=generate_effect)
        if isinstance(generate_effect, Exception)
        else patch("checkowners.cli.generate_codeowners", return_value=generate_effect),
        patch("checkowners.cli.validate_codeowners", return_value=validation),
        patch("checkowners.cli.send_notification", return_value=False),
        patch("checkowners.cli.analyze_trends", side_effect=trends_effect)
        if isinstance(trends_effect, Exception)
        else patch("checkowners.cli.analyze_trends", return_value=trends_effect),
        patch(
            "checkowners.cli.analyze_balance",
            return_value=BalanceReport(
                loads=(),
                average=0.0,
                overloaded=(),
                suggestions=(),
                source="git_authorship",
            ),
        ),
        patch(
            "checkowners.cli.infer_topology",
            return_value=TopologyReport(clusters=(), mismatches=()),
        ),
        patch(
            "checkowners.cli.generate_onboarding_path",
            return_value=OnboardingPath(target="src", steps=()),
        ),
        patch("checkowners.cli.rank_expertise", return_value=()),
        patch("checkowners.cli.declared_teams_from_github", return_value={}),
        patch(
            "checkowners.cli.build_explanation",
            return_value=PathExplanation(
                target="src/main.py",
                kind="file",
                files=("src/main.py",),
                inferred=(),
                candidates=(),
                evidence_quality=1.0,
                declared=(),
                team_resolution=(),
                assessment="aligned",
                lineage=(),
                knobs=(),
                weights={},
                why_not=None,
            ),
        ),
        patch("checkowners.cli._build_or_load_graph", return_value=object()),
        patch("checkowners.cli.to_text", return_value="graph"),
        patch(
            "checkowners.cli.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git", stderr="commit failed"),
        )
        if git_sync
        else patch(
            "checkowners.cli.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=""),
        ),
        patch("checkowners.cli.find_codeowners_path", return_value=codeowners_path),
        patch(
            "checkowners.cli.resolve_as_of",
            side_effect=ValueError("Invalid as-of value: 'nope'"),
        )
        if kind == "config" and "--as-of" in args
        else patch("checkowners.cli.resolve_as_of", return_value=_NOW),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, args)
    assert result.exit_code == expected, result.output
    if command == "github-action" and kind == "findings":
        assert (tmp_path / "drift.json").is_file()

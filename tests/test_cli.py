"""Tests for checkowners.cli module."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
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
from checkowners.analyze import resolve_as_of
from checkowners.cli import _merge_identities, _owner_payload, app
from checkowners.models import (
    OWNERSHIP_MODEL_VERSION,
    ConfidenceScore,
    DecayWarning,
    DriftEntry,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    SignalScore,
)
from checkowners.trends import TrendPoint, TrendReport
from checkowners.validate import ValidationError

runner = CliRunner()

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
    assert owners[0]["handle"] == "alice@example.com"
    assert owners[0]["ownership_score"] == 0.92
    assert owners[0]["confidence"] == 0.92
    assert owners[0]["evidence_quality"] == 1.0
    assert owners[0]["signals"]["recency"] == {"score": 1.0, "available": True}
    assert owners[0]["signals"]["review"] == {"available": False}
    path_data = data["inferred"]["src/main.py"]
    assert path_data["qualified_owner_count"] == 2
    assert path_data["bus_factor"] == 2
    assert path_data["qualified_owner_count_cap"] == 3
    assert data["model_version"] == OWNERSHIP_MODEL_VERSION
    assert data["deprecated_keys"] == ["bus_factor", "confidence"]
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_epoch"] == _NOW.isoformat()


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
    assert result.exit_code == 1


# --- generate ---


def test_generate_rich() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 0
    assert "Generated" in result.stdout


def test_generate_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "CODEOWNERS" in data["path"]


# --- print ---


def test_print_json() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["print", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "src/main.py" in data
    assert data["src/main.py"]["qualified_owner_count"] == 2
    assert data["src/main.py"]["bus_factor"] == 2
    assert data["src/main.py"]["qualified_owner_count_cap"] == 3
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
    assert result.exit_code == 1
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
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["valid"] is False
    assert len(data["errors"]) == 1


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
    assert result.exit_code == 0
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
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["drift_detected"] is True
    assert data["severity"] == "critical"
    assert data["max_confidence_delta"] == 1.0


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


# --- sync ---


def test_sync_rich() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "committed" in result.stdout.lower()


def test_sync_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["committed"] is True


def test_sync_git_commit_error() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch(
            "checkowners.cli.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git", stderr="nothing to commit"),
        ),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1


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
    assert exit_code == 1
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
    assert "schema_version" not in data["checkowners_drift"]
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
    assert exit_code == (1 if fail_on_drift else 0)
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
    assert result.exit_code == 1
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
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch("checkowners.cli._has_uncommitted_changes", return_value=False),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["committed"] is False
    mock_run.assert_not_called()


def test_generate_refuses_handwritten_before_analyzing(tmp_path: Path) -> None:
    target = tmp_path / "CODEOWNERS"
    target.write_text("# Hand-curated\nsrc/ @human\n", encoding="utf-8")
    with (
        patch("checkowners.cli.find_codeowners_path", return_value=target),
        patch("checkowners.cli.analyze_ownership") as mock_analyze,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 1
    mock_analyze.assert_not_called()


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
    assert result.exit_code == 1
    assert "[companyId]" in result.stdout

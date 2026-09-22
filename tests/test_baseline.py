"""Tests for the accepted-findings baseline and suppressions."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from checkowners.action_report import summarize_bus_factor, summarize_drift
from checkowners.baseline import (
    apply_ratchet,
    findings_from_drift,
    load_baseline,
    write_baseline,
)
from checkowners.cli import app
from checkowners.config import load_config
from checkowners.drift import detect_drift
from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    DriftEntry,
    DriftResult,
    Finding,
    Suppression,
)
from tests.test_cli import (
    _DRIFT_DETECTED,
    _MOCK_PATH,
    _MOCK_TOKEN,
    _NOW,
    _OWNERSHIP,
    _run_github_action,
)
from tests.test_drift import _config, _owner, _ownership, _write_codeowners

runner = CliRunner()
_MOCK_LS_FILES = "checkowners.drift._tracked_files"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "state"))
    with (
        patch("checkowners.cli.resolve_as_of", return_value=_NOW),
        patch("checkowners.cli.head_commit_sha", return_value="deadbeef"),
    ):
        yield


def _write_config(root: Path, content: str) -> None:
    config_dir = root / ".github"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "checkowners.yml").write_text(content, encoding="utf-8")


def _cli_run(args: list[str], *, drift: DriftResult = _DRIFT_DETECTED) -> tuple[int, str]:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=drift),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, args)
    return result.exit_code, result.stdout


def test_baseline_create_then_new_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / ".checkowners-baseline.json"
    exit_code, _stdout = _cli_run(["baseline", "create", "--output", str(baseline)])
    assert exit_code == 0
    created = load_baseline(baseline)
    assert created
    assert all(item.rule in {"missing", "stale", "changed", "single-expert"} for item in created)

    exit_code, stdout = _cli_run(["drift", "--json", "--baseline", str(baseline)])
    assert exit_code == 0
    data = json.loads(stdout)
    assert data["drift_detected"] is False
    assert data["missing"] == []
    assert data["stale"] == []
    assert data["changed"] == []
    assert data["baselined"] == len(created)
    assert data["suppressed"] == 0
    assert data["stale_baseline"] == []

    extra = DriftResult(
        stale=_DRIFT_DETECTED.stale,
        missing=(
            *_DRIFT_DETECTED.missing,
            DriftEntry(path="/extra.py", confidence_delta=0.5, reason="new path"),
        ),
        changed=_DRIFT_DETECTED.changed,
        drift_detected=True,
    )
    exit_code, stdout = _cli_run(
        ["drift", "--json", "--baseline", str(baseline)],
        drift=extra,
    )
    assert exit_code == 0
    data = json.loads(stdout)
    assert data["drift_detected"] is True
    assert [item["path"] for item in data["missing"]] == ["/extra.py"]
    assert data["stale"] == []
    assert data["changed"] == []


def test_finding_identity_survives_codeowners_reorder(tmp_path: Path) -> None:
    ownership = _ownership(
        {
            "src/main.py": (_owner("@carol"),),
            "docs/readme.md": (_owner("@dave"),),
        }
    )
    first = _write_codeowners(tmp_path, "src/ @alice\ndocs/ @bob\n")
    with patch(_MOCK_LS_FILES, return_value=("src/main.py", "docs/readme.md")):
        before = detect_drift(tmp_path, ownership, _config(min_delta=0.0), codeowners_path=first)
    first.write_text("docs/ @bob\nsrc/ @alice\n", encoding="utf-8")
    with patch(_MOCK_LS_FILES, return_value=("src/main.py", "docs/readme.md")):
        after = detect_drift(tmp_path, ownership, _config(min_delta=0.0), codeowners_path=first)
    assert {item.identity() for item in findings_from_drift(before)} == {
        item.identity() for item in findings_from_drift(after)
    }
    before_reasons = {item.path: item.reason for item in before.changed}
    after_reasons = {item.path: item.reason for item in after.changed}
    assert before_reasons != after_reasons


def test_expired_suppression_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(
        tmp_path,
        "\n".join(
            [
                "suppressions:",
                "  - path: /old.py",
                "    rule: stale",
                "    expires: 2020-01-01",
                '    reason: "Scheduled for retirement"',
                "",
            ]
        ),
    )
    exit_code, stdout = _cli_run(["drift"])
    assert exit_code == 1
    assert "Expired suppression" in stdout
    assert "2020-01-01" in stdout
    assert "Scheduled for retirement" in stdout


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "suppressions:\n  - path: legacy/**\n    rule: single-expert\n",
            "reason is required",
        ),
        (
            "suppressions:\n  - path: legacy/**\n    rule: single-expert\n    reason: '   '\n",
            "reason is required",
        ),
        (
            "suppressions:\n  - path: legacy/**\n    rule: not-a-rule\n    reason: later\n",
            "rule",
        ),
        (
            (
                "suppressions:\n  - path: legacy/**\n    rule: stale\n"
                "    expires: soon\n    reason: later\n"
            ),
            "YYYY-MM-DD",
        ),
    ],
)
def test_invalid_suppression_rejected_at_config_load(tmp_path: Path, body: str, match: str) -> None:
    _write_config(tmp_path, body)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=tmp_path)


def test_valid_suppression_and_baseline_file_round_trip(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "\n".join(
            [
                "drift:",
                "  baseline_file: .checkowners-baseline.json",
                "suppressions:",
                "  - path: legacy/**",
                "    rule: single-expert",
                "    expires: 2026-12-31",
                '    reason: "Scheduled for retirement in Q4"',
                "",
            ]
        ),
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.drift.baseline_file == ".checkowners-baseline.json"
    assert cfg.suppressions == (
        Suppression(
            path="legacy/**",
            rule="single-expert",
            reason="Scheduled for retirement in Q4",
            expires=date(2026, 12, 31),
        ),
    )


def test_stale_baseline_reported_after_fix() -> None:
    current = DriftResult(
        stale=(),
        missing=(DriftEntry(path="src/a.py", confidence_delta=0.4, reason="missing"),),
        changed=(),
        drift_detected=True,
    )
    baseline = (
        Finding(rule="missing", path="src/a.py"),
        Finding(rule="missing", path="src/b.py"),
    )
    outcome = apply_ratchet(
        current,
        baseline=baseline,
        suppressions=(),
        as_of=_NOW.date(),
    )
    assert [item.path for item in outcome.stale_baseline] == ["src/b.py"]
    assert outcome.counts.stale_baseline == 1
    assert outcome.counts.baselined == 1
    assert outcome.drift.drift_detected is False


def test_github_action_honors_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    findings = (
        *findings_from_drift(_DRIFT_DETECTED),
        Finding(rule="single-expert", path="src/auth.py", owners=("dave@example.com",)),
    )
    baseline = tmp_path / ".checkowners-baseline.json"
    write_baseline(baseline, findings)
    exit_code, _stdout, _output, _summary = _run_github_action(
        tmp_path,
        monkeypatch,
        ["github-action", "--baseline", str(baseline)],
        drift=_DRIFT_DETECTED,
    )
    assert exit_code == 0
    drift = json.loads((tmp_path / "drift.json").read_text(encoding="utf-8"))
    bus = json.loads((tmp_path / "bus_factor.json").read_text(encoding="utf-8"))
    assert drift["drift_detected"] is False
    assert drift["baselined"] == 4
    assert drift["suppressed"] == 0
    assert drift["stale_baseline"] == []
    summary = summarize_drift(drift, 50)
    assert summary["counts"]["baselined"] == 4
    assert summary["counts"]["suppressed"] == 0
    assert summary["counts"]["stale_baseline"] == 0
    bus_summary = summarize_bus_factor(bus, 50)
    assert bus_summary["counts"]["baselined"] == 4
    assert "src/auth.py" not in bus["critical_paths"]


def test_github_action_honors_baseline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    findings = (
        *findings_from_drift(_DRIFT_DETECTED),
        Finding(rule="single-expert", path="src/auth.py", owners=("dave@example.com",)),
    )
    baseline = tmp_path / "accepted.json"
    write_baseline(baseline, findings)
    monkeypatch.setenv("CHECKOWNERS_BASELINE", str(baseline))
    exit_code, _stdout, _output, _summary = _run_github_action(
        tmp_path,
        monkeypatch,
        ["github-action"],
        drift=_DRIFT_DETECTED,
    )
    assert exit_code == 0
    drift = json.loads((tmp_path / "drift.json").read_text(encoding="utf-8"))
    assert drift["drift_detected"] is False
    assert drift["baselined"] == 4


def test_write_baseline_is_sorted_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "base.json"
    write_baseline(
        path,
        (
            Finding(rule="changed", path="b.py", owners=("@Bob", "@alice")),
            Finding(rule="changed", path="a.py", owners=("@carol",)),
        ),
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == COMMAND_SCHEMA_VERSION
    assert [item["path"] for item in raw["findings"]] == ["a.py", "b.py"]
    assert raw["findings"][1]["owners"] == ["@alice", "@Bob"]

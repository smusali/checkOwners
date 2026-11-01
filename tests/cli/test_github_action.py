"""Contracts for the Action entry point, the report, and the cache and identity stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.conftest import Cli, Route, fake_api

from checkowners import __version__


def test_github_action_writes_the_documented_outputs(
    cli: Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli.codeowners("* @nobody\n")
    output = tmp_path / "github_output"
    summary = tmp_path / "step_summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    summary.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    result = cli.run("github-action", "--no-fail-on-drift")
    assert result.exit_code == 0
    written = output.read_text(encoding="utf-8")
    assert "drift_summary" in written
    assert "artifact_name" in written
    assert summary.read_text(encoding="utf-8").strip()


def test_github_action_json_carries_the_drift_summary(cli: Cli) -> None:
    cli.codeowners("* @nobody\n")
    payload = cli.json("github-action", "--no-fail-on-drift")
    drift = payload.child("drift_summary")
    assert "drift_detected" in drift
    assert "severity" in drift


def test_github_action_can_fail_on_drift(cli: Cli) -> None:
    cli.codeowners("* @nobody\n")
    blocking = cli.run("github-action", "--fail-on-drift")
    allowed = cli.run("github-action", "--no-fail-on-drift")
    assert blocking.exit_code == 3
    assert allowed.exit_code == 0


def test_report_writes_one_offline_html_file(cli: Cli, tmp_path: Path) -> None:
    target = tmp_path / "report.html"
    result = cli.run("report", "--html", str(target))
    assert result.exit_code == 0
    html = target.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Ownership report" in html
    assert "http://" not in html


def test_cache_info_names_this_repository(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    payload = cli.json("cache", "info")
    assert payload.flag("current_state_exists") is True
    assert payload.text("current_repo_id")
    assert payload.whole("state_files") >= 1


def test_cache_info_exits_one_without_a_state_file(cli: Cli) -> None:
    result = cli.run("cache", "info")
    assert result.exit_code == 1


def test_cache_path_prints_the_directory(cli: Cli) -> None:
    result = cli.run("cache", "path")
    assert result.exit_code == 0
    assert Path(result.stdout.strip()).name


def test_cache_clear_keeps_handles_and_purge_removes_them(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    cleared = cli.run("cache", "clear")
    assert cleared.exit_code == 0
    assert cli.run("cache", "info").exit_code == 1
    purged = cli.run("cache", "purge")
    assert purged.exit_code == 0
    assert "purged" in purged.stdout


def test_identity_audit_reports_no_duplicates_for_distinct_authors(cli: Cli) -> None:
    payload = cli.json("identity", "audit")
    assert payload.whole("duplicate_count") == 0
    assert payload.rows("duplicates") == []


def test_identity_merge_then_unmerge_round_trips(cli: Cli) -> None:
    merged = cli.run("identity", "merge", "@alice", "@bob", "@alice")
    assert merged.exit_code == 0
    reverted = cli.run("identity", "unmerge", "@alice", "@bob")
    assert reverted.exit_code == 0


def test_as_of_pins_the_analysis_instant(cli: Cli) -> None:
    early = cli.json("--as-of", "2026-05-05T00:00:00+00:00", "print")
    late = cli.json("--as-of", "2026-05-25T00:00:00+00:00", "print")
    assert early.child("paths").data != late.child("paths").data


def test_a_cache_behind_head_is_reanalyzed_unless_stale_is_allowed(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    cli.repo.commit_files(
        {"src/extra.py": "y = 2\n"},
        "add extra",
        date="2026-05-25T12:00:00+00:00",
    )
    refreshed = cli.run("--max-age", "0", "expertise", "src/app.py")
    reused = cli.run("--allow-stale", "expertise", "src/app.py")
    assert refreshed.exit_code == 0
    assert "stale" in refreshed.output.casefold()
    assert reused.exit_code == 0
    assert "Using cached analysis" in reused.output


def test_simulate_refuses_a_cache_behind_head(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    cli.repo.commit_files(
        {"src/extra.py": "y = 2\n"},
        "add extra",
        date="2026-05-25T12:00:00+00:00",
    )
    result = cli.run("--max-age", "0", "simulate", "--remove", "@alice")
    assert result.exit_code == 2


def test_no_cache_neither_reads_nor_writes_state(cli: Cli) -> None:
    assert cli.run("--no-cache", "analyze").exit_code == 0
    assert cli.run("cache", "info").exit_code == 1


def test_no_incremental_scores_the_full_lookback(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    result = cli.run("--no-incremental", "analyze")
    assert result.exit_code == 0
    assert "Incremental analysis" not in result.output


def test_diff_only_scopes_a_read_only_command(cli: Cli) -> None:
    scoped = cli.run("--diff-only", "HEAD~1...HEAD", "analyze")
    assert scoped.exit_code == 0
    assert "src/api.py" not in scoped.stdout


def test_diff_only_is_rejected_on_a_writing_command(cli: Cli) -> None:
    result = cli.run("--diff-only", "HEAD~1...HEAD", "generate", "--force")
    assert result.exit_code == 2
    assert "read-only" in result.output


def test_a_missing_token_warns_without_failing(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    config = cli.repo.path / ".github" / "checkowners.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("github:\n  api_enabled: true\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = cli.run("analyze")
    assert result.exit_code == 0
    assert "github.api_enabled is true but GITHUB_TOKEN is not set" in result.output


def test_an_api_enabled_run_resolves_teams_through_the_http_seam(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = cli.repo.path / ".github" / "checkowners.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("github:\n  api_enabled: true\n  org: acme\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    routes = (
        Route("GET", "/orgs/acme/teams?", body=[{"slug": "platform", "parent": None}]),
        Route("GET", "/orgs/acme/teams/platform/members", body=[{"login": "alice"}]),
        Route("GET", "/rate_limit", body={"resources": {"core": {"remaining": 100}}}),
    )
    with fake_api(*routes) as api:
        result = cli.run("topology")
    assert result.exit_code == 0
    assert any(path.startswith("/orgs/acme/teams") for path in api.paths())


def test_the_json_payload_carries_the_package_version_and_head(cli: Cli) -> None:
    payload = cli.json("analyze")
    assert payload.text("checkowners_version") == __version__
    assert len(payload.text("head_sha")) == 40
    assert "models" not in payload


def test_source_date_epoch_pins_the_generated_timestamp(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    payload = cli.json("analyze")
    assert payload.text("generated_at").startswith("2023-11-14")


def test_the_baseline_file_is_findings_only_json(cli: Cli) -> None:
    payload = cli.json("baseline", "create")
    stored = json.loads((cli.repo.path / payload.text("path")).read_text(encoding="utf-8"))
    assert set(stored) == {"findings"}

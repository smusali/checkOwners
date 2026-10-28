"""Tests for CODEOWNERS doctor and audit."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from checkowners.audit import (
    AuditFacts,
    AuditReport,
    AuditRow,
    EnvironmentFacts,
    audit_body,
    audit_report_findings,
    build_audit,
    environment_rows,
    format_audit,
    without_hidden,
)
from checkowners.github import TeamDirectory, TeamRecord
from checkowners.models import Config, DriftResult
from checkowners.patterns import parse_rules
from tests.conftest import git_commit, init_git_repo
from tests.test_cli import _EMPTY_OWNERSHIP, _MOCK_TOKEN, app, runner


def _facts(tmp_path: Path, text: str = "* @alice\n") -> AuditFacts:
    path = tmp_path / "CODEOWNERS"
    path.write_text(text, encoding="utf-8")
    return AuditFacts(
        repo_root=tmp_path,
        config=Config(),
        codeowners_path=path,
        text=text,
        rules=parse_rules(text),
        tracked=("README",),
        head_branch="main",
        base_branch="main",
        file_bytes=path.stat().st_size,
        teams=None,
        ownership=None,
        analysis_error="Analysis did not run.",
        errors=(),
        remote_errors=None,
        private=False,
        ref="main",
        default_branch="main",
        access={},
        review="unknown",
        token_present=False,
        include_inference=False,
    )


def _row(report: AuditReport, rule: str) -> AuditRow:
    matched = [row for row in report.rows if row.rule == rule]
    assert matched, rule
    return matched[0]


def test_text_and_json_fixtures(tmp_path: Path) -> None:
    report = build_audit(_facts(tmp_path))
    text = format_audit(report)
    assert text == (
        "ok: branch: CODEOWNERS was read from main.\n"
        "ok: file-size: CODEOWNERS is 9 bytes.\n"
        "ok: invalid-owner: Syntax source is local.\n"
        "unverifiable: owner-permission: Write access requires a GitHub token.\n"
        "ok: empty-team: No team owners.\n"
        "ok: shadowed-rule: No rule is shadowed.\n"
        "ok: unowned-file: Every tracked file matches a rule.\n"
        "ok: stale: Every pattern matches a tracked file.\n"
        "unverifiable: single-expert: Analysis did not run.\n"
    )
    payload = audit_body(report)
    assert payload["rows"][0] == {
        "owners": [],
        "path": "",
        "rule": "branch",
        "source": "",
        "status": "ok",
        "summary": "CODEOWNERS was read from main.",
    }


def test_owner_without_write_access_fails(tmp_path: Path) -> None:
    report = build_audit(
        replace(
            _facts(tmp_path, "* @ghost\n"),
            token_present=True,
            access={"@ghost": "read"},
        )
    )
    row = _row(report, "owner-permission")
    assert row.status == "fail"
    assert row.findings[0].rule == "owner-permission"
    findings = audit_report_findings(report)
    assert any(item.rule_id == "checkowners/owner-permission" for item in findings)


def test_invisible_team_fails(tmp_path: Path) -> None:
    directory = TeamDirectory(
        org="acme",
        fetched_at="2026-01-01T00:00:00+00:00",
        teams=(
            TeamRecord(
                slug="secret",
                members=frozenset(),
                parent="",
                children=(),
                visible=False,
            ),
        ),
    )
    report = build_audit(
        replace(_facts(tmp_path, "* @acme/secret\n"), teams=directory, token_present=True)
    )
    row = _row(report, "invisible-team")
    assert row.status == "fail"
    assert row.owners == ("@acme/secret",)


def test_shadowing_star_is_a_warning(tmp_path: Path) -> None:
    report = build_audit(_facts(tmp_path, "* @alice\n* @bob\n"))
    row = _row(report, "shadowed-rule")
    assert row.status == "warn"
    assert "shadowed" in row.summary


def test_offline_run_is_unverifiable(tmp_path: Path) -> None:
    report = build_audit(_facts(tmp_path))
    syntax = _row(report, "invalid-owner")
    access = _row(report, "owner-permission")
    assert syntax.status == "ok"
    assert syntax.source == "local"
    assert access.status == "unverifiable"
    hidden = without_hidden(report, set())
    assert _row(hidden, "owner-permission").status == "unverifiable"


def test_environment_rows_do_not_pass_a_missing_token() -> None:
    rows = environment_rows(
        EnvironmentFacts(
            git_repo=True,
            full_history=True,
            token_present=False,
            token_scopes=(),
            codeowners_path="CODEOWNERS",
            api_reachable=None,
            team_visibility="unverifiable",
            review_history="unverifiable",
            blame_ignore_revs=False,
            cache_writable=True,
            version="0.6.0",
            config_parsed=True,
        )
    )
    text = format_audit(AuditReport(rows=(), env=rows))
    assert "unverifiable: token:" in text
    assert "ok: token:" not in text


def test_help_lists_audit_and_doctor() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "audit" in result.stdout
    assert "doctor" in result.stdout


def test_bare_command_runs_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "README").write_text("fixture\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "init",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir(parents=True)
    codeowners.write_text("* @alice\n", encoding="utf-8")
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
        patch(
            "checkowners.cli.detect_drift",
            return_value=DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        ),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "file-size" in result.stdout
    assert "unverifiable: owner-permission:" in result.stdout


def test_doctor_json_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "README").write_text("fixture\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "init",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text("* @alice\n", encoding="utf-8")
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
        patch("checkowners.cli.find_codeowners_path", return_value=codeowners),
        _MOCK_TOKEN,
    ):
        env = runner.invoke(app, ["doctor", "--env"])
        listed = runner.invoke(app, ["doctor", "--json"])
    assert env.exit_code == 0, env.output
    assert "environment:" in env.stdout
    assert "unverifiable: token:" in env.stdout
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stdout)
    assert isinstance(payload["rows"], list)

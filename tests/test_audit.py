from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

import pytest
import typer

from checkowners.audit import (
    CODEOWNERS_SIZE_LIMIT,
    AuditFacts,
    AuditReport,
    AuditRow,
    EnvironmentFacts,
    audit_body,
    audit_report_findings,
    build_audit,
    environment_rows,
    fail_findings,
    format_audit,
    without_hidden,
)
from checkowners.cli import (
    _audit_report,
    _branch_name,
    _cache_writable,
    _load_teams,
)
from checkowners.github import TeamDirectory, TeamRecord, set_offline
from checkowners.models import (
    Config,
    ContributorStatus,
    DecayWarning,
    DriftEntry,
    DriftResult,
    Finding,
    GithubConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.patterns import parse_rules
from checkowners.validate import RemoteCodeownersError, ValidationFinding
from tests.conftest import git_commit, init_git_repo
from tests.test_cli import _EMPTY_OWNERSHIP, _MOCK_TOKEN, app, runner


def _facts(tmp_path: Path, text: str = "* @alice\n") -> AuditFacts:
    path = tmp_path / "CODEOWNERS"
    path.write_text(text, encoding="utf-8", newline="\n")
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


def _owner(
    handle: str,
    score: float = 0.9,
    status: ContributorStatus = "active",
) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=datetime(2026, 1, 1, tzinfo=UTC),
        commits=3,
        contributor_status=status,
    )


def _mapped(paths: dict[str, PathOwnership]) -> OwnershipMap:
    return OwnershipMap(paths=paths, last_analyzed=datetime(2026, 1, 1, tzinfo=UTC))


def test_audit_rows_cover_failures_and_warnings(tmp_path: Path) -> None:
    outside = Path("/no/such/codeowners")
    oversized = replace(
        _facts(tmp_path, "* @alice\n"),
        codeowners_path=outside,
        file_bytes=CODEOWNERS_SIZE_LIMIT + 1,
        head_branch="",
        base_branch="",
        text="!secret @alice\nsrc/[a].py bad\n",
        remote_errors=(),
        private=True,
        ref="feature",
        default_branch="main",
        errors=(
            ValidationFinding(0, "", "CODEOWNERS file not found"),
            ValidationFinding(3, "bad", "Invalid owner format: bad"),
        ),
        token_present=True,
        access={"@alice": "write", "alice@example.com": "unknown"},
        rules=parse_rules("* @alice alice@example.com\n"),
        tracked=(),
        include_inference=True,
        analysis_error="",
        review="disabled",
    )
    report = build_audit(oversized)
    assert _row(report, "branch").status == "unverifiable"
    assert _row(report, "file-size").status == "fail"
    assert _row(report, "invalid-owner").status == "unverifiable"
    assert "alice@example.com" in _row(report, "owner-permission").summary
    assert _row(report, "unowned-file").status == "unverifiable"
    assert _row(report, "self-protection").status == "unverifiable"
    assert _row(report, "ruleset").status == "warn"
    assert _row(report, "ruleset").findings[0].path == "branch"
    assert _row(report, "changed").summary == "Drift was not compared."
    assert "Analysis did not run." in _row(report, "decayed-owner").summary
    disagreed = build_audit(
        replace(
            _facts(tmp_path, "* @alice\n"),
            remote_errors=(
                RemoteCodeownersError(
                    1, 1, "Invalid pattern", "negation", "!x @alice", "CODEOWNERS"
                ),
            ),
            token_present=True,
        )
    )
    assert "disagree" in _row(disagreed, "invalid-owner").summary
    noted = build_audit(
        replace(
            _facts(tmp_path, "* @alice\n"),
            remote_errors=(
                RemoteCodeownersError(1, 1, "Invalid owner", "no", "* @alice", "CODEOWNERS"),
            ),
        )
    )
    assert "Other GitHub kinds: Invalid owner." in _row(noted, "invalid-owner").summary
    syntax = build_audit(
        replace(
            _facts(tmp_path, "* bad\n"),
            errors=(ValidationFinding(2, "* bad", "Invalid owner format: bad"),),
        )
    )
    assert fail_findings(syntax)[0].path.endswith(":2")
    assert audit_report_findings(syntax)[0].start_line == 2
    granted = build_audit(
        replace(_facts(tmp_path, "* @alice\n"), token_present=True, access={"@alice": "write"})
    )
    assert _row(granted, "owner-permission").status == "ok"
    nobody = build_audit(replace(_facts(tmp_path, "*\n"), token_present=True))
    assert _row(nobody, "owner-permission").summary == "CODEOWNERS names no owners."
    missing_access = build_audit(
        replace(_facts(tmp_path, "* @alice\n"), token_present=True, access={})
    )
    assert _row(missing_access, "owner-permission").status == "unverifiable"
    branched = build_audit(replace(_facts(tmp_path), head_branch="feature", base_branch="main"))
    assert _row(branched, "branch").status == "warn"
    directory = TeamDirectory(
        org="acme",
        fetched_at="2026-01-01T00:00:00+00:00",
        teams=(
            TeamRecord("secret", frozenset(), "", (), False),
            TeamRecord("empty", frozenset(), "", (), True),
            TeamRecord("core", frozenset({"alice"}), "", (), True),
        ),
    )
    teams = build_audit(
        replace(
            _facts(tmp_path, "* @acme/secret @acme/empty @acme/core\n"),
            teams=directory,
            token_present=True,
        )
    )
    assert _row(teams, "invisible-team").status == "fail"
    assert _row(teams, "empty-team").status == "fail"
    uncovered = build_audit(
        replace(_facts(tmp_path, "docs/ @alice\n"), tracked=("README", "src/a.py", "src/b.py"))
    )
    assert _row(uncovered, "unowned-file").status == "warn"
    assert _row(uncovered, "stale").status == "warn"
    owned = _mapped(
        {
            "README": PathOwnership(owners=(_owner("@alice"),), qualified_owner_count=1),
            "src/a.py": PathOwnership(
                owners=(_owner("@alice", 0.2), _owner("@bob", 0.2), _owner("@cara", 0.2)),
                qualified_owner_count=3,
            ),
        }
    )
    warned = build_audit(
        replace(
            _facts(tmp_path, "* @alice\n* @bob\nREADME @alice\n"),
            ownership=owned,
            include_inference=True,
            token_present=True,
            review="required",
            tracked=("README", "src/a.py", "src/b.py"),
            drift=DriftResult(
                stale=(),
                missing=(DriftEntry(path="src/a.py", confidence_delta=0.4),),
                changed=(DriftEntry(path="README", confidence_delta=0.5, owners=("@alice",)),),
                drift_detected=True,
            ),
            analysis_error="",
        )
    )
    assert _row(warned, "shadowed-rule").status == "warn"
    assert _row(warned, "single-expert").status == "warn"
    assert _row(warned, "duplicate-rule").status == "warn"
    assert _row(warned, "broad-rule").status == "warn"
    assert _row(warned, "self-protection").status == "ok"
    assert _row(warned, "ruleset").status == "ok"
    assert "1 changed and 1 missing" in _row(warned, "changed").summary
    assert _row(warned, "decayed-owner").status == "ok"
    assert _row(warned, "observed-unowned").status == "ok"
    quiet = _mapped({"src/a.py": PathOwnership(owners=(), qualified_owner_count=0)})
    decayed = _mapped(
        {
            "README": PathOwnership(
                owners=(_owner("@alice", status="former"), _owner("@bob", 0.1)),
                qualified_owner_count=1,
                decay_warnings=(
                    DecayWarning(
                        handle="@alice",
                        path="README",
                        last_commit=datetime(2024, 1, 1, tzinfo=UTC),
                        days_since_last_commit=400,
                        historical_confidence=0.8,
                    ),
                ),
            ),
            "src/b.py": PathOwnership(
                owners=(_owner("@bob", 0.8), _owner("@alice", 0.1, status="former")),
                qualified_owner_count=2,
            ),
        }
    )
    risks = build_audit(
        replace(
            _facts(tmp_path, "docs/ @alice\n"),
            ownership=decayed,
            include_inference=True,
            tracked=("README",),
            drift=DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
            config=Config(contributor_roster=(("bob", "active"),)),
        )
    )
    assert _row(risks, "decayed-owner").status == "warn"
    assert _row(risks, "orphaned-owner").status == "warn"
    assert _row(risks, "broad-rule").status == "ok"
    assert _row(risks, "self-protection").status == "warn"
    quiet_report = build_audit(
        replace(
            _facts(tmp_path, "* @alice\n"),
            ownership=quiet,
            include_inference=True,
            drift=DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        )
    )
    assert _row(quiet_report, "observed-unowned").status == "warn"
    rostered = build_audit(
        replace(
            _facts(tmp_path, "* @bob\n"),
            ownership=_mapped(
                {"README": PathOwnership(owners=(_owner("@bob"),), qualified_owner_count=2)}
            ),
            include_inference=True,
            config=Config(contributor_roster=(("bob", "active"),)),
            drift=DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        )
    )
    assert _row(rostered, "orphaned-owner").status == "ok"
    assert _row(rostered, "single-expert").status == "ok"
    hidden = without_hidden(
        warned,
        {item.identity() for item in _row(warned, "changed").findings[:1]},
    )
    assert _row(hidden, "changed").findings[0].path == "README"
    plain = build_audit(replace(_facts(tmp_path, "* @acme/backend\n"), teams=None))
    assert "GitHub API" in _row(plain, "invisible-team").summary
    unknown = AuditRow(rule="not-a-rule", status="warn", summary="no id")
    bare = AuditRow(rule="ruleset", status="warn", summary="review is off")
    vacuum = AuditRow(
        rule="knowledge-vacuum",
        status="warn",
        summary="vacuum",
        findings=(Finding(rule="knowledge-vacuum", path="README"),),
    )
    emitted = audit_report_findings(AuditReport(rows=(unknown, bare, vacuum)))
    assert [item.rule_id for item in emitted] == ["checkowners/ruleset"]
    body = audit_body(AuditReport(rows=report.rows[:1], env=report.rows[:1]))
    assert "env" in body
    env = environment_rows(
        EnvironmentFacts(
            git_repo=False,
            full_history=True,
            token_present=True,
            token_scopes=("repo",),
            codeowners_path="",
            api_reachable=True,
            team_visibility="visible",
            review_history="available",
            blame_ignore_revs=True,
            cache_writable=False,
            version="0.6.0",
            config_parsed=False,
        )
    )
    text = format_audit(AuditReport(rows=(), env=env))
    assert "fail: git-repo: Git repo: no." in text
    assert "ok: api: GitHub API answered." in text
    assert "CODEOWNERS path is unset." in text
    assert "fail: config:" in text


def test_audit_command_uses_github_baseline_and_sarif(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    codeowners.write_text("* @ghost\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "checkowners.yml").write_text(
        "version: 1\ngithub:\n  org: acme\n",
        encoding="utf-8",
    )
    (tmp_path / ".git-blame-ignore-revs").write_text("\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("GITHUB_BASE_REF", "develop")
    directory = TeamDirectory(
        org="acme",
        fetched_at="2026-01-01T00:00:00+00:00",
        teams=(TeamRecord("core", frozenset({"ann"}), "", (), True),),
    )
    hidden = TeamDirectory(
        org="acme",
        fetched_at="2026-01-01T00:00:00+00:00",
        teams=(TeamRecord("core", frozenset(), "", (), False),),
    )
    set_offline(False)
    with (
        patch("checkowners.cli.get_github_token", return_value="tok"),
        patch("checkowners.cli.repository_visibility", return_value=(True, "main")),
        patch("checkowners.cli.fetch_codeowners_errors", return_value=()),
        patch("checkowners.cli.owner_access", return_value="read"),
        patch("checkowners.cli.code_owner_review_required", return_value="disabled"),
        patch("checkowners.cli.load_team_directory", return_value=directory),
        patch("checkowners.cli.api_status", return_value=(True, ("repo",))),
        patch("checkowners.cli.organization_members", return_value=None),
        patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
        patch(
            "checkowners.cli.detect_drift",
            return_value=DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        ),
    ):
        sarif = runner.invoke(app, ["audit", "--format", "sarif"])
    assert sarif.exit_code == 3, sarif.output
    assert "owner-permission" in sarif.stdout
    with (
        patch("checkowners.cli.get_github_token", return_value="tok"),
        patch("checkowners.cli.repository_visibility", return_value=(True, "main")),
        patch("checkowners.cli.fetch_codeowners_errors", return_value=()),
        patch("checkowners.cli.owner_access", return_value="write"),
        patch("checkowners.cli.code_owner_review_required", return_value="required"),
        patch("checkowners.cli.load_team_directory", return_value=directory),
        patch("checkowners.cli.api_status", return_value=(True, ("repo",))),
        patch("checkowners.cli.organization_members", return_value=None),
        patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
    ):
        env = runner.invoke(app, ["doctor", "--env"])
    assert env.exit_code == 0, env.output
    assert "Token scopes: repo." in env.stdout
    assert "Team visibility: visible." in env.stdout
    baseline = tmp_path / "accepted.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "findings": [{"rule": "owner-permission", "path": "@ghost", "owners": ["@ghost"]}],
            }
        ),
        encoding="utf-8",
    )
    with (
        patch("checkowners.cli.get_github_token", return_value="tok"),
        patch("checkowners.cli.repository_visibility", return_value=None),
        patch("checkowners.cli.fetch_codeowners_errors", return_value=()),
        patch("checkowners.cli.owner_access", return_value="read"),
        patch("checkowners.cli.code_owner_review_required", return_value="required"),
        patch("checkowners.cli.load_team_directory", return_value=hidden),
        patch("checkowners.cli.api_status", return_value=(False, ())),
        patch("checkowners.cli.organization_members", return_value=None),
        patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
        patch("checkowners.cli.detect_drift", side_effect=typer.Exit()),
    ):
        accepted = runner.invoke(app, ["audit", "--baseline", str(baseline)])
        missing = runner.invoke(app, ["audit", "--baseline", str(tmp_path / "missing.json")])
        hidden_env = runner.invoke(app, ["doctor", "--env"])
        assert _load_teams(Config(github=GithubConfig(org="acme"))) is hidden
        codeowners.write_text("*\n", encoding="utf-8")
        quiet, _ownership, _rules = _audit_report(
            Config(github=GithubConfig(org="acme")),
            include_inference=False,
            env=False,
        )
    assert accepted.exit_code == 0, accepted.output
    assert "owner-permission" not in accepted.stdout
    assert missing.exit_code == 2, missing.output
    assert hidden_env.exit_code == 3, hidden_env.output
    assert "Team visibility: invisible." in hidden_env.stdout
    assert _row(quiet, "owner-permission").summary == "CODEOWNERS names no owners."
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr("checkowners.cli.cache_directory", lambda: blocked)
    assert _cache_writable() is False

    class _Head:
        stdout = "HEAD\n"

    monkeypatch.setattr("checkowners.cli.run_git", lambda *_args, **_kwargs: _Head())
    assert _branch_name(tmp_path) == ""

    def _fail_git(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("no git")

    monkeypatch.setattr("checkowners.cli.run_git", _fail_git)
    assert _branch_name(tmp_path) == ""
    with (
        patch("checkowners.cli.get_github_token", return_value=""),
        patch("checkowners.cli.analyze_ownership", side_effect=OSError("git")),
    ):
        report, ownership, _parsed = _audit_report(Config(), include_inference=True, env=True)
    assert ownership is None
    assert any(row.rule == "git-repo" and row.status == "fail" for row in report.env)

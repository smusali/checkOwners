"""Contracts for the commands that read the committed CODEOWNERS."""

from __future__ import annotations

import json

import pytest
from tests.conftest import Cli, Route, fake_api

from checkowners import __version__

_OWNED = "* @alice\n"


def test_bare_invocation_runs_audit(cli: Cli) -> None:
    cli.codeowners(_OWNED)
    bare = cli.run()
    named = cli.run("audit")
    assert bare.exit_code == named.exit_code
    assert "unowned-file" in bare.stdout


def test_help_still_prints_help(cli: Cli) -> None:
    result = cli.run("--help")
    assert result.exit_code == 0
    assert "audit" in result.stdout
    assert "doctor" in result.stdout


def test_version_prints_the_package_version(cli: Cli) -> None:
    result = cli.run("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"checkowners {__version__}"


def test_audit_reports_a_row_per_rule(cli: Cli) -> None:
    cli.codeowners(_OWNED)
    payload = cli.json("audit")
    rules = {row.text("rule") for row in payload.rows("rows")}
    assert {"branch", "file-size", "unowned-file"} <= rules
    assert payload.text("checkowners_version") == __version__


def test_audit_without_a_codeowners_file_reports_findings(cli: Cli) -> None:
    result = cli.run("audit")
    assert result.exit_code == 3
    assert "CODEOWNERS file not found" in result.stdout


def test_exit_zero_hides_findings_only(cli: Cli) -> None:
    findings = cli.run("audit")
    hidden = cli.run("--exit-zero", "audit")
    assert findings.exit_code == 3
    assert hidden.exit_code == 0
    assert "CODEOWNERS file not found" in hidden.stdout


def test_doctor_adds_configuration_rows(cli: Cli) -> None:
    cli.codeowners(_OWNED)
    payload = cli.json("doctor")
    assert payload.rows("rows")
    env = cli.run("doctor", "--env")
    assert env.exit_code in {0, 3}
    assert "git" in env.stdout.casefold()


def test_validate_accepts_a_well_formed_file(cli: Cli) -> None:
    cli.codeowners(_OWNED)
    result = cli.run("validate")
    assert result.exit_code == 0
    payload = cli.json("validate")
    assert payload.flag("valid") is True
    assert payload.rows("errors") == []


def test_validate_reports_a_syntax_error(cli: Cli) -> None:
    cli.codeowners("!build @alice\n")
    result = cli.run("validate")
    assert result.exit_code == 3
    assert "negation" in result.stdout.casefold()


@pytest.mark.parametrize("dialect", ["bitbucket", "gerrit", "svn"])
def test_validate_rejects_a_dialect_it_does_not_implement(cli: Cli, dialect: str) -> None:
    cli.codeowners(_OWNED)
    result = cli.run("validate", "--dialect", dialect)
    assert result.exit_code == 2
    assert "Unknown dialect" in result.stdout


def test_explain_path_shows_the_matching_rule(cli: Cli) -> None:
    cli.codeowners("/docs/ @docs\n* @alice\n")
    payload = cli.json("explain-path", "docs/guide.md")
    assert payload.text("path") == "docs/guide.md"
    winner = payload.child("winner")
    assert winner.strings("owners") == ["@alice"]
    assert winner.text("pattern") == "*"


def test_explain_path_reports_an_unowned_path(cli: Cli) -> None:
    cli.codeowners("/docs/ @docs\n")
    payload = cli.json("explain-path", "src/app.py")
    assert payload["winner"] is None
    assert payload.rows("matches") == []


def test_offline_names_the_unavailable_evidence(cli: Cli) -> None:
    cli.codeowners(_OWNED)
    result = cli.run("--offline", "audit")
    assert result.exit_code in {0, 3}
    assert "Network access: disabled" in result.output


def _github_routes(*, permission: str, scopes: str, team_visible: bool) -> tuple[Route, ...]:
    """Return the replies an API-enabled audit reads for the acme/app repository."""
    members: Route = (
        Route("GET", "/orgs/acme/teams/core/members", body=[{"login": "ann"}])
        if team_visible
        else Route("GET", "/orgs/acme/teams/core/members", status=403)
    )
    return (
        Route("GET", "/repos/acme/app/collaborators/", body={"permission": permission}),
        Route("GET", "/repos/acme/app/codeowners/errors", body={"errors": []}),
        Route("GET", "/repos/acme/app/rulesets", body=[]),
        Route("GET", "/repos/acme/app/branches/", status=404),
        Route("GET", "/repos/acme/app", body={"private": True, "default_branch": "main"}),
        Route("GET", "/orgs/acme/teams?", body=[{"slug": "core", "parent": None}]),
        members,
        Route("GET", "/user", body={"login": "ann"}, headers={"x-oauth-scopes": scopes}),
    )


def _enable_api(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    config = cli.repo.path / ".github" / "checkowners.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("github:\n  org: acme\n  api_enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")


def test_audit_reports_an_owner_without_write_access(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli.codeowners("* @ghost\n")
    _enable_api(cli, monkeypatch)
    routes = _github_routes(permission="read", scopes="repo", team_visible=True)
    with fake_api(*routes):
        result = cli.run("audit", "--format", "sarif")
    assert result.exit_code == 3
    assert "owner-permission" in result.stdout


def test_doctor_env_names_the_token_scopes_and_team_visibility(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli.codeowners("* @ghost\n")
    _enable_api(cli, monkeypatch)
    with fake_api(*_github_routes(permission="write", scopes="repo", team_visible=True)):
        visible = cli.run("doctor", "--env")
    with fake_api(*_github_routes(permission="write", scopes="repo", team_visible=False)):
        hidden = cli.run("doctor", "--env")
    assert "Token scopes: repo." in visible.stdout
    assert "Team visibility: visible." in visible.stdout
    assert "Team visibility: invisible." in hidden.stdout


def test_a_baseline_accepts_a_recorded_finding(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli.codeowners("* @ghost\n")
    _enable_api(cli, monkeypatch)
    baseline = cli.repo.path / "accepted.json"
    baseline.write_text(
        json.dumps(
            {"findings": [{"rule": "owner-permission", "path": "@ghost", "owners": ["@ghost"]}]}
        ),
        encoding="utf-8",
    )
    routes = _github_routes(permission="read", scopes="repo", team_visible=True)
    with fake_api(*routes):
        accepted = cli.run("audit", "--baseline", str(baseline))
    assert accepted.exit_code == 0
    assert "owner-permission" not in accepted.stdout


def test_a_missing_baseline_file_exits_two(cli: Cli) -> None:
    cli.codeowners(_OWNED)
    result = cli.run("audit", "--baseline", str(cli.repo.path / "missing.json"))
    assert result.exit_code == 2

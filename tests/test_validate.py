"""Tests for checkowners.validate module."""

from __future__ import annotations

from pathlib import Path

import pytest

from checkowners.validate import (
    ConformanceCase,
    RemoteCodeownersError,
    conformance_report,
    validate_codeowners,
)
from tests.conftest import GitRepo


def _write_codeowners(tmp_path: Path, content: str) -> None:
    github_dir = tmp_path / ".github"
    github_dir.mkdir(exist_ok=True)
    (github_dir / "CODEOWNERS").write_text(content, encoding="utf-8")


def test_validate_valid_file(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "# Header\n\n/src/main.py @alice\n/docs/ @bob\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_missing_file(tmp_path: Path) -> None:
    errors = validate_codeowners(tmp_path)
    assert len(errors) == 1
    assert "not found" in errors[0].message


def test_validate_ownerless_rule_is_valid(tmp_path: Path) -> None:
    """GitHub documents owner-less rules as the way to exempt paths."""
    _write_codeowners(tmp_path, "/apps/ @octocat\n/apps/github\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_relative_patterns_are_valid(tmp_path: Path) -> None:
    """GitHub's own examples use relative patterns like docs/* and apps/."""
    _write_codeowners(tmp_path, "docs/* @octocat\napps/ @hubot\nfrontend/package.json @a\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_negation_rejected(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "!/src/main.py @alice\n")
    errors = validate_codeowners(tmp_path)
    assert len(errors) == 1
    assert "negation" in errors[0].message


def test_validate_character_range_rejected(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/src/[a-z].py @alice\n")
    errors = validate_codeowners(tmp_path)
    assert len(errors) == 1
    assert "character ranges" in errors[0].message


def test_validate_escaped_space_pattern(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "docs/getting\\ started.md @docs-team\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_invalid_owner(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/src/main.py not-an-owner!\n")
    errors = validate_codeowners(tmp_path)
    assert len(errors) == 1
    assert "Invalid owner" in errors[0].message


def test_validate_skips_comments_and_blanks(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "# comment\n\n  # indented comment\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_multiple_owners(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/src/ @alice @org/team bob@example.com\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_wildcard_path(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "*.py @alice\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_multiple_errors(tmp_path: Path) -> None:
    content = "/valid.py @alice\n![a].py bad--owner\n"
    _write_codeowners(tmp_path, content)
    errors = validate_codeowners(tmp_path)
    assert len(errors) == 3  # negation + character range + invalid owner


def test_validate_line_numbers(tmp_path: Path) -> None:
    content = "# header\n\n/ok.py @alice\n/bad.py not-an-owner!\n"
    _write_codeowners(tmp_path, content)
    errors = validate_codeowners(tmp_path)
    assert len(errors) == 1
    assert errors[0].line_number == 4


def test_validate_dotted_login_rejected(tmp_path: Path) -> None:
    """GitHub logins cannot contain dots; @user.name is not a real handle."""
    _write_codeowners(tmp_path, "/src/ @user.name\n")
    errors = validate_codeowners(tmp_path)
    assert len(errors) == 1
    assert "Invalid owner" in errors[0].message


def test_validate_email_owner(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/src/ alice@example.com\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_org_team_owner(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/src/ @org/team-name\n")
    errors = validate_codeowners(tmp_path)
    assert errors == []


def test_validate_root_codeowners(tmp_path: Path) -> None:
    (tmp_path / "CODEOWNERS").write_text("/src/ @alice\n", encoding="utf-8")
    errors = validate_codeowners(tmp_path, codeowners_path=tmp_path / "CODEOWNERS")
    assert errors == []


def test_validate_strips_inline_confidence_comment(tmp_path: Path) -> None:
    _write_codeowners(
        tmp_path,
        "/src/main.py alice@example.com  # alice@example.com(0.92)\n",
    )
    errors = validate_codeowners(tmp_path)
    assert errors == []


_RELATIVE_CODEOWNERS = (
    "docs/* @octocat\n"
    "apps/ @hubot\n"
    "frontend/package.json @a\n"
    "docs/getting\\ started.md @docs-team\n"
)


@pytest.mark.integration
def test_validate_committed_relative_patterns(git_repo: GitRepo) -> None:
    codeowners = git_repo.path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir(parents=True)
    codeowners.write_text(_RELATIVE_CODEOWNERS, encoding="utf-8")
    git_repo.commit(
        "add codeowners",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    assert validate_codeowners(git_repo.path) == []
    codeowners.write_text(_RELATIVE_CODEOWNERS + "/src/[a-z].py @alice\n", encoding="utf-8")
    errors = validate_codeowners(git_repo.path)
    assert len(errors) == 1
    assert "character ranges" in errors[0].message
    assert "[a-z]" in errors[0].line


def _remote(line: int, source: str, kind: str = "Invalid pattern") -> RemoteCodeownersError:
    return RemoteCodeownersError(
        line=line,
        column=1,
        kind=kind,
        message=kind,
        source=source,
        path=".github/CODEOWNERS",
    )


def test_conformance_agrees_and_ignores_unknown_owners() -> None:
    case = ConformanceCase(
        repo="example/widgets",
        ref="main",
        default_branch="main",
        private=False,
        text="* @ghost\n",
        api_errors=(_remote(1, "* @ghost", "Invalid owner"),),
    )
    report = conformance_report((case,))
    assert report["pass_rate"] == 1.0
    assert report["compared"] == 1
    assert report["divergences"] == []


def test_conformance_reports_a_syntax_divergence() -> None:
    case = ConformanceCase(
        repo="example/widgets",
        ref="main",
        default_branch="main",
        private=False,
        text="!build @alice\n",
        api_errors=(),
    )
    report = conformance_report((case,))
    assert report["pass_rate"] == 0.0
    assert report["divergences"]


def test_conformance_excludes_private_branch_silence() -> None:
    case = ConformanceCase(
        repo="example/private",
        ref="feature",
        default_branch="main",
        private=True,
        text="!secret @alice\n",
        api_errors=(),
    )
    report = conformance_report((case,))
    assert report["compared"] == 0
    assert report["excluded"] == 1
    assert report["pass_rate"] is None
    assert report["divergences"] == []

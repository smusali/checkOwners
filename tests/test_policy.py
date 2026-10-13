"""Tests for enforcement policy: what blocks, and what stays informational."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from checkowners.cli import app
from checkowners.config import _build_policy_config, load_config
from checkowners.models import CheckPolicy, Config, OwnershipMap, PathOwnership, PolicyConfig
from tests.conftest import git_commit, init_git_repo
from tests.test_cli import (
    _DRIFT_DETECTED,
    _EMPTY_OWNERSHIP,
    _MOCK_PATH,
    _MOCK_TOKEN,
    _NO_DRIFT,
    _NOW,
    _OWNERSHIP,
    _entry,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _pin_clock() -> None:
    with (
        patch("checkowners.cli.resolve_as_of", return_value=_NOW),
        patch("checkowners.cli.head_commit_sha", return_value="deadbeef"),
    ):
        yield


def _write_config(root: Path, content: str) -> Path:
    config_dir = root / ".github"
    config_dir.mkdir(parents=True, exist_ok=True)
    text = content
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first and not first.startswith("version:"):
        text = f"version: 1\n{text}"
    (config_dir / "checkowners.yml").write_text(text, encoding="utf-8")
    return root


def test_policy_without_a_fail_flag_stays_off(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    omitted = tmp_path / "omitted"
    blank = tmp_path / "blank"
    empty.mkdir()
    omitted.mkdir()
    blank.mkdir()
    _write_config(empty, "version: 1\npolicy: {}\n")
    _write_config(omitted, "version: 1\npolicy:\n  incomplete_analysis: {}\n")
    _write_config(blank, "version: 1\npolicy:\n  incomplete_analysis: []\n")
    assert load_config(repo_root=empty).policy.incomplete_analysis.fail is False
    assert load_config(repo_root=omitted).policy.incomplete_analysis.fail is False
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(repo_root=blank)


def test_policy_fail_rejects_non_booleans(tmp_path: Path) -> None:
    current = tmp_path / "current"
    legacy = tmp_path / "legacy"
    current.mkdir()
    legacy.mkdir()
    _write_config(current, "version: 1\npolicy:\n  incomplete_analysis:\n    fail: 1\n")
    _write_config(legacy, "policy:\n  incomplete_analysis:\n    fail: 1\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValueError, match="must be a boolean"):
            load_config(repo_root=current)
        with pytest.raises(ValueError, match="must be a boolean"):
            load_config(repo_root=legacy)


def test_unknown_policy_check_is_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "version: 1\npolicy:\n  not_a_check: {}\n")
    with pytest.raises(ValueError, match=r"policy\.not_a_check"):
        load_config(repo_root=root)


def test_policy_builder_rejects_a_non_boolean_fail_flag() -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        _build_policy_config({"incomplete_analysis": {"fail": 1}})


def test_incomplete_analysis_policy_exits_findings() -> None:
    policy = Config(
        policy=PolicyConfig(
            incomplete_analysis=CheckPolicy(fail=True, severity="warning"),
        )
    )
    with (
        patch("checkowners.cli.load_config", return_value=policy),
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        _MOCK_TOKEN,
    ):
        failed = runner.invoke(app, ["analyze"])
        cleared = runner.invoke(app, ["--exit-zero", "analyze"])
    assert failed.exit_code == 3
    assert cleared.exit_code == 0


def test_default_policy_blocks_only_the_structural_defect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    github = tmp_path / ".github"
    github.mkdir()
    (github / "CODEOWNERS").write_text("* not-an-owner\n", encoding="utf-8")
    ownership = OwnershipMap(
        paths={
            "src/app.py": PathOwnership(
                owners=(_entry("@alice", 1.0),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
        analysis_ref="deadbeef",
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=ownership),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift", "--json"])
    assert result.exit_code == 3, result.output
    data = json.loads(result.stdout)
    assert {item["rule"] for item in data["blocking"]} == {"invalid-owner"}


def test_security_paths_block_while_docs_stay_informational(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    github = tmp_path / ".github"
    github.mkdir()
    (github / "checkowners.yml").write_text(
        "version: 1\nsecurity:\n  paths:\n    - security/**\n  minimum_effective_owners: 2\n",
        encoding="utf-8",
    )
    (github / "CODEOWNERS").write_text("security/ @alice\ndocs/ @alice\n", encoding="utf-8")
    ownership = OwnershipMap(
        paths={
            "security/auth.py": PathOwnership(
                owners=(_entry("@alice", 1.0),),
                qualified_owner_count=1,
            ),
            "docs/readme.md": PathOwnership(
                owners=(_entry("@alice", 1.0),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
        analysis_ref="deadbeef",
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=ownership),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift", "--json"])
    assert result.exit_code == 3, result.output
    blocked = {item["path"] for item in json.loads(result.stdout)["blocking"]}
    assert "security/auth.py" in blocked
    assert "docs/readme.md" not in blocked


def test_reported_policy_matches_configuration_and_the_fail_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    github = tmp_path / ".github"
    github.mkdir()
    (github / "checkowners.yml").write_text(
        "\n".join(
            [
                "version: 1",
                "policy:",
                "  drift:",
                "    fail: false",
                "    severity: warning",
                "  single_expert:",
                "    severity: warning",
                "  unowned:",
                "    severity: error",
                "  invalid_owner:",
                "    fail: true",
                "security:",
                "  paths:",
                "    - auth/**",
                "  minimum_effective_owners: 2",
                "  require_declared_owner: true",
                "  require_active_expert: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (github / "CODEOWNERS").write_text("* @alice\n", encoding="utf-8")
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        _MOCK_TOKEN,
    ):
        plain = runner.invoke(app, ["drift", "--json"])
        forced = runner.invoke(
            app,
            ["github-action", "--json", "--fail-on-drift", "--no-include-decay"],
        )
    assert plain.exit_code == 0, plain.output
    assert forced.exit_code == 0, forced.output
    reported = json.loads(plain.stdout)["policy"]
    overridden = json.loads(forced.stdout)["drift_summary"]["policy"]
    assert reported["drift"] == {"fail": False, "severity": "warning"}
    assert reported["single_expert"] == {"fail": False, "severity": "warning"}
    assert reported["unowned"] == {"fail": True, "severity": "error"}
    assert reported["invalid_owner"] == {"fail": True, "severity": "error"}
    assert reported["incomplete_analysis"] == {"fail": False, "severity": "warning"}
    assert reported["security"] == {
        "paths": ["auth/**"],
        "minimum_effective_owners": 2,
        "require_declared_owner": True,
        "require_active_expert": False,
    }
    assert overridden["drift"]["fail"] is True
    assert overridden["security"]["paths"] == ["auth/**"]


def test_suppression_hides_blocking_and_keeps_the_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(
        tmp_path,
        "\n".join(
            [
                "policy:",
                "  drift:",
                "    fail: true",
                "  unowned:",
                "    fail: true",
                "suppressions:",
                "  - path: /old.py",
                "    rule: stale",
                '    reason: "accepted"',
                "  - path: /changed.py",
                "    rule: changed",
                '    reason: "accepted"',
                "  - path: /new.py",
                "    rule: missing",
                '    reason: "accepted"',
                "",
            ]
        ),
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["suppressed"] == 3
    assert data["blocking"] == []
    assert data["baselined"] == 0


def test_solo_maintainer_repository_does_not_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    github = tmp_path / ".github"
    github.mkdir()
    (github / "CODEOWNERS").write_text("* test@example.com\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "init",
        author="Solo",
        email="test@example.com",
        date="2026-01-15T00:00:00Z",
    )
    with _MOCK_TOKEN:
        result = runner.invoke(app, ["github-action", "--json"])
    assert result.exit_code == 0, result.output

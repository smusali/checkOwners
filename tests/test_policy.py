"""Tests for enforcement policy: what blocks, and what stays informational."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from checkowners.action_report import build, summarize_drift
from checkowners.cli import app
from checkowners.config import load_config
from checkowners.models import (
    CheckPolicy,
    Config,
    Finding,
    FreshnessStatus,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    PolicyConfig,
    SecurityPolicy,
)
from checkowners.patterns import CodeownersRule
from checkowners.policy import (
    blocking_findings,
    blocks,
    check_of,
    invalid_owner_findings,
    security_findings,
)
from checkowners.validate import ValidationFinding
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


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("version: 1\npolicy:\n  drift:\n    severity: critical\n", "severity"),
        ("version: 1\npolicy:\n  drift:\n    severity: 1\n", "severity"),
        ("version: 1\nsecurity:\n  paths: auth/**\n", "security.paths"),
        ("version: 1\nsecurity:\n  paths:\n    - ''\n", "security.paths"),
        ("version: 1\nsecurity:\n  paths:\n    - '  '\n", "security.paths"),
    ],
)
def test_policy_and_security_config_reject_bad_values(
    tmp_path: Path,
    content: str,
    match: str,
) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=root)


def test_null_security_paths_are_empty(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "version: 1\nsecurity:\n  paths:\n")
    assert load_config(repo_root=root).security.paths == ()


def test_security_paths_only_keep_the_strict_defaults(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "version: 1\nsecurity:\n  paths:\n    - ' auth/** '\n")
    security = load_config(repo_root=root).security
    assert security.paths == ("auth/**",)
    assert security.minimum_effective_owners == 2
    assert security.require_declared_owner is True
    assert security.require_active_expert is True


def test_invalid_owner_findings_cover_missing_files_and_tokens(tmp_path: Path) -> None:
    external = tmp_path.parent / "outside-codeowners"
    missing = ValidationFinding(line_number=0, line="", message="CODEOWNERS file not found")
    blank = ValidationFinding(line_number=2, line="* ", message="Invalid owner format:   ")
    syntax = ValidationFinding(line_number=4, line="!x", message="negation is rejected")
    skipped = invalid_owner_findings(tmp_path, external, [missing], include_missing=False)
    assert skipped == ()
    kept = invalid_owner_findings(
        tmp_path,
        external,
        [missing, blank, syntax],
        include_missing=True,
    )
    assert kept[0].path == external.as_posix()
    assert kept[0].owners == ()
    assert kept[1].owners == ()
    assert kept[1].path.endswith(":2")
    assert kept[2].owners == ("negation is rejected",)


def _fresh(handle: str, status: FreshnessStatus | None) -> OwnerEntry:
    freshness = None
    if status is not None:
        freshness = OwnershipFreshness(
            active_expertise=0.2,
            historical_expertise=0.8,
            maintenance_recency=0.2,
            status=status,
            half_life_days=90.0,
        )
    return OwnerEntry(
        handle=handle,
        ownership_score=1.0,
        last_commit=_NOW,
        commits=4,
        freshness=freshness,
    )


def test_security_rules_raise_only_matching_paths() -> None:
    inactive = _fresh("@old", "inactive")
    stable_a = _fresh("@ann", "stable")
    stable_b = _fresh("@bob", "stable")
    unscored = _fresh("@new", None)
    ownership = OwnershipMap(
        paths={
            "auth/solo.py": PathOwnership(
                owners=(_fresh("@shown", "stable"),),
                qualified_owner_count=1,
                scored_owners=(inactive,),
            ),
            "auth/pair.py": PathOwnership(
                owners=(stable_a, stable_b),
                qualified_owner_count=2,
            ),
            "auth/plain.py": PathOwnership(
                owners=(unscored, _fresh("@new2", None)),
                qualified_owner_count=2,
            ),
            "auth/empty.py": PathOwnership(owners=(), qualified_owner_count=0),
            "docs/readme.md": PathOwnership(
                owners=(_fresh("@doc", "inactive"),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
        analysis_ref="deadbeef",
    )
    config = Config(
        security=SecurityPolicy(
            paths=("auth/**",),
            minimum_effective_owners=2,
            require_declared_owner=True,
            require_active_expert=True,
        )
    )
    declared = (CodeownersRule(pattern="auth/pair.py", owners=("@ann",), line_number=1),)
    assert security_findings(ownership, Config(), declared) == ()
    found = security_findings(ownership, config, declared)
    by_path: dict[str, set[str]] = {}
    for item in found:
        by_path.setdefault(item.path, set()).add(item.rule)
    assert by_path["auth/solo.py"] == {"single-expert", "missing"}
    assert found[0].owners == ("@old",)
    assert "auth/pair.py" not in by_path
    assert by_path["auth/plain.py"] == {"missing"}
    assert by_path["auth/empty.py"] == {"single-expert", "missing"}
    assert "docs/readme.md" not in by_path
    solo = Finding(rule="single-expert", path="/auth/solo.py", owners=("@old",))
    missing_solo = Finding(rule="missing", path="auth/solo.py")
    missing_pair = Finding(rule="missing", path="auth/pair.py")
    stale = Finding(rule="stale", path="auth/solo.py")
    unknown = Finding(rule="single-expert", path="missing.py")
    vacuum = Finding(rule="knowledge-vacuum", path="auth/solo.py")
    assert blocks(solo, config, ownership, declared) is True
    assert blocks(missing_solo, config, ownership, ()) is True
    assert blocks(missing_pair, config, ownership, declared) is False
    ownerless = (CodeownersRule(pattern="auth/solo.py", owners=(), line_number=2),)
    assert blocks(missing_solo, config, ownership, ownerless) is True
    assert blocks(stale, config, ownership, declared) is False
    assert blocks(unknown, config, ownership, declared) is False
    assert blocks(solo, config, None, declared) is False
    assert check_of("knowledge-vacuum") is None
    assert blocks(vacuum, config, ownership, ()) is False
    kept = blocking_findings((solo, stale), config, ownership, declared)
    assert kept == (solo,)


def test_job_summary_names_blocking_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "drift.json").write_text(
        json.dumps(
            {
                "drift_detected": False,
                "missing": [],
                "stale": [],
                "changed": [],
                "notes": [],
                "blocking": ["skip", {"rule": 1, "path": "nope"}],
            }
        ),
        encoding="utf-8",
    )
    assert "Blocking: none" in build(limit=5)
    (tmp_path / "drift.json").write_text(
        json.dumps(
            {
                "drift_detected": True,
                "missing": [{"path": "a.py", "reason": "missing"}],
                "stale": [],
                "changed": [],
                "notes": [],
                "blocking": [{"rule": "invalid-owner", "path": ".github/CODEOWNERS:1"}],
            }
        ),
        encoding="utf-8",
    )
    assert "Blocking: invalid-owner .github/CODEOWNERS:1" in build(limit=5)
    skipped = summarize_drift(
        {
            "policy": "nope",
            "blocking": "nope",
            "missing": [],
            "stale": [],
            "changed": [],
            "notes": [],
        },
        5,
    )
    assert "policy" not in skipped
    assert "blocking" not in skipped
    copied = summarize_drift(
        {
            "policy": {"drift": {"fail": False, "severity": "warning"}},
            "blocking": [],
            "missing": [],
            "stale": [],
            "changed": [],
            "notes": [],
        },
        5,
    )
    assert copied["blocking"] == []
    assert copied["policy"]["drift"]["fail"] is False

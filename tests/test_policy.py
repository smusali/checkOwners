"""Tests for enforcement policy: what blocks, and what stays informational."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners.action_report import build, summarize_drift
from checkowners.config import (
    Config,
    SecurityPolicy,
    load_config,
)
from checkowners.models import (
    Finding,
    FreshnessStatus,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
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
from tests.conftest import NOW, Cli, Payload

runner = CliRunner()


def _write_config(root: Path, content: str) -> Path:
    config_dir = root / ".github"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "checkowners.yml").write_text(content, encoding="utf-8")
    return root


def _config(cli: Cli, content: str) -> None:
    """Write `content` as the repository's checkowners config."""
    _write_config(cli.repo.path, content)


def test_audit_rules_use_the_existing_policy_checks() -> None:
    assert check_of("owner-permission") == "invalid_owner"
    assert check_of("invisible-team") == "invalid_owner"
    assert check_of("empty-team") == "invalid_owner"
    assert check_of("file-size") == "invalid_owner"
    assert check_of("unowned-file") == "unowned"
    assert check_of("observed-unowned") == "unowned"
    assert check_of("duplicate-rule") is None


def test_policy_without_a_fail_flag_stays_off(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    omitted = tmp_path / "omitted"
    blank = tmp_path / "blank"
    empty.mkdir()
    omitted.mkdir()
    blank.mkdir()
    _write_config(empty, "policy: {}\n")
    _write_config(omitted, "policy:\n  incomplete_analysis: {}\n")
    _write_config(blank, "policy:\n  incomplete_analysis: []\n")
    assert load_config(repo_root=empty).policy.incomplete_analysis.fail is False
    assert load_config(repo_root=omitted).policy.incomplete_analysis.fail is False
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(repo_root=blank)


def test_policy_fail_rejects_non_booleans(tmp_path: Path) -> None:
    current = tmp_path / "current"
    legacy = tmp_path / "legacy"
    current.mkdir()
    legacy.mkdir()
    _write_config(current, "policy:\n  incomplete_analysis:\n    fail: 1\n")
    _write_config(legacy, "policy:\n  incomplete_analysis:\n    fail: 1\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValueError, match="must be a boolean"):
            load_config(repo_root=current)
        with pytest.raises(ValueError, match="must be a boolean"):
            load_config(repo_root=legacy)


def test_unknown_policy_check_is_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "policy:\n  not_a_check: {}\n")
    with pytest.raises(ValueError, match=r"policy\.not_a_check"):
        load_config(repo_root=root)


def test_incomplete_analysis_can_be_made_blocking(cli: Cli) -> None:
    _config(cli, "policy:\n  incomplete_analysis:\n    fail: true\n")
    failed = cli.run("analyze")
    cleared = cli.run("--exit-zero", "analyze")
    assert failed.exit_code == 3
    assert cleared.exit_code == 0


def test_the_default_policy_blocks_only_the_structural_defect(cli: Cli) -> None:
    cli.codeowners("* not-an-owner\n")
    payload = cli.json("drift")
    blocking = payload["blocking"]
    assert isinstance(blocking, list)
    assert {str(item["rule"]) for item in blocking} == {"invalid-owner"}


def test_a_security_path_blocks_while_another_path_stays_informational(cli: Cli) -> None:
    _config(
        cli,
        "security:\n  paths:\n    - src/**\n  minimum_effective_owners: 2\n",
    )
    cli.codeowners("src/ @alice\ndocs/ @alice\n")
    payload = cli.json("drift")
    blocking = payload["blocking"]
    assert isinstance(blocking, list)
    blocked = {str(item["path"]) for item in blocking}
    assert any(path.startswith("src/") for path in blocked)
    assert not any(path.startswith("docs/") for path in blocked)


def test_the_reported_policy_matches_the_configuration(cli: Cli) -> None:
    _config(
        cli,
        "\n".join(
            [
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
    )
    generated = cli.json("generate", "--force")
    cli.codeowners(str(generated["content"]))
    reported = cli.json("drift")["policy"]
    assert isinstance(reported, dict)
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


def test_fail_on_drift_overrides_the_configured_policy(cli: Cli) -> None:
    _config(cli, "policy:\n  drift:\n    fail: false\n")
    cli.codeowners("* @nobody\n")
    allowed = cli.run("github-action", "--no-fail-on-drift")
    forced = cli.run("github-action", "--fail-on-drift")
    assert allowed.exit_code == 0
    assert forced.exit_code == 3


def test_a_suppression_hides_a_blocking_finding_and_keeps_the_count(cli: Cli) -> None:
    _config(cli, "policy:\n  drift:\n    fail: true\n")
    cli.codeowners("* @nobody\n")
    detected = cli.json("drift")
    changed = detected["changed"]
    assert isinstance(changed, list)
    paths = sorted({str(item["path"]) for item in changed})
    assert paths
    assert detected["blocking"]
    rules = "\n".join(
        f'  - path: "{path}"\n    rule: changed\n    reason: "accepted"' for path in paths
    )
    _config(cli, f"policy:\n  drift:\n    fail: true\nsuppressions:\n{rules}\n")
    payload = cli.json("drift")
    assert payload["suppressed"] == len(paths)
    assert payload["blocking"] == []
    assert payload["baselined"] == 0


def test_a_solo_maintainer_repository_does_not_fail(cli: Cli) -> None:
    cli.codeowners("* @alice\n")
    result = cli.run("github-action", "--no-fail-on-drift")
    assert result.exit_code == 0


def test_null_security_paths_are_empty(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "security:\n  paths:\n")
    assert load_config(repo_root=root).security.paths == ()


def test_security_paths_only_keep_the_strict_defaults(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "security:\n  paths:\n    - ' auth/** '\n")
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
        last_commit=NOW,
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
        last_analyzed=NOW,
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
    summary = Payload(copied)
    assert summary.rows("blocking") == []
    assert summary.child("policy").child("drift").flag("fail") is False

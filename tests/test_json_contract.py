"""Validate real --json stdout and schema examples against the command contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema.exceptions import ValidationError

from checkowners.action_report import summarize_bus_factor, summarize_decay, summarize_drift
from checkowners.balance import BalanceReport
from checkowners.explain import PathExplanation
from checkowners.onboard import OnboardingPath
from checkowners.topology import TopologyReport
from tests.conftest import git_commit, init_git_repo
from tests.test_cli import (
    _GENERATED,
    _MOCK_TOKEN,
    _NO_DRIFT,
    _OWNERSHIP,
    _TREND_REPORT,
    app,
    runner,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from check_json_contract import load_schema, validate_instance  # noqa: E402

_COMMANDS: tuple[tuple[list[str], str], ...] = (
    (["analyze", "--json"], "analyze"),
    (["generate", "--json"], "generate"),
    (["print", "--json"], "print"),
    (["validate", "--json"], "validate"),
    (["explain-path", "src/main.py", "--json"], "explain-path"),
    (["baseline", "create", "--json"], "baseline-create"),
    (["drift", "--json"], "drift"),
    (["sync", "--json"], "sync"),
    (["github-action", "--json", "--no-fail-on-drift"], "github-action"),
    (["decay", "--json"], "decay"),
    (["qualified-owners", "--all", "--json"], "qualified-owners"),
    (["bus-factor", "--all", "--json"], "qualified-owners"),
    (["balance", "--json"], "balance"),
    (["topology", "--json"], "topology"),
    (["onboard", "src/", "--json"], "onboard"),
    (["explain", "src/main.py", "--json"], "explain"),
    (["owners", "src/main.py", "--json"], "owners"),
    (["who", "src/main.py", "--json"], "owners"),
    (["expertise", "src/main.py", "--json"], "expertise"),
    (["trends", "--json"], "trends"),
)


def _invoke(tmp_path: Path, args: list[str]) -> dict[str, object]:
    init_git_repo(tmp_path)
    (tmp_path / "README").write_text("fixture\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "init",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    if args[0] == "explain-path":
        (tmp_path / "CODEOWNERS").write_text("* @alice\n", encoding="utf-8")
    explanation = PathExplanation(
        target="src/main.py",
        kind="file",
        files=("src/main.py",),
        inferred=(),
        candidates=(),
        evidence_quality=1.0,
        declared=(),
        team_resolution=(),
        assessment="aligned",
        lineage=(),
        knobs=(),
        weights={},
        why_not=None,
    )
    empty_balance = BalanceReport(
        loads=(),
        average=0.0,
        overloaded=(),
        suggestions=(),
        source="git_authorship",
    )
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        patch("checkowners.cli.generate_codeowners", return_value=_GENERATED),
        patch("checkowners.cli.validate_codeowners", return_value=[]),
        patch("checkowners.cli.analyze_trends", return_value=_TREND_REPORT),
        patch("checkowners.cli.analyze_balance", return_value=empty_balance),
        patch(
            "checkowners.cli.infer_topology",
            return_value=TopologyReport(clusters=(), mismatches=()),
        ),
        patch(
            "checkowners.cli.generate_onboarding_path",
            return_value=OnboardingPath(target="src/", steps=()),
        ),
        patch("checkowners.cli.rank_expertise", return_value=()),
        patch("checkowners.cli.declared_teams_from_github", return_value={}),
        patch("checkowners.cli.build_explanation", return_value=explanation),
        patch("checkowners.cli.find_codeowners_path", return_value=tmp_path / "CODEOWNERS"),
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    loaded: object = json.loads(result.stdout)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize(("args", "command"), _COMMANDS)
def test_command_stdout_matches_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    command: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    payload = _invoke(tmp_path, args)
    validate_instance(command, payload)
    if command != "github-action":
        return
    drift = payload["drift_summary"]
    bus = payload["bus_factor_summary"]
    decay = payload["decay_summary"]
    assert isinstance(drift, dict)
    assert isinstance(bus, dict)
    assert isinstance(decay, dict)
    validate_instance("action-drift-summary", summarize_drift(drift, 50))
    validate_instance("action-bus-summary", summarize_bus_factor(bus, 50))
    validate_instance("action-decay-summary", summarize_decay(decay, 50))


def test_schema_examples_validate() -> None:
    document = load_schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    with_examples: set[str] = set()
    for name, schema in defs.items():
        if not isinstance(name, str) or not isinstance(schema, dict):
            continue
        examples = schema.get("examples")
        if not isinstance(examples, list):
            continue
        assert examples, name
        for example in examples:
            validate_instance(name, example)
        with_examples.add(name)
    needed = {command for _, command in _COMMANDS}
    assert needed <= with_examples


def test_shape_change_without_version_bump_fails() -> None:
    document = load_schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    analyze = defs["analyze"]
    assert isinstance(analyze, dict)
    examples = analyze["examples"]
    assert isinstance(examples, list)
    original = examples[0]
    assert isinstance(original, dict)
    missing = dict(original)
    del missing["schema_version"]
    with pytest.raises(ValidationError):
        validate_instance("analyze", missing)
    unknown = dict(original)
    unknown["not_in_the_contract"] = True
    with pytest.raises(ValidationError):
        validate_instance("analyze", unknown)

"""Validate real --json stdout and the schema examples against the command contract."""

from __future__ import annotations

import pytest
from jsonschema.exceptions import ValidationError

from checkowners.action_report import summarize_bus_factor, summarize_decay, summarize_drift
from tests.conftest import Cli
from tests.json_contract import load_schema, validate_instance

#: Every command that emits JSON, with the schema definition it must satisfy.
_COMMANDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("analyze",), "analyze"),
    (("generate", "--force"), "generate"),
    (("print",), "print"),
    (("validate",), "validate"),
    (("explain-path", "src/app.py"), "explain-path"),
    (("baseline", "create"), "baseline-create"),
    (("drift",), "drift"),
    (("github-action", "--no-fail-on-drift"), "github-action"),
    (("risk",), "risk"),
    (("qualified-owners", "--all"), "qualified-owners"),
    (("suggest",), "suggest"),
    (("topology",), "topology"),
    (("onboard", "src/"), "onboard"),
    (("explain", "src/app.py"), "explain"),
    (("owners", "src/app.py"), "owners"),
    (("expertise", "src/app.py"), "expertise"),
    (("trends",), "trends"),
    (("identity", "audit"), "identity-audit"),
    (("diff", "HEAD~1...HEAD"), "change"),
    (("doctor",), "doctor"),
    (("audit",), "audit"),
)


@pytest.mark.parametrize(("args", "command"), _COMMANDS)
def test_command_stdout_matches_schema(cli: Cli, args: tuple[str, ...], command: str) -> None:
    cli.codeowners("* @alice\n")
    validate_instance(command, cli.json(*args).data)


def test_sync_stdout_matches_schema(cli: Cli) -> None:
    generated = cli.json("generate", "--force")
    cli.codeowners(generated.text("content"))
    validate_instance("sync", cli.json("sync", "--dry-run").data)


def test_simulate_stdout_matches_schema(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    validate_instance("simulate", cli.json("simulate", "--remove", "@alice").data)


def test_the_action_summaries_match_their_schemas(cli: Cli) -> None:
    cli.codeowners("* @alice\n")
    payload = cli.json("github-action", "--no-fail-on-drift")
    drift = payload.child("drift_summary").data
    bus = payload.child("bus_factor_summary").data
    decay = payload.child("decay_summary").data
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


def test_unknown_payload_field_fails_the_contract() -> None:
    document = load_schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    analyze = defs["analyze"]
    assert isinstance(analyze, dict)
    examples = analyze["examples"]
    assert isinstance(examples, list)
    original = examples[0]
    assert isinstance(original, dict)
    unknown = dict(original)
    unknown["not_in_the_contract"] = True
    with pytest.raises(ValidationError):
        validate_instance("analyze", unknown)

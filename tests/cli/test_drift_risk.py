"""Contracts for the commands that report drift, risk and history."""

from __future__ import annotations

import pytest
from tests.conftest import Cli

_STALE = "* @nobody\n"


def test_drift_reports_a_stale_rule(cli: Cli) -> None:
    cli.codeowners(_STALE)
    payload = cli.json("drift")
    assert payload.flag("drift_detected") is True
    assert payload.number("max_confidence_delta") > 0
    assert payload.rows("changed") or payload.rows("missing")


def test_drift_is_quiet_when_the_file_matches_the_evidence(cli: Cli) -> None:
    generated = cli.json("generate", "--force")
    cli.codeowners(generated.text("content"))
    payload = cli.json("drift")
    assert payload.flag("drift_detected") is False
    assert payload.rows("blocking") == []


def test_drift_fix_plan_prints_edits_without_writing(cli: Cli) -> None:
    target = cli.codeowners(_STALE)
    result = cli.run("drift", "--fix-plan")
    assert result.exit_code in {0, 3}
    assert target.read_text(encoding="utf-8") == _STALE


@pytest.mark.parametrize("fmt", ["text", "json", "sarif", "junit"])
def test_drift_renders_every_documented_format(cli: Cli, fmt: str) -> None:
    cli.codeowners(_STALE)
    result = cli.run("drift", "--format", fmt)
    assert result.exit_code in {0, 3}
    assert result.stdout.strip()


def test_drift_rejects_an_unknown_format(cli: Cli) -> None:
    cli.codeowners(_STALE)
    result = cli.run("drift", "--format", "yaml")
    assert result.exit_code == 2


def test_sarif_names_the_published_sarif_release(cli: Cli) -> None:
    cli.codeowners(_STALE)
    result = cli.run("drift", "--format", "sarif")
    assert '"version": "2.1.0"' in result.stdout


def test_risk_ranks_paths_and_names_the_strong_factors(cli: Cli) -> None:
    payload = cli.json("risk")
    entries = payload.rows("entries")
    assert entries
    first = entries[0]
    assert 0 <= first.whole("risk") <= 100
    assert first.text("tier") in {"critical", "high", "medium", "low"}
    assert first.text("reason")


def test_qualified_owners_caps_the_count_it_reports(cli: Cli) -> None:
    payload = cli.json("qualified-owners", "--all")
    assert payload.whole("qualified_owner_count_cap") == 3
    assert payload.rows("entries")


def test_qualified_owners_accepts_one_path(cli: Cli) -> None:
    result = cli.run("qualified-owners", "src/app.py")
    assert result.exit_code == 0
    assert "src/app.py" in result.stdout


def test_simulate_reports_what_a_departure_costs(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    result = cli.run("simulate", "--remove", "@alice")
    assert result.exit_code == 0
    assert "Files losing their ONLY owner" in result.stdout
    assert "Repo truck factor" in result.stdout


def test_simulate_rejects_an_unknown_identity(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    result = cli.run("simulate", "--remove", "@ghost")
    assert result.exit_code == 2


def test_trends_reports_one_point_per_period(cli: Cli) -> None:
    payload = cli.json("trends", "--periods", "3", "--period-days", "30")
    assert payload.whole("periods") == 3
    assert payload.whole("period_days") == 30
    assert payload.text("metric") == "historical_activity_confidence"
    assert len(payload.rows("points")) == 3


def test_trends_can_name_the_ownership_metric_beside_its_own(cli: Cli) -> None:
    payload = cli.json("trends", "--compare-ownership")
    assert payload.child("compared_to").text("metric") == "ownership_score"
    plain = cli.json("trends")
    assert "compared_to" not in plain


def test_policy_decides_whether_drift_blocks(cli: Cli) -> None:
    cli.codeowners(_STALE)
    config = cli.repo.path / ".github" / "checkowners.yml"
    config.write_text("policy:\n  drift:\n    fail: true\n", encoding="utf-8")
    blocking = cli.run("drift")
    config.write_text("policy:\n  drift:\n    fail: false\n", encoding="utf-8")
    warning = cli.run("drift")
    assert blocking.exit_code == 3
    assert warning.exit_code == 0


def test_simulate_refuses_without_a_cached_analysis(cli: Cli) -> None:
    result = cli.run("simulate", "--remove", "@alice")
    assert result.exit_code == 2
    assert "run `checkowners analyze`" in result.output

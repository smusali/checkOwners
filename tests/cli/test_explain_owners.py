"""Contracts for the commands that explain one path or one person."""

from __future__ import annotations

import pytest
from tests.conftest import Cli


def test_owners_ranks_the_inferred_owners(cli: Cli) -> None:
    payload = cli.json("owners", "src/app.py")
    assert payload["path"] == "src/app.py"
    owners = payload["owners"]
    assert isinstance(owners, list)
    first = owners[0]
    assert isinstance(first, dict)
    assert first["identity"] == "@alice"
    assert 0 < float(first["ownership_score"]) <= 1


def test_owners_reports_an_unknown_path_as_empty(cli: Cli) -> None:
    payload = cli.json("owners", "src/missing.py")
    assert payload["owners"] == []


def test_explain_decomposes_the_score_into_signals(cli: Cli) -> None:
    payload = cli.json("explain", "src/app.py")
    assert payload["path"] == "src/app.py"
    owners = payload["owners"]
    assert isinstance(owners, list)
    signals = owners[0]
    assert isinstance(signals, dict)
    assert signals["handle"] == "@alice"
    assert set(signals["signals"]) == {
        "recency",
        "frequency",
        "blame",
        "review",
        "change_survival",
    }
    assert payload["knobs"]


def test_explain_why_not_names_the_reason(cli: Cli) -> None:
    payload = cli.json("explain", "src/app.py", "--why-not", "@bob")
    why_not = payload["why_not"]
    assert isinstance(why_not, dict)
    assert why_not["kind"] in {"below_threshold", "never_contributed", "already_inferred"}
    assert why_not["reasons"]


def test_explain_can_narrow_to_one_owner(cli: Cli) -> None:
    result = cli.run("explain", "src/app.py", "--owner", "@alice")
    assert result.exit_code == 0
    assert "@alice" in result.stdout
    assert "@bob" not in result.stdout


def test_expertise_ranks_from_the_cached_analysis(cli: Cli) -> None:
    assert cli.run("analyze").exit_code == 0
    payload = cli.json("expertise", "src/app.py")
    ranking = payload["ranking"]
    assert isinstance(ranking, list)
    assert ranking
    top = ranking[0]
    assert isinstance(top, dict)
    assert top["handle"] == "@alice"
    assert int(top["commits"]) >= 1


def test_diff_reports_the_impact_of_a_change(cli: Cli) -> None:
    payload = cli.json("diff", "HEAD~1...HEAD")
    assert payload["files"] == ["src/app.py"]
    assert payload["recommended"]
    assert payload["risks"]


def test_suggest_uses_the_authorship_proxy_when_the_api_is_off(cli: Cli) -> None:
    payload = cli.json("suggest")
    assert payload["source"] == "git_authorship"
    assert payload["load_signal"] == "git_authorship_proxy"
    assert isinstance(payload["loads"], list)


def test_suggest_can_scope_to_a_change(cli: Cli) -> None:
    result = cli.run("suggest", "--diff", "HEAD~1...HEAD")
    assert result.exit_code == 0
    assert result.stdout.strip()


@pytest.mark.parametrize("export", ["dot", "graphml", "gexf", "json"])
def test_graph_renders_every_documented_export(cli: Cli, export: str) -> None:
    result = cli.run("graph", "--export", export)
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_graph_rejects_an_unknown_export(cli: Cli) -> None:
    result = cli.run("graph", "--export", "mermaid")
    assert result.exit_code == 2
    assert "dot" in result.stdout


def test_topology_reports_clusters_without_research_framing(cli: Cli) -> None:
    payload = cli.json("topology")
    assert payload["algorithm"] == "louvain"
    assert payload["seed"] == 0
    assert "experimental" not in payload
    text = cli.run("topology")
    assert "Experimental" not in text.output
    assert "hypothesis" not in text.output.casefold()


def test_onboard_lists_steps_and_mentors(cli: Cli) -> None:
    payload = cli.json("onboard", "src/")
    assert payload["target"] == "src/"
    assert isinstance(payload["steps"], list)
    text = cli.run("onboard", "src/")
    assert "Experimental" not in text.output


def test_redact_emails_hides_addresses(cli: Cli) -> None:
    cli.repo.commit_files(
        {"src/plain.py": "x = 1\n"},
        "plain address",
        author="Carol",
        email="carol@example.com",
        date="2026-05-21T12:00:00+00:00",
    )
    exposed = cli.run("print")
    hidden = cli.run("--redact-emails", "print")
    assert "carol@example.com" in exposed.stdout
    assert "carol@example.com" not in hidden.stdout

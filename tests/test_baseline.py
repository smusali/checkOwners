"""Tests for the accepted-findings baseline and suppressions."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners.action_report import summarize_drift
from checkowners.baseline import (
    active_suppressions,
    apply_ratchet,
    findings_from_bus,
    findings_from_drift,
    load_baseline,
    suppression_matches,
    write_baseline,
)
from checkowners.cli.common import resolve_baseline
from checkowners.cli.emit import filter_bus_payload
from checkowners.cli.render import print_expired_suppressions
from checkowners.concentration import BusFactorReport
from checkowners.config import (
    BusFactorConfig,
    Config,
    DriftConfig,
    Suppression,
    load_config,
)
from checkowners.drift import detect_drift
from checkowners.models import (
    BusFactor,
    DriftEntry,
    DriftResult,
    Finding,
)
from tests.conftest import (
    DRIFT_DETECTED,
    NOW,
    Cli,
    GitRepo,
    declared_ownership,
    drift_config,
    owner_entry,
    write_codeowners,
)

runner = CliRunner()


def _write_config(root: Path, content: str) -> None:
    config_dir = root / ".github"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "checkowners.yml").write_text(content, encoding="utf-8")


_STALE = "* @nobody\n"
_BLOCKING = "policy:\n  drift:\n    fail: true\n  unowned:\n    fail: true\n"


def test_baseline_create_then_a_new_finding_blocks(cli: Cli) -> None:
    _write_config(cli.repo.path, _BLOCKING)
    cli.codeowners(_STALE)
    baseline = cli.repo.path / ".checkowners-baseline.json"
    assert cli.run("baseline", "create", "--output", str(baseline)).exit_code == 0
    created = load_baseline(baseline)
    assert created
    assert all(item.rule in {"missing", "stale", "changed", "single-expert"} for item in created)

    accepted = cli.json("drift", "--baseline", str(baseline))
    assert accepted["drift_detected"] is False
    assert accepted["changed"] == []
    assert accepted["baselined"] == len(created)
    assert accepted["suppressed"] == 0
    assert accepted["stale_baseline"] == []

    cli.codeowners("src/ @nobody\ndocs/ @nobody\n")
    blocked = cli.json("drift", "--baseline", str(baseline))
    assert blocked["drift_detected"] is True
    assert blocked["changed"]


def test_an_expired_suppression_fails(cli: Cli) -> None:
    cli.codeowners(_STALE)
    _write_config(
        cli.repo.path,
        "\n".join(
            [
                "suppressions:",
                "  - path: '*'",
                "    rule: changed",
                "    expires: 2020-01-01",
                '    reason: "Scheduled for retirement"',
                "",
            ]
        ),
    )
    result = cli.run("drift")
    assert result.exit_code == 3
    assert "Expired suppression" in result.stdout
    assert "2020-01-01" in result.stdout
    assert "Scheduled for retirement" in result.stdout


def test_finding_identity_survives_codeowners_reorder(git_repo: GitRepo) -> None:
    ownership = declared_ownership(
        {
            "src/main.py": (owner_entry("@carol"),),
            "docs/readme.md": (owner_entry("@dave"),),
        }
    )
    git_repo.commit_paths("src/main.py", "docs/readme.md")
    first = write_codeowners(git_repo.path, "src/ @alice\ndocs/ @bob\n")
    before = detect_drift(
        git_repo.path, ownership, drift_config(min_delta=0.0), codeowners_path=first
    )
    first.write_text("docs/ @bob\nsrc/ @alice\n", encoding="utf-8")
    after = detect_drift(
        git_repo.path, ownership, drift_config(min_delta=0.0), codeowners_path=first
    )
    assert {item.identity() for item in findings_from_drift(before)} == {
        item.identity() for item in findings_from_drift(after)
    }
    before_reasons = {item.path: item.reason for item in before.changed}
    after_reasons = {item.path: item.reason for item in after.changed}
    assert before_reasons != after_reasons


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "suppressions:\n  - path: legacy/**\n    rule: single-expert\n",
            "reason is required",
        ),
        (
            "suppressions:\n  - path: legacy/**\n    rule: single-expert\n    reason: '   '\n",
            "reason is required",
        ),
        (
            "suppressions:\n  - path: legacy/**\n    rule: not-a-rule\n    reason: later\n",
            "rule",
        ),
        (
            (
                "suppressions:\n  - path: legacy/**\n    rule: stale\n"
                "    expires: soon\n    reason: later\n"
            ),
            "YYYY-MM-DD",
        ),
        ("suppressions: {}\n", "must be a list"),
        ("suppressions:\n  - just-a-string\n", "expected a mapping"),
        (
            "suppressions:\n  - rule: stale\n    reason: later\n",
            "path is required",
        ),
        (
            "suppressions:\n  - path: legacy/**\n    reason: later\n",
            "rule is required",
        ),
        (
            "suppressions:\n  - path: '  '\n    rule: stale\n    reason: later\n",
            "path is required",
        ),
        (
            "suppressions:\n  - path: legacy/**\n    rule: '  '\n    reason: later\n",
            "rule is required",
        ),
    ],
)
def test_invalid_suppression_rejected_at_config_load(tmp_path: Path, body: str, match: str) -> None:
    _write_config(tmp_path, body)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=tmp_path)


def test_valid_suppression_and_baseline_file_round_trip(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "\n".join(
            [
                "drift:",
                "  baseline_file: .checkowners-baseline.json",
                "suppressions:",
                "  - path: legacy/**",
                "    rule: single-expert",
                "    expires: 2026-12-31",
                '    reason: "Scheduled for retirement in Q4"',
                "",
            ]
        ),
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.drift.baseline_file == ".checkowners-baseline.json"
    assert cfg.suppressions == (
        Suppression(
            path="legacy/**",
            rule="single-expert",
            reason="Scheduled for retirement in Q4",
            expires=date(2026, 12, 31),
        ),
    )


def test_stale_baseline_reported_after_fix() -> None:
    current = DriftResult(
        stale=(),
        missing=(DriftEntry(path="src/a.py", confidence_delta=0.4, reason="missing"),),
        changed=(),
        drift_detected=True,
    )
    baseline = (
        Finding(rule="missing", path="src/a.py"),
        Finding(rule="missing", path="src/b.py"),
    )
    outcome = apply_ratchet(
        current,
        baseline=baseline,
        suppressions=(),
        as_of=NOW.date(),
    )
    assert [item.path for item in outcome.stale_baseline] == ["src/b.py"]
    assert outcome.counts.stale_baseline == 1
    assert outcome.counts.baselined == 1
    assert outcome.drift.drift_detected is False


@pytest.mark.parametrize("by_env", [False, True], ids=["flag", "environment"])
def test_github_action_honors_a_baseline(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
    by_env: bool,
) -> None:
    cli.codeowners("* @nobody\n")
    detected = cli.json("github-action", "--no-fail-on-drift")
    drift = detected["drift_summary"]
    assert isinstance(drift, dict)
    assert drift["drift_detected"] is True
    baseline = cli.repo.path / "accepted.json"
    created = cli.json("baseline", "create", "--output", str(baseline))
    assert created["findings"]
    if by_env:
        monkeypatch.setenv("CHECKOWNERS_BASELINE", str(baseline))
        accepted = cli.json("github-action", "--fail-on-drift")
    else:
        accepted = cli.json("github-action", "--fail-on-drift", "--baseline", str(baseline))
    summary = accepted["drift_summary"]
    assert isinstance(summary, dict)
    assert summary["drift_detected"] is False
    stored = json.loads((cli.repo.path / "drift.json").read_text(encoding="utf-8"))
    assert int(stored["baselined"]) > 0
    assert stored["suppressed"] == 0
    assert stored["stale_baseline"] == []
    counts = summarize_drift(stored, 50)["counts"]
    assert isinstance(counts, dict)
    assert int(counts["baselined"]) > 0


def test_write_baseline_is_sorted_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "base.json"
    write_baseline(
        path,
        (
            Finding(rule="changed", path="b.py", owners=("@Bob", "@alice")),
            Finding(rule="changed", path="a.py", owners=("@carol",)),
        ),
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert [item["path"] for item in raw["findings"]] == ["a.py", "b.py"]
    assert raw["findings"][1]["owners"] == ["@alice", "@Bob"]


def test_load_baseline_rejects_invalid_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="not found"):
        load_baseline(missing)

    cases: tuple[tuple[str, str], ...] = (
        ("[", "Invalid baseline"),
        ("[]", "JSON object"),
        ('{"findings": {}}', "findings must be a list"),
        ('{"findings": [1]}', "expected an object"),
        ('{"findings": [{}]}', "rule is required"),
        ('{"findings": [{"rule": "", "path": "a"}]}', "rule is required"),
        (
            '{"findings": [{"rule": "nope", "path": "a"}]}',
            "unsupported rule",
        ),
        ('{"findings": [{"rule": "missing"}]}', "path is required"),
        (
            '{"findings": [{"rule": "missing", "path": ""}]}',
            "path is required",
        ),
        (
            '{"findings": [{"rule": "missing", "path": "a", "owners": 1}]}',
            "owners must be a list of strings",
        ),
        (
            '{"findings": [{"rule": "missing", "path": "a", "owners": [1]}]}',
            "owners must be a list of strings",
        ),
    )
    target = tmp_path / "base.json"
    for payload, match in cases:
        target.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            load_baseline(target)


def test_null_and_blank_expiry_suppressions_are_active(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "\n".join(
            [
                "suppressions:",
                "  - path: legacy/**",
                "    rule: stale",
                "    reason: keep",
                "  - path: other/**",
                "    rule: missing",
                "    reason: keep",
                "    expires:",
                "",
            ]
        ),
    )
    cfg = load_config(repo_root=tmp_path)
    assert [item.expires for item in cfg.suppressions] == [None, None]


def test_suppressions_null_is_empty(tmp_path: Path) -> None:
    _write_config(tmp_path, "suppressions: null\n")
    assert load_config(repo_root=tmp_path).suppressions == ()


def test_baseline_env_overrides_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, "drift:\n  baseline_file: from-yaml.json\n")
    monkeypatch.setenv("CHECKOWNERS_BASELINE", "from-env.json")
    cfg = load_config(repo_root=tmp_path)
    assert cfg.drift.baseline_file == "from-env.json"


def test_suppression_matches_rule_path_and_glob() -> None:
    finding = Finding(rule="stale", path="legacy/foo.py", owners=("@a",))
    assert not suppression_matches(
        Suppression(path="legacy/**", rule="missing", reason="x"), finding
    )
    assert suppression_matches(Suppression(path="legacy/foo.py", rule="stale", reason="x"), finding)
    assert suppression_matches(Suppression(path="legacy/**", rule="stale", reason="x"), finding)
    assert not suppression_matches(Suppression(path="other/**", rule="stale", reason="x"), finding)


def test_active_suppressions_split_by_as_of() -> None:
    items = (
        Suppression(path="a", rule="stale", reason="open"),
        Suppression(path="b", rule="stale", reason="today", expires=date(2026, 5, 28)),
        Suppression(path="c", rule="stale", reason="done", expires=date(2026, 5, 27)),
    )
    active, expired = active_suppressions(items, date(2026, 5, 28))
    assert [item.path for item in active] == ["a", "b"]
    assert [item.path for item in expired] == ["c"]


def test_apply_ratchet_suppresses_bus_and_ignores_unevaluated_stale() -> None:
    result = DriftResult(
        stale=(DriftEntry(path="legacy/old.py", confidence_delta=1.0, owners=("@a",)),),
        missing=(),
        changed=(),
        drift_detected=True,
    )
    bus = BusFactorReport(
        entries=(
            BusFactor(
                path="legacy/solo.py",
                qualified_owner_count=1,
                contributors_above_threshold=("@a",),
                recommended_backups=(),
            ),
            BusFactor(
                path="ok.py",
                qualified_owner_count=3,
                contributors_above_threshold=("@a", "@b", "@c"),
                recommended_backups=(),
            ),
        ),
        qualified_owner_count_cap=3,
        config=BusFactorConfig(),
    )
    assert [item.path for item in findings_from_bus(bus)] == ["legacy/solo.py"]
    suppressions = (
        Suppression(path="legacy/**", rule="stale", reason="later"),
        Suppression(path="legacy/**", rule="single-expert", reason="later"),
    )
    leftover = Finding(rule="single-expert", path="gone.py", owners=("@z",))
    with_bus = apply_ratchet(
        result,
        baseline=(leftover,),
        suppressions=suppressions,
        as_of=date(2026, 5, 28),
        bus=bus,
    )
    assert with_bus.drift.stale == ()
    assert with_bus.counts.suppressed == 2
    assert with_bus.hidden_bus_paths == frozenset({"legacy/solo.py"})
    assert [item.path for item in with_bus.stale_baseline] == ["gone.py"]

    without_bus = apply_ratchet(
        result,
        baseline=(leftover,),
        suppressions=(),
        as_of=date(2026, 5, 28),
    )
    assert without_bus.stale_baseline == ()
    assert without_bus.hidden_bus_paths == frozenset()


def test_baseline_create_reports_its_default_path(cli: Cli) -> None:
    payload = cli.json("baseline", "create")
    assert payload["path"] == ".checkowners-baseline.json"
    assert isinstance(payload["findings"], list)


def test_a_missing_baseline_file_exits_two(cli: Cli) -> None:
    result = cli.run("drift", "--baseline", "missing.json")
    assert result.exit_code == 2
    assert "not found" in result.stdout


def test_drift_names_a_stale_baseline_entry(cli: Cli) -> None:
    cli.codeowners(_STALE)
    baseline = cli.repo.path / "base.json"
    write_baseline(
        baseline,
        (
            *findings_from_drift(DRIFT_DETECTED),
            Finding(rule="changed", path="gone.py", owners=("@alice",)),
        ),
    )
    result = cli.run("drift", "--baseline", str(baseline))
    assert "stale baseline" in result.stdout
    assert "gone.py" in result.stdout
    assert "@alice" in result.stdout


def test_the_config_baseline_file_is_used_without_a_flag(cli: Cli) -> None:
    cli.codeowners(_STALE)
    detected = cli.json("drift")
    assert detected["drift_detected"] is True
    baseline = cli.repo.path / "accepted.json"
    assert cli.run("baseline", "create", "--output", str(baseline)).exit_code == 0
    _write_config(cli.repo.path, "drift:\n  baseline_file: accepted.json\n")
    accepted = cli.json("drift")
    assert accepted.flag("drift_detected") is False
    assert accepted.whole("baselined") > 0


def test_resolve_baseline_flag_and_config() -> None:
    empty = Config()
    assert resolve_baseline(" snap.json ", empty) == Path("snap.json")
    assert resolve_baseline("  ", empty) is None
    assert resolve_baseline(None, empty) is None
    configured = Config(drift=DriftConfig(baseline_file="from-config.json"))
    assert resolve_baseline(None, configured) == Path("from-config.json")
    assert resolve_baseline("", configured) == Path("from-config.json")


def test_filter_bus_payload_hidden_and_non_lists() -> None:
    hidden = frozenset({"gone.py"})
    assert filter_bus_payload({"entries": [], "critical_paths": []}, frozenset())["entries"] == []
    payload: dict[str, object] = {"entries": "nope", "critical_paths": "nope"}
    assert filter_bus_payload(payload, hidden) == payload
    filtered = filter_bus_payload(
        {
            "entries": [{"path": "gone.py"}, {"path": "keep.py"}, "skip"],
            "critical_paths": ["gone.py", "keep.py"],
        },
        hidden,
    )
    assert filtered["entries"] == [{"path": "keep.py"}, "skip"]
    assert filtered["critical_paths"] == ["keep.py"]


def test_print_expired_suppression_without_date() -> None:
    print_expired_suppressions((Suppression(path="legacy/**", rule="stale", reason="no date"),))

"""Tests for the accepted-findings baseline and suppressions."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from checkowners.action_report import summarize_bus_factor, summarize_drift
from checkowners.baseline import (
    active_suppressions,
    apply_ratchet,
    findings_from_bus,
    findings_from_drift,
    load_baseline,
    suppression_matches,
    write_baseline,
)
from checkowners.busfactor import BusFactorReport
from checkowners.cli import (
    _filter_bus_payload,
    _print_expired_suppressions,
    _resolve_baseline,
    app,
)
from checkowners.config import load_config
from checkowners.drift import detect_drift
from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    BusFactor,
    BusFactorConfig,
    Config,
    DriftConfig,
    DriftEntry,
    DriftResult,
    Finding,
    Suppression,
)
from tests.test_cli import (
    _DRIFT_DETECTED,
    _MOCK_PATH,
    _MOCK_TOKEN,
    _NOW,
    _OWNERSHIP,
    _run_github_action,
)
from tests.test_drift import _config, _owner, _ownership, _write_codeowners

runner = CliRunner()
_MOCK_LS_FILES = "checkowners.drift._tracked_files"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "state"))
    with (
        patch("checkowners.cli.resolve_as_of", return_value=_NOW),
        patch("checkowners.cli.head_commit_sha", return_value="deadbeef"),
    ):
        yield


def _write_config(root: Path, content: str) -> None:
    config_dir = root / ".github"
    config_dir.mkdir(parents=True, exist_ok=True)
    text = content
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first and not first.startswith("version:"):
        text = f"version: 1\n{text}"
    (config_dir / "checkowners.yml").write_text(text, encoding="utf-8")


def _cli_run(args: list[str], *, drift: DriftResult = _DRIFT_DETECTED) -> tuple[int, str]:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=drift),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, args)
    return result.exit_code, result.stdout


def test_baseline_create_then_new_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / ".checkowners-baseline.json"
    exit_code, _stdout = _cli_run(["baseline", "create", "--output", str(baseline)])
    assert exit_code == 0
    created = load_baseline(baseline)
    assert created
    assert all(item.rule in {"missing", "stale", "changed", "single-expert"} for item in created)

    exit_code, stdout = _cli_run(["drift", "--json", "--baseline", str(baseline)])
    assert exit_code == 0
    data = json.loads(stdout)
    assert data["drift_detected"] is False
    assert data["missing"] == []
    assert data["stale"] == []
    assert data["changed"] == []
    assert data["baselined"] == len(created)
    assert data["suppressed"] == 0
    assert data["stale_baseline"] == []

    extra = DriftResult(
        stale=_DRIFT_DETECTED.stale,
        missing=(
            *_DRIFT_DETECTED.missing,
            DriftEntry(path="/extra.py", confidence_delta=0.5, reason="new path"),
        ),
        changed=_DRIFT_DETECTED.changed,
        drift_detected=True,
    )
    exit_code, stdout = _cli_run(
        ["drift", "--json", "--baseline", str(baseline)],
        drift=extra,
    )
    assert exit_code == 3
    data = json.loads(stdout)
    assert data["drift_detected"] is True
    assert [item["path"] for item in data["missing"]] == ["/extra.py"]
    assert data["stale"] == []
    assert data["changed"] == []


def test_finding_identity_survives_codeowners_reorder(tmp_path: Path) -> None:
    ownership = _ownership(
        {
            "src/main.py": (_owner("@carol"),),
            "docs/readme.md": (_owner("@dave"),),
        }
    )
    first = _write_codeowners(tmp_path, "src/ @alice\ndocs/ @bob\n")
    with patch(_MOCK_LS_FILES, return_value=("src/main.py", "docs/readme.md")):
        before = detect_drift(tmp_path, ownership, _config(min_delta=0.0), codeowners_path=first)
    first.write_text("docs/ @bob\nsrc/ @alice\n", encoding="utf-8")
    with patch(_MOCK_LS_FILES, return_value=("src/main.py", "docs/readme.md")):
        after = detect_drift(tmp_path, ownership, _config(min_delta=0.0), codeowners_path=first)
    assert {item.identity() for item in findings_from_drift(before)} == {
        item.identity() for item in findings_from_drift(after)
    }
    before_reasons = {item.path: item.reason for item in before.changed}
    after_reasons = {item.path: item.reason for item in after.changed}
    assert before_reasons != after_reasons


def test_expired_suppression_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(
        tmp_path,
        "\n".join(
            [
                "suppressions:",
                "  - path: /old.py",
                "    rule: stale",
                "    expires: 2020-01-01",
                '    reason: "Scheduled for retirement"',
                "",
            ]
        ),
    )
    exit_code, stdout = _cli_run(["drift"])
    assert exit_code == 3
    assert "Expired suppression" in stdout
    assert "2020-01-01" in stdout
    assert "Scheduled for retirement" in stdout


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
        as_of=_NOW.date(),
    )
    assert [item.path for item in outcome.stale_baseline] == ["src/b.py"]
    assert outcome.counts.stale_baseline == 1
    assert outcome.counts.baselined == 1
    assert outcome.drift.drift_detected is False


def test_github_action_honors_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    findings = (
        *findings_from_drift(_DRIFT_DETECTED),
        Finding(rule="single-expert", path="src/auth.py", owners=("dave@example.com",)),
    )
    baseline = tmp_path / ".checkowners-baseline.json"
    write_baseline(baseline, findings)
    exit_code, _stdout, _output, _summary = _run_github_action(
        tmp_path,
        monkeypatch,
        ["github-action", "--baseline", str(baseline)],
        drift=_DRIFT_DETECTED,
    )
    assert exit_code == 0
    drift = json.loads((tmp_path / "drift.json").read_text(encoding="utf-8"))
    bus = json.loads((tmp_path / "bus_factor.json").read_text(encoding="utf-8"))
    assert drift["drift_detected"] is False
    assert drift["baselined"] == 4
    assert drift["suppressed"] == 0
    assert drift["stale_baseline"] == []
    summary = summarize_drift(drift, 50)
    assert summary["counts"]["baselined"] == 4
    assert summary["counts"]["suppressed"] == 0
    assert summary["counts"]["stale_baseline"] == 0
    bus_summary = summarize_bus_factor(bus, 50)
    assert bus_summary["counts"]["baselined"] == 4
    assert "src/auth.py" not in bus["critical_paths"]


def test_github_action_honors_baseline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    findings = (
        *findings_from_drift(_DRIFT_DETECTED),
        Finding(rule="single-expert", path="src/auth.py", owners=("dave@example.com",)),
    )
    baseline = tmp_path / "accepted.json"
    write_baseline(baseline, findings)
    monkeypatch.setenv("CHECKOWNERS_BASELINE", str(baseline))
    exit_code, _stdout, _output, _summary = _run_github_action(
        tmp_path,
        monkeypatch,
        ["github-action"],
        drift=_DRIFT_DETECTED,
    )
    assert exit_code == 0
    drift = json.loads((tmp_path / "drift.json").read_text(encoding="utf-8"))
    assert drift["drift_detected"] is False
    assert drift["baselined"] == 4


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
    assert raw["schema_version"] == COMMAND_SCHEMA_VERSION
    assert [item["path"] for item in raw["findings"]] == ["a.py", "b.py"]
    assert raw["findings"][1]["owners"] == ["@alice", "@Bob"]


def test_load_baseline_rejects_invalid_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="not found"):
        load_baseline(missing)

    cases: tuple[tuple[str, str], ...] = (
        ("[", "Invalid baseline"),
        ("[]", "JSON object"),
        ('{"schema_version": "0.9", "findings": []}', "schema_version"),
        ('{"schema_version": "1", "findings": {}}', "findings must be a list"),
        ('{"schema_version": "1", "findings": [1]}', "expected an object"),
        ('{"schema_version": "1", "findings": [{}]}', "rule is required"),
        ('{"schema_version": "1", "findings": [{"rule": "", "path": "a"}]}', "rule is required"),
        (
            '{"schema_version": "1", "findings": [{"rule": "nope", "path": "a"}]}',
            "unsupported rule",
        ),
        ('{"schema_version": "1", "findings": [{"rule": "missing"}]}', "path is required"),
        (
            '{"schema_version": "1", "findings": [{"rule": "missing", "path": ""}]}',
            "path is required",
        ),
        (
            '{"schema_version": "1", "findings": [{"rule": "missing", "path": "a", "owners": 1}]}',
            "owners must be a list of strings",
        ),
        (
            '{"schema_version": "1", "findings": '
            '[{"rule": "missing", "path": "a", "owners": [1]}]}',
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


def test_baseline_create_json_and_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code, stdout = _cli_run(["baseline", "create", "--json"])
    assert exit_code == 0
    data = json.loads(stdout)
    assert data["schema_version"] == COMMAND_SCHEMA_VERSION
    assert data["path"] == ".checkowners-baseline.json"
    assert isinstance(data["findings"], list)

    exit_code, stdout = _cli_run(["drift", "--baseline", "missing.json"])
    assert exit_code == 2
    assert "not found" in stdout


def test_drift_human_notes_and_stale_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "base.json"
    write_baseline(
        baseline,
        (
            *findings_from_drift(_DRIFT_DETECTED),
            Finding(rule="changed", path="gone.py", owners=("@alice",)),
        ),
    )
    noted = DriftResult(
        stale=_DRIFT_DETECTED.stale,
        missing=_DRIFT_DETECTED.missing,
        changed=_DRIFT_DETECTED.changed,
        drift_detected=True,
        notes=("handle skip",),
    )
    exit_code, stdout = _cli_run(["drift", "--baseline", str(baseline)], drift=noted)
    assert exit_code == 0
    assert "handle skip" in stdout
    assert "stale baseline" in stdout
    assert "gone.py" in stdout
    assert "@alice" in stdout


def test_config_baseline_file_is_used_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    findings = (
        *findings_from_drift(_DRIFT_DETECTED),
        Finding(rule="single-expert", path="src/auth.py", owners=("dave@example.com",)),
    )
    write_baseline(tmp_path / "accepted.json", findings)
    _write_config(tmp_path, "drift:\n  baseline_file: accepted.json\n")
    exit_code, stdout = _cli_run(["drift", "--json"])
    assert exit_code == 0
    data = json.loads(stdout)
    assert data["drift_detected"] is False
    assert data["baselined"] == 4


def test_resolve_baseline_flag_and_config() -> None:
    empty = Config()
    assert _resolve_baseline(" snap.json ", empty) == Path("snap.json")
    assert _resolve_baseline("  ", empty) is None
    assert _resolve_baseline(None, empty) is None
    configured = Config(drift=DriftConfig(baseline_file="from-config.json"))
    assert _resolve_baseline(None, configured) == Path("from-config.json")
    assert _resolve_baseline("", configured) == Path("from-config.json")


def test_filter_bus_payload_hidden_and_non_lists() -> None:
    hidden = frozenset({"gone.py"})
    assert _filter_bus_payload({"entries": [], "critical_paths": []}, frozenset())["entries"] == []
    payload = {"entries": "nope", "critical_paths": "nope"}
    assert _filter_bus_payload(payload, hidden) == payload
    filtered = _filter_bus_payload(
        {
            "entries": [{"path": "gone.py"}, {"path": "keep.py"}, "skip"],
            "critical_paths": ["gone.py", "keep.py"],
        },
        hidden,
    )
    assert filtered["entries"] == [{"path": "keep.py"}, "skip"]
    assert filtered["critical_paths"] == ["keep.py"]


def test_print_expired_suppression_without_date() -> None:
    _print_expired_suppressions((Suppression(path="legacy/**", rule="stale", reason="no date"),))

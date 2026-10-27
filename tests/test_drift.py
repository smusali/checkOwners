"""Tests for checkowners.drift module (pattern-aware comparison)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from checkowners.drift import (
    _earlier_generated,
    _signal_evidence,
    _tracked_files,
    apply_severity_hysteresis,
    compute_severity,
    detect_drift,
    drift_entry_payload,
    evidence_gaps,
    format_fix_plan,
    owner_overlap,
    reconciliation_body,
    write_github_output,
)
from checkowners.github import clear_api_evidence, note_api_call
from checkowners.models import (
    IDENTITY_COMPARISON_REASON,
    TEAM_MEMBERSHIP_REASON,
    AnalysisGapJson,
    BusFactorConfig,
    ConfidenceScore,
    Config,
    DecayWarning,
    DriftConfig,
    DriftEntry,
    DriftMode,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    SignalScore,
)
from checkowners.patterns import match_path, rules_with_source
from tests.conftest import (
    GitRepo,
    analyze_regression_repo,
    regression_analysis_config,
    script_regression_repo,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_MOCK_LS_FILES = "checkowners.drift._tracked_files"


def _owner(handle: str, confidence: float = 0.8) -> OwnerEntry:
    return OwnerEntry(handle=handle, ownership_score=confidence, last_commit=_NOW, commits=5)


def _ownership(paths: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    return OwnershipMap(
        paths={
            path: PathOwnership(owners=owners, qualified_owner_count=len(owners))
            for path, owners in paths.items()
        },
        last_analyzed=_NOW,
    )


def _config(mode: DriftMode = "both", min_delta: float = 0.0) -> Config:
    return Config(drift=DriftConfig(mode=mode, min_confidence_delta=min_delta))


def _write_codeowners(tmp_path: Path, content: str) -> Path:
    target = tmp_path / ".github" / "CODEOWNERS"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_no_codeowners_all_inferred_missing(tmp_path: Path) -> None:
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)) as tracked:
        result = detect_drift(tmp_path, ownership, _config())
    tracked.assert_called_once_with(tmp_path)
    assert [e.path for e in result.missing] == ["src/main.py"]
    assert result.missing[0].reason == "path not covered by any CODEOWNERS rule"
    assert result.missing[0].drift_type == "missing_observed_expert"
    assert result.missing[0].confidence_delta == 0.8
    assert result.missing[0].decay is False
    assert result.missing[0].observed_owners == ("@alice",)
    assert result.missing[0].qualified_owner_count == 1
    assert result.notes == ()
    assert result.drift_detected
    assert format_fix_plan(result, ownership) == "No suggested changes.\n"


def test_directory_rule_covers_inferred_files(tmp_path: Path) -> None:
    """A src/ rule must cover src/main.py: no missing entry, no false drift."""
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.missing == ()
    assert result.changed == ()
    assert result.stale == ()
    assert not result.drift_detected


def test_glob_rule_covers_inferred_files(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "*.py @alice\n")
    ownership = _ownership({"deep/nested/tool.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("deep/nested/tool.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert not result.drift_detected


def test_stale_rule_matches_no_tracked_file(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/src/ @alice\n/deleted-dir/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert [e.path for e in result.stale] == ["/deleted-dir/"]
    assert result.stale[0].confidence_delta == 1.0
    assert result.stale[0].reason == "pattern matches no tracked file (line 2)"
    assert result.stale[0].drift_type == "stale_rule"
    assert result.stale[0].owners == ("@alice",)
    assert result.stale[0].line_number == 2


def test_stale_not_reported_in_commit_mode(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/deleted-dir/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config(mode="commit"))
    assert result.stale == ()
    assert [e.path for e in result.missing] == ["src/main.py"]


def test_changed_owner_set_reported_per_rule(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @bob\n")
    ownership = _ownership(
        {
            "src/main.py": (_owner("@alice", 0.9),),
            "src/util.py": (_owner("@alice", 0.7),),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("src/main.py", "src/util.py")):
        result = detect_drift(tmp_path, ownership, _config())
    assert len(result.changed) == 1
    entry = result.changed[0]
    assert entry.path == "src/"
    assert "2 of 2 covered path(s)" in entry.reason
    assert entry.confidence_delta > 0


def test_owner_comparison_is_case_insensitive(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @Alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed == ()
    assert not result.drift_detected


def test_min_delta_suppresses_small_changes(tmp_path: Path) -> None:
    """A newly added owner with tiny confidence stays below min_delta."""
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership(
        {"src/main.py": (_owner("@alice", 0.9), _owner("@carol", 0.1))},
    )
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config(min_delta=0.5))
    assert result.changed == ()


def test_removed_owner_scores_full_delta(tmp_path: Path) -> None:
    """An owner present in CODEOWNERS but absent from inference is max-alarm."""
    _write_codeowners(tmp_path, "src/ @alice @departed\n")
    ownership = _ownership({"src/main.py": (_owner("@alice", 0.9),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert len(result.changed) == 1
    assert result.changed[0].confidence_delta == 1.0
    assert result.changed[0].line_number == 1
    assert result.changed[0].owners == ("@alice", "@departed")
    assert result.changed[0].qualified_owner_count == 1
    assert result.changed[0].reason == "owners diverge on 1 of 1 covered path(s) (line 1)"


def test_ownerless_rule_exempts_paths(tmp_path: Path) -> None:
    """GitHub's owner-less rules mean 'intentionally unowned': not missing."""
    _write_codeowners(tmp_path, "* @alice\ninternal/\n")
    ownership = _ownership({"internal/tool.py": (_owner("@bob"),)})
    with patch(_MOCK_LS_FILES, return_value=("internal/tool.py",)):
        result = detect_drift(tmp_path, ownership, _config(mode="commit"))
    assert result.missing == ()
    assert result.changed == ()


def test_identity_incomparable_emails_vs_handles(tmp_path: Path) -> None:
    """Raw-email inference vs @handle CODEOWNERS: note, not 100% false drift."""
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("alice@example.com"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed == ()
    assert result.drift_detected is False
    assert result.notes == (
        (
            "inferred owners are commit emails but CODEOWNERS uses @handles; "
            "owner comparison skipped. Set GITHUB_TOKEN (github.resolve_handles) "
            "to compare owner sets."
        ),
    )
    gaps = evidence_gaps(result)
    assert len(gaps) == 1
    assert gaps[0].code == "ambiguous_identity"
    assert gaps[0].reason == IDENTITY_COMPARISON_REASON


def test_manual_rule_shadow_is_a_review_note(tmp_path: Path) -> None:
    content = (
        "# checkowners:generated:start\n"
        "/payments/** @payments\n"
        "# checkowners:generated:end\n"
        "\n"
        "# checkowners:manual:start\n"
        "/payments/** @security-team\n"
        "# checkowners:manual:end\n"
    )
    _write_codeowners(tmp_path, content)
    ownership = _ownership({"payments/pay.py": (_owner("@payments", 0.91),)})
    with patch(_MOCK_LS_FILES, return_value=("payments/pay.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    rules = rules_with_source(content)
    generated = next(rule for rule in rules if rule.source == "generated")
    covered = [("payments/pay.py", ownership.paths["payments/pay.py"])]
    assert _earlier_generated(rules, generated, covered) is None
    winner = match_path(rules, "payments/pay.py")
    assert winner is not None
    assert winner.source == "manual"
    assert winner.owners == ("@security-team",)
    assert result.changed == ()
    assert result.drift_detected is False
    note = (
        "declared ownership without observed expertise: "
        "/payments/** @security-team wins over /payments/** @payments (line 6). "
        "Review this manual rule."
    )
    assert result.notes == (note,)
    plan = format_fix_plan(result, ownership)
    assert plan == f"{note}\n"
    assert "-/payments/**" not in plan
    edged = format_fix_plan(
        DriftResult(
            stale=(),
            missing=(),
            changed=(
                DriftEntry(
                    path="*",
                    confidence_delta=1.0,
                    owners=("@old", "@older"),
                    observed_owners=("@ghost", "@spirit"),
                ),
                DriftEntry(
                    path="/src/**",
                    confidence_delta=0.5,
                    owners=("@platform",),
                    observed_owners=("@platform", "@ghost"),
                ),
                DriftEntry(
                    path="docs/",
                    confidence_delta=1.0,
                    owners=(),
                    observed_owners=("@ghost",),
                ),
            ),
            drift_detected=True,
        ),
        ownership,
    )
    assert edged == (
        "-* @old @older\n"
        "+* @ghost @spirit\n"
        "\n"
        "Consider adding @ghost while retaining @platform. "
        "@ghost has 0.00 observed expertise and is not currently represented.\n"
        "\n"
        "-docs/\n"
        "+docs/ @ghost\n"
    )


def test_manual_rule_without_generated_shadow_is_still_a_review(tmp_path: Path) -> None:
    _write_codeowners(
        tmp_path,
        "# checkowners:manual:start\n/src/** @alice @bob\n# checkowners:manual:end\n",
    )
    warning = DecayWarning(
        handle="@alice",
        path="src/main.py",
        last_commit=_NOW,
        days_since_last_commit=400,
        historical_confidence=0.8,
    )
    ownership = OwnershipMap(
        paths={
            "src/main.py": PathOwnership(
                owners=(_owner("@alice", 0.4), _owner("@carol", 0.4)),
                qualified_owner_count=2,
                decay_warnings=(warning,),
            )
        },
        last_analyzed=_NOW,
    )
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed == ()
    review = result.reviews[0]
    assert review.path == "/src/**"
    assert review.owners == ("@alice", "@bob")
    assert review.observed_owners == ("@alice", "@carol")
    assert review.decay is True
    assert review.line_number == 2
    assert review.qualified_owner_count == 2
    assert review.drift_type == "stale_declared_owner"
    assert result.notes == (
        (
            "declared ownership without observed expertise: "
            "/src/** @alice @bob (line 2). Review this manual rule."
        ),
    )


def test_repo_mode_compares_rule_owners(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @bob\n")
    ownership = _ownership({"src/main.py": (_owner("@alice", 0.9),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config(mode="repo"))
    assert [entry.path for entry in result.changed] == ["src/"]
    assert result.missing == ()


def test_team_rules_skipped_with_note(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @org/backend-team\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed == ()
    assert result.drift_detected is False
    assert any("team" in note for note in result.notes)
    assert any(gap.code == "team_membership" for gap in evidence_gaps(result))


def test_last_matching_rule_wins(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "* @bob\nsrc/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed == ()


def test_missing_carries_bus_factor_and_decay(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "docs/ @alice\n")
    warning = DecayWarning(
        handle="@bob",
        path="src/solo.py",
        last_commit=_NOW,
        days_since_last_commit=400,
        historical_confidence=0.9,
    )
    ownership = OwnershipMap(
        paths={
            "docs/readme.md": PathOwnership(
                owners=(_owner("@alice"),),
                qualified_owner_count=1,
            ),
            "src/solo.py": PathOwnership(
                owners=(_owner("@bob", 0.9),),
                qualified_owner_count=1,
                decay_warnings=(warning,),
            ),
        },
        last_analyzed=_NOW,
    )
    with patch(_MOCK_LS_FILES, return_value=("docs/readme.md", "src/solo.py")):
        result = detect_drift(tmp_path, ownership, _config(mode="commit"))
    assert [entry.path for entry in result.missing] == ["src/solo.py"]
    assert result.missing[0].qualified_owner_count == 1
    assert result.missing[0].confidence_delta == 0.9
    assert result.missing[0].decay is True


def test_missing_sorted_by_delta(tmp_path: Path) -> None:
    ownership = _ownership(
        {
            "a/low.py": (_owner("@a", 0.4),),
            "b/high.py": (_owner("@b", 0.9),),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("a/low.py", "b/high.py")):
        result = detect_drift(tmp_path, ownership, _config(mode="commit"))
    assert [e.path for e in result.missing] == ["b/high.py", "a/low.py"]


def test_github_output_written(tmp_path: Path) -> None:
    output_file = tmp_path / "gh_output.txt"
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with (
        patch(_MOCK_LS_FILES, return_value=("src/main.py",)),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        result = detect_drift(tmp_path, ownership, _config())
        assert result.drift_detected
        assert not output_file.exists()
        write_github_output(result, 3)
    content = output_file.read_text(encoding="utf-8")
    assert content.startswith("drift_summary=")
    assert '"drift_detected": true' in content


def test_stale_rule_payload_recommends_removal(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "/deleted-dir/ @alice\n/src/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    payload = drift_entry_payload(result.stale[0], 3)
    assert payload["drift"]["type"] == "stale_rule"
    assert payload["recommendation"]["action"] == "remove_stale_rule"
    assert "suggested_team" not in payload["recommendation"]


def test_partial_overlap_with_decay_is_stale_declared_owner(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @alice @bob\n")
    warning = DecayWarning(
        handle="@alice",
        path="src/main.py",
        last_commit=_NOW,
        days_since_last_commit=400,
        historical_confidence=0.8,
    )
    ownership = OwnershipMap(
        paths={
            "src/main.py": PathOwnership(
                owners=(_owner("@alice"), _owner("@carol")),
                qualified_owner_count=2,
                decay_warnings=(warning,),
            )
        },
        last_analyzed=_NOW,
    )
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    entry = result.changed[0]
    assert entry.drift_type == "stale_declared_owner"
    payload = drift_entry_payload(entry, 3)
    assert payload["drift"]["type"] == "stale_declared_owner"
    assert payload["recommendation"]["action"] == "review_codeowners_rule"
    assert payload["decay"] is True


def test_drift_entry_suggests_resolved_team() -> None:
    entry = DriftEntry(
        path="src/",
        confidence_delta=0.4,
        reason="owners diverge",
        owners=("@alice",),
        observed_owners=("@bob",),
        observed_teams=("@org/backend", "@org/other"),
        drift_type="owner_mismatch",
        qualified_owner_count=1,
    )
    payload = drift_entry_payload(entry, 3)
    assert payload["recommendation"]["suggested_team"] == "@org/backend"
    assert payload["qualified_owner_count"] == 1


def test_tracked_files_lists_paths_and_propagates_git_failure(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(
        args=["git", "ls-files"],
        returncode=0,
        stdout="src/a.py\n\nsrc/b.py\n",
        stderr="",
    )
    with patch("checkowners.analyze.subprocess.run", return_value=completed) as run:
        assert _tracked_files(tmp_path) == ("src/a.py", "src/b.py")
    assert run.call_args.args[0] == [
        "git",
        "-c",
        "core.quotePath=false",
        "-c",
        "core.longpaths=true",
        "ls-files",
    ]
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert run.call_args.kwargs["check"] is True
    with (
        patch(
            "checkowners.analyze.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["git", "ls-files"]),
        ),
        pytest.raises(subprocess.CalledProcessError),
    ):
        _tracked_files(tmp_path)


def test_empty_ownership_and_no_codeowners(tmp_path: Path) -> None:
    ownership = _ownership({})
    with patch(_MOCK_LS_FILES, return_value=()):
        result = detect_drift(tmp_path, ownership, _config())
    assert not result.drift_detected


def _drift_with(
    *,
    delta: float = 1.0,
    qualified_owner_count: int | None = None,
    decay: bool = False,
    detected: bool = True,
) -> DriftResult:
    if not detected:
        return DriftResult(stale=(), missing=(), changed=(), drift_detected=False)
    entry = DriftEntry(
        path="/src/main.py",
        confidence_delta=delta,
        reason="test",
        qualified_owner_count=qualified_owner_count,
        decay=decay,
    )
    return DriftResult(stale=(entry,), missing=(), changed=(), drift_detected=True)


def test_compute_severity_low_medium_high_critical() -> None:
    assert compute_severity(_drift_with(delta=0.1)) == "low"
    assert compute_severity(_drift_with(delta=0.3)) == "medium"
    assert compute_severity(_drift_with(delta=0.4)) == "medium"
    assert compute_severity(_drift_with(delta=0.7)) == "high"
    assert compute_severity(_drift_with(delta=0.8)) == "high"
    assert compute_severity(_drift_with(delta=0.8, qualified_owner_count=1)) == "critical"
    assert compute_severity(_drift_with(delta=0.1, decay=True)) == "critical"


def test_compute_severity_no_drift_is_low() -> None:
    assert compute_severity(_drift_with(detected=False)) == "low"


def test_hysteresis_default_reports_raw() -> None:
    config = Config()
    reported, pending, streak = apply_severity_hysteresis("medium", 0.35, config, (None, None, 0))
    assert (reported, pending, streak) == ("medium", "medium", 1)


def test_hysteresis_holds_until_streak() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3))
    first = apply_severity_hysteresis("medium", 0.35, config, ("low", None, 0))
    assert first[0] == "low"
    assert first[1] == "medium"
    assert first[2] == 1
    second = apply_severity_hysteresis("medium", 0.35, config, first)
    assert second[0] == "low"
    assert second[2] == 2
    third = apply_severity_hysteresis("medium", 0.35, config, second)
    assert third[0] == "medium"
    assert third[2] == 3


def test_hysteresis_margin_flips_immediately() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3, min_confidence_delta=0.2))
    reported, pending, streak = apply_severity_hysteresis("high", 0.4, config, ("low", None, 0))
    assert (reported, pending, streak) == ("high", "high", 1)
    held = apply_severity_hysteresis(
        "high",
        0.2,
        Config(drift=DriftConfig(hysteresis_runs=2, min_confidence_delta=0.2)),
        ("low", None, 0),
    )
    assert held[0] == "low"
    immediate = apply_severity_hysteresis(
        "high",
        0.1,
        Config(drift=DriftConfig(hysteresis_runs=1, min_confidence_delta=0.2)),
        ("low", "high", 2),
    )
    assert immediate == ("high", "high", 1)


def test_hysteresis_same_as_reported_resets() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3))
    reported, pending, streak = apply_severity_hysteresis("low", 0.1, config, ("low", "medium", 2))
    assert (reported, pending, streak) == ("low", "low", 1)


def test_hysteresis_pending_resets_when_severity_changes() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3))
    reported, pending, streak = apply_severity_hysteresis(
        "high", 0.35, config, ("low", "medium", 2)
    )
    assert (reported, pending, streak) == ("low", "high", 1)


def test_compute_severity_uses_configured_critical_threshold() -> None:
    config = Config(bus_factor=BusFactorConfig(critical_threshold=2, warn_threshold=3))
    assert compute_severity(_drift_with(delta=0.1, qualified_owner_count=2), config) == "critical"
    assert compute_severity(_drift_with(delta=0.1, qualified_owner_count=2)) == "low"
    assert compute_severity(_drift_with(delta=0.1, qualified_owner_count=1)) == "critical"


def _canonical_analysis(ownership: OwnershipMap) -> str:
    payload = {
        path: [
            {
                "commits": owner.commits,
                "handle": owner.handle,
                "score": owner.ownership_score,
            }
            for owner in path_ownership.owners
        ]
        for path, path_ownership in ownership.paths.items()
    }
    return json.dumps(payload, sort_keys=True)


@pytest.mark.integration
def test_directory_rule_covers_files_from_real_git(git_repo: GitRepo) -> None:
    script_regression_repo(git_repo)
    ownership = analyze_regression_repo(git_repo)
    codeowners = git_repo.path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir(parents=True)
    codeowners.write_text("src/ @alice\n/gone/ @alice\n", encoding="utf-8")
    result = detect_drift(
        git_repo.path,
        ownership,
        Config(analysis=regression_analysis_config().analysis, drift=DriftConfig(mode="both")),
    )
    assert "src/app.py" in ownership.paths
    assert "notes/ünicode.py" in ownership.paths
    assert "docs/getting started.md" in ownership.paths
    assert "Dir/File.py" in ownership.paths
    assert any(path.startswith("deep/") for path in ownership.paths)
    assert "src/app.py" not in {entry.path for entry in result.missing}
    assert "/gone/" in {entry.path for entry in result.stale}


@pytest.mark.integration
def test_scripted_repo_analysis_is_deterministic(tmp_path: Path) -> None:
    first = GitRepo.create(tmp_path / "first")
    second = GitRepo.create(tmp_path / "second")
    script_regression_repo(first)
    script_regression_repo(second)
    assert first.head_sha() == second.head_sha()
    assert _canonical_analysis(analyze_regression_repo(first)) == _canonical_analysis(
        analyze_regression_repo(second)
    )


def test_reconciliation_body_states_evidence_when_owners_agree() -> None:
    body = reconciliation_body(
        DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        _ownership({}),
        Config(),
    )
    assert body == (
        "<!-- checkowners-reconcile -->\n"
        "\n"
        "Inferred ownership is evidence, not accountability.\n"
        "\n"
        "Severity: low\n"
        "\n"
        "The generated file changed without an owner-set disagreement.\n"
    )


def test_reconciliation_body_includes_scores_severity_and_direction() -> None:
    assert _signal_evidence(_owner("@ghost")) == ""
    partial = ConfidenceScore(
        total=0.0,
        recency=SignalScore(available=False),
        frequency=SignalScore(available=True, score=0.7),
        blame=SignalScore(available=False),
        review=SignalScore(available=True, score=0.2),
    )
    assert (
        _signal_evidence(
            OwnerEntry(
                handle="@ghost",
                ownership_score=0.0,
                last_commit=None,
                commits=0,
                score_breakdown=partial,
            )
        )
        == "frequency 0.70, review 0.20"
    )
    breakdown = ConfidenceScore(
        total=0.9,
        recency=SignalScore(available=True, score=0.8),
        frequency=SignalScore(available=True, score=0.7),
        blame=SignalScore(available=True, score=0.1),
        review=SignalScore(available=True, score=0.2),
    )
    ownership = _ownership(
        {
            "src/a.py": (
                _owner("@bob", 0.5),
                _owner("@alice", 0.4),
                OwnerEntry(
                    handle="@alice",
                    ownership_score=0.9,
                    last_commit=_NOW,
                    commits=8,
                    evidence_quality=0.95,
                    score_breakdown=breakdown,
                ),
                OwnerEntry(
                    handle="@alice",
                    ownership_score=0.9,
                    last_commit=_NOW,
                    commits=1,
                    evidence_quality=0.1,
                ),
                _owner("@alice", 0.2),
            )
        }
    )
    result = DriftResult(
        stale=(
            DriftEntry(
                path="/old/",
                confidence_delta=0.1,
                reason="matches nothing",
                qualified_owner_count=2,
                drift_type="stale_rule",
            ),
        ),
        missing=(
            DriftEntry(
                path="src/a.py",
                confidence_delta=0.9,
                reason="no declared owner",
                observed_owners=("@ghost", "@alice"),
                drift_type="missing_observed_expert",
            ),
        ),
        changed=(
            DriftEntry(
                path="/src/",
                confidence_delta=0.5,
                reason="owners diverge",
                owners=("@old", "@older"),
                observed_owners=("@alice",),
                drift_type="organizational_mismatch",
            ),
        ),
        drift_detected=True,
    )
    body = reconciliation_body(
        result,
        ownership,
        Config(bus_factor=BusFactorConfig(critical_threshold=5)),
    )
    observed = (
        "Observed: @alice ownership score 0.90, evidence quality 0.95, "
        "commits 8, last commit 2026-05-28T12:00:00+00:00\n"
        "Evidence: recency 0.80, frequency 0.70, blame 0.10, review 0.20"
    )
    assert body == (
        "<!-- checkowners-reconcile -->\n"
        "\n"
        "Inferred ownership is evidence, not accountability.\n"
        "\n"
        "Severity: critical\n"
        "\n"
        "## /old/\n"
        "\n"
        "Direction: stale rule\n"
        "Severity: critical\n"
        "Why: matches nothing\n"
        "\n"
        "## src/a.py\n"
        "\n"
        "Direction: missing observed expert\n"
        "Severity: high\n"
        "Why: no declared owner\n"
        "Observed: @ghost\n"
        f"{observed}\n"
        "\n"
        "## /src/\n"
        "\n"
        "Direction: organizational mismatch\n"
        "Severity: medium\n"
        "Why: owners diverge\n"
        "Declared: @old @older\n"
        f"{observed}\n"
    )


def test_owner_overlap_rounds_the_jaccard_ratio() -> None:
    assert owner_overlap((), ()) == 0.0
    assert owner_overlap(("@A",), ("@a", "@b", "@c")) == 0.3333
    assert owner_overlap(("@a", "@b"), ("@b",)) == 0.5


def test_disjoint_owners_are_a_complete_replacement(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@bob", 0.4),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed[0].drift_type == "complete_ownership_replacement"
    assert result.changed[0].confidence_delta == 1.0


def test_added_owner_keeps_its_confidence_delta(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice", 0.4), _owner("@bob", 0.5))})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config(min_delta=0.5))
    assert len(result.changed) == 1
    assert result.changed[0].confidence_delta == 0.5
    assert result.changed[0].drift_type == "owner_mismatch"


def test_two_diverging_files_count_both(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership(
        {
            "src/a.py": (_owner("@alice", 0.3), _owner("@bob", 0.2)),
            "src/b.py": (_owner("@alice", 0.3), _owner("@carol", 0.6)),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("src/a.py", "src/b.py")):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed[0].reason == "owners diverge on 2 of 2 covered path(s) (line 1)"
    assert result.changed[0].qualified_owner_count == 2
    assert result.changed[0].observed_owners == ("@alice", "@carol")


def test_a_matching_file_does_not_hide_a_later_mismatch(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership(
        {
            "src/ok.py": (_owner("@alice"),),
            "src/bad.py": (_owner("@bob", 0.4),),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("src/ok.py", "src/bad.py")):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed[0].observed_owners == ("@bob",)


def test_equal_deltas_keep_the_first_file(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership(
        {
            "src/a.py": (_owner("@alice", 0.3), _owner("@bob", 0.4)),
            "src/b.py": (_owner("@alice", 0.3), _owner("@carol", 0.4)),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("src/a.py", "src/b.py")):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed[0].observed_owners == ("@alice", "@bob")


def test_zero_confidence_addition_keeps_the_initial_measurement(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice", 0.5), _owner("@zero", 0.0))})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    entry = result.changed[0]
    assert entry.decay is False
    assert entry.observed_owners == ()
    assert entry.qualified_owner_count is None


def test_an_agreeing_rule_does_not_hide_a_later_mismatch(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "docs/ @alice\nsrc/ @bob\n")
    ownership = _ownership(
        {
            "docs/a.py": (_owner("@alice"),),
            "src/a.py": (_owner("@carol"),),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("docs/a.py", "src/a.py")):
        result = detect_drift(tmp_path, ownership, _config())
    assert [entry.path for entry in result.changed] == ["src/"]
    assert result.changed[0].owners == ("@bob",)


def test_a_manual_rule_does_not_hide_a_later_generated_mismatch(tmp_path: Path) -> None:
    _write_codeowners(
        tmp_path,
        "# checkowners:generated:start\n"
        "lib/ @bob\n"
        "# checkowners:generated:end\n"
        "# checkowners:manual:start\n"
        "docs/ @security\n"
        "# checkowners:manual:end\n",
    )
    ownership = _ownership(
        {
            "docs/a.py": (_owner("@alice"),),
            "lib/a.py": (_owner("@alice"),),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("docs/a.py", "lib/a.py")):
        result = detect_drift(tmp_path, ownership, _config())
    assert [entry.path for entry in result.changed] == ["lib/"]
    assert result.reviews[0].path == "docs/"


def test_leading_slash_and_literal_x_still_match_rules(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/* @alice\nXsrc/main.py @bob\n")
    ownership = _ownership(
        {
            "/src/main.py": (_owner("@alice"),),
            "Xsrc/main.py": (_owner("@bob"),),
            " src/spaced.py": (_owner("@alice"),),
            "src/a.py/": (_owner("@alice"),),
        }
    )
    with patch(
        _MOCK_LS_FILES,
        return_value=("src/main.py", "Xsrc/main.py", " src/spaced.py", "src/a.py/"),
    ):
        result = detect_drift(tmp_path, ownership, _config())
    assert [entry.path for entry in result.missing] == [" src/spaced.py", "src/a.py/"]
    assert result.changed == ()


def test_unowned_inference_has_no_confidence_delta(tmp_path: Path) -> None:
    ownership = _ownership({"src/empty.py": ()})
    with patch(_MOCK_LS_FILES, return_value=("src/empty.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.missing[0].confidence_delta == 0.0
    assert result.notes == ()


def test_email_inference_without_rules_is_not_an_identity_gap(tmp_path: Path) -> None:
    ownership = _ownership({"src/main.py": (_owner("ada@example.com"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.notes == ()
    assert evidence_gaps(result) == ()


def test_team_rules_record_the_membership_gap(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @acme/backend\nlib/ @bob\n")
    ownership = _ownership(
        {
            "src/main.py": (_owner("@alice"),),
            "lib/a.py": (_owner("@alice"),),
        }
    )
    with patch(_MOCK_LS_FILES, return_value=("src/main.py", "lib/a.py")):
        result = detect_drift(tmp_path, ownership, _config())
    assert [entry.path for entry in result.changed] == ["lib/"]
    assert result.changed[0].owners == ("@bob",)
    assert result.notes == (
        "rules owned by teams (@org/team) are not compared against inferred individuals.",
    )
    gaps = evidence_gaps(result)
    assert len(gaps) == 1
    assert gaps[0].code == "team_membership"
    assert gaps[0].reason == TEAM_MEMBERSHIP_REASON


def test_fix_plan_names_a_retained_owner_score() -> None:
    entry = DriftEntry(
        path="src/a.py",
        confidence_delta=0.5,
        owners=("@alice", "@carol"),
        observed_owners=("@alice", "@carol", "@bob"),
    )
    plan = format_fix_plan(
        DriftResult(stale=(), missing=(), changed=(entry,), drift_detected=True),
        _ownership({"src/a.py": (_owner("@bob", 0.42),)}),
    )
    assert plan == (
        "Consider adding @bob while retaining @alice @carol. "
        "@bob has 0.42 observed expertise and is not currently represented.\n"
    )


def test_drift_entry_payload_uses_stable_keys() -> None:
    entry = DriftEntry(
        path="src/",
        confidence_delta=1 / 3,
        reason="owners diverge",
        qualified_owner_count=2,
        owners=("@alice", "@bob"),
        observed_owners=("@carol",),
        observed_teams=("@acme/web",),
        drift_type="owner_mismatch",
    )
    payload = drift_entry_payload(
        entry,
        3,
        Config(bus_factor=BusFactorConfig(critical_threshold=2)),
    )
    assert payload == {
        "path": "src/",
        "declared": {"owners": ["@alice", "@bob"]},
        "observed": {"owners": ["@carol"], "teams": ["@acme/web"]},
        "drift": {
            "severity": "critical",
            "type": "owner_mismatch",
            "declared_observed_overlap": 0.0,
        },
        "recommendation": {
            "action": "review_codeowners_rule",
            "suggested_team": "@acme/web",
        },
        "confidence_delta": round(1 / 3, 4),
        "reason": "owners diverge",
        "qualified_owner_count": 2,
        "qualified_owner_count_cap": 3,
    }


def test_github_output_writes_the_sorted_summary(tmp_path: Path) -> None:
    clear_api_evidence()
    output_file = tmp_path / "gh_output.txt"
    result = DriftResult(
        stale=(
            DriftEntry(
                path="/old/",
                confidence_delta=0.2,
                reason="gone",
                owners=("@alice",),
                qualified_owner_count=2,
                drift_type="stale_rule",
            ),
        ),
        missing=(
            DriftEntry(
                path="src/a.py",
                confidence_delta=0.4,
                reason="absent",
                observed_owners=("@bob",),
                observed_teams=("@acme/backend",),
                qualified_owner_count=2,
                drift_type="missing_observed_expert",
            ),
        ),
        changed=(
            DriftEntry(
                path="src/",
                confidence_delta=0.6,
                reason="diverged",
                owners=("@carol",),
                observed_owners=("@dave",),
                qualified_owner_count=2,
                drift_type="owner_mismatch",
            ),
        ),
        drift_detected=True,
        notes=("watch the emails",),
    )
    config = Config(bus_factor=BusFactorConfig(critical_threshold=2))
    gaps: list[AnalysisGapJson] = [{"code": "team_membership", "reason": "because"}]
    try:
        with patch.dict(
            os.environ,
            {
                "GITHUB_OUTPUT": str(output_file),
                "GITHUB_REPOSITORY": "acme/widgets",
                "SOURCE_DATE_EPOCH": "1700000000",
            },
        ):
            note_api_call()
            write_github_output(
                result,
                3,
                analysis_ref="abc123",
                analysis_epoch="2020-01-02T00:00:00+00:00",
                config=config,
                analysis_completeness=0.25,
                analysis_gaps=gaps,
            )
            content = output_file.read_text(encoding="utf-8")
            payload = json.loads(content.removeprefix("drift_summary=").strip())
            assert content.startswith("drift_summary=")
            assert content.endswith("\n")
            assert list(payload) == sorted(payload)
            assert payload["drift_detected"] is True
            assert payload["max_confidence_delta"] == 0.6
            assert payload["notes"] == ["watch the emails"]
            assert payload["stale"][0]["path"] == "/old/"
            assert payload["stale"][0]["qualified_owner_count_cap"] == 3
            assert payload["stale"][0]["drift"]["severity"] == "critical"
            assert payload["changed"][0]["qualified_owner_count_cap"] == 3
            assert payload["changed"][0]["drift"]["severity"] == "critical"
            assert payload["missing"][0]["path"] == "src/a.py"
            assert payload["missing"][0]["qualified_owner_count_cap"] == 3
            assert payload["missing"][0]["drift"]["severity"] == "critical"
            assert payload["changed"][0]["path"] == "src/"
            assert payload["head_sha"] == "abc123"
            assert payload["generated_at"] == "2020-01-02T00:00:00+00:00"
            assert payload["analysis_completeness"] == 0.25
            assert payload["repository"] == "acme/widgets"
            assert payload["analysis_gaps"] == gaps
            assert payload["repository_head"] == "abc123"
            assert (
                payload["github_evidence_collected_at"]
                == datetime.fromtimestamp(1700000000, tz=UTC).isoformat()
            )
            output_file.unlink()
            write_github_output(result, 3)
            omitted = json.loads(
                output_file.read_text(encoding="utf-8").removeprefix("drift_summary=")
            )
            assert omitted["head_sha"] == ""
            assert omitted["generated_at"] == ""
    finally:
        clear_api_evidence()

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
    _tracked_files,
    apply_severity_hysteresis,
    compute_severity,
    detect_drift,
    drift_entry_payload,
    evidence_gaps,
    write_github_output,
)
from checkowners.models import (
    BusFactorConfig,
    Config,
    DecayWarning,
    DriftConfig,
    DriftEntry,
    DriftMode,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
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
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert [e.path for e in result.missing] == ["src/main.py"]
    assert result.drift_detected


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
    _write_codeowners(tmp_path, "/deleted-dir/ @alice\n/src/ @alice\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert [e.path for e in result.stale] == ["/deleted-dir/"]
    assert result.stale[0].confidence_delta == 1.0
    assert "line 1" in result.stale[0].reason


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
    assert any("commit emails" in note for note in result.notes)
    assert any(gap.code == "ambiguous_identity" for gap in evidence_gaps(result))


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
    ownership = _ownership({"src/solo.py": (_owner("@bob", 0.9),)})
    with patch(_MOCK_LS_FILES, return_value=("src/solo.py", "docs/readme.md")):
        result = detect_drift(tmp_path, ownership, _config(mode="commit"))
    assert len(result.missing) == 1
    assert result.missing[0].qualified_owner_count == 1
    assert result.missing[0].confidence_delta == 0.9


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
    assert content.startswith("checkowners_drift=")
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
    with patch("checkowners.drift.subprocess.run", return_value=completed) as run:
        assert _tracked_files(tmp_path) == ("src/a.py", "src/b.py")
    assert run.call_args.args[0] == ["git", "ls-files"]
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert run.call_args.kwargs["check"] is True
    with (
        patch(
            "checkowners.drift.subprocess.run",
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
    assert compute_severity(_drift_with(delta=0.4)) == "medium"
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
    reported, pending, streak = apply_severity_hysteresis("high", 0.45, config, ("low", None, 0))
    assert (reported, pending, streak) == ("high", "high", 1)


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

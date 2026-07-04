"""Tests for checkowners.drift module (pattern-aware comparison)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from checkowners.drift import detect_drift
from checkowners.models import (
    Config,
    DriftConfig,
    DriftMode,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_MOCK_LS_FILES = "checkowners.drift._tracked_files"


def _owner(handle: str, confidence: float = 0.8) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=_NOW, commits=5)


def _ownership(paths: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    return OwnershipMap(
        paths={
            path: PathOwnership(owners=owners, bus_factor=len(owners))
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
    assert any("commit emails" in note for note in result.notes)


def test_team_rules_skipped_with_note(tmp_path: Path) -> None:
    _write_codeowners(tmp_path, "src/ @org/backend-team\n")
    ownership = _ownership({"src/main.py": (_owner("@alice"),)})
    with patch(_MOCK_LS_FILES, return_value=("src/main.py",)):
        result = detect_drift(tmp_path, ownership, _config())
    assert result.changed == ()
    assert any("team" in note for note in result.notes)


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
    assert result.missing[0].bus_factor == 1
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
    content = output_file.read_text(encoding="utf-8")
    assert content.startswith("checkowners_drift=")
    assert '"drift_detected": true' in content


def test_empty_ownership_and_no_codeowners(tmp_path: Path) -> None:
    ownership = _ownership({})
    with patch(_MOCK_LS_FILES, return_value=()):
        result = detect_drift(tmp_path, ownership, _config())
    assert not result.drift_detected

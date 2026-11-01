"""Tests for checkowners.drift module (pattern-aware comparison)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from checkowners.config import (
    Config,
    DriftConfig,
)
from checkowners.drift import (
    detect_drift,
    drift_entry_payload,
    evidence_gaps,
    format_fix_plan,
    write_github_output,
)
from checkowners.models import (
    IDENTITY_COMPARISON_REASON,
    DecayWarning,
    DriftResult,
    OwnershipMap,
    PathOwnership,
)
from tests.conftest import (
    NOW,
    GitRepo,
    analyze_regression_repo,
    declared_ownership,
    drift_config,
    owner_entry,
    regression_analysis_config,
    script_regression_repo,
    write_codeowners,
)


def _compare(
    git_repo: GitRepo,
    ownership: OwnershipMap,
    *tracked: str,
    codeowners: str | None = None,
    config: Config | None = None,
) -> DriftResult:
    git_repo.commit_paths(*tracked)
    if codeowners is not None:
        write_codeowners(git_repo.path, codeowners)
    return detect_drift(git_repo.path, ownership, config or drift_config())


def test_no_codeowners_all_inferred_missing(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    result = _compare(git_repo, ownership, "src/main.py")
    assert [entry.path for entry in result.missing] == ["src/main.py"]
    assert result.missing[0].reason == "path not covered by any CODEOWNERS rule"
    assert result.missing[0].drift_type == "missing_observed_expert"
    assert result.missing[0].confidence_delta == 0.8
    assert result.missing[0].decay is False
    assert result.missing[0].observed_owners == ("@alice",)
    assert result.missing[0].qualified_owner_count == 1
    assert result.notes == ()
    assert result.drift_detected
    assert format_fix_plan(result, ownership) == "No suggested changes.\n"


def test_directory_rule_covers_inferred_files(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    result = _compare(git_repo, ownership, "src/main.py", codeowners="src/ @alice\n")
    assert result.missing == ()
    assert result.changed == ()
    assert result.stale == ()
    assert not result.drift_detected


def test_glob_rule_covers_inferred_files(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"deep/nested/tool.py": (owner_entry("@alice"),)})
    result = _compare(git_repo, ownership, "deep/nested/tool.py", codeowners="*.py @alice\n")
    assert not result.drift_detected


def test_stale_rule_matches_no_tracked_file(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    result = _compare(
        git_repo,
        ownership,
        "src/main.py",
        codeowners="/src/ @alice\n/deleted-dir/ @alice\n",
    )
    assert [entry.path for entry in result.stale] == ["/deleted-dir/"]
    assert result.stale[0].confidence_delta == 1.0
    assert result.stale[0].reason == "pattern matches no tracked file (line 2)"
    assert result.stale[0].drift_type == "stale_rule"
    assert result.stale[0].owners == ("@alice",)
    assert result.stale[0].line_number == 2


def test_stale_not_reported_in_commit_mode(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    result = _compare(
        git_repo,
        ownership,
        "src/main.py",
        codeowners="/deleted-dir/ @alice\n",
        config=drift_config(mode="commit"),
    )
    assert result.stale == ()
    assert [entry.path for entry in result.missing] == ["src/main.py"]


def test_changed_owner_set_reported_per_rule(git_repo: GitRepo) -> None:
    ownership = declared_ownership(
        {
            "src/main.py": (owner_entry("@alice", 0.9),),
            "src/util.py": (owner_entry("@alice", 0.7),),
        }
    )
    result = _compare(git_repo, ownership, "src/main.py", "src/util.py", codeowners="src/ @bob\n")
    assert len(result.changed) == 1
    entry = result.changed[0]
    assert entry.path == "src/"
    assert "2 of 2 covered path(s)" in entry.reason
    assert entry.confidence_delta > 0


def test_owner_comparison_is_case_insensitive(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    result = _compare(git_repo, ownership, "src/main.py", codeowners="src/ @Alice\n")
    assert result.changed == ()
    assert not result.drift_detected


def test_min_delta_suppresses_small_changes(git_repo: GitRepo) -> None:
    ownership = declared_ownership(
        {"src/main.py": (owner_entry("@alice", 0.9), owner_entry("@carol", 0.1))},
    )
    result = _compare(
        git_repo,
        ownership,
        "src/main.py",
        codeowners="src/ @alice\n",
        config=drift_config(min_delta=0.5),
    )
    assert result.changed == ()


def test_removed_owner_scores_full_delta(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice", 0.9),)})
    result = _compare(git_repo, ownership, "src/main.py", codeowners="src/ @alice @departed\n")
    assert len(result.changed) == 1
    assert result.changed[0].confidence_delta == 1.0
    assert result.changed[0].line_number == 1
    assert result.changed[0].owners == ("@alice", "@departed")
    assert result.changed[0].qualified_owner_count == 1
    assert result.changed[0].reason == "owners diverge on 1 of 1 covered path(s) (line 1)"


def test_ownerless_rule_exempts_paths(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"internal/tool.py": (owner_entry("@bob"),)})
    result = _compare(
        git_repo,
        ownership,
        "internal/tool.py",
        codeowners="* @alice\ninternal/\n",
        config=drift_config(mode="commit"),
    )
    assert result.missing == ()
    assert result.changed == ()


def test_identity_incomparable_emails_vs_handles(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("alice@example.com"),)})
    result = _compare(git_repo, ownership, "src/main.py", codeowners="src/ @alice\n")
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


def test_repo_mode_compares_rule_owners(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice", 0.9),)})
    result = _compare(
        git_repo,
        ownership,
        "src/main.py",
        codeowners="src/ @bob\n",
        config=drift_config(mode="repo"),
    )
    assert [entry.path for entry in result.changed] == ["src/"]
    assert result.missing == ()


def test_last_matching_rule_wins(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    result = _compare(git_repo, ownership, "src/main.py", codeowners="* @bob\nsrc/ @alice\n")
    assert result.changed == ()


def test_missing_carries_bus_factor_and_decay(git_repo: GitRepo) -> None:
    warning = DecayWarning(
        handle="@bob",
        path="src/solo.py",
        last_commit=NOW,
        days_since_last_commit=400,
        historical_confidence=0.9,
    )
    ownership = OwnershipMap(
        paths={
            "docs/readme.md": PathOwnership(
                owners=(owner_entry("@alice"),),
                qualified_owner_count=1,
            ),
            "src/solo.py": PathOwnership(
                owners=(owner_entry("@bob", 0.9),),
                qualified_owner_count=1,
                decay_warnings=(warning,),
            ),
        },
        last_analyzed=NOW,
    )
    result = _compare(
        git_repo,
        ownership,
        "docs/readme.md",
        "src/solo.py",
        codeowners="docs/ @alice\n",
        config=drift_config(mode="commit"),
    )
    assert [entry.path for entry in result.missing] == ["src/solo.py"]
    assert result.missing[0].qualified_owner_count == 1
    assert result.missing[0].confidence_delta == 0.9
    assert result.missing[0].decay is True


def test_missing_sorted_by_delta(git_repo: GitRepo) -> None:
    ownership = declared_ownership(
        {
            "a/low.py": (owner_entry("@a", 0.4),),
            "b/high.py": (owner_entry("@b", 0.9),),
        }
    )
    result = _compare(
        git_repo, ownership, "a/low.py", "b/high.py", config=drift_config(mode="commit")
    )
    assert [entry.path for entry in result.missing] == ["b/high.py", "a/low.py"]


def test_github_output_written(
    git_repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "gh_output.txt"
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    git_repo.commit_paths("src/main.py")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.drift_detected
    assert not output_file.exists()
    write_github_output(result, 3)
    content = output_file.read_text(encoding="utf-8")
    assert content.startswith("drift_summary=")
    assert '"drift_detected": true' in content


def test_stale_rule_payload_recommends_removal(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    result = _compare(
        git_repo, ownership, "src/main.py", codeowners="/deleted-dir/ @alice\n/src/ @alice\n"
    )
    payload = drift_entry_payload(result.stale[0], 3)
    assert payload["drift"]["type"] == "stale_rule"
    assert payload["recommendation"]["action"] == "remove_stale_rule"
    assert "suggested_team" not in payload["recommendation"]


def test_partial_overlap_with_decay_is_stale_declared_owner(git_repo: GitRepo) -> None:
    warning = DecayWarning(
        handle="@alice",
        path="src/main.py",
        last_commit=NOW,
        days_since_last_commit=400,
        historical_confidence=0.8,
    )
    ownership = OwnershipMap(
        paths={
            "src/main.py": PathOwnership(
                owners=(owner_entry("@alice"), owner_entry("@carol")),
                qualified_owner_count=2,
                decay_warnings=(warning,),
            )
        },
        last_analyzed=NOW,
    )
    result = _compare(git_repo, ownership, "src/main.py", codeowners="src/ @alice @bob\n")
    entry = result.changed[0]
    assert entry.drift_type == "stale_declared_owner"
    payload = drift_entry_payload(entry, 3)
    assert payload["drift"]["type"] == "stale_declared_owner"
    assert payload["recommendation"]["action"] == "review_codeowners_rule"
    assert payload["decay"] is True


def test_empty_ownership_and_no_codeowners(git_repo: GitRepo) -> None:
    git_repo.commit_files({"README.md": "x\n"}, "seed")
    result = detect_drift(git_repo.path, declared_ownership({}), drift_config())
    assert not result.drift_detected


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


def test_scripted_repo_analysis_is_deterministic(tmp_path: Path) -> None:
    first = GitRepo.create(tmp_path / "first")
    second = GitRepo.create(tmp_path / "second")
    script_regression_repo(first)
    script_regression_repo(second)
    assert first.head_sha() == second.head_sha()
    assert _canonical_analysis(analyze_regression_repo(first)) == _canonical_analysis(
        analyze_regression_repo(second)
    )


def test_leading_slash_and_literal_x_still_match_rules(git_repo: GitRepo) -> None:
    ownership = declared_ownership(
        {
            "src/main.py": (owner_entry("@alice"),),
            "Xsrc/main.py": (owner_entry("@bob"),),
        }
    )
    result = _compare(
        git_repo,
        ownership,
        "src/main.py",
        "Xsrc/main.py",
        codeowners="src/* @alice\nXsrc/main.py @bob\n",
    )
    assert result.missing == ()
    assert result.changed == ()

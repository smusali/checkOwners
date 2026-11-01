"""Incremental replay: a reused baseline must match a full analysis."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

from checkowners.analyze import (
    analyze_ownership,
    collect_analysis,
)
from checkowners.git import (
    run_git,
)
from checkowners.models import (
    OwnershipMap,
)
from checkowners.state import HistoryBaseline, write_state
from checkowners.storage import state_path
from tests.conftest import (
    NOW,
    GitRepo,
    scoring_config,
    store_analysis,
)


def _canonical_ownership(ownership: OwnershipMap) -> str:
    def convert(value: object) -> object:
        if isinstance(value, datetime):
            aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
            return aware.astimezone(UTC).isoformat()
        if isinstance(value, dict):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [convert(item) for item in value]
        return value

    return json.dumps(convert(asdict(ownership)), sort_keys=True, separators=(",", ":"))


def _assert_same_ownership(left: OwnershipMap, right: OwnershipMap) -> None:
    assert _canonical_ownership(left) == _canonical_ownership(right)


def _baseline(
    ref: str,
    *,
    truncated: bool = False,
    rename_detection: str = "true",
) -> HistoryBaseline:
    return HistoryBaseline(
        analysis_ref=ref,
        files={},
        modified={},
        renames=frozenset(),
        blame={},
        blame_inputs="",
        rename_detection=rename_detection,
        truncated=truncated,
    )


def test_incremental_matches_full_analysis_and_blames_only_new_paths(git_repo: GitRepo) -> None:
    config = scoring_config()
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add a",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("a.py", "def alpha():\n    return 2\n")
    git_repo.coauthored_commit(
        "update a",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
        coauthor="Alice",
        coemail="alice@example.com",
    )
    store_analysis(git_repo, config)
    git_repo.commit_files(
        {"b.py": "def beta():\n    return 1\n"},
        "add b",
        author="Carol",
        email="carol@example.com",
        date="2026-05-03T12:00:00+00:00",
    )
    incremental = collect_analysis(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert incremental.mode == "incremental"
    assert incremental.fallback == ""
    full = analyze_ownership(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    _assert_same_ownership(incremental.ownership, full)


def test_incremental_handles_rename_deletion_and_window_expiry(git_repo: GitRepo) -> None:
    config = scoring_config()
    git_repo.commit_files(
        {"foo.py": "value = 1\n", "gone.py": "keep = 1\n"},
        "seed",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    store_analysis(git_repo, config)
    git_repo.rename("foo.py", "bar.py")
    (git_repo.path / "gone.py").unlink()
    git_repo.commit(
        "rename and delete",
        author="Bob",
        email="bob@example.com",
        date="2026-05-04T12:00:00+00:00",
    )
    incremental = collect_analysis(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    full = analyze_ownership(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    _assert_same_ownership(incremental.ownership, full)
    assert "foo.py" not in incremental.ownership.paths
    assert "gone.py" not in incremental.ownership.paths
    assert "bar.py" in incremental.ownership.paths

    expired = GitRepo.create(git_repo.path.parent / "expired")
    short = scoring_config(lookback_days=1)
    expired.commit_files(
        {"old.py": "old = 1\n"},
        "old",
        author="Alice",
        email="alice@example.com",
        date="2026-01-01T12:00:00+00:00",
    )
    early = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    first = collect_analysis(expired.path, short, as_of=early, analysis_ref=expired.head_sha())
    write_state(expired.path, first.ownership, config=short, history=first.baseline)
    expired.commit_files(
        {"new.py": "new = 1\n"},
        "new",
        author="Bob",
        email="bob@example.com",
        date="2026-03-01T12:00:00+00:00",
    )
    later = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    warmed = collect_analysis(expired.path, short, as_of=later, analysis_ref=expired.head_sha())
    cold = analyze_ownership(expired.path, short, as_of=later, analysis_ref=expired.head_sha())
    _assert_same_ownership(warmed.ownership, cold)
    assert warmed.mode == "incremental"
    assert "old.py" not in warmed.ownership.paths
    assert "new.py" in warmed.ownership.paths


def test_invalid_baseline_falls_back_to_full_analysis(git_repo: GitRepo) -> None:
    config = scoring_config()
    git_repo.commit_files(
        {"a.py": "value = 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    store_analysis(git_repo, config)
    target = state_path(git_repo.path)
    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["history"]["files"].pop(next(iter(stored["history"]["files"])))
    target.write_text(json.dumps(stored), encoding="utf-8")
    gapped = collect_analysis(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert gapped.mode == "full"
    assert gapped.fallback == "incremental baseline does not cover this lookback window"
    _assert_same_ownership(
        gapped.ownership,
        analyze_ownership(git_repo.path, config, as_of=NOW, analysis_ref=git_repo.head_sha()),
    )
    changed = scoring_config(lookback_days=30)
    rejected = collect_analysis(
        git_repo.path,
        changed,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert rejected.mode == "full"
    assert rejected.fallback == "scoring configuration changed"
    _assert_same_ownership(
        rejected.ownership,
        analyze_ownership(
            git_repo.path,
            changed,
            as_of=NOW,
            analysis_ref=git_repo.head_sha(),
        ),
    )
    forced = collect_analysis(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
        incremental=False,
    )
    assert forced.mode == "full"
    assert forced.fallback == "incremental analysis disabled"
    _assert_same_ownership(
        forced.ownership,
        analyze_ownership(git_repo.path, config, as_of=NOW, analysis_ref=git_repo.head_sha()),
    )
    run_git(git_repo.path, ["git", "checkout", "--orphan", "side"], check=True)
    git_repo.commit_files(
        {"side.py": "side = 1\n"},
        "side",
        author="Carol",
        email="carol@example.com",
        date="2026-05-06T12:00:00+00:00",
    )
    diverged = collect_analysis(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert diverged.mode == "full"
    assert diverged.fallback == "cached ref is not an ancestor of HEAD"
    _assert_same_ownership(
        diverged.ownership,
        analyze_ownership(git_repo.path, config, as_of=NOW, analysis_ref=git_repo.head_sha()),
    )

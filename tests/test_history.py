"""Git history: log parsing, co-authors, mailmap, renames and change survival."""

from __future__ import annotations

import subprocess
import time
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from checkowners.analyze import (
    analyze_ownership,
)
from checkowners.blame import (
    gather_blame_coverage,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    GitConfig,
)
from checkowners.history import (
    RawCommit,
    aggregate_contributions,
    current_commits,
    follow_path,
    get_commit_history,
    parse_log_output,
)
from checkowners.models import (
    OwnerEntry,
    OwnershipMap,
)
from tests.conftest import (
    FIRST_DATE,
    NOW,
    OLD,
    RECENT,
    SECOND_DATE,
    GitRepo,
    git_commit,
    init_git_repo,
    scoring_config,
)


def _owner(ownership: OwnershipMap, path: str, handle: str) -> OwnerEntry:
    return next(owner for owner in ownership.paths[path].owners if owner.handle == handle)


def test_aggregate_contributions_takes_latest_timestamp() -> None:
    commits = [
        RawCommit("alice@example.com", OLD, ("a.py",)),
        RawCommit("alice@example.com", RECENT, ("a.py",)),
        RawCommit("bob@example.com", OLD, ("a.py",)),
    ]
    result = aggregate_contributions(commits)
    assert result["a.py"]["alice@example.com"].commits == 2
    assert result["a.py"]["alice@example.com"].last_commit == RECENT
    assert result["a.py"]["bob@example.com"].commits == 1


def test_get_commit_history_subprocess_error(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        get_commit_history(tmp_path, NOW - timedelta(days=180), NOW)


def test_parse_log_output_empty() -> None:
    assert parse_log_output("") == []
    assert parse_log_output("  \n  ") == []


def test_parse_log_output_skips_missing_timestamp() -> None:
    raw = "COMMIT_START\nalice@example.com\nnot-a-timestamp\nfile.py\n"
    assert parse_log_output(raw) == []
    short = "COMMIT_START\nalice@example.com\n"
    assert parse_log_output(short) == []
    no_files = "COMMIT_START\nalice@example.com\n2026-05-28T12:00:00+00:00\n"
    assert parse_log_output(no_files) == []


def test_rename_keeps_history_on_the_current_path(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/api/billing.py": "amount = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("src/api/billing.py", "amount = 2\n")
    git_repo.commit(
        "edit",
        author="Alice",
        email="alice@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    git_repo.rename("src/api/billing.py", "services/payments/billing.py")
    git_repo.commit(
        "rename",
        author="Bob",
        email="bob@example.com",
        date="2026-05-03T12:00:00+00:00",
    )
    git_repo.write("services/payments/billing.py", "amount = 3\n")
    git_repo.commit(
        "edit again",
        author="Bob",
        email="bob@example.com",
        date="2026-05-04T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        scoring_config(),
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    handles = {owner.handle for owner in ownership.paths["services/payments/billing.py"].owners}
    assert "alice@example.com" in handles
    assert "src/api/billing.py" not in ownership.paths


def test_full_rewrite_supersedes_the_original_author(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/pay.py": "a = 1\nb = 2\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("src/pay.py", "x = 9\ny = 8\n")
    git_repo.commit(
        "replace",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    alice = _owner(ownership, "src/pay.py", "alice@example.com")
    breakdown = alice.score_breakdown
    freshness = alice.freshness
    assert breakdown is not None
    assert freshness is not None
    assert breakdown.change_survival.available
    assert breakdown.change_survival.score == pytest.approx(0.0, abs=0.05)
    assert freshness.status == "superseded"


def test_untouched_file_keeps_full_survival(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/stay.py": "a = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    alice = _owner(ownership, "src/stay.py", "alice@example.com")
    breakdown = alice.score_breakdown
    freshness = alice.freshness
    assert breakdown is not None
    assert freshness is not None
    assert breakdown.change_survival.score == pytest.approx(1.0, abs=0.05)
    assert freshness.status == "stable"


def test_rename_keeps_change_survival(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/old.py": "a = 1\nb = 2\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    git_repo.rename("src/old.py", "src/new.py")
    git_repo.commit(
        "rename",
        author="Bob",
        email="bob@example.com",
        date="2026-05-21T12:00:00+00:00",
    )
    git_repo.write("src/new.py", "a = 1\nb = 2\nc = 3\n")
    git_repo.commit(
        "append",
        author="Bob",
        email="bob@example.com",
        date="2026-05-22T12:00:00+00:00",
    )
    ownership = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    alice = _owner(ownership, "src/new.py", "alice@example.com")
    breakdown = alice.score_breakdown
    freshness = alice.freshness
    assert breakdown is not None
    assert freshness is not None
    assert breakdown.change_survival.score == pytest.approx(1.0, abs=0.05)
    assert freshness.status == "stable"


def test_directory_rename_keeps_every_file(git_repo: GitRepo) -> None:
    for index in range(300):
        git_repo.write(f"old/file_{index}.py", f"value = {index}\n")
    git_repo.commit(
        "seed directory",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.rename("old", "new")
    git_repo.commit(
        "rename directory",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    started = time.perf_counter()
    history = get_commit_history(
        git_repo.path,
        None,
        NOW,
        follow_renames=True,
        rename_similarity=50,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert any(move.kind == "D" for moves in history.moves.values() for move in moves)
    contributions = aggregate_contributions(current_commits(history))
    for index in range(300):
        authors = contributions[f"new/file_{index}.py"]
        assert authors["alice@example.com"].commits >= 1


def test_copy_inherits_history_only_when_the_source_changes(git_repo: GitRepo) -> None:
    body = "".join(f"line = {index}\n" for index in range(20))
    git_repo.commit_files(
        {"src.py": body},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.write("src.py", body + "line = extra\n")
    git_repo.copy("src.py", "copied.py")
    git_repo.commit(
        "copy and edit source",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    git_repo.copy("src.py", "plain.py")
    git_repo.commit(
        "copy untouched source",
        author="Bob",
        email="bob@example.com",
        date="2026-05-03T12:00:00+00:00",
    )
    history = get_commit_history(git_repo.path, None, NOW, follow_renames=True)
    contributions = aggregate_contributions(current_commits(history))
    assert contributions["copied.py"]["alice@example.com"].commits >= 1
    assert contributions["src.py"]["alice@example.com"].commits >= 1
    assert "alice@example.com" not in contributions["plain.py"]


def test_follow_renames_false_keeps_literal_paths(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"foo.py": "value = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.rename("foo.py", "bar.py")
    git_repo.commit(
        "rename",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    config = Config(
        analysis=AnalysisConfig(follow_renames=False, min_commits=1, confidence_threshold=0.0)
    )
    ownership = analyze_ownership(
        git_repo.path,
        config,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    handles = {owner.handle for owner in ownership.paths["bar.py"].owners}
    assert handles == {"bob@example.com"}


@given(
    st.lists(
        st.text(alphabet="abc", min_size=1, max_size=3),
        min_size=1,
        max_size=6,
        unique=True,
    )
)
def test_lineage_follow_stops(names: list[str]) -> None:
    forward = {names[index]: names[index + 1] for index in range(len(names) - 1)}
    assert follow_path(names[0], forward) == names[-1]
    if len(names) > 1:
        forward[names[-1]] = names[0]
        assert follow_path(names[0], forward) in set(names)


def test_moved_file_keeps_original_author(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "a.py").write_text(
        "def compute_total(items):\n    return sum(item.amount for item in items)\n",
        encoding="utf-8",
    )
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=FIRST_DATE)
    (repo / "b.py").write_text(
        "def compute_total(items):\n    return sum(item.amount for item in items)\n",
        encoding="utf-8",
    )
    (repo / "a.py").unlink()
    git_commit(repo, "bob moves", author="Bob", email="bob@example.com", date=SECOND_DATE)
    blame = gather_blame_coverage(["b.py"], repo)
    assert blame.coverage["b.py"].get("alice@example.com", 0.0) > 0.5

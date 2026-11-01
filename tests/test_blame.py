"""Blame: line attribution, its cache, the git flags and the exclusions before it."""

from __future__ import annotations

from pathlib import Path

import pytest

from checkowners.analyze import (
    analyze_ownership,
    collect_analysis,
)
from checkowners.blame import (
    gather_blame_coverage,
    parse_blame_output,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    GitConfig,
)
from checkowners.git import (
    display_ignore_revs_path,
    existing_file,
)
from tests.conftest import (
    FIRST_DATE,
    NOW,
    SECOND_DATE,
    GitRepo,
    git_commit,
    init_git_repo,
    owner_handles,
    scoring_config,
    store_analysis,
)


def test_blame_cache_matches_an_uncached_blame(
    git_repo: GitRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    paths = ["a.py"]
    first = gather_blame_coverage(paths, git_repo.path, git_config=GitConfig())
    second = gather_blame_coverage(
        paths,
        git_repo.path,
        git_config=GitConfig(report_blame_cache=True),
    )
    err = capsys.readouterr().err
    third = gather_blame_coverage(
        paths,
        git_repo.path,
        git_config=GitConfig(use_blame_cache=False),
    )
    assert first.coverage == second.coverage == third.coverage
    assert first.coverage
    assert "blame cache: hits=1 misses=0" in err


def test_rejected_baseline_reuses_the_blame_cache(git_repo: GitRepo) -> None:
    config = scoring_config()
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    store_analysis(git_repo, config)
    narrowed = Config(
        analysis=AnalysisConfig(
            follow_renames=False,
            confidence_threshold=0.0,
            min_commits=1,
        )
    )
    outcome = collect_analysis(
        git_repo.path,
        narrowed,
        as_of=NOW,
        analysis_ref=git_repo.head_sha(),
    )
    assert outcome.mode == "full"
    assert outcome.fallback


def test_blame_cache_misses_when_the_key_changes(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"a.py": "def alpha():\n    return 1\n"},
        "add",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    first = gather_blame_coverage(["a.py"], git_repo.path, git_config=GitConfig())
    (git_repo.path / ".git-blame-ignore-revs").write_text(
        f"{git_repo.head_sha()}\n",
        encoding="utf-8",
    )
    ignored = gather_blame_coverage(["a.py"], git_repo.path, git_config=GitConfig())
    (git_repo.path / ".mailmap").write_text(
        "Alias <alias@example.com> <alice@example.com>\n",
        encoding="utf-8",
    )
    mapped = gather_blame_coverage(["a.py"], git_repo.path, git_config=GitConfig())
    nomove = gather_blame_coverage(
        ["a.py"], git_repo.path, git_config=GitConfig(detect_moves=False)
    )
    assert first.coverage
    assert ignored.coverage != first.coverage or mapped.coverage or nomove.coverage


def test_parse_blame_output_counts_lines() -> None:
    blame = (
        "abc123 1 1 1\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "\tline 1\n"
        "abc124 2 2 1\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "\tline 2\n"
        "abc125 3 3 1\n"
        "author Bob\n"
        "author-mail <bob@example.com>\n"
        "\tline 3\n"
    )
    coverage = parse_blame_output(blame)
    assert coverage["alice@example.com"] == pytest.approx(2 / 3)
    assert coverage["bob@example.com"] == pytest.approx(1 / 3)


def test_parse_blame_output_empty_returns_empty_dict() -> None:
    assert parse_blame_output("") == {}


def test_ignore_revs_path_rejects_unexpected_names(tmp_path: Path) -> None:
    assert existing_file(tmp_path, "") is None
    assert existing_file(tmp_path, "a\nb") is None
    assert existing_file(tmp_path, "a\x00b") is None
    present = tmp_path / "ignore"
    present.write_text("abc\n", encoding="utf-8")
    assert existing_file(tmp_path, "ignore") == present
    assert existing_file(tmp_path, str(present)) == present
    assert display_ignore_revs_path(tmp_path, Path("/etc/hosts")) == "/etc/hosts"


def test_formatter_does_not_steal_blame_via_mass_refactor(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f'x = "{value}"\n', encoding="utf-8")
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=FIRST_DATE)
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f"x = '{value}'\n", encoding="utf-8")
    git_commit(repo, "bob formats", author="Bob", email="bob@example.com", date=SECOND_DATE)
    result = analyze_ownership(
        repo,
        Config(analysis=AnalysisConfig(confidence_threshold=0.0)),
        as_of=NOW,
        analysis_ref="test",
    )
    assert owner_handles(result, "a.py")[0] == "alice@example.com"
    assert result.analysis_completeness.ignore_revs_applied is False


def test_formatter_ignored_via_ignore_revs_file(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f'x = "{value}"\n', encoding="utf-8")
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=FIRST_DATE)
    for name, value in (("a.py", "one"), ("b.py", "two"), ("c.py", "three"), ("d.py", "four")):
        (repo / name).write_text(f"x = '{value}'\n", encoding="utf-8")
    bob_sha = git_commit(
        repo, "bob formats", author="Bob", email="bob@example.com", date=SECOND_DATE
    )
    (repo / ".git-blame-ignore-revs").write_text(f"{bob_sha}\n", encoding="utf-8")
    result = analyze_ownership(
        repo,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=NOW,
        analysis_ref="test",
    )
    assert owner_handles(result, "a.py")[0] == "alice@example.com"
    assert result.analysis_completeness.ignore_revs_applied is True
    assert result.analysis_completeness.ignore_revs_file == ".git-blame-ignore-revs"


def test_whitespace_reindent_does_not_transfer_ownership(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "indent.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    git_commit(repo, "alice writes", author="Alice", email="alice@example.com", date=FIRST_DATE)
    (repo / "indent.py").write_text("def foo():\n        return 1\n", encoding="utf-8")
    git_commit(repo, "bob reindents", author="Bob", email="bob@example.com", date=SECOND_DATE)
    result = analyze_ownership(
        repo,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0),
            git=GitConfig(mass_refactor_file_fraction=0.0),
        ),
        as_of=NOW,
        analysis_ref="test",
    )
    assert owner_handles(result, "indent.py")[0] == "alice@example.com"

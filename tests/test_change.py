"""Ownership impact for the paths a diff touched."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from checkowners.change import (
    build_change,
    changed_paths,
    load_paths,
    merge_base,
    split_spec,
)
from checkowners.config import (
    Config,
)
from tests.clirunner import Cli
from tests.gitrepo import GitRepo

_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
_AS_OF = datetime(2026, 10, 27, tzinfo=UTC)


def test_empty_paths_do_not_analyze(tmp_path: Path) -> None:
    report = build_change(tmp_path, Config(), (), base_revision="abc")
    assert report.total == 0
    assert report.clusters == 0
    assert report.recommended == ()


def test_scoped_blame_follows_the_diff(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"a.py": "a.py\n", "b.py": "b.py\n", "c.py": "c.py\n", "d.py": "d.py\n"},
        "base",
        author="Test",
        email="test@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    git_repo.commit_files(
        {"a.py": "changed\n"},
        "head",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    spec = "HEAD~1...HEAD"
    paths = changed_paths(git_repo.path, spec)
    assert paths == ("a.py",)
    report = build_change(
        git_repo.path,
        Config(),
        paths,
        base_revision=merge_base(git_repo.path, spec),
    )
    assert report.files == ("a.py",)
    assert report.total == 1


def test_diff_does_not_write_the_cache(cli: Cli) -> None:
    result = cli.run("--offline", "diff", "HEAD~1...HEAD", "--json")
    assert result.exit_code == 0, result.stdout
    assert cli.run("cache", "info").exit_code == 1


def test_split_spec_fills_missing_sides() -> None:
    assert split_spec("base...head") == ("base", "head")
    assert split_spec("...head") == ("HEAD", "head")
    assert split_spec("base...") == ("base", "HEAD")
    assert split_spec("HEAD") == ("HEAD", "HEAD")


def test_load_paths_skips_analysis_when_empty(tmp_path: Path) -> None:
    empty = load_paths(tmp_path, Config(), (), None, as_of=_NOW)
    assert empty.ownership.paths == {}
    assert empty.ownership.analysis_ref == ""
    dated = load_paths(tmp_path, Config(), (), "abc", as_of=_AS_OF).ownership
    assert dated.analysis_ref == "abc"
    assert dated.last_analyzed == _AS_OF


def test_changed_paths_drop_excludes(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"keep.py": "a\n", "skip.py": "b\n"},
        "base",
        author="Test",
        email="test@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    git_repo.commit_files(
        {"keep.py": "c\n", "skip.py": "d\n"},
        "head",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    assert changed_paths(git_repo.path, "HEAD~1...HEAD", exclude=("skip.py",)) == ("keep.py",)


def test_default_loader_scores_without_a_caller_loader(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"a.py": "a\n"},
        "base",
        author="Test",
        email="test@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    git_repo.commit_files(
        {"a.py": "b\n"},
        "head",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    report = build_change(
        git_repo.path,
        Config(),
        ("a.py",),
        base_revision=merge_base(git_repo.path, "HEAD~1...HEAD"),
    )
    assert report.total == 1
    assert report.drift == ("a.py has no CODEOWNERS owner",)

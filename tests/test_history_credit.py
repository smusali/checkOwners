"""Credit across renames, copies, mailmaps, and co-author trailers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from checkowners.analyze import (
    analyze_ownership,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    GitConfig,
)
from checkowners.history import (
    RawCommit,
    author_index,
    cap_mechanical_authors,
)
from checkowners.models import (
    ConfiguredMergeStrategy,
)
from checkowners.scoring import (
    Contribution,
)
from tests.conftest import (
    FIRST_DATE,
    NOW,
    SECOND_DATE,
    THIRD_DATE,
    git_commit,
    init_git_repo,
    owner_handles,
)


def test_mailmap_collapses_three_addresses(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    target = repo / "owned.py"
    target.write_text("v1\n", encoding="utf-8")
    git_commit(repo, "work", author="Alice", email="alice@work.com", date=FIRST_DATE)
    target.write_text("v2\n", encoding="utf-8")
    git_commit(repo, "home", author="Alice", email="alice@home.com", date=SECOND_DATE)
    target.write_text("v3\n", encoding="utf-8")
    git_commit(repo, "old", author="Alice", email="alice@old.com", date=THIRD_DATE)
    (repo / ".mailmap").write_text(
        "Alice Example <alice@example.com> <alice@work.com>\n"
        "Alice Example <alice@example.com> <alice@home.com>\n"
        "Alice Example <alice@example.com> <alice@old.com>\n",
        encoding="utf-8",
    )
    base = Config(
        analysis=AnalysisConfig(confidence_threshold=0.0),
        git=GitConfig(mass_refactor_file_fraction=0.0),
    )
    merged = analyze_ownership(repo, base, as_of=NOW, analysis_ref="test")
    assert owner_handles(merged, "owned.py") == ["alice@example.com"]
    assert merged.paths["owned.py"].qualified_owner_count == 1
    assert merged.analysis_completeness.mailmap_applied is True
    assert merged.analysis_completeness.mailmap_file == ".mailmap"

    raw = analyze_ownership(
        repo,
        Config(
            analysis=AnalysisConfig(confidence_threshold=0.0),
            git=GitConfig(mass_refactor_file_fraction=0.0, use_mailmap=False),
        ),
        as_of=NOW,
        analysis_ref="test",
    )
    assert set(owner_handles(raw, "owned.py")) == {
        "alice@work.com",
        "alice@home.com",
        "alice@old.com",
    }
    assert raw.paths["owned.py"].qualified_owner_count == 3
    assert raw.analysis_completeness.mailmap_applied is False


def _credit_config(
    *,
    count_co_authors: bool = True,
    co_author_weight: float = 1.0,
    merge_strategy: ConfiguredMergeStrategy = "auto",
) -> Config:
    return Config(
        analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
        git=GitConfig(
            mass_refactor_file_fraction=0.0,
            count_co_authors=count_co_authors,
            co_author_weight=co_author_weight,
            merge_strategy=merge_strategy,
        ),
    )


def _git(repo: Path, args: list[str], *, date: str | None = None) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(repo),
        "XDG_CONFIG_HOME": str(repo),
    }
    if date is not None:
        env["GIT_AUTHOR_NAME"] = "Merger"
        env["GIT_AUTHOR_EMAIL"] = "merger@example.com"
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_NAME"] = "Merger"
        env["GIT_COMMITTER_EMAIL"] = "merger@example.com"
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_coauthor_trailers_credit_authors(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "zero.py").write_text("v = 0\n", encoding="utf-8")
    git_commit(repo, "zero trailers", author="Alice", email="alice@example.com", date=FIRST_DATE)
    (repo / "one.py").write_text("v = 1\n", encoding="utf-8")
    git_commit(
        repo,
        "one trailer\n\nCo-authored-by: Bob <bob@example.com>",
        author="Alice",
        email="alice@example.com",
        date=SECOND_DATE,
    )
    (repo / "two.py").write_text("v = 2\n", encoding="utf-8")
    git_commit(
        repo,
        (
            "two trailers\n\n"
            "Co-authored-by: Bob <bob@example.com>\n"
            "Co-authored-by: Carol <carol@example.com>\n"
            "Co-authored-by: Bob <bob@example.com>"
        ),
        author="Alice",
        email="alice@example.com",
        date=THIRD_DATE,
    )
    (repo / "bad.py").write_text("v = 3\n", encoding="utf-8")
    git_commit(
        repo,
        (
            "malformed\n\n"
            "Co-authored-by: not an email\n"
            "Co-authored-by: Dana <dana@example.com>\n"
            "Co-authored-by: <missing-at>"
        ),
        author="Alice",
        email="alice@example.com",
        date="2026-05-04T12:00:00+00:00",
    )
    result = analyze_ownership(repo, _credit_config(), as_of=NOW, analysis_ref="test")
    assert result.analysis_completeness.merge_strategy == "rebase"
    assert result.analysis_completeness.co_author_count == 4
    assert owner_handles(result, "zero.py") == ["alice@example.com"]
    assert set(owner_handles(result, "one.py")) == {"alice@example.com", "bob@example.com"}
    assert set(owner_handles(result, "two.py")) == {
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
    }
    assert set(owner_handles(result, "bad.py")) == {"alice@example.com", "dana@example.com"}

    full = {owner.handle: owner for owner in result.paths["one.py"].owners}
    weighed = analyze_ownership(
        repo,
        _credit_config(co_author_weight=0.5),
        as_of=NOW,
        analysis_ref="test",
    )
    half = {owner.handle: owner for owner in weighed.paths["one.py"].owners}
    full_bob = full["bob@example.com"].score_breakdown
    half_bob = half["bob@example.com"].score_breakdown
    full_alice = full["alice@example.com"].score_breakdown
    half_alice = half["alice@example.com"].score_breakdown
    assert full_bob is not None
    assert half_bob is not None
    assert full_alice is not None
    assert half_alice is not None
    assert half_bob.frequency.score < full_bob.frequency.score
    assert half_alice.frequency.score == pytest.approx(full_alice.frequency.score)

    author_only = analyze_ownership(
        repo,
        _credit_config(count_co_authors=False),
        as_of=NOW,
        analysis_ref="test",
    )
    assert owner_handles(author_only, "one.py") == ["alice@example.com"]
    assert owner_handles(author_only, "two.py") == ["alice@example.com"]
    assert author_only.analysis_completeness.co_author_count == 0

    mapped = init_git_repo(tmp_path / "mapped")
    (mapped / "pair.py").write_text("v = 1\n", encoding="utf-8")
    git_commit(mapped, "bob writes", author="Bob", email="bob@example.com", date=FIRST_DATE)
    (mapped / "pair.py").write_text("v = 2\n", encoding="utf-8")
    git_commit(
        mapped,
        "alice with bob home\n\nCo-authored-by: Bob Home <bob@home.example>",
        author="Alice",
        email="alice@example.com",
        date=SECOND_DATE,
    )
    (mapped / ".mailmap").write_text(
        "Bob <bob@example.com> <bob@home.example>\n",
        encoding="utf-8",
    )
    git_commit(mapped, "mailmap", author="Alice", email="alice@example.com", date=THIRD_DATE)
    collapsed = analyze_ownership(mapped, _credit_config(), as_of=NOW, analysis_ref="test")
    handles = owner_handles(collapsed, "pair.py")
    assert "bob@example.com" in handles
    assert "bob@home.example" not in handles


def test_squash_and_merge_histories_differ(tmp_path: Path) -> None:
    merged = init_git_repo(tmp_path / "merge")
    (merged / "a.py").write_text("a = 1\n", encoding="utf-8")
    git_commit(merged, "alice adds a", author="Alice", email="alice@example.com", date=FIRST_DATE)
    _git(merged, ["checkout", "-b", "feature"])
    (merged / "b.py").write_text("b = 1\n", encoding="utf-8")
    git_commit(merged, "bob adds b", author="Bob", email="bob@example.com", date=SECOND_DATE)
    _git(merged, ["checkout", "main"])
    _git(
        merged,
        ["merge", "--no-ff", "-m", "Merge branch feature", "feature"],
        date=THIRD_DATE,
    )
    squashed = init_git_repo(tmp_path / "squash")
    (squashed / "a.py").write_text("a = 1\n", encoding="utf-8")
    (squashed / "b.py").write_text("b = 1\n", encoding="utf-8")
    git_commit(
        squashed,
        "Add files (#7)\n\nCo-authored-by: Bob <bob@example.com>",
        author="Alice",
        email="alice@example.com",
        date=FIRST_DATE,
    )
    config = _credit_config()
    merge_result = analyze_ownership(merged, config, as_of=NOW, analysis_ref="test")
    squash_result = analyze_ownership(squashed, config, as_of=NOW, analysis_ref="test")
    assert merge_result.analysis_completeness.merge_strategy == "merge"
    assert merge_result.analysis_completeness.co_author_count == 0
    assert owner_handles(merge_result, "a.py") == ["alice@example.com"]
    assert owner_handles(merge_result, "b.py") == ["bob@example.com"]
    assert squash_result.analysis_completeness.merge_strategy == "squash"
    assert squash_result.analysis_completeness.co_author_count == 1
    assert set(owner_handles(squash_result, "a.py")) == {"alice@example.com", "bob@example.com"}
    assert set(owner_handles(squash_result, "b.py")) == {"alice@example.com", "bob@example.com"}
    squash_owners = {owner.handle: owner for owner in squash_result.paths["a.py"].owners}
    alice = squash_owners["alice@example.com"].score_breakdown
    bob = squash_owners["bob@example.com"].score_breakdown
    assert alice is not None
    assert bob is not None
    assert alice.blame.available is True
    assert alice.blame.score == pytest.approx(1.0)
    assert bob.blame.available is True
    assert bob.blame.score == pytest.approx(0.0)
    forced = analyze_ownership(
        merged,
        _credit_config(merge_strategy="rebase"),
        as_of=NOW,
        analysis_ref="test",
    )
    assert forced.analysis_completeness.merge_strategy == "rebase"


def test_author_index_skips_blank_fields_and_caps_only_mechanical_authors() -> None:
    indexed = author_index(
        [
            RawCommit(author="ada@x.com", timestamp=NOW, files=("a.py",)),
            RawCommit(
                author="ada@x.com",
                timestamp=NOW,
                files=("a.py",),
                sha="abc",
                author_name="Ada",
                subject="bump deps",
            ),
        ]
    )
    assert indexed.shas == {"ada@x.com": "abc"}
    assert indexed.names == {"ada@x.com": ("Ada",)}
    commits = [
        RawCommit(author="bot@x.com", timestamp=NOW, files=("a.py",), subject="bump deps")
        for _ in range(20)
    ]
    commits.append(RawCommit(author="ada@x.com", timestamp=NOW, files=("a.py",), subject="fix"))
    capped = cap_mechanical_authors(
        {
            "a.py": {
                "bot@x.com": Contribution(commits=20, last_commit=NOW, credit=20.0),
                "ada@x.com": Contribution(commits=1, last_commit=NOW, credit=4.0),
            }
        },
        commits,
    )
    assert capped["a.py"]["bot@x.com"].credit == 1.0
    assert capped["a.py"]["ada@x.com"].credit == 4.0

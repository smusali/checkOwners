from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners.balance import BalanceReport, ReviewLoad
from checkowners.change import (
    build_change,
    changed_paths,
    load_paths,
    merge_base,
    render_diff,
    render_suggest,
)
from checkowners.cli import app
from checkowners.models import (
    Config,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
)
from tests.conftest import git_commit, init_git_repo

_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
_AS_OF = datetime(2026, 10, 27, tzinfo=UTC)
runner = CliRunner()


def _owner(handle: str, score: float = 0.9, *, stale: bool = False) -> OwnerEntry:
    freshness = None
    if stale:
        freshness = OwnershipFreshness(
            active_expertise=0.1,
            historical_expertise=0.9,
            maintenance_recency=0.1,
            status="inactive",
            half_life_days=30.0,
        )
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=_NOW,
        commits=4,
        evidence_quality=0.8,
        freshness=freshness,
    )


def _map(path: str, owner: OwnerEntry, count: int) -> OwnershipMap:
    return OwnershipMap(
        paths={path: PathOwnership(owners=(owner,), qualified_owner_count=count)},
        last_analyzed=_NOW,
        analysis_ref="abc",
    )


def test_empty_paths_do_not_analyze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> OwnershipMap:
        raise AssertionError("analyze")

    monkeypatch.setattr("checkowners.change.analyze_ownership", boom)
    report = build_change(tmp_path, Config(), (), base_revision="abc")
    assert report.total == 0
    assert report.clusters == 0
    assert report.recommended == ()


def test_scoped_blame_follows_the_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_git_repo(tmp_path)
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "base",
        author="Test",
        email="test@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    (tmp_path / "a.py").write_text("changed\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "head",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    blamed: list[str] = []

    def fake_blame(
        _repo: Path,
        path: str,
        _argv: list[str],
        _sink: object = None,
    ) -> dict[str, float]:
        blamed.append(path)
        return {}

    monkeypatch.setattr("checkowners.analyze._blame_for_path", fake_blame)
    spec = "HEAD~1...HEAD"
    paths = changed_paths(tmp_path, spec)
    assert paths == ("a.py",)

    def load(selected: tuple[str, ...], revision: str | None) -> OwnershipMap:
        return load_paths(tmp_path, Config(), selected, revision, as_of=_AS_OF)

    build_change(tmp_path, Config(), paths, base_revision=merge_base(tmp_path, spec), load=load)
    assert blamed == ["a.py", "a.py"]


def test_diff_does_not_write_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "base",
        author="Test",
        email="test@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    (tmp_path / "a.py").write_text("b\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "head",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    monkeypatch.chdir(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cache write")

    monkeypatch.setattr("checkowners.cli.write_state", boom)
    result = runner.invoke(app, ["--offline", "diff", "HEAD~1...HEAD", "--json"])
    assert result.exit_code == 0, result.stdout


def test_overload_demotes_only_real_review_load(tmp_path: Path) -> None:
    path = "src/a.py"
    head = OwnershipMap(
        paths={
            path: PathOwnership(
                owners=(_owner("@alice", 0.95), _owner("@bob", 0.7)),
                qualified_owner_count=2,
            )
        },
        last_analyzed=_NOW,
        analysis_ref="head",
    )
    base = head

    def load(_paths: tuple[str, ...], _revision: str | None) -> OwnershipMap:
        return head if _revision is None else base

    report = build_change(
        tmp_path,
        Config(),
        (path,),
        base_revision="base",
        load=load,
        review_counts={"@alice": 8, "@bob": 0},
    )
    assert [row.handle for row in report.recommended] == ["@bob"]
    assert report.not_recommended[0].handle == "@alice"
    assert report.not_recommended[0].demote_reason == "8 open reviews already (overloaded)"
    assert "recency" in report.recommended[0].signals
    text = render_suggest(report)
    assert "Not recommended:" in text
    assert "8 open reviews already (overloaded)" in text
    short = render_diff(report)
    assert "Changed: src/a.py" in short
    assert "@bob" in short


def test_authorship_proxy_does_not_demote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_balance(*_args: object, **_kwargs: object) -> BalanceReport:
        return BalanceReport(
            loads=(ReviewLoad(handle="@alice", reviews=8, load_band="high"),),
            average=4.0,
            overloaded=(),
            suggestions=(),
            source="git_authorship",
        )

    monkeypatch.setattr("checkowners.change.analyze_balance", fake_balance)
    owner = _owner("@alice")
    current = _map("src/a.py", owner, 1)

    def load(_paths: tuple[str, ...], _revision: str | None) -> OwnershipMap:
        return current

    report = build_change(
        tmp_path,
        Config(),
        ("src/a.py",),
        base_revision="base",
        load=load,
    )
    assert [row.handle for row in report.recommended] == ["@alice"]
    assert report.not_recommended == ()


def test_bus_factor_regression(tmp_path: Path) -> None:
    (tmp_path / "CODEOWNERS").write_text("* @alice\n", encoding="utf-8")
    before = _map("src/a.py", _owner("@alice"), 2)
    after = _map("src/a.py", _owner("@alice", stale=True), 1)

    def load(_paths: tuple[str, ...], revision: str | None) -> OwnershipMap:
        return after if revision is None else before

    report = build_change(
        tmp_path,
        Config(),
        ("src/a.py",),
        base_revision="base",
        load=load,
        review_counts={"@alice": 0},
    )
    assert report.impacts[0].kind == "regression"
    assert report.impacts[0].before_count == 2
    assert report.impacts[0].after_count == 1
    assert report.single_owner_after == 1
    assert "bus factor 2 -> 1" in render_diff(report)
    assert any("stale" in line for line in report.risks)
    assert any("single" in line or "one inferred" in line for line in report.risks)

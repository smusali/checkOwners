from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners.balance import BalanceReport, ReviewLoad
from checkowners.change import (
    ChangeReport,
    PathImpact,
    ReviewerRow,
    _drift_line,
    _overload_reason,
    _row_line,
    build_change,
    change_payload,
    changed_paths,
    load_paths,
    merge_base,
    render_diff,
    render_suggest,
    split_spec,
)
from checkowners.cli import app
from checkowners.models import (
    ConfidenceScore,
    Config,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    PathsConfig,
    SignalScore,
)
from checkowners.patterns import CodeownersRule
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
        _digest: str | None = None,
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


def test_split_spec_fills_missing_sides() -> None:
    assert split_spec("base...head") == ("base", "head")
    assert split_spec("...head") == ("HEAD", "head")
    assert split_spec("base...") == ("base", "HEAD")
    assert split_spec("HEAD") == ("HEAD", "HEAD")


def test_merge_base_rejects_an_empty_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        stdout = "\n"

    monkeypatch.setattr("checkowners.change.run_git", lambda *_a, **_k: _Result())
    with pytest.raises(ValueError, match="No merge base"):
        merge_base(tmp_path, "HEAD")


def test_load_paths_skips_analysis_when_empty(tmp_path: Path) -> None:
    empty = load_paths(tmp_path, Config(), (), None, as_of=_NOW)
    assert empty.paths == {}
    assert empty.analysis_ref == ""
    dated = load_paths(tmp_path, Config(), (), "abc", as_of=_AS_OF)
    assert dated.analysis_ref == "abc"
    assert dated.last_analyzed == _AS_OF


def test_load_paths_passes_revision_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "base",
        author="Test",
        email="test@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    seen: list[dict[str, object]] = []

    def fake(_repo: Path, _config: Config, **kwargs: object) -> OwnershipMap:
        seen.append(kwargs)
        return OwnershipMap(paths={}, last_analyzed=_NOW, analysis_ref="ref")

    monkeypatch.setattr("checkowners.change.analyze_ownership", fake)

    def provider(*_args: object, **_kwargs: object) -> None:
        return None

    load_paths(tmp_path, Config(), ("a.py",), "abc", review_provider=provider)
    load_paths(tmp_path, Config(), ("a.py",), None, review_provider=provider)
    assert seen[0]["revision"] == "abc"
    assert seen[0]["review_provider"] is None
    assert seen[0]["mass_revs"] == ()
    assert seen[1]["revision"] is None
    assert seen[1]["review_provider"] is provider
    assert seen[1]["mass_revs"] is None


def test_changed_paths_drop_excludes(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    (tmp_path / "keep.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "skip.py").write_text("b\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "base",
        author="Test",
        email="test@example.com",
        date="2026-05-20T12:00:00+00:00",
    )
    (tmp_path / "keep.py").write_text("c\n", encoding="utf-8")
    (tmp_path / "skip.py").write_text("d\n", encoding="utf-8")
    git_commit(
        tmp_path,
        "head",
        author="Test",
        email="test@example.com",
        date="2026-05-28T12:00:00+00:00",
    )
    assert changed_paths(tmp_path, "HEAD~1...HEAD", exclude=("skip.py",)) == ("keep.py",)


def test_default_loader_scores_without_a_caller_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []

    def fake(_repo: Path, _config: Config, **kwargs: object) -> OwnershipMap:
        revision = kwargs.get("revision")
        calls.append(revision if isinstance(revision, str) or revision is None else "bad")
        return OwnershipMap(paths={}, last_analyzed=_NOW, analysis_ref="ref")

    monkeypatch.setattr("checkowners.change.analyze_ownership", fake)
    monkeypatch.setattr("checkowners.change.head_commit_sha", lambda _root: "head")
    monkeypatch.setattr("checkowners.change.resolve_as_of", lambda *_args: _NOW)
    report = build_change(tmp_path, Config(), ("a.py",), base_revision="base")
    assert calls == [None, "base"]
    assert report.total == 1
    assert report.drift == ("a.py has no CODEOWNERS owner",)


def test_change_report_covers_kinds_filters_and_text(tmp_path: Path) -> None:
    (tmp_path / "CODEOWNERS").write_text(
        "src/b.py @alice\nsrc/c.py @bob\nsrc/d.py\nsrc/e.py @zoe\n",
        encoding="utf-8",
    )
    quiet = ConfidenceScore(
        total=0.5,
        recency=SignalScore(False),
        frequency=SignalScore(False),
        blame=SignalScore(False),
        review=SignalScore(False),
    )
    stable = OwnershipFreshness(
        active_expertise=0.2,
        historical_expertise=0.2,
        maintenance_recency=0.2,
        status="stable",
        half_life_days=30.0,
    )
    stale = OwnershipFreshness(
        active_expertise=0.1,
        historical_expertise=0.9,
        maintenance_recency=0.1,
        status="inactive",
        half_life_days=30.0,
    )

    def person(
        handle: str,
        score: float,
        *,
        last: datetime | None,
        freshness: OwnershipFreshness | None = None,
        breakdown: ConfidenceScore | None = None,
    ) -> OwnerEntry:
        return OwnerEntry(
            handle=handle,
            ownership_score=score,
            last_commit=last,
            commits=2,
            evidence_quality=0.4,
            freshness=freshness,
            score_breakdown=breakdown,
        )

    head = OwnershipMap(
        paths={
            "src/a.py": PathOwnership(
                owners=(
                    person("@skip", 0.9, last=_NOW),
                    person("@tiny", 0.05, last=_NOW),
                    person("@gina", 0.5, last=None, breakdown=quiet),
                    person("@quiet", 0.6, last=None, breakdown=quiet),
                    person("@helen", 0.6, last=datetime(2026, 1, 1, tzinfo=UTC)),
                ),
                qualified_owner_count=2,
            ),
            "src/gina.py": PathOwnership(
                owners=(person("@gina", 0.4, last=_NOW),),
                qualified_owner_count=1,
            ),
            "src/b.py": PathOwnership(
                owners=(person("@alice", 0.4, last=datetime(2026, 1, 1, tzinfo=UTC)),),
                qualified_owner_count=1,
            ),
            "src/alice.py": PathOwnership(
                owners=(person("@alice", 0.95, last=_NOW),),
                qualified_owner_count=1,
            ),
            "src/c.py": PathOwnership(
                owners=(person("@alice", 0.8, last=_AS_OF),),
                qualified_owner_count=1,
            ),
            "src/d.py": PathOwnership(
                owners=(
                    person("@erin", 0.8, last=_NOW, freshness=stable),
                    person("@frank", 0.8, last=_NOW, freshness=stale),
                ),
                qualified_owner_count=2,
            ),
            "src/e.py": PathOwnership(
                owners=(person("@low", 0.1, last=_NOW),),
                qualified_owner_count=0,
            ),
            "shift.py": PathOwnership(
                owners=(person("@alice", 0.9, last=_NOW),),
                qualified_owner_count=2,
            ),
            "new.py": PathOwnership(
                owners=(person("@carol", 0.9, last=_NOW),),
                qualified_owner_count=2,
            ),
            "single.py": PathOwnership(
                owners=(person("@dave", 0.9, last=_NOW),),
                qualified_owner_count=1,
            ),
            "ok.py": PathOwnership(
                owners=(person("@erin", 0.9, last=_NOW),),
                qualified_owner_count=2,
            ),
        },
        last_analyzed=_AS_OF,
        analysis_ref="head",
    )
    base = OwnershipMap(
        paths={
            "shift.py": PathOwnership(
                owners=(person("@bob", 0.9, last=_NOW),),
                qualified_owner_count=2,
            ),
            "single.py": PathOwnership(
                owners=(person("@dave", 0.9, last=_NOW),),
                qualified_owner_count=1,
            ),
            "ok.py": PathOwnership(
                owners=(person("@erin", 0.9, last=_NOW),),
                qualified_owner_count=2,
            ),
        },
        last_analyzed=_NOW,
        analysis_ref="base",
    )
    paths = (
        "src/a.py",
        "src/gina.py",
        "src/b.py",
        "src/alice.py",
        "src/c.py",
        "src/d.py",
        "src/e.py",
        "missing.py",
        "shift.py",
        "new.py",
        "single.py",
        "ok.py",
    )

    def load(_paths: tuple[str, ...], revision: str | None) -> OwnershipMap:
        return head if revision is None else base

    def team(handles: tuple[str, ...]) -> str:
        assert handles
        return "@acme/team"

    report = build_change(
        tmp_path,
        Config(paths=PathsConfig(exclude=("skip.py",)), contributors_exclude=("@skip",)),
        (*paths, "skip.py"),
        base_revision="base",
        load=load,
        review_counts={"@alice": 1, "@bob": 0},
        team_resolve=team,
    )
    kinds = {impact.path: impact.kind for impact in report.impacts}
    assert kinds["shift.py"] == "shift"
    assert kinds["new.py"] == "new"
    assert kinds["single.py"] == "single"
    assert kinds["ok.py"] == "ok"
    assert kinds["missing.py"] == "ok"
    assert report.team == "@acme/team"
    reasons = [row.reason for row in (*report.recommended, *report.not_recommended)]
    assert any("no recent commit" in reason for reason in reasons)
    assert any("last touched" in reason for reason in reasons)
    assert any("active this week" in reason for reason in reasons)
    assert any("stale" in line for line in report.risks)
    assert any("no CODEOWNERS owner" in line for line in report.drift)
    assert any("declared @bob" in line for line in report.drift)
    assert "skip.py" not in report.files
    payload = change_payload(report)
    assert payload["team"] == "@acme/team"
    text = render_suggest(report)
    assert "@acme/team   declared team" in text
    short = render_diff(report)
    assert "@bob -> @alice" in short
    bare = ReviewerRow("@quiet", 0.5, "owns files", 0.4, {}, True, "")
    assert "[" not in _row_line(bare)
    assert "owns files" in _row_line(bare)
    waiting = _overload_reason(ReviewLoad(handle="@alice", reviews=1, prs_waiting=3))
    assert waiting == "3 open reviews already (overloaded)"
    assert _drift_line("src/d.py", (CodeownersRule("src/d.py", (), 1),), ("@frank",)) == (
        "src/d.py has no CODEOWNERS owner"
    )
    assert _drift_line("src/b.py", (CodeownersRule("src/b.py", ("@alice",), 1),), ()) == ""


def test_rendered_change_without_people() -> None:
    impact = PathImpact("a.py", "@old", "@new", 2, 2, "shift")
    report = ChangeReport(
        files=(),
        clusters=0,
        recommended=(),
        not_recommended=(),
        covered=0,
        total=0,
        risks=(),
        drift=(),
        impacts=(impact,),
        single_owner_after=0,
        team="@acme/team",
    )
    assert change_payload(report)["team"] == "@acme/team"
    rendered = render_suggest(report)
    assert "\n  none\n" in rendered
    assert "@acme/team   declared team" in rendered
    short = render_diff(report)
    assert short.startswith("Changed: none")
    assert "Recommended reviewers: @acme/team\n" in short
    assert "@old -> @new" in short

"""Tests for evidence providers, dialects, and ownership reconciliation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from checkowners import RepositoryAnalyzer
from checkowners.analyze import Contribution, analyze_ownership, frequency_prior_for
from checkowners.cli import ExitCode, _dialect_or_exit, _team_resolver
from checkowners.generate import generate_codeowners
from checkowners.models import (
    AnalysisConfig,
    Config,
    GithubConfig,
    OwnerEntry,
    OwnershipEvidence,
    OwnershipMap,
    PathOwnership,
    ScoringConfig,
)
from checkowners.patterns import CodeownersRule, parse_rules
from checkowners.providers import (
    DialectError,
    GitHubTeamProvider,
    _nested_owners,
    _read_text,
    evidence_from_payload,
    path_evidence,
    reconcile_layers,
    replace_reviews,
    require_dialect,
    rescore,
    rescore_map,
)
from checkowners.state import load_ownership, write_state
from checkowners.validate import validate_codeowners
from tests.conftest import GitRepo

_WHEN = datetime(2024, 6, 1, tzinfo=UTC)


def _entry(handle: str, score: float) -> OwnerEntry:
    return OwnerEntry(handle=handle, ownership_score=score, last_commit=_WHEN, commits=1)


def _scores(owners: tuple[OwnerEntry, ...]) -> dict[str, tuple[float, float, float, float, float]]:
    scored: dict[str, tuple[float, float, float, float, float]] = {}
    for owner in owners:
        breakdown = owner.score_breakdown
        assert breakdown is not None
        scored[owner.handle] = (
            owner.ownership_score,
            breakdown.recency.score,
            breakdown.frequency.score,
            breakdown.blame.score,
            breakdown.change_survival.score,
        )
    return scored


def _review_signal(owners: tuple[OwnerEntry, ...]) -> dict[str, float]:
    signals: dict[str, float] = {}
    for owner in owners:
        breakdown = owner.score_breakdown
        assert breakdown is not None
        signals[owner.handle] = breakdown.review.score
    return signals


@pytest.mark.integration
def test_stored_evidence_replays_live_scores(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"payments/core.py": "def pay() -> int:\n    return 1\n"},
        "pay",
        author="Alice",
        email="alice@example.com",
        date="2024-01-15T00:00:00+00:00",
    )
    git_repo.commit_files(
        {"payments/core.py": "def pay() -> int:\n    return 2\n"},
        "pay again",
        author="Alice",
        email="alice@example.com",
        date="2024-05-01T00:00:00+00:00",
    )
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1))
    ownership = analyze_ownership(git_repo.path, config, as_of=_WHEN)
    replayed = rescore_map(ownership, config)
    assert replayed
    for path, path_ownership in ownership.paths.items():
        assert _scores(path_ownership.owners) == _scores(replayed[path])
        assert path_ownership.evidence

    heavier = replace(
        config,
        scoring=replace(config.scoring, recency_weight=0.01, frequency_weight=0.94),
    )
    evidence = [
        record for path_ownership in ownership.paths.values() for record in path_ownership.evidence
    ]
    prior = frequency_prior_for(config.qualification.strategy)
    with patch("checkowners.analyze.run_git", side_effect=AssertionError("git")):
        changed = rescore(
            evidence,
            scoring=heavier.scoring,
            now=ownership.last_analyzed,
            frequency_prior=prior,
            threshold_days=config.decay.threshold_days,
        )
    assert changed
    original_scores = [owner.ownership_score for owners in replayed.values() for owner in owners]
    changed_scores = [owner.ownership_score for owners in changed.values() for owner in owners]
    assert original_scores != changed_scores

    first = evidence[0]
    stubbed = replace_reviews(
        evidence,
        (
            OwnershipEvidence(
                subject=first.subject,
                path=first.path,
                type="review",
                share=1.0,
            ),
        ),
    )
    reviewed = rescore(
        stubbed,
        scoring=config.scoring,
        now=ownership.last_analyzed,
        frequency_prior=prior,
        threshold_days=config.decay.threshold_days,
    )
    assert _review_signal(reviewed[first.path])[first.subject] == 1.0
    live_signals = _scores(replayed[first.path])[first.subject][1:4]
    stub_signals = _scores(reviewed[first.path])[first.subject][1:4]
    assert live_signals == stub_signals

    teams = {"@acme/pay": frozenset({"alice"})}
    team = tuple(GitHubTeamProvider("payments/core.py", teams).collect())
    assert team[0].subject == "@acme/pay"
    assert rescore(team, scoring=config.scoring, now=_WHEN) == {}

    write_state(git_repo.path, ownership, config=config)
    loaded = load_ownership(git_repo.path)
    assert loaded is not None
    loaded_scores = rescore_map(loaded, config)
    for path, path_ownership in loaded.paths.items():
        assert path_ownership.evidence
        stored = _scores(path_ownership.owners)
        replay = _scores(loaded_scores[path])
        assert stored.keys() == replay.keys()
        for handle, values in stored.items():
            assert replay[handle] == pytest.approx(values, abs=5e-5)

    analyzer = RepositoryAnalyzer(git_repo.path, config)
    assert analyzer.reconcile()


def test_gitlab_sections_round_trip(tmp_path: Path) -> None:
    ownership = OwnershipMap(
        paths={
            "payments/core.py": PathOwnership(
                owners=(_entry("@alice", 0.9),),
                qualified_owner_count=1,
            ),
            "README.md": PathOwnership(
                owners=(_entry("@ada", 0.8),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_WHEN,
    )
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.0))
    written = generate_codeowners(
        tmp_path,
        ownership,
        config,
        dialect="gitlab",
        write=False,
    )
    rules = parse_rules(written.content, dialect="gitlab")
    matched = [rule for rule in rules if "payments" in rule.pattern]
    assert matched
    assert matched[-1].owners == ("@alice",)
    assert matched[-1].section == "payments"
    assert matched[-1].approvals == 1
    assert "[payments][1]" in written.content
    assert "[Repository][1]" in written.content
    counted = parse_rules("[Security][2]\n/src/ @ada\n", dialect="gitlab")
    assert counted[0].section == "Security"
    assert counted[0].approvals == 2
    assert counted[0].owners == ("@ada",)
    with pytest.raises(DialectError):
        require_dialect("bitbucket")
    with pytest.raises(DialectError):
        require_dialect("gerrit")
    low = OwnershipMap(
        paths={
            "docs/guide.py": PathOwnership(
                owners=(_entry("@bob", 0.1),),
                qualified_owner_count=0,
            )
        },
        last_analyzed=_WHEN,
    )
    optional = generate_codeowners(
        tmp_path,
        low,
        Config(analysis=AnalysisConfig(confidence_threshold=0.5)),
        dialect="gitlab",
        write=False,
    )
    assert "^[docs]" in optional.content
    optional_rules = parse_rules(optional.content, dialect="gitlab")
    assert optional_rules[-1].section == "docs"
    assert optional_rules[-1].optional
    assert optional_rules[-1].owners == ("@bob",)
    with pytest.raises(typer.Exit) as caught:
        _dialect_or_exit("gerrit")
    assert caught.value.exit_code == ExitCode.CONFIG
    gitlab_file = _write(tmp_path, written.content)
    findings = validate_codeowners(tmp_path, codeowners_path=gitlab_file, dialect="gitlab")
    assert findings == []


def test_reconcile_declared_and_observed_layers(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".github").mkdir(parents=True)
    (root / "payments").mkdir()
    (root / "src").mkdir()
    codeowners = root / ".github" / "CODEOWNERS"
    codeowners.write_text("* @global\n/payments/ @declared\n", encoding="utf-8")
    (root / "payments" / "OWNERS").write_text("# local\nskip\n@local\n", encoding="utf-8")
    (root / "payments" / "CODEOWNERS").write_text(
        "other @nope\n/payments/core.py @anchored\n* @nested\n",
        encoding="utf-8",
    )
    manifest = root / "payments" / "pyproject.toml"
    manifest.write_text("[project]\nname = 'payments'\n", encoding="utf-8")
    (root / "payments" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "payments" / "catalog-info.yaml").write_text(
        "spec:\n  owner: group:payments\n",
        encoding="utf-8",
    )
    (root / "payments" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (root / "payments" / "extra.py").write_text("z = 1\n", encoding="utf-8")
    (root / "src" / "main.py").write_text("y = 1\n", encoding="utf-8")
    (root / "billing").mkdir()
    (root / "billing" / "catalog-info.yaml").write_text("[\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "catalog-info.yaml").write_text("- item\n", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "ops" / "catalog-info.yaml").write_text("spec: hello\n", encoding="utf-8")
    (root / "team").mkdir()
    (root / "team" / "catalog-info.yaml").write_text(
        "spec:\n  owner: 1\n",
        encoding="utf-8",
    )
    ownership = OwnershipMap(
        paths={
            "README.md": PathOwnership(
                owners=(_entry("@root", 0.4),),
                qualified_owner_count=1,
            ),
            "payments/core.py": PathOwnership(
                owners=(_entry("@observed", 0.9),),
                qualified_owner_count=1,
            ),
            "payments/extra.py": PathOwnership(
                owners=(_entry("@observed", 0.7),),
                qualified_owner_count=1,
            ),
            "src/main.py": PathOwnership(
                owners=(_entry("@other", 0.8),),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_WHEN,
    )
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.0))
    rows = reconcile_layers(root, ownership, config)
    layers = {f"{item.level}:{item.component}": item for item in rows}
    payment = layers["file:payments/core.py"]
    assert payment.declared_local == ("@local", "@nested")
    assert payment.declared_global == ("@declared",)
    assert payment.observed == ("@observed",)
    source = layers["file:src/main.py"]
    assert source.declared_local == ()
    assert source.declared_global == ("@global",)
    assert source.observed == ("@other",)
    directory = layers["directory:payments"]
    assert directory.declared_local == ("@local", "@nested")
    assert directory.declared_global == ("@declared",)
    assert directory.observed == ("@observed",)
    package = layers["package:payments"]
    assert package.declared_local == ("@local", "@nested")
    assert package.observed == ("@observed",)
    service = layers["service:payments"]
    assert "group:payments" in service.declared_local
    for name in ("billing", "docs", "ops", "team"):
        missed = layers[f"service:{name}"]
        assert missed.declared_local == ()
        assert missed.observed == ()
    repository = layers["repository:."]
    assert repository.declared_global == ("@global",)
    assert set(repository.observed) == {"@observed", "@other", "@root"}
    empty = tmp_path / "empty"
    empty.mkdir()
    bare = reconcile_layers(empty, ownership, config)
    assert bare[0].declared_global == ()
    assert bare[-1].declared_global == ()


def test_provider_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DialectError):
        require_dialect("fossil")
    assert tuple(GitHubTeamProvider("a.py", {"@acme/empty": frozenset()}).collect()) == ()
    observed = path_evidence(
        "a.py",
        {"@ada": Contribution(commits=1, last_commit=_WHEN, credit=1.0)},
        {},
        {"@ada": 1.0},
        blame_available=False,
        review_available=True,
        survival=None,
    )
    assert [record.type for record in observed] == ["commit", "review"]
    assert _read_text(Path("missing-owners-file")) == ""
    scored = rescore(
        (
            OwnershipEvidence(subject="@ada", path="a.py", type="review"),
            OwnershipEvidence(subject="@ada", path="a.py", type="commit"),
            OwnershipEvidence(subject="@ada", path="a.py", type="blame"),
        ),
        scoring=ScoringConfig(),
        now=_WHEN,
    )
    assert scored["a.py"][0].handle == "@ada"
    assert _nested_owners(
        "payments/skip/file.py",
        (
            ("payments", (CodeownersRule("other", ("@nope",), 1),)),
            ("payments/skip", (CodeownersRule("*", ("@nested",), 2),)),
        ),
    ) == ("@nested",)
    assert evidence_from_payload("a.py", None) is None
    assert evidence_from_payload("a.py", {"type": 1, "subject": "@ada"}) is None
    assert evidence_from_payload("a.py", {"type": "note", "subject": "@ada"}) is None
    parsed = evidence_from_payload(
        "fallback.py",
        {
            "type": "blame",
            "subject": "@ada",
            "timestamp": "not-a-date",
            "weight": True,
            "commits": True,
        },
    )
    assert parsed is not None
    assert parsed.path == "fallback.py"
    assert parsed.timestamp is None
    reviewed = evidence_from_payload(
        "a.py",
        {"type": "review", "subject": "@ada", "path": "b.py", "share": "no", "later_commits": "no"},
    )
    assert reviewed is not None and reviewed.path == "b.py"
    assert _team_resolver(Config(github=GithubConfig(resolve_teams=False, org="acme"))) is None
    assert _team_resolver(Config(github=GithubConfig(resolve_teams=True, org=""))) is None
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setattr(
        "checkowners.cli.create_team_resolver",
        lambda _token, _org: lambda _owners: None,
    )
    resolved = _team_resolver(Config(github=GithubConfig(resolve_teams=True, org="acme")))
    assert resolved is not None


def _write(root: Path, content: str) -> Path:
    target = root / "CODEOWNERS"
    target.write_text(content, encoding="utf-8")
    return target

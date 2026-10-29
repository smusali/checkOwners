"""Future-behavior metrics, weight fit, and the false-positive ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners.analyze import signal_reliabilities
from checkowners.cli import app
from checkowners.config import load_config
from checkowners.evaluation import (
    MIN_FIT_HISTORY_DAYS,
    MIN_FIT_LABELED_PATHS,
    Candidate,
    PathCase,
    collect_cases,
    divergence_warning,
    fit_weights,
    format_badges,
    holdout_ids,
    ledger_problems,
    load_corpus,
    measure,
    rank_by_weights,
    should_replace_defaults,
    split_time,
    write_fitted_config,
)
from checkowners.models import CalibrationProvenance, ScoringConfig
from tests.conftest import GitRepo

_ROOT = Path(__file__).resolve().parents[1]
_RELIABILITIES = signal_reliabilities(ScoringConfig())
_RECENCY = {
    "recency": 1.0,
    "frequency": 0.0,
    "blame": 0.0,
    "review": 0.0,
    "change_survival": 0.0,
}


def _signals(
    recency: float = 0.0,
    frequency: float = 0.0,
    blame: float = 0.0,
    review: float = 0.0,
    survival: float = 0.0,
) -> tuple[tuple[float, bool], ...]:
    return (
        (recency, True),
        (frequency, True),
        (blame, True),
        (review, True),
        (survival, True),
    )


def _case(
    path: str,
    candidates: tuple[Candidate, ...],
    positive: frozenset[str],
    *,
    days: int = 1,
    authors: tuple[str, ...] | None = None,
) -> PathCase:
    cutoff = datetime(2024, 6, 1, tzinfo=UTC)
    ordered = tuple(sorted(positive)) if authors is None else authors
    return PathCase(
        repo_id="fixture",
        path=path,
        positive=positive,
        candidates=candidates,
        future_commits=tuple((author, cutoff + timedelta(days=days)) for author in ordered),
        cutoff=cutoff,
    )


def _rank(case: PathCase) -> tuple[tuple[str, float], ...]:
    return rank_by_weights(case, _RECENCY, _RELIABILITIES)


def test_metrics_match_hand_computed_values() -> None:
    alice = Candidate("alice@example.com", _signals(recency=1.0), commits=2)
    bob = Candidate("bob@example.com", _signals(recency=0.4), commits=1)
    cases = (
        _case("a.py", (alice, bob), frozenset({"alice@example.com"})),
        _case(
            "b.py",
            (bob,),
            frozenset({"alice@example.com", "bob@example.com"}),
            authors=("bob@example.com", "alice@example.com"),
        ),
    )
    metrics = measure(cases, _rank)
    assert metrics.precision_at_1 == pytest.approx(1.0)
    assert metrics.precision_at_3 == pytest.approx(1 / 3)
    assert metrics.precision_at_5 == pytest.approx(0.2)
    assert metrics.recall_at_3 == pytest.approx(0.75)
    assert metrics.mrr == pytest.approx(1.0)
    assert metrics.ndcg == pytest.approx(1.0)
    assert metrics.brier == pytest.approx(0.18)
    assert metrics.calibration_error == pytest.approx(0.3)
    assert metrics.changed_file_coverage == pytest.approx(1.0)
    assert metrics.acceptance_rate == pytest.approx(1.0)
    assert metrics.rejection_rate == pytest.approx(0.0)
    assert metrics.handoff_days is None
    assert metrics.recommendation_correct == 2
    assert metrics.recommendation_wrong == 0


def test_miss_reports_rank_and_handoff() -> None:
    alice = Candidate("alice@example.com", _signals(recency=1.0), commits=1)
    bob = Candidate("bob@example.com", _signals(recency=0.2), commits=4)
    cutoff = datetime(2024, 6, 1, tzinfo=UTC)
    case = PathCase(
        repo_id="fixture",
        path="a.py",
        positive=frozenset({"bob@example.com"}),
        candidates=(alice, bob),
        future_commits=(("bob@example.com", cutoff + timedelta(days=5)),),
        cutoff=cutoff,
    )
    metrics = measure((case,), _rank)
    assert metrics.precision_at_1 == pytest.approx(0.0)
    assert metrics.mrr == pytest.approx(0.5)
    assert metrics.ndcg == pytest.approx(1 / math_log2_3())
    assert metrics.brier == pytest.approx(1.0)
    assert metrics.acceptance_rate == pytest.approx(0.0)
    assert metrics.rejection_rate == pytest.approx(1.0)
    assert metrics.false_positive_rate == pytest.approx(1.0)
    assert metrics.stale_ownership_rate == pytest.approx(1.0)
    assert metrics.handoff_days == pytest.approx(5.0)


def test_fit_improves_held_out_when_blame_matches_labels() -> None:
    cases = tuple(_blame_case(index) for index in range(10))
    training, held = split_time(cases)
    defaults = {
        "recency": 0.30,
        "frequency": 0.20,
        "blame": 0.20,
        "review": 0.15,
        "change_survival": 0.15,
    }
    before = measure(held, lambda case: rank_by_weights(case, defaults, _RELIABILITIES))
    fitted = fit_weights(training, _RELIABILITIES)
    after = measure(held, lambda case: rank_by_weights(case, fitted, _RELIABILITIES))
    assert after.precision_at_1 >= before.precision_at_1 + 0.02
    assert should_replace_defaults(after.precision_at_1, before.precision_at_1)


def test_holdout_repositories_are_disjoint() -> None:
    identifiers = [f"repo-{index:02d}" for index in range(20)]
    held = holdout_ids(identifiers)
    training = set(identifiers) - held
    assert held.isdisjoint(training)
    assert len(held) == 5
    assert "repo-03" in held
    assert "repo-00" in training


def test_prediction_excludes_later_authors(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"foo.py": "one\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2024-01-01T00:00:00+00:00",
    )
    git_repo.commit_files(
        {"foo.py": "one\ntwo\n"},
        "edit",
        author="Alice",
        email="alice@example.com",
        date="2024-06-01T00:00:00+00:00",
    )
    git_repo.commit_files(
        {"foo.py": "one\ntwo\nthree\n"},
        "later",
        author="Carol",
        email="carol@example.com",
        date="2024-12-01T00:00:00+00:00",
    )
    cases = collect_cases(git_repo.path, load_config(git_repo.path))
    handles = {candidate.handle for case in cases for candidate in case.candidates}
    assert "carol@example.com" not in handles
    assert any("carol@example.com" in case.positive for case in cases)


def test_fit_refuses_short_history(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    git_repo.commit_files(
        {"foo.py": "one\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2024-06-01T00:00:00+00:00",
    )
    monkeypatch.chdir(git_repo.path)
    result = CliRunner().invoke(app, ["calibrate", "--fit"])
    assert result.exit_code == 2
    assert str(MIN_FIT_HISTORY_DAYS) in result.output
    assert str(MIN_FIT_LABELED_PATHS) in result.output


def test_calibration_json_omits_training_metrics(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_repo.commit_files(
        {"foo.py": "one\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2024-01-01T00:00:00+00:00",
    )
    git_repo.commit_files(
        {"foo.py": "one\ntwo\n"},
        "later",
        author="Bob",
        email="bob@example.com",
        date="2024-06-01T00:00:00+00:00",
    )
    monkeypatch.chdir(git_repo.path)
    result = CliRunner().invoke(app, ["calibrate", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "training" not in payload
    assert "held_out" in payload
    assert payload["review_metrics"] == "unavailable"


def test_fitted_config_records_provenance_and_keeps_comments(git_repo: GitRepo) -> None:
    config_path = git_repo.path / ".github" / "checkowners.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "version: 1\n# keep me\nscoring:\n"
        "  recency_weight: 0.30\n  frequency_weight: 0.20\n"
        "  blame_weight: 0.20\n  review_weight: 0.15\n"
        "  change_survival_weight: 0.15\n",
        encoding="utf-8",
    )
    weights = {
        "recency": 0.05,
        "frequency": 0.05,
        "blame": 0.80,
        "review": 0.05,
        "change_survival": 0.05,
    }
    provenance = CalibrationProvenance(
        cutoff="2024-06-01T00:00:00+00:00",
        train_paths=24,
        held_out_paths=6,
        history_days=200,
        weights=tuple(weights.items()),
        default_weights=(
            ("blame", 0.20),
            ("change_survival", 0.15),
            ("frequency", 0.20),
            ("recency", 0.30),
            ("review", 0.15),
        ),
        held_out_precision_at_1=0.5,
        held_out_precision_at_3=0.4,
        held_out_recall_at_3=0.4,
        held_out_mrr=0.5,
        held_out_brier=0.2,
    )
    write_fitted_config(git_repo.path, weights, provenance)
    text = config_path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "blame_weight: 0.80" in text
    loaded = load_config(git_repo.path)
    assert loaded.scoring.blame_weight == pytest.approx(0.80)
    assert loaded.calibration is not None
    assert loaded.calibration.held_out_paths == 6
    assert loaded.calibration.cutoff == "2024-06-01T00:00:00+00:00"
    assert divergence_warning(
        weights,
        {
            "recency": 0.30,
            "frequency": 0.20,
            "blame": 0.20,
            "review": 0.15,
            "change_survival": 0.15,
        },
    )


def test_badge_line() -> None:
    assert format_badges(0.98, 2, 0) == (
        "Ownership coverage: 98%   Critical knowledge risk: 2 paths   CODEOWNERS drift: 0"
    )


def test_ledger_entries_are_accountable() -> None:
    assert ledger_problems(_ROOT) == ()


def test_published_corpus_is_pinned() -> None:
    manifest = _ROOT / "benchmarks" / "validation_corpus.json"
    repos = load_corpus(manifest)
    dataset = load_corpus(manifest, key="dataset")
    assert len(repos) >= 20
    assert len(dataset) >= 100
    held = holdout_ids([repo.id for repo in repos])
    training = {repo.id for repo in repos} - held
    assert held
    assert not held & training


def test_published_results_keep_defaults() -> None:
    payload = json.loads((_ROOT / "benchmarks" / "validation_results.json").read_text())
    assert payload["variance_band"] == "exact"
    assert payload["defaults_replaced"] is False
    assert payload["model"] == "ownership-v2"
    assert payload["held_out"]["precision_at_1"] == 0.3105
    assert payload["candidate_held_out_precision_at_1"] == 0.3039
    assert payload["review_metrics"] == "unavailable"
    assert "training" not in payload


def test_dataset_checksums_cover_the_corpus() -> None:
    text = (_ROOT / "benchmarks" / "dataset_checksums.txt").read_text()
    html = [line for line in text.splitlines() if line.endswith(".html")]
    findings = json.loads((_ROOT / "benchmarks" / "dataset_findings.json").read_text())
    assert len(html) >= 100
    assert findings["succeeded"] >= 100
    assert findings["failed"] == 0
    assert findings["audits"] == 50


def test_fit_on_blame_history_beats_defaults(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = {f"pkg/file{index}.py": "alpha\nbeta\n" for index in range(30)}
    edited = dict.fromkeys(created, "alpha\nbeta2\n")
    later = dict.fromkeys(created, "alpha2\nbeta2\n")
    git_repo.commit_files(
        created,
        "create",
        author="Blame",
        email="aaa-blame@example.com",
        date="2024-01-01T00:00:00+00:00",
    )
    git_repo.commit_files(
        edited,
        "touch",
        author="Recency",
        email="zzz-recency@example.com",
        date="2024-10-27T00:00:00+00:00",
    )
    git_repo.commit_files(
        later,
        "return",
        author="Blame",
        email="aaa-blame@example.com",
        date="2024-12-01T00:00:00+00:00",
    )
    config_path = git_repo.path / ".github" / "checkowners.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "version: 1\n# keep me\ngit:\n  mass_refactor_file_fraction: 0\n"
        "analysis:\n  lookback_days: 365\n",
        encoding="utf-8",
    )
    before = measure(
        split_time(collect_cases(git_repo.path, load_config(git_repo.path)))[1],
        lambda case: rank_by_weights(
            case,
            {
                "recency": 0.30,
                "frequency": 0.20,
                "blame": 0.20,
                "review": 0.15,
                "change_survival": 0.15,
            },
            _RELIABILITIES,
        ),
    )
    monkeypatch.chdir(git_repo.path)
    result = CliRunner().invoke(app, ["calibrate", "--fit", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "training" not in payload
    held_out = payload["held_out"]
    assert isinstance(held_out, dict)
    assert float(held_out["precision_at_1"]) >= before.precision_at_1 + 0.02
    text = config_path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "calibration:" in text
    loaded = load_config(git_repo.path)
    assert loaded.calibration is not None
    assert loaded.calibration.history_days >= MIN_FIT_HISTORY_DAYS


def _blame_case(index: int) -> PathCase:
    cutoff = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)
    blame = Candidate("aaa-blame@example.com", _signals(blame=1.0), commits=1)
    recency = Candidate("zzz-recency@example.com", _signals(recency=1.0), commits=1)
    return PathCase(
        repo_id="fixture",
        path=f"file{index}.py",
        positive=frozenset({"aaa-blame@example.com"}),
        candidates=(recency, blame),
        future_commits=(("aaa-blame@example.com", cutoff + timedelta(days=1)),),
        cutoff=cutoff,
    )


def math_log2_3() -> float:
    return 1.5849625007211563

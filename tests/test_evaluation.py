"""Future-behavior metrics, weight fit, and the false-positive ledger."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
import warnings
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners import evaluation
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
from checkowners.html_report import ReportDocument
from checkowners.models import (
    OWNERSHIP_SIGNALS,
    CalibrationProvenance,
    ConfidenceScore,
    Config,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    ScoringConfig,
    SignalScore,
)
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


def _provenance() -> CalibrationProvenance:
    weights = (
        ("blame", 0.20),
        ("change_survival", 0.15),
        ("frequency", 0.20),
        ("recency", 0.30),
        ("review", 0.15),
    )
    return CalibrationProvenance(
        cutoff="2024-06-01T00:00:00+00:00",
        train_paths=24,
        held_out_paths=6,
        history_days=200,
        weights=weights,
        default_weights=weights,
        held_out_precision_at_1=0.5,
        held_out_precision_at_3=0.4,
        held_out_recall_at_3=0.4,
        held_out_mrr=0.5,
        held_out_brier=0.2,
    )


def _corpus_row(repo_id: str, url: str) -> dict[str, str]:
    return {
        "id": repo_id,
        "url": url,
        "sha": "a" * 40,
        "repository_type": "library",
        "language": "Python",
        "monorepo_scale": "single",
        "merge_strategy": "merge",
        "turnover": "low",
    }


def _write_manifest(path: Path, rows: list[dict[str, str]], *, key: str) -> None:
    path.write_text(json.dumps({key: rows}), encoding="utf-8")


def _held_case() -> PathCase:
    cutoff = datetime(2024, 6, 1, tzinfo=UTC)
    unavailable = (
        (0.0, False),
        (0.2, True),
        (0.0, False),
        (0.0, True),
        (0.1, True),
    )
    return PathCase(
        repo_id="d",
        path="kept.py",
        positive=frozenset({"later@example.com"}),
        candidates=(Candidate("later@example.com", unavailable, commits=2),),
        future_commits=(("later@example.com", cutoff + timedelta(days=3)),),
        cutoff=cutoff,
        dimensions=(("language", "Python"), ("repository_type", "")),
    )


def _report() -> ReportDocument:
    return ReportDocument(
        head_sha="abc",
        generated_at="2026-01-01T00:00:00+00:00",
        analysis_completeness=None,
        models=(),
        overview=(),
        files=(),
        timeline=(),
        comparisons=(),
        clusters=(),
        teams_reason="",
        ownership_coverage=0.5,
        critical_components=2,
        drift_count=1,
        finding="kept.py is critical: one owner",
    )


def _ledger_root(tmp_path: Path, payload: object, *, page: str = "", tests: str = "") -> Path:
    (tmp_path / "benchmarks").mkdir(exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "benchmarks" / "false_positives.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    if page:
        (tmp_path / "docs" / "false-positives.md").write_text(page, encoding="utf-8")
    if tests:
        (tmp_path / "tests" / "test_golden.py").write_text(tests, encoding="utf-8")
    return tmp_path


def _ledger_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "sample",
        "prediction": "alice",
        "judgment": "bob",
        "reason": "later edits",
        "fix": "keep alice",
        "category": "recommendation_wrong",
        "status": "fixed",
        "evidence": "the golden test",
        "test_id": "test_golden_formatting",
    }
    entry.update(overrides)
    return entry


def test_metrics_cover_empty_and_boundary_scores() -> None:
    assert measure((), lambda _case: ()).labeled_paths == 0
    cutoff = datetime(2024, 1, 1, tzinfo=UTC)
    empty_rank = PathCase(
        repo_id="fixture",
        path="empty.py",
        positive=frozenset({"gone@example.com"}),
        candidates=(),
        future_commits=(("gone@example.com", cutoff + timedelta(days=2)),),
        cutoff=cutoff,
    )
    unlabeled = PathCase(
        repo_id="fixture",
        path="blank.py",
        positive=frozenset(),
        candidates=(Candidate("a@example.com", _signals(recency=1.0), commits=1),),
        future_commits=(),
        cutoff=cutoff,
    )
    perfect = PathCase(
        repo_id="fixture",
        path="perfect.py",
        positive=frozenset({"a@example.com"}),
        candidates=(Candidate("a@example.com", _signals(recency=1.0), commits=2),),
        future_commits=(("a@example.com", cutoff + timedelta(days=1)),),
        cutoff=cutoff,
    )
    metrics = measure(
        (empty_rank, unlabeled, perfect),
        lambda case: (("a@example.com", 1.0),) if case.candidates else (),
    )
    assert metrics.predicted_paths == 2
    assert metrics.recommendation_wrong == 2
    short = Candidate("b@example.com", ((1.0, True), (0.0, False)), commits=1)
    assert evaluation.score_candidate(short, _RECENCY, _RELIABILITIES) == pytest.approx(1.0)
    assert evaluation._precision_only((), _RECENCY, _RELIABILITIES) == 0.0
    assert evaluation._mean(()) == 0.0
    unavailable = Candidate(
        "c@example.com",
        ((0.0, False), (0.2, True), (0.0, False), (0.0, True), (0.1, True)),
        commits=1,
    )
    assert evaluation._blame_score(unavailable) == -1.0
    assert evaluation._recency_score(unavailable) == -1.0
    assert evaluation._blame_score(Candidate("d@example.com", _signals(blame=0.4), 1)) == 0.4


def test_calibration_text_bins_and_divergence() -> None:
    metrics = measure((), lambda _case: ())
    weights = {
        "recency": 0.30,
        "frequency": 0.20,
        "blame": 0.20,
        "review": 0.15,
        "change_survival": 0.15,
    }
    warned = evaluation.CalibrationRun(
        cutoff="2024-06-01T00:00:00+00:00",
        history_days=200,
        weights=weights,
        fitted=True,
        warning="weights moved",
        held_out=metrics,
        baselines=(),
    )
    text = evaluation.format_calibration_text(warned)
    assert "warning: weights moved" in text
    assert "wrote fitted weights" in text
    plain = replace(warned, fitted=False, warning=None)
    plain_text = evaluation.format_calibration_text(plain)
    assert "warning:" not in plain_text
    assert "wrote fitted weights" not in plain_text
    assert evaluation.divergence_warning(weights, weights) is None
    assert evaluation.is_calibrated(()) is False
    near = evaluation.CalibrationBin(0.0, 0.1, 1, 0.50, 0.55)
    far = evaluation.CalibrationBin(0.0, 0.1, 1, 0.10, 0.90)
    blank = evaluation.CalibrationBin(0.0, 0.1, 1, None, 0.50)
    assert evaluation.is_calibrated((near,)) is True
    assert evaluation.is_calibrated((far,)) is False
    assert evaluation.is_calibrated((blank,)) is False


def test_collect_cases_skips_excluded_and_unscored_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later = datetime(2024, 12, 1, tzinfo=UTC)
    scored = OwnerEntry(
        "kept@example.com",
        0.4,
        later,
        2,
        score_breakdown=ConfidenceScore(
            total=0.4,
            recency=SignalScore(True, 0.4),
            frequency=SignalScore(True, 0.4),
            blame=SignalScore(True, 0.4),
            review=SignalScore(False),
            change_survival=SignalScore(False),
        ),
    )
    blank = OwnerEntry("blank@example.com", 0.1, None, 1)
    ownership = OwnershipMap(
        paths={
            "kept.py": PathOwnership(
                owners=(scored,), qualified_owner_count=1, candidates=(blank, scored)
            ),
            "owners.py": PathOwnership(owners=(scored,), qualified_owner_count=1),
        },
        last_analyzed=later,
    )

    def fake_analyze(*_args: object, **_kwargs: object) -> OwnershipMap:
        return ownership

    def fake_instants(_root: Path) -> tuple[tuple[str, datetime], ...]:
        return (
            ("a" * 40, datetime(2024, 1, 1, tzinfo=UTC)),
            ("b" * 40, later),
        )

    def fake_future(
        _root: Path,
        _sha: str,
        *,
        use_mailmap: bool,
    ) -> dict[str, list[tuple[str, datetime]]]:
        del use_mailmap
        return {
            "skip/file.py": [("kept@example.com", later)],
            "empty.py": [("", later)],
            "missing.py": [("kept@example.com", later)],
            "kept.py": [("kept@example.com", later)],
            "owners.py": [("kept@example.com", later)],
        }

    monkeypatch.setattr(evaluation, "analyze_ownership", fake_analyze)
    monkeypatch.setattr(evaluation, "_commit_instants", fake_instants)
    monkeypatch.setattr(evaluation, "_future_paths", fake_future)
    config = replace(Config(), paths=replace(Config().paths, exclude=("skip/*",)))
    cases = collect_cases(tmp_path, config, repo_id="fixture")
    assert [case.path for case in cases] == ["kept.py", "missing.py", "owners.py"]
    kept = cases[0]
    assert len(kept.candidates) == 1
    assert kept.candidates[0].handle == "kept@example.com"
    assert cases[1].candidates == ()
    assert evaluation.candidate_from_owner(blank) is None


def test_git_history_parsers_skip_blank_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_git(
        _root: Path,
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if "log" in args and "--name-only" in args:
            stdout = (
                "\x1e\n"
                "\x1enotab\nfile.py\n"
                "\x1e\t2024-06-01T00:00:00+00:00\norphan.py\n"
                "\x1ealice@example.com\t2024-06-01T00:00:00\n  kept.py  \n\n"
            )
        else:
            stdout = "\n\t2024-01-01T00:00:00+00:00\nabc\t2024-01-01T00:00:00\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(evaluation, "run_git", fake_git)
    instants = evaluation._commit_instants(tmp_path)
    assert instants[0][0] == "abc"
    assert instants[0][1].tzinfo is UTC
    future = evaluation._future_paths(tmp_path, "abc", use_mailmap=True)
    assert future == {"kept.py": [("alice@example.com", datetime(2024, 6, 1, tzinfo=UTC))]}


def test_corpus_manifest_rejects_bad_rows(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.json"
    manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        load_corpus(manifest)
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing repositories"):
        load_corpus(manifest)
    row = _corpus_row("acme/lib", "https://example.invalid/lib.git")
    manifest.write_text(json.dumps({"repositories": ["nope"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        load_corpus(manifest)
    broken = dict(row)
    broken["language"] = "  "
    manifest.write_text(json.dumps({"repositories": [broken]}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing language"):
        load_corpus(manifest)
    numeric: dict[str, object] = {**row, "language": 1}
    manifest.write_text(json.dumps({"repositories": [numeric]}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing language"):
        load_corpus(manifest)
    short = dict(row)
    short["sha"] = "abc"
    manifest.write_text(json.dumps({"repositories": [short]}), encoding="utf-8")
    with pytest.raises(ValueError, match="full commit id"):
        load_corpus(manifest)
    repo = load_corpus(
        _write_loaded(tmp_path / "ok.json", {"repositories": [row]}),
    )[0]
    assert repo.dimensions()[1] == ("language", "Python")


def _write_loaded(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_materialize_clones_once(git_repo: GitRepo, tmp_path: Path) -> None:
    sha = git_repo.commit_files(
        {"a.py": "value = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2024-01-01T00:00:00+00:00",
    )
    repo = evaluation.CorpusRepo(
        id="acme/lib",
        url=str(git_repo.path),
        sha=sha,
        repository_type="library",
        language="Python",
        monorepo_scale="single",
        merge_strategy="merge",
        turnover="low",
    )
    checkout = tmp_path / "checkout"
    first = evaluation.materialize(repo, checkout)
    second = evaluation.materialize(repo, checkout)
    assert first == second
    assert (first / "a.py").is_file()


def test_run_study_reports_held_out_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "manifest.json"
    rows = [
        _corpus_row(repo_id, "https://example.invalid/repo.git") for repo_id in ("a", "b", "c", "d")
    ]
    _write_manifest(manifest, rows, key="repositories")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def fake_materialize(repo: evaluation.CorpusRepo, _checkout: Path) -> Path:
        del repo
        return checkout

    def held_only(
        _root: Path,
        _config: Config,
        *,
        repo_id: str = "",
        dimensions: tuple[tuple[str, str], ...] = (),
    ) -> tuple[PathCase, ...]:
        del dimensions
        if repo_id != "d":
            return ()
        return (_held_case(),)

    monkeypatch.setattr(evaluation, "materialize", fake_materialize)
    monkeypatch.setattr(evaluation, "collect_cases", held_only)
    payload = evaluation.run_study(manifest, checkout, tmp_path / "results.json")
    assert payload["held_out_ids"] == ["d"]
    assert payload["defaults_replaced"] is False
    breakouts = payload["breakouts"]
    assert isinstance(breakouts, dict)
    languages = breakouts["language"]
    assert isinstance(languages, dict)
    assert languages["Python"]["count"] == 1

    def nothing(
        _root: Path,
        _config: Config,
        *,
        repo_id: str = "",
        dimensions: tuple[tuple[str, str], ...] = (),
    ) -> tuple[PathCase, ...]:
        del repo_id, dimensions
        return ()

    monkeypatch.setattr(evaluation, "collect_cases", nothing)
    with pytest.raises(ValueError, match="no labeled paths"):
        evaluation.run_study(manifest, checkout, tmp_path / "empty.json")

    def both_slices(
        _root: Path,
        _config: Config,
        *,
        repo_id: str = "",
        dimensions: tuple[tuple[str, str], ...] = (),
    ) -> tuple[PathCase, ...]:
        del dimensions
        if repo_id == "a":
            return (replace(_held_case(), repo_id="a"),)
        if repo_id == "d":
            return (_held_case(),)
        return ()

    def fake_fit(
        cases: tuple[PathCase, ...],
        reliabilities: Mapping[str, float],
    ) -> dict[str, float]:
        del cases, reliabilities
        return dict.fromkeys(OWNERSHIP_SIGNALS, 0.2)

    monkeypatch.setattr(evaluation, "collect_cases", both_slices)
    monkeypatch.setattr(evaluation, "fit_weights", fake_fit)
    monkeypatch.setattr(evaluation, "should_replace_defaults", lambda _candidate, _current: True)
    replaced = evaluation.run_study(manifest, checkout, tmp_path / "replaced.json")
    assert replaced["defaults_replaced"] is True


def test_fit_refuses_an_empty_holdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation, "history_span_days", lambda _root: 200)
    monkeypatch.setattr(evaluation, "collect_cases", lambda *_args, **_kwargs: (_blame_case(0),))
    with pytest.raises(evaluation.InsufficientHistoryError, match="1 labeled paths"):
        evaluation.calibrate_repository(tmp_path, Config(), fit=True)
    monkeypatch.setattr(evaluation, "MIN_FIT_HISTORY_DAYS", 0)
    monkeypatch.setattr(evaluation, "MIN_FIT_LABELED_PATHS", 1)
    monkeypatch.setattr(evaluation, "history_span_days", lambda _root: 10)
    with pytest.raises(evaluation.InsufficientHistoryError, match="held-out slice is empty"):
        evaluation.calibrate_repository(tmp_path, Config(), fit=True)
    monkeypatch.setattr(
        evaluation,
        "split_time",
        lambda cases: (cases, ()),
    )
    monkeypatch.setattr(
        evaluation,
        "collect_cases",
        lambda *_args, **_kwargs: (_blame_case(0), _blame_case(1)),
    )
    with pytest.raises(evaluation.InsufficientHistoryError, match="held-out slice is empty"):
        evaluation.calibrate_repository(tmp_path, Config(), fit=True)
    monkeypatch.setattr(evaluation, "collect_cases", lambda *_args, **_kwargs: ())
    empty = evaluation.calibrate_repository(tmp_path, Config(), fit=False)
    assert empty.cutoff == ""
    assert empty.fitted is False


def test_publish_dataset_counts_success_failure_and_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "dataset.json"
    rows = [
        _corpus_row("good/repo", "https://example.invalid/good.git"),
        _corpus_row("bad/repo", "https://example.invalid/bad.git"),
    ]
    _write_manifest(manifest, rows, key="dataset")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def fake_materialize(repo: evaluation.CorpusRepo, _checkout: Path) -> Path:
        if repo.id == "bad/repo":
            raise subprocess.CalledProcessError(128, ["git", "clone"])
        return checkout

    monkeypatch.setattr(evaluation, "materialize", fake_materialize)
    monkeypatch.setattr(evaluation, "build_document", lambda _root: _report())
    monkeypatch.setattr(evaluation, "render_report", lambda *_args, **_kwargs: "<html></html>")
    output = tmp_path / "out"
    (output).mkdir()
    (output / ".DS_Store").write_text("junk", encoding="utf-8")
    findings = evaluation.publish_dataset(manifest, checkout, output, limit=1)
    assert findings == {
        "repositories": 1,
        "succeeded": 1,
        "failed": 0,
        "critical_path_repositories": 1,
        "audits": 1,
    }
    assert (output / "audits.jsonl").read_text(encoding="utf-8").endswith("\n")
    assert ".DS_Store" not in (output / "checksums.txt").read_text(encoding="utf-8")

    failed = tmp_path / "failed"
    findings = evaluation.publish_dataset(manifest, checkout, failed)
    assert findings["succeeded"] == 1
    assert findings["failed"] == 1
    assert findings["critical_path_repositories"] == 1

    def always_fail(repo: evaluation.CorpusRepo, _checkout: Path) -> Path:
        del repo
        raise OSError("offline")

    many = [_corpus_row(f"repo-{index}", "https://example.invalid/r.git") for index in range(51)]
    _write_manifest(manifest, many, key="dataset")
    monkeypatch.setattr(
        evaluation,
        "build_document",
        lambda _root: replace(_report(), critical_components=0),
    )
    capped = tmp_path / "capped"
    capped_findings = evaluation.publish_dataset(manifest, checkout, capped)
    assert capped_findings["succeeded"] == 51
    assert capped_findings["audits"] == 50
    assert capped_findings["critical_path_repositories"] == 0

    monkeypatch.setattr(evaluation, "materialize", always_fail)
    empty = tmp_path / "empty-audits"
    findings = evaluation.publish_dataset(manifest, checkout, empty, limit=1)
    assert findings["failed"] == 1
    assert findings["audits"] == 0
    assert (empty / "audits.jsonl").read_text(encoding="utf-8") == ""


def test_checksum_tree_skips_noise(tmp_path: Path) -> None:
    assert evaluation.checksum_tree(tmp_path) == ""
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "keep.txt").write_text("y", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("z", encoding="utf-8")
    text = evaluation.checksum_tree(tmp_path, skip={"skip.txt"})
    assert "keep.txt" in text
    assert "skip.txt" not in text
    assert ".DS_Store" not in text


def test_repository_badges_use_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation, "build_document", lambda _root: _report())
    assert evaluation.repository_badges(tmp_path) == (
        "Ownership coverage: 50%   Critical knowledge risk: 2 paths   CODEOWNERS drift: 1"
    )


def test_ledger_rejects_bad_entries(tmp_path: Path) -> None:
    assert evaluation.ledger_problems(_ledger_root(tmp_path, [])) == (
        "ledger entries must be a list",
    )
    assert evaluation.ledger_problems(_ledger_root(tmp_path, {"entries": "no"})) == (
        "ledger entries must be a list",
    )
    assert evaluation.ledger_problems(_ledger_root(tmp_path, {"entries": []})) == (
        "ledger has no entries",
    )
    page = "sample\n"
    tests = "def test_golden_formatting(\n"
    payload = {
        "entries": [
            "not-an-object",
            {"id": 1},
            _ledger_entry(
                prediction="same",
                judgment="same",
                evidence="same",
                reason="  ",
                category="nope",
                status="closed",
            ),
            _ledger_entry(id="unlisted", status="fixed", test_id="missing_test"),
            _ledger_entry(id="open-item", status="open", issue_url="  "),
            _ledger_entry(id="sample", status="open", issue_url="https://example.invalid/1"),
            _ledger_entry(id="open-number", status="open", issue_url=1),
        ]
    }
    problems = "\n".join(
        evaluation.ledger_problems(_ledger_root(tmp_path, payload, page=page, tests=tests))
    )
    assert "ledger entry must be an object" in problems
    assert "<missing> is missing prediction" in problems
    assert "sample is missing reason" in problems
    assert "sample judgment overwrites the prediction" in problems
    assert "sample judgment overwrites the evidence" in problems
    assert "sample has an unknown category" in problems
    assert "sample status must be fixed or open" in problems
    assert "unlisted fixed entry has no test" in problems
    assert "unlisted is missing from the public ledger page" in problems
    assert "open-item open entry has no issue url" in problems


def test_weight_editor_covers_nested_and_missing_files(tmp_path: Path) -> None:
    weights = {
        "recency": 0.30,
        "frequency": 0.20,
        "blame": 0.80,
        "review": 0.15,
        "change_survival": 0.15,
    }
    provenance = _provenance()
    created = write_fitted_config(tmp_path, weights, provenance)
    text = created.read_text(encoding="utf-8")
    assert text.startswith("version: 1\n")
    assert "blame_weight: 0.80" in text
    assert "calibration:" in text

    nested = tmp_path / "nested"
    config_dir = nested / ".github"
    config_dir.mkdir(parents=True)
    (config_dir / "checkowners.yml").write_text(
        "version: 1\n"
        "model:\n"
        "  signals:\n"
        "    reviews:\n"
        "      weight: 0.10 # note\n"
        "    recency:\n"
        "      weight: 0.10\n",
        encoding="utf-8",
    )
    write_fitted_config(nested, weights, provenance)
    nested_text = (config_dir / "checkowners.yml").read_text(encoding="utf-8")
    assert "weight: 0.15 # note" in nested_text
    assert "weight: 0.30" in nested_text

    rewritten = evaluation._replace_weights(
        "\n".join(
            [
                "recency_weight:",
                "frequency_weight: 0.20 # tuned",
                "foo bar:",
                "# reviews:",
                "reviews:",
                "  weight: 0.10",
                "scoring:",
                "  weight: 0.20",
            ]
        ),
        weights,
    )
    assert "frequency_weight: 0.20 # tuned" in rewritten
    assert "weight: 0.15" in rewritten
    no_newline = evaluation._replace_weights("version: 1", weights)
    assert "scoring:" in no_newline
    assert no_newline.endswith("\n")
    flat = evaluation._replace_weights("recency_weight: 0.10", weights)
    assert flat.startswith("recency_weight: 0.30")
    block = evaluation._replace_block(
        f"keep\n{evaluation.CALIBRATION_START}\nold\n{evaluation.CALIBRATION_END}", "BLOCK\n"
    )
    assert block == "keep\nBLOCK\n"
    assert evaluation._replace_block("version: 1", "BLOCK\n") == "version: 1\nBLOCK\n"


def test_main_dispatches_study_dataset_and_badges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    output = tmp_path / "out.json"
    seen: dict[str, int | None] = {}

    def fake_study(_manifest: Path, _checkout: Path, _output: Path) -> dict[str, object]:
        return {"ok": True}

    def fake_dataset(
        _manifest: Path,
        _checkout: Path,
        _output: Path,
        *,
        limit: int | None = None,
    ) -> dict[str, object]:
        seen["limit"] = limit
        return {}

    monkeypatch.setattr(evaluation, "run_study", fake_study)
    monkeypatch.setattr(evaluation, "publish_dataset", fake_dataset)
    monkeypatch.setattr(evaluation, "repository_badges", lambda _root: "Ownership coverage: 100%")
    study_args = [
        "study",
        "--manifest",
        str(manifest),
        "--checkout",
        str(checkout),
        "--output",
        str(output),
    ]
    assert evaluation.main(study_args) == 0
    assert (
        evaluation.main(
            [
                "dataset",
                "--manifest",
                str(manifest),
                "--checkout",
                str(checkout),
                "--output",
                str(output),
                "--limit",
                "2",
            ]
        )
        == 0
    )
    assert seen["limit"] == 2
    assert evaluation.main(["badges", "--repo", str(tmp_path)]) == 0
    assert "Ownership coverage: 100%" in capsys.readouterr().out
    readme = tmp_path / "README.md"
    readme.write_text("prefix Ownership coverage: 100% suffix\n", encoding="utf-8")
    assert evaluation.main(["badges", "--repo", str(tmp_path), "--check", str(readme)]) == 0
    readme.write_text("no badge\n", encoding="utf-8")
    assert evaluation.main(["badges", "--repo", str(tmp_path), "--check", str(readme)]) == 1
    assert "Ownership coverage: 100%" in capsys.readouterr().out

    def broken(_manifest: Path, _checkout: Path, _output: Path) -> dict[str, object]:
        msg = "boom"
        raise ValueError(msg)

    monkeypatch.setattr(evaluation, "run_study", broken)
    assert evaluation.main(study_args) == 1
    assert "boom" in capsys.readouterr().out


def test_calibrate_text_prints_warning_and_report_failure_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = measure((), lambda _case: ())
    weights = {
        "recency": 0.30,
        "frequency": 0.20,
        "blame": 0.20,
        "review": 0.15,
        "change_survival": 0.15,
    }
    run = evaluation.CalibrationRun(
        cutoff="2024-06-01T00:00:00+00:00",
        history_days=200,
        weights=weights,
        fitted=True,
        warning="weights moved",
        held_out=metrics,
        baselines=(),
    )
    monkeypatch.setattr("checkowners.cli.calibrate_repository", lambda *_args, **_kwargs: run)
    warned = CliRunner().invoke(app, ["calibrate"])
    assert warned.exit_code == 0, warned.output
    assert "held-out precision@1" in warned.output
    assert "warning: weights moved" in warned.output
    assert "wrote fitted weights" in warned.output
    quiet = replace(run, fitted=False, warning=None)
    monkeypatch.setattr("checkowners.cli.calibrate_repository", lambda *_args, **_kwargs: quiet)
    plain = CliRunner().invoke(app, ["calibrate"])
    assert plain.exit_code == 0, plain.output
    assert "wrote fitted weights" not in plain.output
    assert "warning:" not in plain.output

    def fail(*_args: object, **_kwargs: object) -> Path:
        msg = "report failed"
        raise ValueError(msg)

    monkeypatch.setattr("checkowners.cli.write_report", fail)
    failed = CliRunner().invoke(app, ["report", "--html", "out.html"])
    assert failed.exit_code == 1
    assert "report failed" in failed.output


def test_module_entry_point_runs_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["checkowners.evaluation", "badges", "--repo", str(tmp_path)],
    )
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="'checkowners.evaluation' found in sys.modules",
            category=RuntimeWarning,
        )
        with pytest.raises(SystemExit) as caught:
            runpy.run_module("checkowners.evaluation", run_name="__main__")
    assert caught.value.code == 1

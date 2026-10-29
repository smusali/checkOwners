"""Measure ownership scores against authors after a cutoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from checkowners.analyze import (
    analyze_ownership,
    combine_available_signals,
    mailmap_flag,
    run_git,
    signal_reliabilities,
    signal_weights,
)
from checkowners.config import _resolve_config_path, load_config
from checkowners.expertise import fnmatch_posix
from checkowners.html_report import build_document, render_report
from checkowners.models import (
    OWNERSHIP_SIGNALS,
    CalibrationProvenance,
    Config,
    OwnerEntry,
    ScoringConfig,
)

MIN_FIT_HISTORY_DAYS = 180
MIN_FIT_LABELED_PATHS = 30
HOLDOUT_STRIDE = 4
WEIGHT_STEP = 20
MIN_DEFAULT_GAIN = 0.02
DIVERGENCE_L1 = 0.20
CALIBRATION_BINS = 10
CUTOFF_FRACTION = 0.6
CALIBRATION_START = "# checkowners:calibration:start"
CALIBRATION_END = "# checkowners:calibration:end"
LEDGER_CATEGORIES = (
    "recommendation_correct",
    "recommendation_wrong",
    "right_person_unavailable",
    "team_owns_it",
    "security_owner_required",
    "compliance_owner_required",
    "temporary_migration",
    "departed_contributor",
    "useful_but_incomplete",
    "no_ownership",
    "should_not_own",
)
UNOBSERVED_LABELS = tuple(
    label
    for label in LEDGER_CATEGORIES
    if label not in {"recommendation_correct", "recommendation_wrong"}
)
BREAKOUTS = (
    "repository_type",
    "language",
    "monorepo_scale",
    "merge_strategy",
    "turnover",
)
BASELINE_NAMES = ("most_commits", "git_blame", "recency_only", "checkowners")
_FLAT_WEIGHTS = {
    "recency": "recency_weight",
    "frequency": "frequency_weight",
    "blame": "blame_weight",
    "review": "review_weight",
    "change_survival": "change_survival_weight",
}
_YAML_SIGNALS = {
    "recency": "recency",
    "frequency": "frequency",
    "blame": "blame",
    "reviews": "review",
    "change_survival": "change_survival",
}
_LEDGER_FIELDS = (
    "id",
    "prediction",
    "judgment",
    "reason",
    "fix",
    "category",
    "status",
    "evidence",
)

SignalPair = tuple[float, bool]
Ranker = Callable[["PathCase"], tuple[tuple[str, float], ...]]


class InsufficientHistoryError(ValueError):
    """Raised when history or labeled paths are below the fit minimums."""


@dataclass(frozen=True)
class Candidate:
    handle: str
    signals: tuple[SignalPair, ...]
    commits: int


@dataclass(frozen=True)
class PathCase:
    repo_id: str
    path: str
    positive: frozenset[str]
    candidates: tuple[Candidate, ...]
    future_commits: tuple[tuple[str, datetime], ...]
    cutoff: datetime
    dimensions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CalibrationBin:
    bin_start: float
    bin_end: float
    count: int
    mean_score: float | None
    hit_rate: float | None


@dataclass(frozen=True)
class Metrics:
    precision_at_1: float
    precision_at_3: float
    precision_at_5: float
    recall_at_3: float
    mrr: float
    ndcg: float
    brier: float
    calibration_error: float
    changed_file_coverage: float
    acceptance_rate: float
    rejection_rate: float
    false_positive_rate: float
    stale_ownership_rate: float
    handoff_days: float | None
    labeled_paths: int
    predicted_paths: int
    recommendation_correct: int
    recommendation_wrong: int
    calibration_curve: tuple[CalibrationBin, ...]


@dataclass(frozen=True)
class BaselineScore:
    method: str
    precision_at_1: float
    recall_at_3: float


@dataclass(frozen=True)
class CalibrationRun:
    cutoff: str
    history_days: int
    weights: dict[str, float]
    fitted: bool
    warning: str | None
    held_out: Metrics
    baselines: tuple[BaselineScore, ...]


@dataclass(frozen=True)
class CorpusRepo:
    id: str
    url: str
    sha: str
    repository_type: str
    language: str
    monorepo_scale: str
    merge_strategy: str
    turnover: str

    def dimensions(self) -> tuple[tuple[str, str], ...]:
        return (
            ("repository_type", self.repository_type),
            ("language", self.language),
            ("monorepo_scale", self.monorepo_scale),
            ("merge_strategy", self.merge_strategy),
            ("turnover", self.turnover),
        )


def default_weights() -> dict[str, float]:
    return signal_weights(ScoringConfig())


def holdout_ids(repo_ids: Sequence[str]) -> frozenset[str]:
    """Return every fourth sorted repository id, which the fit must not see."""
    ordered = sorted(set(repo_ids))
    return frozenset(
        repo_id for index, repo_id in enumerate(ordered) if index % HOLDOUT_STRIDE == 3
    )


def candidate_from_owner(owner: OwnerEntry) -> Candidate | None:
    """Return a rescorable candidate for `owner`, or None when signals were not stored."""
    breakdown = owner.score_breakdown
    if breakdown is None:
        return None
    return Candidate(
        handle=owner.handle,
        signals=(
            (breakdown.recency.score, breakdown.recency.available),
            (breakdown.frequency.score, breakdown.frequency.available),
            (breakdown.blame.score, breakdown.blame.available),
            (breakdown.review.score, breakdown.review.available),
            (breakdown.change_survival.score, breakdown.change_survival.available),
        ),
        commits=owner.commits,
    )


def choose_cutoff(
    instants: Sequence[tuple[str, datetime]],
    fraction: float = CUTOFF_FRACTION,
) -> tuple[str, datetime]:
    """Return the commit nearest `fraction` of the way from the first instant to the last."""
    start = instants[0][1]
    end = instants[-1][1]
    target = start + (end - start) * fraction
    return min(instants, key=lambda item: (abs(item[1] - target), item[0]))


def collect_cases(
    repo_root: Path,
    config: Config,
    *,
    repo_id: str = "",
    fraction: float = CUTOFF_FRACTION,
    dimensions: tuple[tuple[str, str], ...] = (),
) -> tuple[PathCase, ...]:
    """Return one case per path changed after the cutoff in `repo_root`.

    Prediction uses only history at or before the cutoff. Later authors are the label.
    """
    instants = _commit_instants(repo_root)
    if len(instants) < 2:
        return ()
    sha, when = choose_cutoff(instants, fraction)
    ownership = analyze_ownership(
        repo_root,
        config,
        as_of=when,
        revision=sha,
        retain_all=True,
        incremental=False,
    )
    future = _future_paths(repo_root, sha, use_mailmap=config.git.use_mailmap)
    cases: list[PathCase] = []
    for path, events in sorted(future.items()):
        if _excluded(path, config.paths.exclude):
            continue
        positive = frozenset(email for email, _stamp in events if email)
        if not positive:
            continue
        path_ownership = ownership.paths.get(path)
        candidates: list[Candidate] = []
        if path_ownership is not None:
            source = path_ownership.candidates or path_ownership.owners
            for owner in source:
                candidate = candidate_from_owner(owner)
                if candidate is not None:
                    candidates.append(candidate)
        cases.append(
            PathCase(
                repo_id=repo_id or repo_root.name,
                path=path,
                positive=positive,
                candidates=tuple(candidates),
                future_commits=tuple(events),
                cutoff=when,
                dimensions=dimensions,
            )
        )
    return tuple(cases)


def history_span_days(repo_root: Path) -> int:
    """Return whole days from the first commit to the last, or 0 when fewer than two exist."""
    instants = _commit_instants(repo_root)
    if len(instants) < 2:
        return 0
    return (instants[-1][1] - instants[0][1]).days


def measure(cases: Sequence[PathCase], ranker: Ranker) -> Metrics:
    curve = _empty_bins()
    if not cases:
        return _metrics((), (), (), curve, 0, 0)
    precisions: dict[int, list[float]] = {1: [], 3: [], 5: []}
    recalls: list[float] = []
    reciprocal: list[float] = []
    gains: list[float] = []
    brier: list[float] = []
    hits: list[tuple[float, int]] = []
    handoffs: list[float] = []
    correct = 0
    wrong = 0
    predicted = 0
    for case in cases:
        ranked = ranker(case)
        for cutoff, bucket in precisions.items():
            bucket.append(_precision_at(ranked, case.positive, cutoff))
        recalls.append(_recall_at(ranked, case.positive, 3))
        reciprocal.append(_reciprocal(ranked, case.positive))
        gains.append(_ndcg(ranked, case.positive))
        if not ranked:
            wrong += 1
            continue
        predicted += 1
        top_handle, top_score = ranked[0]
        hit = 1 if top_handle in case.positive else 0
        brier.append((top_score - hit) ** 2)
        hits.append((top_score, hit))
        if hit:
            correct += 1
        else:
            wrong += 1
        handoff = _handoff_days(case, top_handle)
        if handoff is not None:
            handoffs.append(handoff)
    populated = _populate_bins(hits)
    return _metrics(
        [_mean(precisions[1]), _mean(precisions[3]), _mean(precisions[5]), _mean(recalls)],
        [_mean(reciprocal), _mean(gains), _mean(brier) if brier else 0.0],
        handoffs,
        populated,
        len(cases),
        predicted,
        correct=correct,
        wrong=wrong,
    )


def weight_grid() -> tuple[dict[str, float], ...]:
    """Return signal weights on a 0.05 grid that sum to 1."""
    found: list[dict[str, float]] = []

    def walk(remaining: int, index: int, counts: list[int]) -> None:
        if index == len(OWNERSHIP_SIGNALS) - 1:
            counts.append(remaining)
            found.append(
                {name: counts[pos] / WEIGHT_STEP for pos, name in enumerate(OWNERSHIP_SIGNALS)}
            )
            counts.pop()
            return
        for value in range(remaining + 1):
            counts.append(value)
            walk(remaining - value, index + 1, counts)
            counts.pop()

    walk(WEIGHT_STEP, 0, [])
    return tuple(found)


def score_candidate(
    candidate: Candidate,
    weights: Mapping[str, float],
    reliabilities: Mapping[str, float],
) -> float:
    signals: dict[str, tuple[float, bool]] = {
        name: candidate.signals[index]
        for index, name in enumerate(OWNERSHIP_SIGNALS)
        if index < len(candidate.signals)
    }
    total, _quality = combine_available_signals(signals, weights, reliabilities)
    return total


def rank_by_weights(
    case: PathCase,
    weights: Mapping[str, float],
    reliabilities: Mapping[str, float],
) -> tuple[tuple[str, float], ...]:
    """Return `case` candidates ordered by `weights`, then handle."""
    return _order(case, lambda candidate: score_candidate(candidate, weights, reliabilities))


def fit_weights(
    cases: Sequence[PathCase],
    reliabilities: Mapping[str, float],
) -> dict[str, float]:
    """Return the grid weights with the best precision@1 on `cases`.

    Equal precision keeps the vector closer to the shipped defaults.
    """
    defaults = default_weights()
    best = defaults
    best_score = _precision_only(cases, defaults, reliabilities)
    best_distance = 0.0
    for weights in weight_grid():
        score = _precision_only(cases, weights, reliabilities)
        distance = weight_l1(weights, defaults)
        better = score > best_score + 1e-12
        tied = abs(score - best_score) <= 1e-12 and distance < best_distance
        if better or tied:
            best = weights
            best_score = score
            best_distance = distance
    return best


def weight_l1(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    return sum(abs(left[name] - right[name]) for name in OWNERSHIP_SIGNALS)


def divergence_warning(fitted: Mapping[str, float], defaults: Mapping[str, float]) -> str | None:
    distance = weight_l1(fitted, defaults)
    if distance <= DIVERGENCE_L1:
        return None
    return (
        f"Fitted weights differ from the defaults by L1 {distance:.2f}. "
        "Unusual history can cause that. The fitted weights are not necessarily a better model."
    )


def split_time(cases: Sequence[PathCase]) -> tuple[tuple[PathCase, ...], tuple[PathCase, ...]]:
    """Return an earliest 80 percent training slice and a latest 20 percent holdout."""
    ordered = tuple(
        sorted(cases, key=lambda case: (case.future_commits[0][1], case.path, case.repo_id))
    )
    hold = max(1, len(ordered) // 5)
    return ordered[:-hold], ordered[-hold:]


def calibrate_repository(repo_root: Path, config: Config, *, fit: bool) -> CalibrationRun:
    """Back-test `repo_root`. When `fit` is true, learn weights on the earlier slice.

    The returned metrics are the held-out slice when fitting, and the whole
    future slice when not fitting. Training metrics are not returned.
    """
    cases = collect_cases(repo_root, config, repo_id=repo_root.name)
    days = history_span_days(repo_root)
    reliabilities = signal_reliabilities(config.scoring)
    cutoff = cases[0].cutoff.astimezone(UTC).isoformat() if cases else ""
    if not fit:
        weights = signal_weights(config.scoring)
        return CalibrationRun(
            cutoff=cutoff,
            history_days=days,
            weights=weights,
            fitted=False,
            warning=None,
            held_out=measure(cases, lambda case: rank_by_weights(case, weights, reliabilities)),
            baselines=_baselines(cases, weights, reliabilities),
        )
    if days < MIN_FIT_HISTORY_DAYS or len(cases) < MIN_FIT_LABELED_PATHS:
        raise InsufficientHistoryError(
            "Refusing to fit. "
            f"Need at least {MIN_FIT_HISTORY_DAYS} days of history "
            f"and {MIN_FIT_LABELED_PATHS} labeled paths. "
            f"This repository has {days} days and {len(cases)} labeled paths."
        )
    training, held = split_time(cases)
    if not training or not held:
        raise InsufficientHistoryError(
            "Refusing to fit. The held-out slice is empty. "
            f"Need at least {MIN_FIT_LABELED_PATHS} labeled paths."
        )
    weights = fit_weights(training, reliabilities)
    warning = divergence_warning(weights, default_weights())
    provenance = _provenance(cases[0].cutoff, training, held, weights, days)
    write_fitted_config(repo_root, weights, provenance)
    return CalibrationRun(
        cutoff=cutoff,
        history_days=days,
        weights=weights,
        fitted=True,
        warning=warning,
        held_out=measure(held, lambda case: rank_by_weights(case, weights, reliabilities)),
        baselines=_baselines(held, weights, reliabilities),
    )


def calibration_payload(run: CalibrationRun) -> dict[str, object]:
    """Return the machine-readable body for `run`. Training metrics are omitted."""
    body: dict[str, object] = {
        "cutoff": run.cutoff,
        "history_days": run.history_days,
        "fitted": run.fitted,
        "warning": run.warning,
        "weights": {name: round(run.weights[name], 2) for name in OWNERSHIP_SIGNALS},
        "held_out": _metrics_payload(run.held_out),
        "baselines": [
            {
                "method": item.method,
                "precision_at_1": round(item.precision_at_1, 4),
                "recall_at_3": round(item.recall_at_3, 4),
            }
            for item in run.baselines
        ],
        "review_metrics": "unavailable",
        "review_metrics_reason": "review history was not collected",
    }
    return body


def format_calibration_text(run: CalibrationRun) -> str:
    metrics = run.held_out
    lines = [
        f"held-out precision@1: {metrics.precision_at_1:.4f}",
        f"held-out precision@3: {metrics.precision_at_3:.4f}",
        f"held-out recall@3: {metrics.recall_at_3:.4f}",
        f"held-out MRR: {metrics.mrr:.4f}",
        f"held-out Brier: {metrics.brier:.4f}",
        f"calibration bins: {CALIBRATION_BINS}",
    ]
    if run.warning:
        lines.append(f"warning: {run.warning}")
    if run.fitted:
        lines.append("wrote fitted weights")
    return "\n".join(lines) + "\n"


def write_fitted_config(
    repo_root: Path,
    weights: Mapping[str, float],
    provenance: CalibrationProvenance,
) -> Path:
    """Write `weights` and `provenance` into the repository config. Return that path."""
    path = _resolve_config_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else "version: 1\n"
    updated = _replace_weights(existing, weights)
    block = _calibration_block(provenance)
    path.write_text(_replace_block(updated, block), encoding="utf-8")
    return path


def should_replace_defaults(candidate: float, current: float) -> bool:
    """Return whether `candidate` beats `current` by the pre-registered margin."""
    return candidate >= current + MIN_DEFAULT_GAIN


def is_calibrated(curve: Sequence[CalibrationBin]) -> bool:
    """Return whether every occupied bin is within 0.1 of its hit rate."""
    occupied = [item for item in curve if item.count > 0]
    if not occupied:
        return False
    return all(_bin_close(item) for item in occupied)


def _bin_close(item: CalibrationBin) -> bool:
    score = item.mean_score
    rate = item.hit_rate
    if score is None or rate is None:
        return False
    return abs(score - rate) <= 0.1


def load_corpus(path: Path, *, key: str = "repositories") -> tuple[CorpusRepo, ...]:
    """Return repositories from the `key` list in `path`."""
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = "corpus manifest must be an object"
        raise ValueError(msg)
    rows = loaded.get(key)
    if not isinstance(rows, list):
        msg = f"corpus manifest is missing {key}"
        raise ValueError(msg)
    repos: list[CorpusRepo] = []
    for row in rows:
        repos.append(_corpus_repo(row))
    return tuple(repos)


def materialize(repo: CorpusRepo, checkout: Path) -> Path:
    """Clone `repo` into `checkout` if needed and check out its pinned commit."""
    target = checkout / repo.id.replace("/", "__")
    if not (target / ".git").exists():
        _git(["git", "clone", "--quiet", repo.url, str(target)], cwd=None)
    _git(["git", "checkout", "--quiet", repo.sha], cwd=target)
    return target


def run_study(manifest: Path, checkout: Path, output: Path) -> dict[str, object]:
    """Fit on training repositories and write held-out results to `output`.

    Held-out repository ids are never passed to the weight search.
    """
    repos = load_corpus(manifest)
    held_ids = holdout_ids(tuple(repo.id for repo in repos))
    cases: list[PathCase] = []
    for repo in repos:
        root = materialize(repo, checkout)
        cases.extend(
            collect_cases(
                root,
                load_config(root),
                repo_id=repo.id,
                dimensions=repo.dimensions(),
            )
        )
    training = tuple(case for case in cases if case.repo_id not in held_ids)
    held = tuple(case for case in cases if case.repo_id in held_ids)
    if not held:
        msg = "Held-out repositories produced no labeled paths."
        raise ValueError(msg)
    reliabilities = signal_reliabilities(ScoringConfig())
    defaults = default_weights()
    candidate = fit_weights(training, reliabilities) if training else defaults
    current = _precision_only(held, defaults, reliabilities)
    proposed = _precision_only(held, candidate, reliabilities)
    selected = candidate if should_replace_defaults(proposed, current) else defaults
    payload = _study_payload(
        held_ids=held_ids,
        training_ids=frozenset(repo.id for repo in repos) - held_ids,
        selected=selected,
        candidate=candidate,
        replaced=selected is not defaults and weight_l1(selected, defaults) > 1e-12,
        held=held,
        reliabilities=reliabilities,
        proposed=proposed,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def publish_dataset(
    manifest: Path,
    checkout: Path,
    output: Path,
    *,
    limit: int | None = None,
) -> dict[str, object]:
    """Write HTML reports, a 50-audit list, counts, and checksums under `output`."""
    repos = load_corpus(manifest, key="dataset")
    if limit is not None:
        repos = repos[:limit]
    reports = output / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    audits: list[dict[str, str]] = []
    succeeded = 0
    failed = 0
    critical_repos = 0
    for repo in repos:
        try:
            root = materialize(repo, checkout)
            document = build_document(root)
            slug = repo.id.replace("/", "__")
            html_path = reports / f"{slug}.html"
            html_path.write_text(
                render_report(document, load_config(root), repo.id),
                encoding="utf-8",
            )
            summary = {
                "id": repo.id,
                "sha": repo.sha,
                "critical_components": document.critical_components,
                "drift_count": document.drift_count,
                "ownership_coverage": document.ownership_coverage,
                "finding": document.finding,
            }
            (reports / f"{slug}.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            succeeded += 1
            if document.critical_components > 0:
                critical_repos += 1
            if len(audits) < 50:
                audits.append(
                    {"id": repo.id, "finding": document.finding, "report": f"{slug}.html"}
                )
        except (OSError, ValueError, subprocess.CalledProcessError):
            failed += 1
    findings: dict[str, object] = {
        "repositories": len(repos),
        "succeeded": succeeded,
        "failed": failed,
        "critical_path_repositories": critical_repos,
        "audits": len(audits),
    }
    (output / "findings.json").write_text(
        json.dumps(findings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    audit_lines = [json.dumps(row, sort_keys=True) for row in audits]
    body = "\n".join(audit_lines)
    if body:
        body += "\n"
    (output / "audits.jsonl").write_text(body, encoding="utf-8")
    checksum_path = output / "checksums.txt"
    checksum_path.write_text(checksum_tree(output, skip={checksum_path.name}), encoding="utf-8")
    return findings


def checksum_tree(root: Path, *, skip: set[str] | None = None) -> str:
    """Return sha256 lines for files under `root`, skipping names in `skip`."""
    ignored = {".DS_Store"}
    if skip:
        ignored.update(skip)
    lines: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name in ignored:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return "\n".join(lines) + ("\n" if lines else "")


def format_badges(coverage: float, critical_paths: int, drift_count: int) -> str:
    percent = round(coverage * 100)
    return (
        f"Ownership coverage: {percent}%   "
        f"Critical knowledge risk: {critical_paths} paths   "
        f"CODEOWNERS drift: {drift_count}"
    )


def repository_badges(repo_root: Path) -> str:
    document = build_document(repo_root)
    return format_badges(
        document.ownership_coverage,
        document.critical_components,
        document.drift_count,
    )


def ledger_problems(root: Path) -> tuple[str, ...]:
    """Return problems in the false-positive ledger under `root`."""
    ledger_path = root / "benchmarks" / "false_positives.json"
    page_path = root / "docs" / "false-positives.md"
    tests_path = root / "tests" / "test_golden.py"
    loaded: object = json.loads(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), list):
        return ("ledger entries must be a list",)
    page = page_path.read_text(encoding="utf-8") if page_path.is_file() else ""
    tests = tests_path.read_text(encoding="utf-8") if tests_path.is_file() else ""
    problems: list[str] = []
    entries = loaded["entries"]
    if not entries:
        problems.append("ledger has no entries")
    for entry in entries:
        problems.extend(_entry_problems(entry, page, tests))
    return tuple(problems)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checkowners.evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    study = commands.add_parser("study")
    study.add_argument("--manifest", type=Path, required=True)
    study.add_argument("--checkout", type=Path, required=True)
    study.add_argument("--output", type=Path, required=True)
    dataset = commands.add_parser("dataset")
    dataset.add_argument("--manifest", type=Path, required=True)
    dataset.add_argument("--checkout", type=Path, required=True)
    dataset.add_argument("--output", type=Path, required=True)
    dataset.add_argument("--limit", type=int, default=None)
    badges = commands.add_parser("badges")
    badges.add_argument("--repo", type=Path, default=Path())
    badges.add_argument("--check", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "study":
            run_study(args.manifest, args.checkout, args.output)
            return 0
        if args.command == "dataset":
            publish_dataset(args.manifest, args.checkout, args.output, limit=args.limit)
            return 0
        line = repository_badges(args.repo)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc))
        return 1
    if args.check is None:
        print(line)
        return 0
    text = args.check.read_text(encoding="utf-8")
    if line not in text:
        print(line)
        return 1
    return 0


def _metrics(
    ranking: Sequence[float],
    quality: Sequence[float],
    handoffs: Sequence[float],
    curve: tuple[CalibrationBin, ...],
    labeled: int,
    predicted: int,
    *,
    correct: int = 0,
    wrong: int = 0,
) -> Metrics:
    acceptance = (correct / predicted) if predicted else 0.0
    rejection = 1.0 - acceptance if predicted else 0.0
    precision_at_1 = ranking[0] if ranking else 0.0
    precision_at_3 = ranking[1] if len(ranking) > 1 else 0.0
    precision_at_5 = ranking[2] if len(ranking) > 2 else 0.0
    recall_at_3 = ranking[3] if len(ranking) > 3 else 0.0
    return Metrics(
        precision_at_1=precision_at_1,
        precision_at_3=precision_at_3,
        precision_at_5=precision_at_5,
        recall_at_3=recall_at_3,
        mrr=quality[0] if quality else 0.0,
        ndcg=quality[1] if len(quality) > 1 else 0.0,
        brier=quality[2] if len(quality) > 2 else 0.0,
        calibration_error=_calibration_error(curve),
        changed_file_coverage=(predicted / labeled) if labeled else 0.0,
        acceptance_rate=acceptance,
        rejection_rate=rejection,
        false_positive_rate=rejection,
        stale_ownership_rate=rejection,
        handoff_days=(_mean(list(handoffs)) if handoffs else None),
        labeled_paths=labeled,
        predicted_paths=predicted,
        recommendation_correct=correct,
        recommendation_wrong=wrong,
        calibration_curve=curve,
    )


def _metrics_payload(metrics: Metrics) -> dict[str, object]:
    return {
        "precision_at_1": round(metrics.precision_at_1, 4),
        "precision_at_3": round(metrics.precision_at_3, 4),
        "precision_at_5": round(metrics.precision_at_5, 4),
        "recall_at_3": round(metrics.recall_at_3, 4),
        "mrr": round(metrics.mrr, 4),
        "ndcg": round(metrics.ndcg, 4),
        "brier": round(metrics.brier, 4),
        "calibration_error": round(metrics.calibration_error, 4),
        "changed_file_coverage": round(metrics.changed_file_coverage, 4),
        "acceptance_rate": round(metrics.acceptance_rate, 4),
        "rejection_rate": round(metrics.rejection_rate, 4),
        "false_positive_rate": round(metrics.false_positive_rate, 4),
        "stale_ownership_rate": round(metrics.stale_ownership_rate, 4),
        "handoff_days": None if metrics.handoff_days is None else round(metrics.handoff_days, 4),
        "labeled_paths": metrics.labeled_paths,
        "predicted_paths": metrics.predicted_paths,
        "recommendation_correct": metrics.recommendation_correct,
        "recommendation_wrong": metrics.recommendation_wrong,
        "calibration_curve": [
            {
                "bin_start": item.bin_start,
                "bin_end": item.bin_end,
                "count": item.count,
                "mean_score": item.mean_score,
                "hit_rate": item.hit_rate,
            }
            for item in metrics.calibration_curve
        ],
        "reviewer_overload": None,
        "review_latency_hours": None,
        "time_to_first_useful_review_hours": None,
    }


def _study_payload(
    *,
    held_ids: frozenset[str],
    training_ids: frozenset[str],
    selected: Mapping[str, float],
    candidate: Mapping[str, float],
    replaced: bool,
    held: Sequence[PathCase],
    reliabilities: Mapping[str, float],
    proposed: float,
) -> dict[str, object]:
    def ranker(case: PathCase) -> tuple[tuple[str, float], ...]:
        return rank_by_weights(case, selected, reliabilities)

    metrics = measure(held, ranker)
    return {
        "variance_band": "exact",
        "cutoff_fraction": CUTOFF_FRACTION,
        "model": "ownership-v2",
        "held_out_ids": sorted(held_ids),
        "training_ids": sorted(training_ids),
        "defaults_replaced": replaced,
        "selected_weights": {name: round(selected[name], 2) for name in OWNERSHIP_SIGNALS},
        "candidate_weights": {name: round(candidate[name], 2) for name in OWNERSHIP_SIGNALS},
        "candidate_held_out_precision_at_1": round(proposed, 4),
        "calibrated_probability": is_calibrated(metrics.calibration_curve),
        "held_out": _metrics_payload(metrics),
        "baselines": [
            {
                "method": item.method,
                "precision_at_1": round(item.precision_at_1, 4),
                "recall_at_3": round(item.recall_at_3, 4),
            }
            for item in _baselines(held, selected, reliabilities)
        ],
        "breakouts": _breakouts(held, selected, reliabilities),
        "unobserved_labels": list(UNOBSERVED_LABELS),
        "review_metrics": "unavailable",
        "review_metrics_reason": "review history was not collected",
    }


def _baselines(
    cases: Sequence[PathCase],
    weights: Mapping[str, float],
    reliabilities: Mapping[str, float],
) -> tuple[BaselineScore, ...]:
    rankers: dict[str, Ranker] = {
        "most_commits": lambda case: _order(case, lambda candidate: float(candidate.commits)),
        "git_blame": lambda case: _order(case, _blame_score),
        "recency_only": lambda case: _order(case, _recency_score),
        "checkowners": lambda case: rank_by_weights(case, weights, reliabilities),
    }
    scores: list[BaselineScore] = []
    for name in BASELINE_NAMES:
        metrics = measure(cases, rankers[name])
        scores.append(
            BaselineScore(
                method=name,
                precision_at_1=metrics.precision_at_1,
                recall_at_3=metrics.recall_at_3,
            )
        )
    return tuple(scores)


def _breakouts(
    cases: Sequence[PathCase],
    weights: Mapping[str, float],
    reliabilities: Mapping[str, float],
) -> dict[str, dict[str, dict[str, float | int]]]:
    grouped: dict[str, dict[str, list[PathCase]]] = {name: {} for name in BREAKOUTS}
    for case in cases:
        values = dict(case.dimensions)
        for name in BREAKOUTS:
            label = values.get(name, "")
            if not label:
                continue
            grouped[name].setdefault(label, []).append(case)
    payload: dict[str, dict[str, dict[str, float | int]]] = {}
    for name, buckets in grouped.items():
        payload[name] = {}
        for label, bucket in sorted(buckets.items()):
            metrics = measure(bucket, lambda case: rank_by_weights(case, weights, reliabilities))
            payload[name][label] = {
                "count": len(bucket),
                "precision_at_1": round(metrics.precision_at_1, 4),
                "recall_at_3": round(metrics.recall_at_3, 4),
            }
    return payload


def _precision_only(
    cases: Sequence[PathCase],
    weights: Mapping[str, float],
    reliabilities: Mapping[str, float],
) -> float:
    if not cases:
        return 0.0
    hits = [
        _precision_at(rank_by_weights(case, weights, reliabilities), case.positive, 1)
        for case in cases
    ]
    return _mean(hits)


def _order(case: PathCase, score_of: Callable[[Candidate], float]) -> tuple[tuple[str, float], ...]:
    scored = [(candidate.handle, score_of(candidate)) for candidate in case.candidates]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return tuple(scored)


def _blame_score(candidate: Candidate) -> float:
    score, available = candidate.signals[2]
    return score if available else -1.0


def _recency_score(candidate: Candidate) -> float:
    score, available = candidate.signals[0]
    return score if available else -1.0


def _precision_at(
    ranked: Sequence[tuple[str, float]],
    positive: frozenset[str],
    cutoff: int,
) -> float:
    top = [handle for handle, _score in ranked[:cutoff]]
    return sum(1 for handle in top if handle in positive) / cutoff


def _recall_at(ranked: Sequence[tuple[str, float]], positive: frozenset[str], cutoff: int) -> float:
    if not positive:
        return 0.0
    top = {handle for handle, _score in ranked[:cutoff]}
    return len(top & positive) / len(positive)


def _reciprocal(ranked: Sequence[tuple[str, float]], positive: frozenset[str]) -> float:
    for index, (handle, _score) in enumerate(ranked, start=1):
        if handle in positive:
            return 1.0 / index
    return 0.0


def _ndcg(ranked: Sequence[tuple[str, float]], positive: frozenset[str]) -> float:
    if not positive or not ranked:
        return 0.0
    dcg = 0.0
    for index, (handle, _score) in enumerate(ranked, start=1):
        if handle in positive:
            dcg += 1.0 / math.log2(index + 1)
    ideal = min(len(positive), len(ranked))
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def _handoff_days(case: PathCase, top: str) -> float | None:
    counts: dict[str, int] = {}
    for author, when in case.future_commits:
        counts[author] = counts.get(author, 0) + 1
        leader = max(counts, key=lambda name: (counts[name], name))
        if counts[leader] * 2 > sum(counts.values()) and leader != top:
            return (when - case.cutoff).total_seconds() / 86400
    return None


def _empty_bins() -> tuple[CalibrationBin, ...]:
    return tuple(
        CalibrationBin(
            bin_start=index / CALIBRATION_BINS,
            bin_end=(index + 1) / CALIBRATION_BINS,
            count=0,
            mean_score=None,
            hit_rate=None,
        )
        for index in range(CALIBRATION_BINS)
    )


def _populate_bins(hits: Sequence[tuple[float, int]]) -> tuple[CalibrationBin, ...]:
    grouped: list[list[tuple[float, int]]] = [[] for _index in range(CALIBRATION_BINS)]
    for score, hit in hits:
        index = min(CALIBRATION_BINS - 1, max(0, int(score * CALIBRATION_BINS)))
        if score >= 1.0:
            index = CALIBRATION_BINS - 1
        grouped[index].append((score, hit))
    bins: list[CalibrationBin] = []
    for index, rows in enumerate(grouped):
        if not rows:
            bins.append(
                CalibrationBin(
                    bin_start=index / CALIBRATION_BINS,
                    bin_end=(index + 1) / CALIBRATION_BINS,
                    count=0,
                    mean_score=None,
                    hit_rate=None,
                )
            )
            continue
        bins.append(
            CalibrationBin(
                bin_start=index / CALIBRATION_BINS,
                bin_end=(index + 1) / CALIBRATION_BINS,
                count=len(rows),
                mean_score=_mean([score for score, _hit in rows]),
                hit_rate=_mean([float(hit) for _score, hit in rows]),
            )
        )
    return tuple(bins)


def _calibration_error(curve: Sequence[CalibrationBin]) -> float:
    total = sum(item.count for item in curve)
    if total == 0:
        return 0.0
    error = 0.0
    for item in curve:
        if item.count == 0 or item.mean_score is None or item.hit_rate is None:
            continue
        error += (item.count / total) * abs(item.mean_score - item.hit_rate)
    return error


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _commit_instants(repo_root: Path) -> tuple[tuple[str, datetime], ...]:
    result = run_git(repo_root, ["git", "log", "--reverse", "--format=%H%x09%cI"], check=True)
    rows: list[tuple[str, datetime]] = []
    for line in result.stdout.splitlines():
        sha, separator, stamp = line.partition("\t")
        if not separator or not sha:
            continue
        rows.append((sha, _parse_time(stamp)))
    return tuple(rows)


def _future_paths(
    repo_root: Path,
    cutoff_sha: str,
    *,
    use_mailmap: bool,
) -> dict[str, list[tuple[str, datetime]]]:
    result = run_git(
        repo_root,
        [
            "git",
            "log",
            "--reverse",
            "--no-merges",
            mailmap_flag(use_mailmap),
            "--format=%x1e%aE%x09%cI",
            "--name-only",
            f"{cutoff_sha}..HEAD",
        ],
        check=True,
    )
    found: dict[str, list[tuple[str, datetime]]] = {}
    for chunk in result.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        email, separator, stamp = lines[0].partition("\t")
        if not separator or not email:
            continue
        when = _parse_time(stamp)
        for path in lines[1:]:
            cleaned = path.strip()
            if cleaned:
                found.setdefault(cleaned, []).append((email, when))
    return found


def _parse_time(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _excluded(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch_posix(path, pattern) for pattern in patterns)


def _provenance(
    cutoff: datetime,
    training: Sequence[PathCase],
    held: Sequence[PathCase],
    weights: Mapping[str, float],
    days: int,
) -> CalibrationProvenance:
    reliabilities = signal_reliabilities(ScoringConfig())
    metrics = measure(held, lambda case: rank_by_weights(case, weights, reliabilities))
    defaults = default_weights()
    return CalibrationProvenance(
        cutoff=cutoff.astimezone(UTC).isoformat(),
        train_paths=len(training),
        held_out_paths=len(held),
        history_days=days,
        weights=tuple((name, round(weights[name], 2)) for name in OWNERSHIP_SIGNALS),
        default_weights=tuple((name, round(defaults[name], 2)) for name in OWNERSHIP_SIGNALS),
        held_out_precision_at_1=round(metrics.precision_at_1, 4),
        held_out_precision_at_3=round(metrics.precision_at_3, 4),
        held_out_recall_at_3=round(metrics.recall_at_3, 4),
        held_out_mrr=round(metrics.mrr, 4),
        held_out_brier=round(metrics.brier, 4),
    )


def _calibration_block(provenance: CalibrationProvenance) -> str:
    def pairs(rows: tuple[tuple[str, float], ...]) -> list[str]:
        return [f"    {name}: {value:.2f}" for name, value in rows]

    lines = [
        CALIBRATION_START,
        "calibration:",
        f'  cutoff: "{provenance.cutoff}"',
        f"  train_paths: {provenance.train_paths}",
        f"  held_out_paths: {provenance.held_out_paths}",
        f"  history_days: {provenance.history_days}",
        "  weights:",
        *pairs(provenance.weights),
        "  default_weights:",
        *pairs(provenance.default_weights),
        "  held_out:",
        f"    precision_at_1: {provenance.held_out_precision_at_1:.4f}",
        f"    precision_at_3: {provenance.held_out_precision_at_3:.4f}",
        f"    recall_at_3: {provenance.held_out_recall_at_3:.4f}",
        f"    mrr: {provenance.held_out_mrr:.4f}",
        f"    brier: {provenance.held_out_brier:.4f}",
        CALIBRATION_END,
        "",
    ]
    return "\n".join(lines)


def _replace_block(text: str, block: str) -> str:
    start = text.find(CALIBRATION_START)
    end = text.find(CALIBRATION_END)
    if start != -1 and end > start:
        end_line = text.find("\n", end)
        end_line = len(text) if end_line == -1 else end_line + 1
        return text[:start] + block + text[end_line:]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


def _replace_weights(text: str, weights: Mapping[str, float]) -> str:
    lines = text.splitlines()
    replaced = False
    current: str | None = None
    current_indent = -1
    rewritten: list[str] = []
    for line in lines:
        flat_key, flat_indent, flat_tail = _flat_weight(line)
        if flat_key is not None:
            value = weights[flat_key]
            key = _FLAT_WEIGHTS[flat_key]
            rewritten.append(f"{flat_indent}{key}: {value:.2f}{flat_tail}")
            replaced = True
            continue
        signal, indent = _signal_heading(line)
        if signal is not None:
            current = signal
            current_indent = indent
            rewritten.append(line)
            continue
        weight_indent, weight_tail = _weight_line(line)
        if (
            current is not None
            and weight_indent is not None
            and len(weight_indent) > current_indent
        ):
            value = weights[current]
            rewritten.append(f"{weight_indent}weight: {value:.2f}{weight_tail}")
            replaced = True
            continue
        if line.strip() and not line.lstrip().startswith("#"):
            indent = len(line) - len(line.lstrip(" "))
            if current is not None and indent <= current_indent:
                current = None
        rewritten.append(line)
    rendered = "\n".join(rewritten)
    if text.endswith("\n"):
        rendered += "\n"
    if replaced:
        return rendered
    scoring = ["scoring:"]
    for name in OWNERSHIP_SIGNALS:
        scoring.append(f"  {_FLAT_WEIGHTS[name]}: {weights[name]:.2f}")
    suffix = "\n".join(scoring) + "\n"
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered + suffix


def _flat_weight(line: str) -> tuple[str | None, str, str]:
    stripped = line.lstrip(" ")
    indent = line[: len(line) - len(stripped)]
    for name, key in _FLAT_WEIGHTS.items():
        prefix = f"{key}:"
        if stripped.startswith(prefix):
            tail = stripped[len(prefix) :]
            value, _separator, comment = tail.partition("#")
            comment_tail = f" #{comment}" if comment else ""
            if value.strip():
                return name, indent, comment_tail
    return None, "", ""


def _signal_heading(line: str) -> tuple[str | None, int]:
    stripped = line.lstrip(" ")
    if not stripped.endswith(":") or stripped.startswith("#"):
        return None, -1
    name = stripped[:-1].strip()
    mapped = _YAML_SIGNALS.get(name)
    if mapped is None or " " in name:
        return None, -1
    return mapped, len(line) - len(stripped)


def _weight_line(line: str) -> tuple[str | None, str]:
    stripped = line.lstrip(" ")
    if not stripped.startswith("weight:"):
        return None, ""
    indent = line[: len(line) - len(stripped)]
    tail = stripped[len("weight:") :]
    _value, _separator, comment = tail.partition("#")
    comment_tail = f" #{comment}" if comment else ""
    return indent, comment_tail


def _corpus_repo(row: object) -> CorpusRepo:
    if not isinstance(row, dict):
        msg = "corpus entry must be an object"
        raise ValueError(msg)
    fields = (
        "id",
        "url",
        "sha",
        "repository_type",
        "language",
        "monorepo_scale",
        "merge_strategy",
        "turnover",
    )
    values: dict[str, str] = {}
    for field in fields:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            msg = f"corpus entry is missing {field}"
            raise ValueError(msg)
        values[field] = value
    if len(values["sha"]) != 40:
        msg = f"corpus sha for {values['id']} must be a full commit id"
        raise ValueError(msg)
    return CorpusRepo(
        id=values["id"],
        url=values["url"],
        sha=values["sha"],
        repository_type=values["repository_type"],
        language=values["language"],
        monorepo_scale=values["monorepo_scale"],
        merge_strategy=values["merge_strategy"],
        turnover=values["turnover"],
    )


def _entry_problems(entry: object, page: str, tests: str) -> list[str]:
    if not isinstance(entry, dict):
        return ["ledger entry must be an object"]
    problems: list[str] = []
    identifier = entry.get("id")
    label = identifier if isinstance(identifier, str) else "<missing>"
    for field in _LEDGER_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{label} is missing {field}")
    prediction = entry.get("prediction")
    judgment = entry.get("judgment")
    evidence = entry.get("evidence")
    if isinstance(prediction, str) and prediction == judgment:
        problems.append(f"{label} judgment overwrites the prediction")
    if isinstance(evidence, str) and evidence == judgment:
        problems.append(f"{label} judgment overwrites the evidence")
    category = entry.get("category")
    if isinstance(category, str) and category not in LEDGER_CATEGORIES:
        problems.append(f"{label} has an unknown category")
    status = entry.get("status")
    if status == "fixed":
        test_id = entry.get("test_id")
        if not isinstance(test_id, str) or f"def {test_id}(" not in tests:
            problems.append(f"{label} fixed entry has no test")
    elif status == "open":
        issue_url = entry.get("issue_url")
        if not isinstance(issue_url, str) or not issue_url.strip():
            problems.append(f"{label} open entry has no issue url")
    else:
        problems.append(f"{label} status must be fixed or open")
    if isinstance(identifier, str) and identifier not in page:
        problems.append(f"{label} is missing from the public ledger page")
    return problems


def _git(argv: list[str], *, cwd: Path | None) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())

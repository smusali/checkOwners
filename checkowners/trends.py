"""Historical activity confidence reconstructed from git log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from checkowners.analyze import (
    Contribution,
    _aggregate_contributions,
    _current_commits,
    _frequency_score,
    _get_commit_history,
    _is_excluded,
    _qualify_authors,
    _RawCommit,
    _recency_score,
    combine_available_signals,
    effective_half_life,
    frequency_credit,
    frequency_prior_for,
    resolve_as_of,
    signal_reliabilities,
    signal_weights,
)
from checkowners.models import (
    OWNERSHIP_MODEL_VERSION,
    OWNERSHIP_SIGNALS,
    Config,
    OwnershipSignal,
    ScoringConfig,
)

_DEFAULT_PERIODS = 6
_DEFAULT_PERIOD_DAYS = 30

HistoricalSignal = Literal["recency", "frequency"]
HISTORICAL_MODEL_VERSION: Literal["historical-v1"] = "historical-v1"
HISTORICAL_METRIC: Literal["historical_activity_confidence"] = "historical_activity_confidence"
OWNERSHIP_METRIC: Literal["ownership_score"] = "ownership_score"
HISTORICAL_SIGNALS: tuple[HistoricalSignal, ...] = ("recency", "frequency")
HISTORICAL_LIMITATION = (
    "historical_activity_confidence and avg_qualified_owner_count use recency and "
    "frequency only (historical-v1). They are not ownership_score (ownership-v2)."
)
SHARED_AXIS_REFUSAL = (
    "Pass --acknowledge-different-metrics to name ownership_score next to "
    "historical_activity_confidence. They are different metrics."
)


class ComparedModelJson(TypedDict):
    metric: Literal["ownership_score"]
    model_version: str
    signals: list[OwnershipSignal]


class TrendPointJson(TypedDict):
    period_end: str
    commits: int
    active_contributors: int
    tracked_paths: int
    historical_activity_confidence: float
    avg_qualified_owner_count: float
    qualified_owner_count_cap: int


class TrendReportJson(TypedDict):
    periods: int
    period_days: int
    qualified_owner_count_cap: int
    model_version: Literal["historical-v1"]
    metric: Literal["historical_activity_confidence"]
    signals: list[HistoricalSignal]
    limitation: str
    points: list[TrendPointJson]
    compared_to: NotRequired[ComparedModelJson]


@dataclass(frozen=True)
class TrendPoint:
    period_end: datetime
    commits: int
    active_contributors: int
    tracked_paths: int
    historical_activity_confidence: float
    avg_qualified_owner_count: float


@dataclass(frozen=True)
class TrendReport:
    points: tuple[TrendPoint, ...]
    periods: int
    period_days: int


def historical_model_line() -> str:
    signals = ", ".join(HISTORICAL_SIGNALS)
    return f"model: {HISTORICAL_MODEL_VERSION} ({signals})"


def compared_axis_line() -> str:
    signals = ", ".join(OWNERSHIP_SIGNALS)
    return (
        f"{OWNERSHIP_METRIC} ({OWNERSHIP_MODEL_VERSION}: {signals}) "
        f"is not on the same axis as {HISTORICAL_METRIC}."
    )


def trend_point_payload(point: TrendPoint, cap: int) -> TrendPointJson:
    return {
        "period_end": point.period_end.date().isoformat(),
        "commits": point.commits,
        "active_contributors": point.active_contributors,
        "tracked_paths": point.tracked_paths,
        "historical_activity_confidence": point.historical_activity_confidence,
        "avg_qualified_owner_count": point.avg_qualified_owner_count,
        "qualified_owner_count_cap": cap,
    }


def trend_report_payload(report: TrendReport, cap: int, *, compared: bool) -> TrendReportJson:
    payload: TrendReportJson = {
        "periods": report.periods,
        "period_days": report.period_days,
        "qualified_owner_count_cap": cap,
        "model_version": HISTORICAL_MODEL_VERSION,
        "metric": HISTORICAL_METRIC,
        "signals": list(HISTORICAL_SIGNALS),
        "limitation": HISTORICAL_LIMITATION,
        "points": [trend_point_payload(point, cap) for point in report.points],
    }
    if compared:
        payload["compared_to"] = {
            "metric": OWNERSHIP_METRIC,
            "model_version": OWNERSHIP_MODEL_VERSION,
            "signals": list(OWNERSHIP_SIGNALS),
        }
    return payload


def analyze_trends(
    repo_root: Path,
    config: Config,
    *,
    periods: int = _DEFAULT_PERIODS,
    period_days: int = _DEFAULT_PERIOD_DAYS,
    as_of: datetime | None = None,
) -> TrendReport:
    when = as_of if as_of is not None else resolve_as_of(None, repo_root)
    span_days = max(1, periods * period_days)
    history = _get_commit_history(
        repo_root,
        when - timedelta(days=span_days),
        when,
        use_mailmap=config.git.use_mailmap,
        count_co_authors=config.git.count_co_authors,
        merge_strategy=config.git.merge_strategy,
        follow_renames=config.analysis.follow_renames,
        rename_similarity=config.analysis.rename_similarity,
    )
    return build_trends(
        _current_commits(history),
        config,
        periods=periods,
        period_days=period_days,
        now=when,
    )


def build_trends(
    commits: list[_RawCommit],
    config: Config,
    *,
    periods: int,
    period_days: int,
    now: datetime,
) -> TrendReport:
    periods = max(1, periods)
    period_days = max(1, period_days)
    points: list[TrendPoint] = []
    for index in range(periods):
        as_of = now - timedelta(days=(periods - 1 - index) * period_days)
        window = [commit for commit in commits if commit.timestamp <= as_of]
        points.append(_summarize(window, config, as_of))
    return TrendReport(points=tuple(points), periods=periods, period_days=period_days)


def _summarize(window: list[_RawCommit], config: Config, as_of: datetime) -> TrendPoint:
    contributions = _aggregate_contributions(
        window,
        co_author_weight=config.git.co_author_weight,
    )
    contributions = {
        path: authors
        for path, authors in contributions.items()
        if not _is_excluded(path, config.paths.exclude)
    }
    contributors = {author for authors in contributions.values() for author in authors}
    total_commits = len(window)

    top_scores: list[float] = []
    qualified_owner_counts: list[int] = []
    for authors in contributions.values():
        owners = _score_path(authors, config, as_of)
        if not owners:
            continue
        top_scores.append(owners[0])
        qualified_owner_counts.append(
            sum(1 for score in owners if score >= config.analysis.confidence_threshold)
        )

    tracked = len(top_scores)
    avg_score = round(sum(top_scores) / tracked, 4) if tracked else 0.0
    avg_count = round(sum(qualified_owner_counts) / tracked, 2) if tracked else 0.0
    return TrendPoint(
        period_end=as_of,
        commits=total_commits,
        active_contributors=len(contributors),
        tracked_paths=tracked,
        historical_activity_confidence=avg_score,
        avg_qualified_owner_count=avg_count,
    )


def _score_path(
    authors: dict[str, Contribution],
    config: Config,
    as_of: datetime,
) -> list[float]:
    qualified = _qualify_authors(
        authors,
        min_commits=config.analysis.min_commits,
        strategy=config.qualification.strategy,
        path_blame={},
        blame_override=config.qualification.strong_blame_override,
    )
    if not qualified:
        return []
    max_credit = max(frequency_credit(contrib) for contrib in qualified.values())
    prior = frequency_prior_for(config.qualification.strategy)
    scored = [
        _two_factor_confidence(contrib, max_credit, config.scoring, as_of, prior)
        for contrib in qualified.values()
    ]
    scored = [score for score in scored if score >= config.analysis.confidence_threshold]
    scored.sort(reverse=True)
    return scored[: config.analysis.top_n_owners]


def _two_factor_confidence(
    contrib: Contribution,
    max_commits: float,
    scoring: ScoringConfig,
    as_of: datetime,
    frequency_prior: float = 0.0,
) -> float:
    recency = _recency_score(
        contrib.last_commit,
        as_of,
        effective_half_life(contrib.cadence_days, scoring),
    )
    frequency = _frequency_score(frequency_credit(contrib), max_commits, frequency_prior)
    score, _quality = combine_available_signals(
        {
            "recency": (recency, True),
            "frequency": (frequency, True),
            "blame": (0.0, False),
            "review": (0.0, False),
            "change_survival": (0.0, False),
        },
        signal_weights(scoring),
        signal_reliabilities(scoring),
    )
    return score

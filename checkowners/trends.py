"""Historical ownership-confidence trends.

Reconstructs the ownership snapshot at the end of each of the last N periods
from a single ``git log`` pass and reports how concentration, confidence, and
bus factor have evolved. The snapshot at each period end is cumulative: it uses
every fetched commit up to that point, with recency measured relative to that
period's end. Blame and review factors are not reconstructed historically, so
the trend confidence uses the recency and frequency factors (renormalized).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from checkowners.analyze import (
    _aggregate_contributions,
    _clamp,
    _Contribution,
    _frequency_score,
    _get_commit_history,
    _is_excluded,
    _RawCommit,
    _recency_score,
)
from checkowners.models import Config, ScoringConfig

_DEFAULT_PERIODS = 6
_DEFAULT_PERIOD_DAYS = 30


@dataclass(frozen=True)
class TrendPoint:
    """Ownership metrics reconstructed at the end of one period."""

    period_end: datetime
    commits: int
    active_contributors: int
    tracked_paths: int
    avg_top_confidence: float
    avg_bus_factor: float


@dataclass(frozen=True)
class TrendReport:
    points: tuple[TrendPoint, ...]
    periods: int
    period_days: int


def analyze_trends(
    repo_root: Path,
    config: Config,
    *,
    periods: int = _DEFAULT_PERIODS,
    period_days: int = _DEFAULT_PERIOD_DAYS,
) -> TrendReport:
    """Fetch history and build the trend report for `repo_root`."""
    span_days = max(1, periods * period_days)
    commits = _get_commit_history(repo_root, span_days)
    return build_trends(
        commits,
        config,
        periods=periods,
        period_days=period_days,
        now=datetime.now(UTC),
    )


def build_trends(
    commits: list[_RawCommit],
    config: Config,
    *,
    periods: int,
    period_days: int,
    now: datetime,
) -> TrendReport:
    """Compute per-period ownership metrics from a commit list (pure function)."""
    periods = max(1, periods)
    period_days = max(1, period_days)
    points: list[TrendPoint] = []
    for index in range(periods):
        as_of = now - timedelta(days=(periods - 1 - index) * period_days)
        window = [commit for commit in commits if commit.timestamp <= as_of]
        points.append(_summarize(window, config, as_of))
    return TrendReport(points=tuple(points), periods=periods, period_days=period_days)


def _summarize(window: list[_RawCommit], config: Config, as_of: datetime) -> TrendPoint:
    contributions = _aggregate_contributions(window)
    contributions = {
        path: authors
        for path, authors in contributions.items()
        if not _is_excluded(path, config.paths.exclude)
    }
    contributors = {author for authors in contributions.values() for author in authors}
    # Distinct commits in the window: one commit touching many files counts once.
    total_commits = len(window)

    top_confidences: list[float] = []
    bus_factors: list[int] = []
    for authors in contributions.values():
        owners = _score_path(authors, config, as_of)
        if not owners:
            continue
        top_confidences.append(owners[0])
        bus_factors.append(sum(1 for c in owners if c >= config.analysis.confidence_threshold))

    tracked = len(top_confidences)
    avg_conf = round(sum(top_confidences) / tracked, 4) if tracked else 0.0
    avg_bus = round(sum(bus_factors) / tracked, 2) if tracked else 0.0
    return TrendPoint(
        period_end=as_of,
        commits=total_commits,
        active_contributors=len(contributors),
        tracked_paths=tracked,
        avg_top_confidence=avg_conf,
        avg_bus_factor=avg_bus,
    )


def _score_path(
    authors: dict[str, _Contribution],
    config: Config,
    as_of: datetime,
) -> list[float]:
    """Confidence scores (descending) for the qualified owners of one path."""
    qualified = {
        author: contrib
        for author, contrib in authors.items()
        if contrib.commits >= config.analysis.min_commits
    }
    if not qualified:
        return []
    max_commits = max(contrib.commits for contrib in qualified.values())
    scored = [
        _two_factor_confidence(contrib, max_commits, config.scoring, as_of)
        for contrib in qualified.values()
    ]
    scored = [c for c in scored if c >= config.analysis.confidence_threshold]
    scored.sort(reverse=True)
    return scored[: config.analysis.top_n_owners]


def _two_factor_confidence(
    contrib: _Contribution,
    max_commits: int,
    scoring: ScoringConfig,
    as_of: datetime,
) -> float:
    """Recency + frequency confidence, renormalized (no historical blame/review)."""
    recency = _recency_score(contrib.last_commit, as_of, scoring.recency_half_life_days)
    frequency = _frequency_score(contrib.commits, max_commits)
    weight = scoring.recency_weight + scoring.frequency_weight
    if weight <= 0:
        return 0.0
    blended = scoring.recency_weight * recency + scoring.frequency_weight * frequency
    return _clamp(blended / weight)

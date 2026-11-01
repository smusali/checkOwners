"""Signal scoring: per-author recency, frequency, freshness and confidence.

This module holds the pure scoring primitives so that ``analyze`` (which
gathers the evidence) and ``providers`` (which replays stored evidence) can
both use them without importing each other.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from checkowners.config import (
    ScoringConfig,
)
from checkowners.models import (
    ConfidenceScore,
    FreshnessStatus,
    OwnerEntry,
    OwnershipFreshness,
    QualificationStrategy,
    SignalScore,
)

#: Shrinkage prior that keeps a single commit from scoring like a streak.
FREQUENCY_SHRINKAGE_PRIOR = 3.0


def frequency_prior_for(strategy: QualificationStrategy) -> float:
    """Return the frequency shrinkage prior `strategy` scores with."""
    return FREQUENCY_SHRINKAGE_PRIOR if strategy == "adaptive" else 0.0


@dataclass(frozen=True)
class Contribution:
    """Raw per-(path, author) signal aggregated from git log."""

    commits: int
    last_commit: datetime
    credit: float
    later_commits: int = 0
    cadence_days: float | None = None
    last_sha: str = ""


def combine_available_signals(
    signals: Mapping[str, tuple[float, bool]],
    weights: Mapping[str, float],
    reliabilities: Mapping[str, float],
) -> tuple[float, float]:
    """Return ``(ownership_score, evidence_quality)`` over available signals.

    ``ownership_score`` is the weighted mean of available scores so the range
    is always ``[0, 1]``. ``evidence_quality`` is the share of configured
    weight that was observed, scaled by each signal's reliability.
    """
    score_num = 0.0
    score_den = 0.0
    quality_num = 0.0
    quality_den = 0.0
    for name, (score, available) in signals.items():
        weight = weights.get(name, 0.0)
        quality_den += weight
        if not available:
            continue
        score_num += weight * score
        score_den += weight
        quality_num += weight * reliabilities.get(name, 1.0)
    ownership_score = clamp(score_num / score_den) if score_den else 0.0
    evidence_quality = clamp(quality_num / quality_den) if quality_den else 0.0
    return ownership_score, evidence_quality


def signal_weights(scoring: ScoringConfig) -> dict[str, float]:
    return {
        "recency": scoring.recency_weight,
        "frequency": scoring.frequency_weight,
        "blame": scoring.blame_weight,
        "review": scoring.review_weight,
        "change_survival": scoring.change_survival_weight,
    }


def signal_reliabilities(scoring: ScoringConfig) -> dict[str, float]:
    return {
        "recency": scoring.recency_reliability,
        "frequency": scoring.frequency_reliability,
        "blame": scoring.blame_reliability,
        "review": scoring.review_reliability,
        "change_survival": scoring.change_survival_reliability,
    }


def score_owners(
    qualified: dict[str, Contribution],
    path_blame: dict[str, float],
    path_review: dict[str, float],
    *,
    max_commits: float,
    scoring: ScoringConfig,
    now: datetime,
    blame_available: bool,
    review_available: bool,
    frequency_prior: float = 0.0,
    threshold_days: int = 180,
    survival: Mapping[str, float | None] | None = None,
) -> tuple[OwnerEntry, ...]:
    """Return owners of `qualified` scored at `now`."""
    if not qualified:
        return ()
    scored: list[OwnerEntry] = []
    weights = signal_weights(scoring)
    reliabilities = signal_reliabilities(scoring)
    path_last = max(contrib.last_commit for contrib in qualified.values())
    for author, contrib in qualified.items():
        half_life = effective_half_life(contrib.cadence_days, scoring)
        recency = recency_score(contrib.last_commit, now, half_life)
        frequency = frequency_score(frequency_credit(contrib), max_commits, frequency_prior)
        blame = path_blame.get(author, 0.0) if blame_available else 0.0
        review = clamp(path_review.get(author, 0.0)) if review_available else 0.0
        ratio = None if survival is None else survival.get(author)
        survival_available = blame_available and ratio is not None
        survival_score = 0.0 if ratio is None else ratio
        signals = {
            "recency": (recency, True),
            "frequency": (frequency, True),
            "blame": (blame, blame_available),
            "review": (review, review_available),
            "change_survival": (survival_score, survival_available),
        }
        total, quality = combine_available_signals(signals, weights, reliabilities)
        breakdown = ConfidenceScore(
            total=total,
            recency=SignalScore(available=True, score=recency),
            frequency=SignalScore(available=True, score=frequency),
            blame=SignalScore(available=blame_available, score=blame),
            review=SignalScore(available=review_available, score=review),
            change_survival=SignalScore(available=survival_available, score=survival_score),
        )
        days = elapsed_days(contrib.last_commit, now)
        scored.append(
            OwnerEntry(
                handle=author,
                ownership_score=total,
                last_commit=contrib.last_commit,
                commits=contrib.commits,
                evidence_quality=quality,
                score_breakdown=breakdown,
                freshness=OwnershipFreshness(
                    active_expertise=recency,
                    historical_expertise=historical_expertise(
                        contrib.commits, contrib.later_commits
                    ),
                    maintenance_recency=recency_score(path_last, now, half_life),
                    status=classify_freshness(
                        contrib,
                        days=days,
                        half_life_days=half_life,
                        path_last=path_last,
                        ceiling_days=scoring.recency_half_life_ceiling_days,
                        threshold_days=threshold_days,
                        survival=ratio if survival_available else None,
                    ),
                    half_life_days=half_life,
                ),
            )
        )
    scored.sort(key=lambda e: (-e.confidence, e.handle))
    return tuple(scored)


def effective_half_life(cadence_days: float | None, scoring: ScoringConfig) -> float:
    """Return the recency half-life in days for `cadence_days` under `scoring`."""
    if scoring.recency_strategy == "fixed" or cadence_days is None:
        return as_days(scoring.recency_half_life_days)
    return bounded_half_life(
        cadence_days,
        scoring.recency_half_life_floor_days,
        scoring.recency_half_life_ceiling_days,
    )


def as_days(value: int) -> float:
    return value + 0.0


def bounded_half_life(cadence_days: float, floor_days: int, ceiling_days: int) -> float:
    low = as_days(floor_days)
    high = as_days(ceiling_days)
    if cadence_days < low:
        return low
    if cadence_days > high:
        return high
    return cadence_days


def historical_expertise(commits: int, later_commits: int) -> float:
    total = commits + later_commits
    if total <= 0:
        return 0.0
    return clamp(commits / total)


SURVIVAL_SUPERSEDED = 0.2


def classify_freshness(
    contrib: Contribution,
    *,
    days: int,
    half_life_days: float,
    path_last: datetime,
    ceiling_days: int,
    threshold_days: int,
    survival: float | None = None,
) -> FreshnessStatus:
    if survival is None:
        if contrib.later_commits > contrib.commits:
            return "superseded"
    elif survival < SURVIVAL_SUPERSEDED and contrib.later_commits > 0:
        return "superseded"
    if contrib.cadence_days is None:
        if days > threshold_days:
            return "inactive"
        return "stable"
    if days <= half_life_days:
        return "stable"
    if contrib.last_commit == path_last and days <= ceiling_days:
        return "stable"
    return "inactive"


def recency_score(last_commit: datetime, now: datetime, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    delta_days = max(0.0, (now - last_commit).total_seconds() / 86400.0)
    return clamp(math.pow(0.5, delta_days / half_life_days))


def elapsed_days(last_commit: datetime, now: datetime) -> int:
    seconds = (now - last_commit).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 86400)


def frequency_credit(contrib: Contribution) -> float:
    """Return the frequency numerator for `contrib`."""
    return contrib.credit


def frequency_score(commits: float, max_commits: float, prior: float = 0.0) -> float:
    if max_commits <= 0:
        return 0.0
    return clamp(commits / (max_commits + prior))


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))

"""Scoring math: the weighted mean, evidence quality, freshness and qualification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from checkowners.config import (
    ScoringConfig,
)
from checkowners.models import (
    OwnerEntry,
    QualificationStrategy,
)
from checkowners.scoring import (
    FREQUENCY_SHRINKAGE_PRIOR,
    Contribution,
    combine_available_signals,
    effective_half_life,
    frequency_prior_for,
    score_owners,
    signal_reliabilities,
    signal_weights,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_ADA = "ada@example.com"


def _scored(
    contribution: Contribution,
    *,
    scoring: ScoringConfig | None = None,
    blame: dict[str, float] | None = None,
    reviews: dict[str, float] | None = None,
    max_commits: int = 2,
    blame_available: bool = False,
    review_available: bool = False,
    frequency_prior: float = 0.0,
    survival: float | None = None,
    now: datetime = _NOW,
) -> OwnerEntry:
    """Return the single owner `score_owners` produces for `contribution`."""
    owners = score_owners(
        {_ADA: contribution},
        blame or {},
        reviews or {},
        max_commits=max_commits,
        scoring=scoring or ScoringConfig(),
        now=now,
        blame_available=blame_available,
        review_available=review_available,
        frequency_prior=frequency_prior,
        survival=None if survival is None else {_ADA: survival},
    )
    assert len(owners) == 1
    return owners[0]


def _contribution(
    *,
    commits: int = 2,
    last_commit: datetime = _NOW,
    credit: float = 2.0,
    later_commits: int = 0,
    cadence_days: float | None = None,
) -> Contribution:
    return Contribution(
        commits=commits,
        last_commit=last_commit,
        credit=credit,
        later_commits=later_commits,
        cadence_days=cadence_days,
    )


def test_an_available_review_signal_raises_quality_not_the_score() -> None:
    contribution = _contribution(commits=3, credit=3.0)
    offline = _scored(
        contribution,
        blame={_ADA: 1.0},
        max_commits=3,
        blame_available=True,
    )
    online = _scored(
        contribution,
        blame={_ADA: 1.0},
        reviews={_ADA: 1.0},
        max_commits=3,
        blame_available=True,
        review_available=True,
    )
    assert offline.ownership_score == pytest.approx(1.0)
    assert online.ownership_score == pytest.approx(1.0)
    assert offline.evidence_quality == pytest.approx(0.70)
    assert online.evidence_quality == pytest.approx(0.85)


def test_one_available_signal_still_spans_the_unit_interval() -> None:
    scoring = ScoringConfig()
    score, quality = combine_available_signals(
        {
            "recency": (1.0, True),
            "frequency": (0.0, False),
            "blame": (0.0, False),
            "review": (0.0, False),
            "change_survival": (0.0, False),
        },
        signal_weights(scoring),
        signal_reliabilities(scoring),
    )
    assert score == pytest.approx(1.0)
    assert quality == pytest.approx(0.30)


@pytest.mark.parametrize(
    ("signals", "weights", "reliabilities", "score", "quality"),
    [
        ({}, {}, {}, 0.0, 0.0),
        ({"mystery": (1.0, True)}, {}, {}, 0.0, 0.0),
        (
            {"known": (0.0, False), "mystery": (1.0, True)},
            {"known": 1.0, "mystery": 1.0},
            {"known": 1.0},
            1.0,
            0.5,
        ),
        ({"recency": (1.0, True)}, {"recency": 1.0}, {"recency": 0.5}, 1.0, 0.5),
    ],
    ids=["nothing", "unweighted", "weighted-unknown", "scaled-reliability"],
)
def test_a_signal_without_a_weight_cannot_invent_a_score(
    signals: dict[str, tuple[float, bool]],
    weights: dict[str, float],
    reliabilities: dict[str, float],
    score: float,
    quality: float,
) -> None:
    combined, measured = combine_available_signals(signals, weights, reliabilities)
    assert combined == pytest.approx(score)
    assert measured == pytest.approx(quality)


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [(0, 1.0), (90, 0.5), (180, 0.25)],
)
def test_recency_decays_by_one_half_per_half_life(age_days: int, expected: float) -> None:
    scoring = ScoringConfig(recency_strategy="fixed", recency_half_life_days=90)
    entry = _scored(
        _contribution(last_commit=_NOW - timedelta(days=age_days)),
        scoring=scoring,
    )
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.recency.score == pytest.approx(expected, abs=0.01)


def test_a_zero_half_life_keeps_recency_at_one() -> None:
    scoring = ScoringConfig(recency_strategy="fixed", recency_half_life_days=0)
    entry = _scored(_contribution(last_commit=_NOW - timedelta(days=10)), scoring=scoring)
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.recency.score == 1.0


@pytest.mark.parametrize(
    ("commits", "max_commits", "expected"),
    [(3, 2, 1.0), (1, 4, 0.25), (2, 2, 1.0)],
)
def test_frequency_is_the_damped_share_of_the_busiest_author(
    commits: int,
    max_commits: int,
    expected: float,
) -> None:
    entry = _scored(
        _contribution(commits=commits, credit=float(commits)),
        max_commits=max_commits,
    )
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.frequency.score == pytest.approx(expected)


def test_a_frequency_prior_shrinks_a_small_sample() -> None:
    entry = _scored(_contribution(), frequency_prior=3.0)
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.frequency.score == pytest.approx(2 / 5)


@pytest.mark.parametrize(
    ("commits", "later_commits", "expected"),
    [(1, 0, 1.0), (3, 1, 0.75), (1, 3, 0.25)],
)
def test_historical_expertise_is_this_authors_share_of_the_commits(
    commits: int,
    later_commits: int,
    expected: float,
) -> None:
    entry = _scored(
        _contribution(commits=commits, credit=float(commits), later_commits=later_commits),
        max_commits=commits,
    )
    assert entry.freshness is not None
    assert entry.freshness.historical_expertise == pytest.approx(expected)


@pytest.mark.parametrize(
    ("later_commits", "cadence_days", "age_days", "survival", "expected"),
    [
        (2, None, 0, None, "stable"),
        (3, None, 0, None, "superseded"),
        (3, None, 0, 0.0, "superseded"),
        (3, None, 0, 0.19, "superseded"),
        (3, None, 0, 0.2, "stable"),
        (0, None, 0, 1.0, "stable"),
        (0, None, 180, None, "stable"),
        (0, None, 181, None, "inactive"),
        (0, 10.0, 10, None, "stable"),
        (0, 10.0, 30, None, "stable"),
    ],
    ids=[
        "tied-later-commits",
        "outnumbered",
        "no-lines-left",
        "below-survival-floor",
        "at-survival-floor",
        "all-lines-left",
        "at-decay-threshold",
        "past-decay-threshold",
        "within-cadence",
        "at-cadence-ceiling",
    ],
)
def test_freshness_uses_exact_boundaries(
    later_commits: int,
    cadence_days: float | None,
    age_days: int,
    survival: float | None,
    expected: str,
) -> None:
    contribution = _contribution(
        last_commit=_NOW - timedelta(days=age_days),
        later_commits=later_commits,
        cadence_days=cadence_days,
    )
    entry = _scored(
        contribution,
        scoring=ScoringConfig(recency_strategy="adaptive"),
        survival=survival,
        blame_available=survival is not None,
    )
    assert entry.freshness is not None
    assert entry.freshness.status == expected


def test_an_unavailable_signal_scores_zero_and_is_marked_unavailable() -> None:
    entry = _scored(_contribution(), blame={_ADA: 1.0}, reviews={_ADA: 1.0})
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.blame.score == 0.0
    assert entry.score_breakdown.blame.available is False
    assert entry.score_breakdown.review.score == 0.0
    assert entry.score_breakdown.review.available is False


def test_the_breakdown_total_equals_the_reported_score() -> None:
    scoring = ScoringConfig(
        recency_strategy="fixed",
        recency_half_life_days=90,
        recency_weight=0.25,
        frequency_weight=0.25,
        blame_weight=0.25,
        review_weight=0.25,
        change_survival_weight=0.0,
        recency_reliability=0.5,
        frequency_reliability=0.5,
        blame_reliability=0.5,
        review_reliability=0.5,
    )
    entry = _scored(_contribution(), scoring=scoring, blame_available=True, review_available=True)
    assert entry.commits == 2
    assert entry.last_commit == _NOW
    assert entry.evidence_quality == pytest.approx(0.5)
    assert entry.freshness is not None
    assert entry.freshness.half_life_days == 90.0
    assert entry.freshness.maintenance_recency == pytest.approx(1.0)
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.total == pytest.approx(entry.ownership_score)


@pytest.mark.parametrize(
    ("strategy", "prior"),
    [("adaptive", FREQUENCY_SHRINKAGE_PRIOR), ("threshold", 0.0)],
)
def test_the_frequency_prior_follows_the_qualification_strategy(
    strategy: QualificationStrategy,
    prior: float,
) -> None:
    assert frequency_prior_for(strategy) == prior


def test_a_fixed_half_life_ignores_the_path_cadence() -> None:
    scoring = ScoringConfig(recency_strategy="fixed", recency_half_life_days=90)
    assert effective_half_life(3.0, scoring) == 90.0


@given(
    cadence=st.floats(min_value=0.01, max_value=100_000, allow_nan=False, allow_infinity=False),
    floor=st.integers(min_value=1, max_value=100),
    ceiling=st.integers(min_value=101, max_value=5_000),
)
def test_an_adaptive_half_life_stays_inside_its_bounds(
    cadence: float,
    floor: int,
    ceiling: int,
) -> None:
    scoring = ScoringConfig(
        recency_strategy="adaptive",
        recency_half_life_floor_days=floor,
        recency_half_life_ceiling_days=ceiling,
    )
    assert floor <= effective_half_life(cadence, scoring) <= ceiling


def test_no_qualified_author_scores_nothing() -> None:
    assert (
        score_owners(
            {},
            {},
            {},
            max_commits=0,
            scoring=ScoringConfig(),
            now=_NOW,
            blame_available=True,
            review_available=True,
        )
        == ()
    )

"""Tests for checkowners.models JSON helpers."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    DEFAULT_TRUCK_FACTOR_THRESHOLDS,
    GAP_CATALOG,
    AnalysisCompleteness,
    AnalysisGap,
    ConfidenceScore,
    OwnerEntry,
    OwnershipMap,
    SignalScore,
    _coverage_count,
    completeness_score,
    envelope_completeness,
    flat_signal_scores,
    history_evidence_gaps,
    knowledge_concentration,
    merge_gaps,
    repository_label,
    risk_from_scores,
    run_completeness,
    signal_completeness,
    stamp_json,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_ZERO_RISK = {
    "top_owner_share": 0.0,
    "effective_owners": 0.0,
    "truck_factor_50": 0,
    "truck_factor_75": 0,
    "truck_factor_90": 0,
    "shannon_entropy": 0.0,
    "hhi": 0.0,
    "minor_contributor_share": 0.0,
    "major_contributor_count": 0,
    "truck_factor_thresholds": list(DEFAULT_TRUCK_FACTOR_THRESHOLDS),
}
_POSITIVE_SCORES = st.lists(
    st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=12,
)


def _signal(available: bool, score: float = 0.0) -> SignalScore:
    return SignalScore(available=available, score=score)


def _owner(
    handle: str,
    *,
    review: float | None = None,
    breakdown: bool = True,
) -> OwnerEntry:
    score_breakdown = None
    if breakdown:
        score_breakdown = ConfidenceScore(
            total=0.5,
            recency=_signal(False),
            frequency=_signal(False),
            blame=_signal(False),
            review=_signal(review is not None, review or 0.0),
        )
    return OwnerEntry(
        handle=handle,
        ownership_score=0.5,
        last_commit=_NOW,
        commits=2,
        score_breakdown=score_breakdown,
    )


def test_repository_label_prefers_github_repository(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"GITHUB_REPOSITORY": " acme/widgets "}):
        assert repository_label(tmp_path) == "acme/widgets"


def test_envelope_reports_a_stored_score_and_omits_gaps_when_unset() -> None:
    assert envelope_completeness(None) == (None, None)
    bare = OwnershipMap(paths={}, last_analyzed=_NOW, analysis_ref="abc")
    score, gaps = envelope_completeness(bare)
    assert score == 0.0
    assert gaps is None
    stored = replace(
        bare,
        analysis_completeness=AnalysisCompleteness(
            score=0.5,
            gaps=(AnalysisGap("missing_mailmap", "Missing .mailmap."),),
        ),
    )
    assert run_completeness(stored) == 0.5
    assert envelope_completeness(stored) == (
        0.5,
        [{"code": "missing_mailmap", "reason": "Missing .mailmap."}],
    )
    stamped = stamp_json(
        {"ok": True},
        repository="acme/widgets",
        head_sha="abc",
        generated_at=_NOW.isoformat(),
        analysis_completeness=0.5,
        analysis_gaps=[{"code": "missing_mailmap", "reason": "Missing .mailmap."}],
    )
    assert stamped["analysis_gaps"] == [{"code": "missing_mailmap", "reason": "Missing .mailmap."}]


def test_stamp_json_merges_external_evidence() -> None:
    stamped = stamp_json(
        {"ok": True},
        repository="acme/widgets",
        head_sha="abc",
        generated_at=_NOW.isoformat(),
        analysis_completeness=None,
        evidence={"repository_head": "abc", "github_evidence_collected_at": _NOW.isoformat()},
    )
    assert stamped["ok"] is True
    assert stamped["schema_version"] == COMMAND_SCHEMA_VERSION
    assert stamped["repository_head"] == "abc"
    assert stamped["github_evidence_collected_at"] == _NOW.isoformat()


def test_flat_signal_scores_keep_available_review() -> None:
    assert flat_signal_scores(_owner("@alice", review=0.25)) == {"review": 0.25}


def test_signal_completeness_is_zero_without_owners() -> None:
    assert signal_completeness(()) == (0.0, [])


def test_signal_completeness_skips_missing_breakdowns() -> None:
    fraction, names = signal_completeness(
        (_owner("@bare", breakdown=False), _owner("@alice", review=0.25))
    )
    assert names == ["review"]
    assert fraction == round(1 / 8, 4)


def test_risk_from_scores_without_positive_mass() -> None:
    assert risk_from_scores(()) == _ZERO_RISK
    assert risk_from_scores((0.0, -1.0)) == _ZERO_RISK


@pytest.mark.parametrize(
    ("scores", "effective"),
    [
        ((1.0, 0.0, 0.0, 0.0), 1.0),
        ((0.5, 0.5), 2.0),
        ((0.25, 0.25, 0.25, 0.25), 4.0),
    ],
)
def test_effective_owners_canonical_distributions(
    scores: tuple[float, ...],
    effective: float,
) -> None:
    assert risk_from_scores(scores)["effective_owners"] == effective


def test_dominant_owner_truck_factor_50() -> None:
    risk = risk_from_scores((0.82, 0.07, 0.05, 0.03, 0.02, 0.01))
    assert risk["top_owner_share"] == 0.82
    assert risk["truck_factor_50"] == 1
    assert risk["truck_factor_thresholds"] == [0.5, 0.75, 0.9]


def test_truck_factor_thresholds_are_positional() -> None:
    risk = risk_from_scores((0.82, 0.18), (0.4, 0.6, 0.95))
    assert risk["truck_factor_thresholds"] == [0.4, 0.6, 0.95]
    assert risk["truck_factor_50"] == 1
    assert risk["truck_factor_75"] == 1
    assert risk["truck_factor_90"] == 2


@given(scores=_POSITIVE_SCORES)
def test_concentration_bounds_and_shares(scores: list[float]) -> None:
    total = sum(scores)
    shares = tuple(score / total for score in scores)
    assert abs(sum(shares) - 1.0) < 1e-9
    concentration = knowledge_concentration(tuple(scores))
    assert 1.0 <= concentration.effective_owners <= len(scores) + 1e-4
    assert concentration.top_owner_share == round(max(shares), 4)
    assert (
        concentration.truck_factor_50
        <= concentration.truck_factor_75
        <= concentration.truck_factor_90
    )


@given(
    scores=_POSITIVE_SCORES,
    quantiles=st.lists(
        st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=6,
        unique=True,
    ),
)
def test_truck_factor_is_monotonic_in_q(scores: list[float], quantiles: list[float]) -> None:
    total = sum(scores)
    shares = tuple(sorted((score / total for score in scores), reverse=True))
    counts = tuple(_coverage_count(shares, quantile) for quantile in sorted(quantiles))
    assert counts == tuple(sorted(counts))


def test_coverage_count_uses_every_share_below_the_threshold() -> None:
    assert _coverage_count((0.1, 0.1), 0.75) == 2


def test_each_history_gap_drops_completeness_once() -> None:
    flags = (
        ({"shallow": True}, "Shallow history: the clone does not contain full git history."),
        ({"insufficient": True}, "Insufficient history: no commits in the lookback window."),
        ({"renamed": True}, "Unresolved renames: path history is not followed across renames."),
        ({"mailmap_missing": True}, "Missing .mailmap."),
        ({"ignore_revs_missing": True}, "Missing .git-blame-ignore-revs."),
        (
            {"excluded_gitattributes": 2, "excluded_static": 1},
            "Excluded files: 2 gitattributes, 1 static.",
        ),
        ({"review_missing": True}, "Review history unavailable."),
        ({"runtime_truncated": True}, "Analysis incomplete: runtime budget exhausted."),
    )
    seen: set[str] = set()
    base = {
        "shallow": False,
        "insufficient": False,
        "renamed": False,
        "mailmap_missing": False,
        "ignore_revs_missing": False,
        "excluded_gitattributes": 0,
        "excluded_static": 0,
        "review_missing": False,
        "runtime_truncated": False,
    }
    for flag, reason in flags:
        gaps = history_evidence_gaps(**{**base, **flag})
        assert len(gaps) == 1
        assert gaps[0].reason == reason
        assert completeness_score(gaps) == round((len(GAP_CATALOG) - 1) / len(GAP_CATALOG), 4)
        seen.add(gaps[0].code)
    assert seen == {
        "shallow_history",
        "insufficient_history",
        "unresolved_renames",
        "missing_mailmap",
        "missing_ignore_revs",
        "excluded_files",
        "review_history",
        "runtime_budget",
    }
    duplicated = merge_gaps(gaps, gaps)
    assert len(duplicated) == 1
    assert completeness_score(
        (AnalysisGap("api_budget", "spent"), AnalysisGap("absent_token", "none"))
    ) == round((len(GAP_CATALOG) - 2) / len(GAP_CATALOG), 4)

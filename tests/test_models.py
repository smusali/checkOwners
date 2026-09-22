"""Tests for checkowners.models JSON helpers."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    ConfidenceScore,
    OwnerEntry,
    SignalScore,
    _coverage_count,
    flat_signal_scores,
    repository_label,
    risk_from_scores,
    signal_completeness,
    stamp_json,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_ZERO_RISK = {
    "top_owner_share": 0.0,
    "effective_owners": 0.0,
    "truck_factor_50": 0,
    "truck_factor_75": 0,
}


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


def test_coverage_count_uses_every_share_below_the_threshold() -> None:
    assert _coverage_count((0.1, 0.1), 0.75) == 2

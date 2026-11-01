"""Drift severity: the weighted score, its cutoffs and the hysteresis that holds it."""

from __future__ import annotations

from checkowners.config import (
    BusFactorConfig,
    Config,
    DriftConfig,
)
from checkowners.drift import (
    _signal_evidence,
    apply_severity_hysteresis,
    compute_severity,
    reconciliation_body,
)
from checkowners.models import (
    ConfidenceScore,
    DriftEntry,
    DriftResult,
    OwnerEntry,
    SignalScore,
)
from tests.conftest import (
    NOW,
    declared_ownership,
    owner_entry,
)


def _scored(
    *,
    owners: tuple[str, ...],
    observed: tuple[str, ...],
    delta: float,
    qualified_owner_count: int | None = None,
    decay: bool = False,
) -> DriftResult:
    entry = DriftEntry(
        path="src/a.py",
        confidence_delta=delta,
        owners=owners,
        observed_owners=observed,
        qualified_owner_count=qualified_owner_count,
        decay=decay,
    )
    return DriftResult(stale=(), missing=(), changed=(entry,), drift_detected=True)


def _drift_with(
    *,
    delta: float = 1.0,
    qualified_owner_count: int | None = None,
    decay: bool = False,
    detected: bool = True,
) -> DriftResult:
    if not detected:
        return DriftResult(stale=(), missing=(), changed=(), drift_detected=False)
    entry = DriftEntry(
        path="/src/main.py",
        confidence_delta=delta,
        reason="test",
        qualified_owner_count=qualified_owner_count,
        decay=decay,
    )
    return DriftResult(stale=(entry,), missing=(), changed=(), drift_detected=True)


def test_compute_severity_uses_overlap_not_bus_factor() -> None:
    superset = _scored(
        owners=("@alice", "@bob"),
        observed=("@alice", "@bob", "@carol"),
        delta=0.5,
    )
    disjoint = _scored(owners=("@alice", "@bob"), observed=("@dave", "@erin"), delta=0.5)
    assert compute_severity(superset) == "low"
    assert compute_severity(disjoint) == "medium"
    solo = _scored(owners=("@alice",), observed=("@bob",), delta=0.4, qualified_owner_count=1)
    assert compute_severity(solo) == "high"
    critical = _scored(
        owners=("@alice",),
        observed=("@bob",),
        delta=1.0,
        qualified_owner_count=1,
        decay=True,
    )
    assert compute_severity(critical) == "critical"


def test_compute_severity_no_drift_is_info() -> None:
    assert compute_severity(_drift_with(detected=False)) == "info"


def test_compute_severity_uses_configured_thresholds() -> None:
    loose = Config(drift=DriftConfig(severity_thresholds=(0.5, 0.6, 0.7, 0.8)))
    disjoint = _scored(owners=("@alice", "@bob"), observed=("@dave", "@erin"), delta=0.5)
    assert compute_severity(disjoint) == "medium"
    assert compute_severity(disjoint, loose) == "low"


def test_hysteresis_default_reports_raw() -> None:
    config = Config()
    reported, pending, streak = apply_severity_hysteresis("medium", 0.35, config, (None, None, 0))
    assert (reported, pending, streak) == ("medium", "medium", 1)


def test_hysteresis_holds_until_streak() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3))
    first = apply_severity_hysteresis("medium", 0.35, config, ("low", None, 0))
    assert first[0] == "low"
    assert first[1] == "medium"
    assert first[2] == 1
    second = apply_severity_hysteresis("medium", 0.35, config, first)
    assert second[0] == "low"
    assert second[2] == 2
    third = apply_severity_hysteresis("medium", 0.35, config, second)
    assert third[0] == "medium"
    assert third[2] == 3


def test_hysteresis_margin_flips_immediately() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3, min_confidence_delta=0.2))
    reported, pending, streak = apply_severity_hysteresis("high", 0.4, config, ("low", None, 0))
    assert (reported, pending, streak) == ("high", "high", 1)
    held = apply_severity_hysteresis(
        "high",
        0.2,
        Config(drift=DriftConfig(hysteresis_runs=2, min_confidence_delta=0.2)),
        ("low", None, 0),
    )
    assert held[0] == "low"
    immediate = apply_severity_hysteresis(
        "high",
        0.1,
        Config(drift=DriftConfig(hysteresis_runs=1, min_confidence_delta=0.2)),
        ("low", "high", 2),
    )
    assert immediate == ("high", "high", 1)


def test_hysteresis_same_as_reported_resets() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3))
    reported, pending, streak = apply_severity_hysteresis("low", 0.1, config, ("low", "medium", 2))
    assert (reported, pending, streak) == ("low", "low", 1)


def test_hysteresis_pending_resets_when_severity_changes() -> None:
    config = Config(drift=DriftConfig(hysteresis_runs=3))
    reported, pending, streak = apply_severity_hysteresis(
        "high", 0.35, config, ("low", "medium", 2)
    )
    assert (reported, pending, streak) == ("low", "high", 1)


def test_reconciliation_body_states_evidence_when_owners_agree() -> None:
    body = reconciliation_body(
        DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        declared_ownership({}),
        Config(),
    )
    assert body == (
        "<!-- checkowners-reconcile -->\n"
        "\n"
        "Inferred ownership is evidence, not accountability.\n"
        "\n"
        "Severity: info\n"
        "\n"
        "The generated file changed without an owner-set disagreement.\n"
    )


def test_reconciliation_body_includes_scores_severity_and_direction() -> None:
    without_breakdown = OwnerEntry(
        handle="@ghost",
        ownership_score=0.8,
        last_commit=NOW,
        commits=5,
    )
    assert _signal_evidence(without_breakdown) == ""
    partial = ConfidenceScore(
        total=0.0,
        recency=SignalScore(available=False),
        frequency=SignalScore(available=True, score=0.7),
        blame=SignalScore(available=False),
        review=SignalScore(available=True, score=0.2),
    )
    assert (
        _signal_evidence(
            OwnerEntry(
                handle="@ghost",
                ownership_score=0.0,
                last_commit=None,
                commits=0,
                score_breakdown=partial,
            )
        )
        == "frequency 0.70, review 0.20"
    )
    breakdown = ConfidenceScore(
        total=0.9,
        recency=SignalScore(available=True, score=0.8),
        frequency=SignalScore(available=True, score=0.7),
        blame=SignalScore(available=True, score=0.1),
        review=SignalScore(available=True, score=0.2),
    )
    ownership = declared_ownership(
        {
            "src/a.py": (
                owner_entry("@bob", 0.5),
                owner_entry("@alice", 0.4),
                OwnerEntry(
                    handle="@alice",
                    ownership_score=0.9,
                    last_commit=NOW,
                    commits=8,
                    evidence_quality=0.95,
                    score_breakdown=breakdown,
                ),
                OwnerEntry(
                    handle="@alice",
                    ownership_score=0.9,
                    last_commit=NOW,
                    commits=1,
                    evidence_quality=0.1,
                ),
                owner_entry("@alice", 0.2),
            )
        }
    )
    result = DriftResult(
        stale=(
            DriftEntry(
                path="/old/",
                confidence_delta=0.1,
                reason="matches nothing",
                qualified_owner_count=2,
                drift_type="stale_rule",
            ),
        ),
        missing=(
            DriftEntry(
                path="src/a.py",
                confidence_delta=0.9,
                reason="no declared owner",
                observed_owners=("@ghost", "@alice"),
                drift_type="missing_observed_expert",
            ),
        ),
        changed=(
            DriftEntry(
                path="/src/",
                confidence_delta=0.5,
                reason="owners diverge",
                owners=("@old", "@older"),
                observed_owners=("@alice",),
                drift_type="organizational_mismatch",
            ),
        ),
        drift_detected=True,
    )
    body = reconciliation_body(
        result,
        ownership,
        Config(bus_factor=BusFactorConfig(critical_threshold=5)),
    )
    observed = (
        "Observed: @alice ownership score 0.90, evidence quality 0.95, "
        "commits 8, last commit 2026-05-28T12:00:00+00:00\n"
        "Evidence: recency 0.80, frequency 0.70, blame 0.10, review 0.20"
    )
    assert body == (
        "<!-- checkowners-reconcile -->\n"
        "\n"
        "Inferred ownership is evidence, not accountability.\n"
        "\n"
        "Severity: high\n"
        "\n"
        "## /old/\n"
        "\n"
        "Direction: stale rule\n"
        "Severity: info\n"
        "Why: matches nothing\n"
        "\n"
        "## src/a.py\n"
        "\n"
        "Direction: missing observed expert\n"
        "Severity: high\n"
        "Why: no declared owner\n"
        "Observed: @ghost\n"
        f"{observed}\n"
        "\n"
        "## /src/\n"
        "\n"
        "Direction: organizational mismatch\n"
        "Severity: medium\n"
        "Why: owners diverge\n"
        "Declared: @old @older\n"
        f"{observed}\n"
    )

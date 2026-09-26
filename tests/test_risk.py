"""Tests for checkowners.risk module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from checkowners.models import (
    ConfidenceScore,
    Config,
    DriftResult,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    SignalScore,
)
from checkowners.patterns import CodeownersRule
from checkowners.risk import RiskReport, assess_risk, risk_payload

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

_FACTORS = {
    "ownership_concentration",
    "change_frequency",
    "dependency_criticality",
    "code_criticality",
    "expertise_decay",
}


def _fresh(active: float = 0.9) -> OwnershipFreshness:
    return OwnershipFreshness(
        active_expertise=active,
        historical_expertise=0.5,
        maintenance_recency=active,
        status="stable",
        half_life_days=30.0,
    )


def _signaled(
    handle: str,
    score: float,
    *,
    commits: int = 5,
    blame: float = 0.0,
    review: float = 0.0,
    last_commit: datetime | None = None,
    freshness: OwnershipFreshness | None = None,
) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=_NOW if last_commit is None else last_commit,
        commits=commits,
        score_breakdown=ConfidenceScore(
            total=score,
            recency=SignalScore(available=True, score=score),
            frequency=SignalScore(available=True, score=score),
            blame=SignalScore(available=True, score=blame),
            review=SignalScore(available=True, score=review),
        ),
        freshness=freshness,
    )


def _risk_report(
    paths: dict[str, PathOwnership],
    rules: tuple[CodeownersRule, ...] = (),
) -> RiskReport:
    ownership = OwnershipMap(paths=paths, last_analyzed=_NOW)
    drift = DriftResult(stale=(), missing=(), changed=(), drift_detected=False)
    return assess_risk(ownership, Config(), rules, drift)


def test_high_churn_scores_above_low_churn() -> None:
    expert = _signaled("@alice", 1.0, freshness=_fresh())
    hot = PathOwnership(
        owners=(expert,),
        qualified_owner_count=1,
        scored_owners=(expert,),
        change_events=40,
        cadence_days=0.2,
    )
    cold = PathOwnership(
        owners=(expert,),
        qualified_owner_count=1,
        scored_owners=(expert,),
        change_events=1,
    )
    report = _risk_report({"hot.py": hot, "cold.py": cold})
    payload = risk_payload(report)
    entries = payload["entries"]
    assert isinstance(entries, list)
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["hot.py"]["risk"] > by_path["cold.py"]["risk"]
    for path in ("hot.py", "cold.py"):
        factors = by_path[path]["factors"]
        assert isinstance(factors, dict)
        assert set(factors) == _FACTORS


def test_pathology_kinds() -> None:
    stale = _signaled("@alice", 0.21, last_commit=_NOW - timedelta(days=742))
    vacuum = _risk_report(
        {
            "legacy/auth.py": PathOwnership(
                owners=(stale,),
                qualified_owner_count=0,
                scored_owners=(stale,),
                change_events=2,
                cadence_days=30.0,
            )
        }
    )
    vacuum_hit = next(item for item in vacuum.pathologies if item.kind == "knowledge-vacuum")
    assert "742" in vacuum_hit.detail
    assert "0.21" in vacuum_hit.detail

    alice = _signaled("@alice", 0.9, commits=6)
    phantom = _risk_report(
        {
            "src/auth.py": PathOwnership(
                owners=(alice,),
                qualified_owner_count=1,
                scored_owners=(alice,),
                change_events=4,
                cadence_days=8.0,
            )
        },
        (CodeownersRule("src/auth.py", ("@alice", "@carol"), 1),),
    )
    phantom_hit = next(item for item in phantom.pathologies if item.kind == "phantom-ownership")
    assert phantom_hit.detail == "declared ownership without observed expertise"

    weak = _signaled("@alice", 0.4, commits=2)
    strong = _signaled("@bob", 0.9, commits=8)
    shadow = _risk_report(
        {
            "pay.py": PathOwnership(
                owners=(strong, weak),
                qualified_owner_count=2,
                scored_owners=(strong, weak),
                change_events=5,
                cadence_days=4.0,
            )
        },
        (CodeownersRule("pay.py", ("@alice",), 1),),
    )
    shadow_hit = next(item for item in shadow.pathologies if item.kind == "shadow-maintainer")
    assert "@bob" in shadow_hit.detail
    assert "@alice" in shadow_hit.detail

    writer = _signaled("@alice", 0.8, commits=10, blame=0.9, review=0.1)
    reviewer = _signaled("@bob", 0.7, commits=2, blame=0.2, review=0.85)
    declared = _signaled("@carol", 0.95, commits=4, blame=0.05, review=0.05)
    divergence = _risk_report(
        {
            "src/core.py": PathOwnership(
                owners=(declared, writer, reviewer),
                qualified_owner_count=3,
                scored_owners=(declared, writer, reviewer),
                change_events=6,
                cadence_days=5.0,
            )
        },
        (CodeownersRule("src/core.py", ("@carol",), 1),),
    )
    split = next(
        item for item in divergence.pathologies if item.kind == "ownership-review-divergence"
    )
    assert split.writer == "@alice"
    assert split.reviewer == "@bob"
    assert split.declared == "@carol"


def test_criticality_unavailable_without_sources() -> None:
    owner = _signaled("@alice", 0.8, freshness=_fresh())
    report = _risk_report(
        {
            "src/lib.py": PathOwnership(
                owners=(owner,),
                qualified_owner_count=1,
                scored_owners=(owner,),
            )
        }
    )
    assert report.criticality_unavailable is True

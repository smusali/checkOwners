"""Tests for checkowners.risk module."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from checkowners.models import (
    ConfidenceScore,
    Config,
    DriftEntry,
    DriftResult,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    SignalScore,
)
from checkowners.patterns import CodeownersRule
from checkowners.risk import (
    Pathology,
    RiskReport,
    _geometric_mean,
    _writer,
    assess_risk,
    risk_payload,
)

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


def _breakdown(
    score: float,
    *,
    blame: bool = True,
    review: float | None = None,
) -> ConfidenceScore:
    return ConfidenceScore(
        total=score,
        recency=SignalScore(available=True, score=score),
        frequency=SignalScore(available=True, score=score),
        blame=SignalScore(available=blame, score=score if blame else 0.0),
        review=SignalScore(available=review is not None, score=0.0 if review is None else review),
    )


def _owner(
    handle: str,
    score: float,
    *,
    commits: int = 4,
    last_commit: datetime | None = _NOW,
    freshness: OwnershipFreshness | None = None,
    breakdown: ConfidenceScore | None = None,
) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=last_commit,
        commits=commits,
        score_breakdown=breakdown,
        freshness=freshness,
    )


def _path(
    owners: tuple[OwnerEntry, ...],
    *,
    events: int | None = None,
    cadence: float | None = None,
    degree: float | None = None,
    candidates: tuple[OwnerEntry, ...] = (),
) -> PathOwnership:
    return PathOwnership(
        owners=owners,
        qualified_owner_count=len(owners),
        scored_owners=owners,
        candidates=candidates,
        change_events=events,
        cadence_days=cadence,
        cochange_degree=degree,
    )


def test_factor_edges_reasons_and_drift() -> None:
    tiny = _signaled("@ann", 1.0, freshness=_fresh(0.99))
    overflow = _owner("@bee", 1.0, freshness=_fresh(-0.1))
    balanced = tuple(_owner(name, 1.0) for name in ("@a", "@b", "@c"))
    paths = {
        "slow.py": _path((tiny,), events=4, cadence=1_000_000),
        "over.py": _path((_signaled("@cay", 1.0),), degree=1.5),
        "linked.py": _path((_signaled("@dee", 1.0),), degree=0.4),
        "idle.py": _path((_signaled("@eve", 1.0),), degree=0.0),
        "globbed.py": _path((_signaled("@fay", 1.0),)),
        "low.py": _path((_signaled("@gus", 1.0),), events=20, cadence=1.0, degree=0.9),
        "Dockerfile": _path((_signaled("@hue", 1.0),)),
        "infra/main.tf": _path((_owner("@ivy", 0.8),)),
        "docker-compose.yml": _path((_owner("@jay", 0.8),)),
        "compose.yaml": _path((_owner("@kay", 0.8),)),
        "docker-compose.prod.yml": _path((_owner("@lee", 0.8),)),
        "composer.yml": _path((_owner("@moe", 0.8),)),
        "k8s/app.yaml": _path((_owner("@nia", 0.8),)),
        "deploy/app.yaml": _path((_owner("@oak", 0.8),)),
        "stale.py": _path((overflow,)),
        "balanced.py": _path(balanced),
    }
    config = Config(criticality=(("over.py", 1.5), ("globbed.py", 0.5), ("low.py", 0.1)))
    drift = DriftResult(
        stale=(
            DriftEntry(path="hot", confidence_delta=0.9),
            DriftEntry(path="few", confidence_delta=0.1, qualified_owner_count=1),
            DriftEntry(path="decayed", confidence_delta=0.1, decay=True),
            DriftEntry(path="quiet", confidence_delta=0.1, qualified_owner_count=5),
            DriftEntry(path="unknown", confidence_delta=0.1),
        ),
        missing=(),
        changed=(),
        drift_detected=True,
    )
    report = assess_risk(OwnershipMap(paths=paths, last_analyzed=_NOW), config, (), drift)
    by_path = {entry.path: entry for entry in report.entries}
    assert by_path["slow.py"].factors.change_frequency == 0.0001
    assert by_path["over.py"].factors.code_criticality == 1.0
    assert by_path["over.py"].factors.dependency_criticality == 1.0
    assert by_path["linked.py"].factors.dependency_criticality == 0.4
    assert by_path["idle.py"].factors.dependency_criticality is None
    assert by_path["globbed.py"].factors.code_criticality == 0.5
    assert by_path["Dockerfile"].factors.code_criticality == 0.8
    assert by_path["infra/main.tf"].factors.code_criticality == 0.8
    assert by_path["docker-compose.yml"].factors.code_criticality == 0.8
    assert by_path["slow.py"].factors.expertise_decay == 0.05
    assert by_path["stale.py"].factors.expertise_decay == 1.0
    assert "dependency criticality" in by_path["low.py"].reason
    assert "low criticality" in by_path["low.py"].reason
    assert by_path["balanced.py"].reason == "low criticality"
    assert report.criticality_unavailable is False
    assert report.summary.high_codeowners_drift == 3
    manifest_only = assess_risk(
        OwnershipMap(paths={"Dockerfile": paths["Dockerfile"]}, last_analyzed=_NOW),
        Config(),
        (),
        DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
    )
    assert manifest_only.criticality_unavailable is False
    coupled = assess_risk(
        OwnershipMap(
            paths={"lib.py": _path((_owner("@ann", 0.8),), degree=0.2)},
            last_analyzed=_NOW,
        ),
        Config(),
        (),
        DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
    )
    assert coupled.criticality_unavailable is False


def test_pathology_edges_and_role_payload() -> None:
    unknown = _owner("@ann", 0.1, last_commit=None)
    reviewer_only = _owner("@bea", 0.2, commits=0, breakdown=_breakdown(0.2, review=0.4))
    untouched = _owner("@cam", 0.1, commits=0, breakdown=_breakdown(0.1, review=0.0))
    no_signal = _owner("@dot", 0.4, commits=0)
    writer = _owner("@erin", 0.8, commits=10, breakdown=_breakdown(0.8, blame=False, review=0.1))
    reviewer = _owner("@fran", 0.7, commits=2, breakdown=_breakdown(0.7, blame=False, review=0.85))
    same = _owner("@gale", 0.9, commits=6, breakdown=_breakdown(0.9, review=0.5))
    zero_review = _owner("@hank", 0.6, commits=3, breakdown=_breakdown(0.6, review=0.0))
    fresh = _signaled("@ines", 0.8, freshness=_fresh())
    departed = OwnerEntry(
        handle=fresh.handle,
        ownership_score=fresh.ownership_score,
        last_commit=fresh.last_commit,
        commits=fresh.commits,
        score_breakdown=fresh.score_breakdown,
        freshness=OwnershipFreshness(
            active_expertise=0.2,
            historical_expertise=0.9,
            maintenance_recency=0.2,
            status="departed",
            half_life_days=30.0,
        ),
    )
    paths = {
        "empty.py": _path((), candidates=(no_signal,)),
        "unknown.py": _path((unknown,)),
        "review.py": _path((reviewer_only, untouched)),
        "nosignal.py": _path((no_signal,)),
        "commits.py": _path((writer, reviewer)),
        "same.py": _path((same,)),
        "zero.py": _path((zero_review,)),
        "departed.py": _path((departed,)),
    }
    rules = (
        CodeownersRule("empty.py", ("@ghost",), 1),
        CodeownersRule("review.py", ("@bea", "@cam"), 2),
        CodeownersRule("nosignal.py", ("@dot",), 3),
        CodeownersRule("commits.py", ("@erin", "@fran"), 4),
        CodeownersRule("same.py", ("@gale",), 5),
        CodeownersRule("zero.py", ("@hank",), 6),
    )
    report = _risk_report(paths, rules)
    kinds = {(item.kind, item.path) for item in report.pathologies}
    assert ("knowledge-vacuum", "empty.py") in kinds
    assert ("knowledge-vacuum", "unknown.py") in kinds
    assert ("knowledge-vacuum", "departed.py") in kinds
    assert ("phantom-ownership", "review.py") in kinds
    assert ("phantom-ownership", "empty.py") in kinds
    unknown_hit = next(item for item in report.pathologies if item.path == "unknown.py")
    assert "unknown" in unknown_hit.detail
    assert ("shadow-maintainer", "review.py") not in kinds
    assert ("ownership-review-divergence", "commits.py") not in kinds
    assert ("ownership-review-divergence", "same.py") not in kinds
    core_writer = _owner(
        "@ann", 0.8, commits=10, breakdown=_breakdown(0.8, blame=False, review=0.1)
    )
    core_reviewer = _owner(
        "@bea", 0.7, commits=2, breakdown=_breakdown(0.7, blame=False, review=0.85)
    )
    core_declared = _owner(
        "@cam",
        0.95,
        commits=4,
        breakdown=_breakdown(0.95, blame=False, review=0.05),
    )
    split = _risk_report(
        {"src/core.py": _path((core_writer, core_reviewer, core_declared))},
        (CodeownersRule("src/core.py", ("@cam",), 1),),
    )
    payload = risk_payload(split)
    pathologies = payload["pathologies"]
    assert isinstance(pathologies, list)
    role = next(item for item in pathologies if item["kind"] == "ownership-review-divergence")
    assert isinstance(role, dict)
    assert role["writer"] == "@ann"
    assert role["reviewer"] == "@bea"
    assert role["declared"] == "@cam"


def test_score_guards() -> None:
    assert _geometric_mean(()) == 0
    assert _geometric_mean((2.0,)) == 100
    assert _writer(()) is None


def test_role_payload_omits_absent_fields() -> None:
    empty = _risk_report({})
    roles = (
        Pathology(kind="phantom-ownership", path="none.py", detail="plain"),
        Pathology(
            kind="ownership-review-divergence",
            path="writer.py",
            detail="writer only",
            writer="@ann",
        ),
        Pathology(
            kind="ownership-review-divergence",
            path="reviewer.py",
            detail="reviewer only",
            reviewer="@bea",
        ),
        Pathology(
            kind="ownership-review-divergence",
            path="declared.py",
            detail="declared only",
            declared="@cam",
        ),
    )
    payload = risk_payload(replace(empty, pathologies=roles))
    pathologies = payload["pathologies"]
    assert isinstance(pathologies, list)
    by_path = {item["path"]: item for item in pathologies if isinstance(item, dict)}
    assert "writer" not in by_path["none.py"]
    assert by_path["writer.py"]["writer"] == "@ann"
    assert "reviewer" not in by_path["writer.py"]
    assert by_path["reviewer.py"]["reviewer"] == "@bea"
    assert "declared" not in by_path["reviewer.py"]
    assert by_path["declared.py"]["declared"] == "@cam"
    assert "writer" not in by_path["declared.py"]

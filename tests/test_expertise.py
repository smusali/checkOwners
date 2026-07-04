"""Tests for checkowners.expertise module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from checkowners.expertise import path_matches_glob, rank_expertise
from checkowners.models import OwnerEntry, OwnershipMap, PathOwnership

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float, commits: int = 5) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=_NOW, commits=commits)


def _ownership(raw: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    return OwnershipMap(
        paths={
            p: PathOwnership(owners=owners, bus_factor=len(owners)) for p, owners in raw.items()
        },
        last_analyzed=_NOW,
    )


def test_rank_single_path_orders_by_confidence() -> None:
    ownership = _ownership(
        {
            "src/api.py": (
                _entry("@alice", 0.9, 20),
                _entry("@bob", 0.5, 8),
                _entry("@carol", 0.7, 15),
            )
        }
    )
    ranks = rank_expertise(ownership, "src/api.py")
    handles = [r.handle for r in ranks]
    assert handles == ["@alice", "@carol", "@bob"]


def test_rank_aggregates_across_glob() -> None:
    ownership = _ownership(
        {
            "src/payments/checkout.py": (_entry("@alice", 0.9, 10),),
            "src/payments/billing.py": (_entry("@alice", 0.7, 5), _entry("@bob", 0.6, 8)),
            "src/api.py": (_entry("@carol", 0.95, 30),),
        }
    )
    ranks = rank_expertise(ownership, "src/payments/")
    handles = {r.handle for r in ranks}
    assert handles == {"@alice", "@bob"}
    alice = next(r for r in ranks if r.handle == "@alice")
    assert alice.commits == 15
    assert alice.confidence == 0.9


def test_rank_exact_match_does_not_pick_up_siblings() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9),),
            "src/api/v2.py": (_entry("@bob", 0.9),),
        }
    )
    ranks = rank_expertise(ownership, "src/api.py")
    assert {r.handle for r in ranks} == {"@alice"}


def test_rank_empty_path_returns_empty_tuple() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9),)})
    assert rank_expertise(ownership, "no/such/path") == ()


def test_rank_handles_glob_wildcards() -> None:
    ownership = _ownership(
        {
            "src/payments/checkout.py": (_entry("@alice", 0.9),),
            "src/payments/billing.py": (_entry("@bob", 0.8),),
        }
    )
    ranks = rank_expertise(ownership, "src/payments/*.py")
    assert {r.handle for r in ranks} == {"@alice", "@bob"}


def test_matches_directory_prefix() -> None:
    assert path_matches_glob("src/payments/checkout.py", "src/payments/")
    assert path_matches_glob("src/payments/checkout.py", "src/payments")
    assert not path_matches_glob("src/api.py", "src/payments")


def test_path_matches_glob_exact_and_glob() -> None:
    assert path_matches_glob("src/api.py", "src/api.py")
    assert path_matches_glob("src/api.py", "src/*.py")
    # fnmatch semantics: '*' crosses '/'.
    assert path_matches_glob("src/api/v2.py", "src/*.py")
    assert not path_matches_glob("src/api.py", "tests/*.py")
    # An exact-path pattern does not pick up sibling directories.
    assert not path_matches_glob("src/api/v2.py", "src/api.py")


def test_path_matches_glob_bare_directory_prefix() -> None:
    # Bare directory names (with or without trailing slash) match children.
    assert path_matches_glob("controllers/user.py", "controllers")
    assert path_matches_glob("controllers/user.py", "controllers/")
    assert path_matches_glob("controllers/nested/deep.py", "controllers")
    # Segment-aware: 'controllers' must not match 'controllers_v2/...'.
    assert not path_matches_glob("controllers_v2/user.py", "controllers")
    assert not path_matches_glob("models/user.py", "controllers")


def test_path_matches_glob_leading_slash_normalized() -> None:
    assert path_matches_glob("/src/api.py", "src/api.py")
    assert path_matches_glob("src/api.py", "/src/api.py")


def test_path_matches_glob_consistent_across_modules() -> None:
    """busfactor and onboard target matching share this exact helper."""
    from checkowners.busfactor import compute_bus_factor
    from checkowners.models import AnalysisConfig, Config
    from checkowners.onboard import generate_onboarding_path

    ownership = _ownership(
        {
            "controllers/user.py": (_entry("@alice", 0.9),),
            "controllers/admin.py": (_entry("@bob", 0.8),),
            "models/user.py": (_entry("@carol", 0.7),),
        }
    )
    config = Config(analysis=AnalysisConfig(confidence_threshold=0.3))
    expected = {"controllers/user.py", "controllers/admin.py"}

    ranked_handles = {r.handle for r in rank_expertise(ownership, "controllers")}
    assert ranked_handles == {"@alice", "@bob"}

    bus_report = compute_bus_factor(ownership, config, target="controllers")
    assert {e.path for e in bus_report.entries} == expected

    onboarding = generate_onboarding_path(ownership, config, target="controllers")
    assert {s.path for s in onboarding.steps} == expected


def test_rank_handles_last_commit_aggregation() -> None:
    older = _NOW - timedelta(days=30)
    ownership = _ownership(
        {
            "src/a.py": (OwnerEntry("@alice", 0.8, last_commit=older, commits=5),),
            "src/b.py": (OwnerEntry("@alice", 0.7, last_commit=_NOW, commits=2),),
        }
    )
    ranks = rank_expertise(ownership, "src/")
    assert ranks[0].handle == "@alice"
    assert ranks[0].last_commit == _NOW
    assert ranks[0].commits == 7

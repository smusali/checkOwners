"""Tests for checkowners.decay module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from checkowners.decay import detect_decay
from checkowners.expertise import common_prefix_depth
from checkowners.models import (
    AnalysisConfig,
    Config,
    DecayConfig,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_OLD = _NOW - timedelta(days=300)


def _entry(handle: str, confidence: float, last_commit: datetime = _NOW) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=last_commit, commits=10)


def _make_warning(handle: str, path: str, days: int = 300) -> DecayWarning:
    return DecayWarning(
        handle=handle,
        path=path,
        last_commit=_OLD,
        days_since_last_commit=days,
        historical_confidence=0.4,
    )


def _zero_threshold() -> Config:
    return Config(
        analysis=AnalysisConfig(confidence_threshold=0.0),
        decay=DecayConfig(alert_on_decay=True),
    )


def test_detect_decay_no_warnings_returns_empty() -> None:
    ownership = OwnershipMap(
        paths={
            "src/main.py": PathOwnership(owners=(_entry("@alice", 0.9),), bus_factor=1),
        },
        last_analyzed=_NOW,
    )
    assert detect_decay(ownership, _zero_threshold()) == ()


def test_detect_decay_disabled_via_config() -> None:
    ownership = OwnershipMap(
        paths={
            "src/auth.py": PathOwnership(
                owners=(_entry("@alice", 0.3, _OLD),),
                bus_factor=1,
                decay_warnings=(_make_warning("@alice", "src/auth.py"),),
            ),
        },
        last_analyzed=_NOW,
    )
    config = Config(
        analysis=AnalysisConfig(confidence_threshold=0.0),
        decay=DecayConfig(alert_on_decay=False),
    )
    assert detect_decay(ownership, config) == ()


def test_detect_decay_marks_dormant_vs_departed() -> None:
    ownership = OwnershipMap(
        paths={
            "src/auth.py": PathOwnership(
                owners=(_entry("@alice", 0.3, _OLD),),
                bus_factor=1,
                decay_warnings=(_make_warning("@alice", "src/auth.py"),),
            ),
            "src/billing.py": PathOwnership(
                owners=(_entry("@bob", 0.2, _OLD),),
                bus_factor=1,
                decay_warnings=(_make_warning("@bob", "src/billing.py"),),
            ),
            "src/api.py": PathOwnership(
                owners=(_entry("@alice", 0.9),),
                bus_factor=1,
            ),
        },
        last_analyzed=_NOW,
    )
    reports = detect_decay(ownership, _zero_threshold())
    by_handle = {r.warning.handle: r for r in reports}
    # alice is decaying on auth.py but still active on api.py -> dormant
    assert by_handle["@alice"].departed is False
    # bob is decaying everywhere -> departed
    assert by_handle["@bob"].departed is True


def test_detect_decay_recommends_active_owner_on_same_path() -> None:
    ownership = OwnershipMap(
        paths={
            "src/api.py": PathOwnership(
                owners=(
                    _entry("@alice", 0.3, _OLD),
                    _entry("@bob", 0.85),
                ),
                bus_factor=1,
                decay_warnings=(_make_warning("@alice", "src/api.py"),),
            ),
        },
        last_analyzed=_NOW,
    )
    reports = detect_decay(ownership, _zero_threshold())
    assert len(reports) == 1
    assert reports[0].recommended_transfer == "@bob"


def test_detect_decay_falls_back_to_adjacent_path() -> None:
    ownership = OwnershipMap(
        paths={
            "src/auth/login.py": PathOwnership(
                owners=(_entry("@alice", 0.3, _OLD),),
                bus_factor=1,
                decay_warnings=(_make_warning("@alice", "src/auth/login.py"),),
            ),
            "src/auth/session.py": PathOwnership(
                owners=(_entry("@carol", 0.9),),
                bus_factor=1,
            ),
            "src/billing.py": PathOwnership(
                owners=(_entry("@eve", 0.95),),
                bus_factor=1,
            ),
        },
        last_analyzed=_NOW,
    )
    reports = detect_decay(ownership, _zero_threshold())
    # carol is in the adjacent src/auth/ path; eve is in an unrelated path
    assert reports[0].recommended_transfer == "@carol"


def test_detect_decay_no_candidate_returns_none() -> None:
    ownership = OwnershipMap(
        paths={
            "src/auth.py": PathOwnership(
                owners=(_entry("@alice", 0.3, _OLD),),
                bus_factor=1,
                decay_warnings=(_make_warning("@alice", "src/auth.py"),),
            ),
        },
        last_analyzed=_NOW,
    )
    reports = detect_decay(ownership, _zero_threshold())
    assert reports[0].recommended_transfer is None


def test_detect_decay_sorted_by_oldest_first() -> None:
    ownership = OwnershipMap(
        paths={
            "src/a.py": PathOwnership(
                owners=(_entry("@alice", 0.3, _OLD),),
                bus_factor=1,
                decay_warnings=(_make_warning("@alice", "src/a.py", days=200),),
            ),
            "src/b.py": PathOwnership(
                owners=(_entry("@bob", 0.3, _OLD),),
                bus_factor=1,
                decay_warnings=(_make_warning("@bob", "src/b.py", days=500),),
            ),
        },
        last_analyzed=_NOW,
    )
    reports = detect_decay(ownership, _zero_threshold())
    assert [r.warning.handle for r in reports] == ["@bob", "@alice"]


def test_common_prefix_depth_adjacency() -> None:
    assert common_prefix_depth("src/auth/login.py", "src/auth/session.py") == 2
    assert common_prefix_depth("src/main.py", "src/util.py") == 1
    assert common_prefix_depth("src/main.py", "tests/test_main.py") == 0

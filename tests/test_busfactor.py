"""Tests for checkowners.busfactor module."""

from __future__ import annotations

from datetime import UTC, datetime

from checkowners.busfactor import classify, compute_bus_factor
from checkowners.models import (
    AnalysisConfig,
    BusFactorConfig,
    Config,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float, commits: int = 5) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=_NOW, commits=commits)


def _ownership(raw: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    paths = {
        p: PathOwnership(
            owners=owners,
            bus_factor=sum(1 for o in owners if o.confidence >= 0.3),
        )
        for p, owners in raw.items()
    }
    return OwnershipMap(paths=paths, last_analyzed=_NOW)


def _config(confidence_threshold: float = 0.3) -> Config:
    return Config(
        analysis=AnalysisConfig(confidence_threshold=confidence_threshold),
        bus_factor=BusFactorConfig(critical_threshold=1, warn_threshold=2),
    )


def test_classify_tiers() -> None:
    bf_config = BusFactorConfig(critical_threshold=1, warn_threshold=2)
    assert classify(1, bf_config) == "critical"
    assert classify(2, bf_config) == "warning"
    assert classify(3, bf_config) == "ok"
    assert classify(0, bf_config) == "critical"


def test_compute_bus_factor_all_paths_sorted() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.6)),
            "src/auth.py": (_entry("@dave", 0.4),),
            "src/db.py": (_entry("@carol", 0.85), _entry("@eve", 0.65), _entry("@mallory", 0.4)),
        }
    )
    report = compute_bus_factor(ownership, _config())
    assert [e.path for e in report.entries] == ["src/auth.py", "src/api.py", "src/db.py"]
    assert report.repo_average > 0


def test_compute_bus_factor_filters_by_target_directory() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9),),
            "tests/test_api.py": (_entry("@bob", 0.7),),
        }
    )
    report = compute_bus_factor(ownership, _config(), target="src/")
    assert {e.path for e in report.entries} == {"src/api.py"}


def test_compute_bus_factor_recommends_adjacent_backups() -> None:
    ownership = _ownership(
        {
            "src/auth.py": (_entry("@alice", 0.9),),
            "src/session.py": (_entry("@bob", 0.85),),
            "tests/test_session.py": (_entry("@carol", 0.95),),
        }
    )
    report = compute_bus_factor(ownership, _config(), target="src/auth.py")
    entry = report.entries[0]
    assert entry.recommended_backups[0] == "@bob"
    assert "@carol" not in entry.recommended_backups


def test_compute_bus_factor_critical_paths_listed() -> None:
    ownership = _ownership(
        {
            "src/lonely.py": (_entry("@alice", 0.95),),
            "src/shared.py": (_entry("@alice", 0.9), _entry("@bob", 0.6)),
        }
    )
    report = compute_bus_factor(ownership, _config())
    assert "src/lonely.py" in report.critical_paths
    assert "src/shared.py" not in report.critical_paths


def test_critical_paths_respect_custom_thresholds() -> None:
    ownership = _ownership(
        {
            "src/lonely.py": (_entry("@alice", 0.95),),
            "src/shared.py": (_entry("@alice", 0.9), _entry("@bob", 0.6)),
        }
    )
    config = Config(
        analysis=AnalysisConfig(confidence_threshold=0.3),
        bus_factor=BusFactorConfig(critical_threshold=2, warn_threshold=3),
    )
    report = compute_bus_factor(ownership, config)
    # With critical_threshold=2 both the bus-factor-1 and bus-factor-2 paths are critical.
    assert report.config.critical_threshold == 2
    assert report.tier_for(2) == "critical"
    assert set(report.critical_paths) == {"src/lonely.py", "src/shared.py"}


def test_compute_bus_factor_empty_returns_zero_average() -> None:
    ownership = OwnershipMap(paths={}, last_analyzed=_NOW)
    report = compute_bus_factor(ownership, _config())
    assert report.entries == ()
    assert report.repo_average == 0.0


def test_compute_bus_factor_glob_target() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9),),
            "src/api/v2.py": (_entry("@bob", 0.85),),
            "tests/test_api.py": (_entry("@carol", 0.7),),
        }
    )
    # Unified glob semantics (fnmatch): '*' crosses '/', so nested paths match.
    report = compute_bus_factor(ownership, _config(), target="src/*.py")
    assert {e.path for e in report.entries} == {"src/api.py", "src/api/v2.py"}


def test_compute_bus_factor_bare_directory_target() -> None:
    ownership = _ownership(
        {
            "controllers/user.py": (_entry("@alice", 0.9),),
            "controllers/admin.py": (_entry("@bob", 0.85),),
            "models/user.py": (_entry("@carol", 0.7),),
        }
    )
    # A bare directory name (no trailing slash) matches everything under it.
    report = compute_bus_factor(ownership, _config(), target="controllers")
    assert {e.path for e in report.entries} == {
        "controllers/user.py",
        "controllers/admin.py",
    }


def test_recommend_backups_root_level_falls_back_repo_wide() -> None:
    ownership = _ownership(
        {
            "README.md": (_entry("@alice", 0.95),),
            "src/api.py": (_entry("@bob", 0.85),),
            "src/db.py": (_entry("@carol", 0.7),),
        }
    )
    report = compute_bus_factor(ownership, _config(), target="README.md")
    entry = report.entries[0]
    # No shared leading directory exists, so repo-wide top owners are used.
    assert entry.recommended_backups == ("@bob", "@carol")


def test_recommend_backups_repo_wide_excludes_own_owners() -> None:
    ownership = _ownership(
        {
            "README.md": (_entry("@alice", 0.95), _entry("@bob", 0.2)),
            "src/api.py": (_entry("@bob", 0.85),),
            "src/db.py": (_entry("@carol", 0.7),),
        }
    )
    report = compute_bus_factor(ownership, _config(), target="README.md")
    entry = report.entries[0]
    # @bob already owns README.md (even below threshold), so only @carol remains.
    assert entry.recommended_backups == ("@carol",)

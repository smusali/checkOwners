"""Tests for checkowners.busfactor module."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from checkowners.busfactor import (
    UnknownIdentityError,
    _repo_truck_factor,
    _residual,
    _weighted_percentile,
    classify,
    compute_qualified_owners,
    owner_distribution,
    simulate_removal,
    simulation_payload,
)
from checkowners.models import (
    AnalysisConfig,
    BusFactor,
    BusFactorConfig,
    Config,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float, commits: int = 5) -> OwnerEntry:
    return OwnerEntry(handle=handle, ownership_score=confidence, last_commit=_NOW, commits=commits)


def _ownership(raw: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    paths = {
        p: PathOwnership(
            owners=owners,
            qualified_owner_count=sum(1 for o in owners if o.confidence >= 0.3),
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


def test_compute_qualified_owners_all_paths_sorted() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9), _entry("@bob", 0.6)),
            "src/auth.py": (_entry("@dave", 0.4),),
            "src/db.py": (_entry("@carol", 0.85), _entry("@eve", 0.65), _entry("@mallory", 0.4)),
        }
    )
    report = compute_qualified_owners(ownership, _config())
    assert [e.path for e in report.entries] == ["src/auth.py", "src/api.py", "src/db.py"]
    assert report.distribution.criticality_incomplete is True


def test_compute_qualified_owners_filters_by_target_directory() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9),),
            "tests/test_api.py": (_entry("@bob", 0.7),),
        }
    )
    report = compute_qualified_owners(ownership, _config(), target="src/")
    assert {e.path for e in report.entries} == {"src/api.py"}


def test_compute_qualified_owners_recommends_adjacent_backups() -> None:
    ownership = _ownership(
        {
            "src/auth.py": (_entry("@alice", 0.9),),
            "src/session.py": (_entry("@bob", 0.85), _entry("@low", 0.1)),
            "src/other.py": (_entry("@bob", 0.4),),
            "tests/test_session.py": (_entry("@carol", 0.95),),
        }
    )
    report = compute_qualified_owners(ownership, _config(), target="src/auth.py")
    entry = report.entries[0]
    assert entry.recommended_backups[0] == "@bob"
    assert "@carol" not in entry.recommended_backups


def test_compute_qualified_owners_critical_paths_listed() -> None:
    ownership = _ownership(
        {
            "src/lonely.py": (_entry("@alice", 0.95),),
            "src/shared.py": (_entry("@alice", 0.9), _entry("@bob", 0.6)),
        }
    )
    report = compute_qualified_owners(ownership, _config())
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
    report = compute_qualified_owners(ownership, config)
    # With critical_threshold=2 both the bus-factor-1 and bus-factor-2 paths are critical.
    assert report.config.critical_threshold == 2
    assert report.tier_for(2) == "critical"
    assert set(report.critical_paths) == {"src/lonely.py", "src/shared.py"}


def test_compute_qualified_owners_empty_returns_zero_average() -> None:
    ownership = OwnershipMap(paths={}, last_analyzed=_NOW)
    report = compute_qualified_owners(ownership, _config())
    assert report.entries == ()
    assert report.distribution.minimum == 0.0
    missing = owner_distribution(
        (
            BusFactor(
                path="gone.py",
                qualified_owner_count=1,
                contributors_above_threshold=(),
                recommended_backups=(),
            ),
        ),
        ownership,
        _config(),
    )
    assert missing.minimum == 0.0
    assert missing.knowledge_at_risk == 1.0
    assert _weighted_percentile([(1.0, 0.0)], 1.0, 0.5) == 1.0
    assert report.distribution.critical_path_risk == 0.0
    assert report.distribution.knowledge_at_risk == 0.0
    assert report.distribution.criticality_incomplete is True


def test_compute_qualified_owners_glob_target() -> None:
    ownership = _ownership(
        {
            "src/api.py": (_entry("@alice", 0.9),),
            "src/api/v2.py": (_entry("@bob", 0.85),),
            "tests/test_api.py": (_entry("@carol", 0.7),),
        }
    )
    # Unified glob semantics (fnmatch): '*' crosses '/', so nested paths match.
    report = compute_qualified_owners(ownership, _config(), target="src/*.py")
    assert {e.path for e in report.entries} == {"src/api.py", "src/api/v2.py"}


def test_compute_qualified_owners_bare_directory_target() -> None:
    ownership = _ownership(
        {
            "controllers/user.py": (_entry("@alice", 0.9),),
            "controllers/admin.py": (_entry("@bob", 0.85),),
            "models/user.py": (_entry("@carol", 0.7),),
        }
    )
    # A bare directory name (no trailing slash) matches everything under it.
    report = compute_qualified_owners(ownership, _config(), target="controllers")
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
    report = compute_qualified_owners(ownership, _config(), target="README.md")
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
    report = compute_qualified_owners(ownership, _config(), target="README.md")
    entry = report.entries[0]
    # @bob already owns README.md (even below threshold), so only @carol remains.
    assert entry.recommended_backups == ("@carol",)


def test_critical_paths_outweigh_a_favorable_mean() -> None:
    spread = tuple(_entry(f"@doc{index}", 0.9 - index * 0.1) for index in range(5))
    ownership = _ownership(
        {
            "README.md": spread,
            "docs/guide.md": spread,
            "docs/api.md": spread,
            "payments/settlement.py": (_entry("@pay", 0.95),),
            "auth/crypto.py": (_entry("@auth", 0.95),),
        }
    )
    config = Config(
        analysis=AnalysisConfig(confidence_threshold=0.3),
        bus_factor=BusFactorConfig(critical_threshold=1, warn_threshold=2),
        criticality=(
            ("payments/**", 1.0),
            ("auth/**", 1.0),
            ("docs/**", 0.1),
            ("README.md", 0.1),
        ),
    )
    report = compute_qualified_owners(ownership, config)
    distribution = report.distribution
    unweighted = sum(entry.qualified_owner_count for entry in report.entries) / len(report.entries)
    assert unweighted > 3
    assert distribution.critical_path_risk > 0.5
    assert distribution.knowledge_at_risk > 0.5
    assert distribution.criticality_incomplete is False
    assert distribution.minimum <= distribution.p10 <= distribution.median <= distribution.p90


@given(
    counts=st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=12),
    weights=st.lists(
        st.floats(min_value=0.05, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=12,
    ),
)
def test_distribution_percentile_order(counts: list[int], weights: list[float]) -> None:
    size = min(len(counts), len(weights))
    paths: dict[str, tuple[OwnerEntry, ...]] = {}
    rules: list[tuple[str, float]] = []
    for index in range(size):
        path = f"src/p{index}.py"
        count = counts[index]
        paths[path] = tuple(_entry(f"@u{index}{owner}", 1.0) for owner in range(count))
        rules.append((path, weights[index]))
    config = Config(
        analysis=AnalysisConfig(confidence_threshold=0.3),
        criticality=tuple(rules),
    )
    report = compute_qualified_owners(_ownership(paths), config)
    distribution = report.distribution
    assert distribution.minimum <= distribution.p10 <= distribution.median <= distribution.p90


def _departure_map() -> OwnershipMap:
    return _ownership(
        {
            "services/billing/a.py": (_entry("@alice", 0.9),),
            "services/billing/b.py": (_entry("@alice", 0.8), _entry("@carol", 0.25)),
            "services/keep.py": (_entry("@dave", 0.9),),
            "src/pair.py": (_entry("@alice", 0.9), _entry("@bob", 0.7)),
            "src/other.py": (_entry("@bob", 0.8), _entry("@carol", 0.6)),
        }
    )


def test_simulate_one_and_two_removals() -> None:
    ownership = _departure_map()
    one = simulate_removal(ownership, _config(), ("@alice",))
    assert one.files_losing_only_owner == 2
    assert one.files_losing_only_owner_ratio == round(2 / 5, 4)
    assert one.files_dropping_to_one == 1
    assert [(item.path, item.files) for item in one.orphaned_directories] == [
        ("services/billing/", 2)
    ]
    assert one.repo_truck_factor_before == 2
    assert one.repo_truck_factor_after == 1
    assert one.transfers[0].candidates[0].identity == "@carol"
    assert one.transfers[0].candidates[0].confidence == 0.25
    two = simulate_removal(ownership, _config(), ("@alice", "@bob"))
    assert two.files_losing_only_owner == 2
    assert two.files_dropping_to_one == 1
    assert [(item.path, item.files) for item in two.orphaned_directories] == [
        ("services/billing/", 2)
    ]
    assert two.repo_truck_factor_before == 2
    assert two.repo_truck_factor_after == 0


def test_simulate_removing_every_author_orphans_every_path() -> None:
    ownership = _departure_map()
    report = simulate_removal(ownership, _config(), ("@alice", "@bob", "@carol", "@dave"))
    assert sum(item.files for item in report.orphaned_directories) == len(ownership.paths)
    assert report.repo_truck_factor_after == 0
    assert all(not item.authors_after for item in report.affected)


def test_simulate_non_author_changes_nothing() -> None:
    ownership = _ownership(
        {
            "services/keep.py": (_entry("@dave", 0.9), _entry("@zoe", 0.1)),
        }
    )
    before = simulate_removal(ownership, _config(), ("@dave",))
    report = simulate_removal(ownership, _config(), ("@zoe",))
    assert report.files_losing_only_owner == 0
    assert report.files_dropping_to_one == 0
    assert report.orphaned_directories == ()
    assert report.affected == ()
    assert report.repo_truck_factor_before == report.repo_truck_factor_after
    assert report.repo_truck_factor_before == before.repo_truck_factor_before


def test_simulate_unknown_identity() -> None:
    with pytest.raises(UnknownIdentityError, match="mallory"):
        simulate_removal(_departure_map(), _config(), ("@mallory",))


def test_simulate_rejects_blank_identities_and_collapses_duplicates() -> None:
    ownership = _departure_map()
    with pytest.raises(ValueError, match="at least one identity"):
        simulate_removal(ownership, _config(), (" ", ""))
    report = simulate_removal(ownership, _config(), ("@alice", " ", "@alice"))
    assert report.removed == ("@alice",)
    assert report.files_losing_only_owner == 2


def test_simulate_keeps_the_stronger_stored_score() -> None:
    ownership = OwnershipMap(
        paths={
            "services/billing/a.py": PathOwnership(
                owners=(
                    _entry("@alice", 0.9),
                    _entry("@bob", 0.8),
                    _entry("@carol", 0.22),
                ),
                qualified_owner_count=1,
                scored_owners=(_entry("@alice", 0.9), _entry("@carol", 0.15)),
                candidates=(_entry("@carol", 0.10),),
            ),
            "services/billing/b.py": PathOwnership(
                owners=(
                    _entry("@alice", 0.8),
                    _entry("@carol", 0.28),
                    _entry("@dave", 0.1),
                    _entry("@zero", 0.0),
                ),
                qualified_owner_count=1,
            ),
        },
        last_analyzed=_NOW,
    )
    report = simulate_removal(ownership, _config(), ("@alice",))
    assert report.files_losing_only_owner == 2
    assert [(item.path, item.files) for item in report.orphaned_directories] == [("services/", 2)]
    candidates = report.transfers[0].candidates
    assert [(item.identity, item.confidence) for item in candidates] == [
        ("@bob", 0.8),
        ("@carol", 0.28),
    ]
    bare = _residual(ownership, "services/billing", ("@alice",))
    assert bare[0].identity == "@bob"
    payload = simulation_payload(report)
    transfers = payload["transfers"]
    assert isinstance(transfers, list)
    assert transfers[0]["candidates"][0]["identity"] == "@bob"


def test_repo_truck_factor_empty_and_zero_coverage() -> None:
    assert _repo_truck_factor({}, 0.5) == 0
    assert _repo_truck_factor({"a.py": frozenset()}, 0.0) == 0
    assert _repo_truck_factor({"a.py": frozenset({"@alice"})}, 0.0) == 1

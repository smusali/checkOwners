"""Loading a state file whose fields are malformed."""

from __future__ import annotations

from pathlib import Path

import pytest

from checkowners.state import (
    load_ownership,
)
from tests.conftest import (
    NOW,
    readable_state,
    write_raw_state,
)
from tests.test_state import (
    _stored,
)


def test_load_ownership_skips_invalid_owner_fields(repo: Path) -> None:
    owners = [
        {"handle": "@bool-score", "ownership_score": True, "commits": 1},
        {"handle": "@bad-score", "ownership_score": "high", "commits": 1},
        {"handle": "@bool-quality", "ownership_score": 0.4, "evidence_quality": True, "commits": 1},
        {"handle": "@bad-quality", "ownership_score": 0.4, "evidence_quality": "low", "commits": 1},
        {"handle": "@bad-commits", "ownership_score": 0.4, "commits": 1.5},
        {"handle": 12, "ownership_score": 0.4, "commits": 1},
        "not-an-object",
        {
            "handle": "@ok",
            "ownership_score": 0.4,
            "commits": 2,
            "last_commit": None,
        },
    ]
    write_raw_state(
        repo,
        readable_state(
            {
                "repo": str(repo.resolve()),
                "inferred": {
                    "p.py": {
                        "owners": owners,
                        "qualified_owner_count": 1,
                        "decay_warnings": [],
                    }
                },
                "last_analyzed": NOW.isoformat(),
            }
        ),
    )
    loaded = load_ownership(repo)
    assert loaded is not None
    assert [o.handle for o in loaded.paths["p.py"].owners] == ["@ok"]
    assert loaded.paths["p.py"].owners[0].last_commit is None
    assert loaded.paths["p.py"].scored_owners == loaded.paths["p.py"].owners
    write_raw_state(
        repo,
        readable_state(
            {
                "repo": str(repo.resolve()),
                "inferred": {
                    "p.py": {
                        "owners": owners,
                        "scored_owners": "nope",
                        "qualified_owner_count": 1,
                        "decay_warnings": [],
                    }
                },
                "last_analyzed": NOW.isoformat(),
            }
        ),
    )
    reloaded = load_ownership(repo)
    assert reloaded is not None
    assert reloaded.paths["p.py"].scored_owners == ()


def test_load_ownership_skips_malformed_path(repo: Path) -> None:
    payload = {
        "repo": str(repo.resolve()),
        "inferred": {
            "src/good.py": {
                "owners": [
                    {
                        "handle": "@alice",
                        "confidence": 0.5,
                        "last_commit": NOW.isoformat(),
                        "commits": 3,
                    }
                ],
                "qualified_owner_count": 1,
                "qualified_owner_count_cap": 3,
                "decay_warnings": [],
            },
            "src/bad.py": "garbage",
        },
        "last_analyzed": NOW.isoformat(),
        "drift_detected": False,
    }
    write_raw_state(repo, readable_state(payload))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert set(loaded.paths) == {"src/good.py"}


def _owner_with(**overrides: object) -> dict[str, object]:
    stored = _stored()
    inferred = stored["inferred"]
    assert isinstance(inferred, dict)
    path = inferred["p.py"]
    assert isinstance(path, dict)
    owners = path["owners"]
    assert isinstance(owners, list)
    first = owners[0]
    assert isinstance(first, dict)
    first.update(overrides)
    return stored


def test_a_well_formed_state_loads_every_field(repo: Path) -> None:
    write_raw_state(repo, readable_state(_stored()))
    loaded = load_ownership(repo)
    assert loaded is not None
    entry = loaded.paths["p.py"].owners[0]
    assert entry.ownership_score == pytest.approx(1.0)
    assert entry.score_breakdown is not None
    assert entry.score_breakdown.frequency.available is True
    assert entry.score_breakdown.frequency.score == 0.0
    assert "score" not in entry.score_breakdown.review.as_payload()


@pytest.mark.parametrize(
    "signals",
    [{"blame": "nope"}, {"frequency": {"available": "yes"}}],
    ids=["non-mapping-signal", "non-boolean-availability"],
)
def test_a_malformed_signal_block_drops_the_breakdown(
    repo: Path,
    signals: dict[str, object],
) -> None:
    write_raw_state(repo, readable_state(_owner_with(signals=signals)))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert loaded.paths["p.py"].owners[0].score_breakdown is None


@pytest.mark.parametrize("stamp", ["not-a-date", 123], ids=["unparseable", "non-string"])
def test_a_malformed_owner_timestamp_is_dropped(repo: Path, stamp: object) -> None:
    write_raw_state(repo, readable_state(_owner_with(last_commit=stamp)))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert loaded.paths["p.py"].owners[0].last_commit is None


@pytest.mark.parametrize(
    ("overrides", "expected_paths"),
    [
        ({"owners": "not-a-list"}, set()),
        ({"decay_warnings": "not-a-list"}, {"p.py"}),
    ],
    ids=["non-list-owners", "non-list-warnings"],
)
def test_a_malformed_path_entry_is_skipped_or_emptied(
    repo: Path,
    overrides: dict[str, object],
    expected_paths: set[str],
) -> None:
    write_raw_state(repo, readable_state(_stored(**overrides)))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert set(loaded.paths) == expected_paths
    for path in loaded.paths.values():
        assert path.decay_warnings == ()


def test_a_boolean_owner_count_falls_back_to_zero(repo: Path) -> None:
    write_raw_state(repo, readable_state(_stored(qualified_owner_count=True)))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert loaded.paths["p.py"].qualified_owner_count == 0


def test_a_malformed_decay_warning_is_skipped(repo: Path) -> None:
    warnings = [
        "not-a-dict",
        {
            "handle": "@alice",
            "path": "p.py",
            "last_commit": "not-a-date",
            "days_since_last_commit": 10,
            "historical_confidence": 0.4,
        },
        {
            "handle": "@alice",
            "path": "p.py",
            "last_commit": NOW.isoformat(),
            "days_since_last_commit": 10,
            "historical_confidence": 1,
            "status": "superseded",
        },
    ]
    write_raw_state(repo, readable_state(_stored(decay_warnings=warnings)))
    loaded = load_ownership(repo)
    assert loaded is not None
    kept = loaded.paths["p.py"].decay_warnings
    assert len(kept) == 1
    assert kept[0].status == "superseded"
    assert kept[0].historical_confidence == pytest.approx(1.0)


def test_a_non_string_analysis_ref_becomes_empty(repo: Path) -> None:
    stored = _stored()
    stored["analysis_ref"] = 12
    write_raw_state(repo, readable_state(stored))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert loaded.analysis_ref == ""
    assert loaded.reviews_sampled == 0
    assert loaded.reviews_window_days == 0


def test_an_unparseable_analysis_instant_rejects_the_whole_file(repo: Path) -> None:
    stored = _stored()
    stored["last_analyzed"] = "not-a-date"
    write_raw_state(repo, readable_state(stored))
    assert load_ownership(repo) is None


@pytest.mark.parametrize(
    ("freshness", "kept"),
    [
        (
            {
                "active_expertise": 1,
                "historical_expertise": 1,
                "maintenance_recency": 1,
                "status": "departed",
                "half_life_days": 30,
            },
            True,
        ),
        ({"active_expertise": True, "status": "stable"}, False),
        (
            {
                "active_expertise": 0.2,
                "historical_expertise": 0.2,
                "maintenance_recency": 0.2,
                "status": "retired",
                "half_life_days": 10,
            },
            False,
        ),
    ],
    ids=["integer-floats", "non-numeric", "unknown-status"],
)
def test_freshness_loads_only_when_every_field_parses(
    repo: Path,
    freshness: dict[str, object],
    kept: bool,
) -> None:
    write_raw_state(repo, readable_state(_owner_with(freshness=freshness)))
    loaded = load_ownership(repo)
    assert loaded is not None
    entry = loaded.paths["p.py"].owners[0]
    if not kept:
        assert entry.freshness is None
        return
    assert entry.freshness is not None
    assert entry.freshness.status == "departed"
    assert entry.freshness.half_life_days == pytest.approx(30.0)

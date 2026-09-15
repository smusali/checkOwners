"""Tests for checkowners.state module."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from checkowners.models import (
    OWNERSHIP_MODEL_VERSION,
    BusFactor,
    ConfidenceScore,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    SignalScore,
    TeamCluster,
)
from checkowners.state import (
    SCHEMA_VERSION,
    _state_path,
    load_ownership,
    read_graph_cache,
    read_handle_cache,
    read_state,
    write_graph_cache,
    write_handle_cache,
    write_state,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _make_ownership() -> OwnershipMap:
    breakdown = ConfidenceScore(
        total=0.85,
        recency=SignalScore(available=True, score=0.9),
        frequency=SignalScore(available=True, score=0.7),
        blame=SignalScore(available=True, score=0.8),
        review=SignalScore(available=True, score=0.6),
    )
    owner = OwnerEntry(
        handle="@alice",
        ownership_score=0.85,
        last_commit=_NOW,
        commits=12,
        evidence_quality=1.0,
        score_breakdown=breakdown,
    )
    decay = DecayWarning(
        handle="@bob",
        path="src/auth.py",
        last_commit=_NOW,
        days_since_last_commit=200,
        historical_confidence=0.4,
    )
    po = PathOwnership(owners=(owner,), qualified_owner_count=1, decay_warnings=(decay,))
    return OwnershipMap(paths={"src/auth.py": po}, last_analyzed=_NOW)


def _write_raw_state(repo_root: Path, payload: object) -> None:
    target = _state_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )


def test_state_path_is_per_repo(tmp_path: Path, repo: Path) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    assert _state_path(repo) != _state_path(other)
    assert _state_path(repo).parent.name == "state"


def test_read_state_missing_returns_none(repo: Path) -> None:
    assert read_state(repo) is None


def test_read_state_invalid_json_returns_none(repo: Path) -> None:
    _write_raw_state(repo, "not json")
    assert read_state(repo) is None


def test_read_state_wrong_schema_returns_none(repo: Path) -> None:
    _write_raw_state(repo, {"schema_version": 2, "repo": str(repo.resolve())})
    assert read_state(repo) is None
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "model_version": "ownership-v2",
            "repo": str(repo.resolve()),
        },
    )
    assert read_state(repo) is None


def test_read_state_non_dict_returns_none(repo: Path) -> None:
    _write_raw_state(repo, ["not", "a", "dict"])
    assert read_state(repo) is None


def test_read_state_repo_mismatch_returns_none(repo: Path) -> None:
    """State written under this repo's digest but naming another repo is rejected."""
    _write_raw_state(
        repo,
        {"schema_version": SCHEMA_VERSION, "repo": "/somewhere/else"},
    )
    assert read_state(repo) is None


def test_write_and_read_roundtrip(repo: Path) -> None:
    ownership = _make_ownership()
    topology = (
        TeamCluster(
            name="backend",
            members=("@alice", "@bob"),
            primary_paths=("src/api/",),
            declared=True,
        ),
    )
    bus_factor = (
        BusFactor(
            path="src/auth.py",
            qualified_owner_count=1,
            contributors_above_threshold=("@alice",),
            recommended_backups=("@bob",),
        ),
    )
    target = write_state(
        repo,
        ownership,
        topology=topology,
        bus_factor_summary=bus_factor,
        drift_detected=True,
    )
    assert target.exists()
    data = read_state(repo)
    assert data is not None
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["repo"] == str(repo.resolve())
    assert data["drift_detected"] is True
    assert data["topology"]["clusters"][0]["name"] == "backend"
    assert data["bus_factor_summary"]["critical_paths"] == ["src/auth.py"]
    assert data["bus_factor_summary"]["repo_average"] == 1.0
    assert data["bus_factor_summary"]["qualified_owner_count_cap"] == 3
    assert data["model_version"] == OWNERSHIP_MODEL_VERSION
    assert data["deprecated_keys"] == ["bus_factor", "confidence"]
    assert "src/auth.py" in data["inferred"]
    inferred = data["inferred"]["src/auth.py"]
    assert inferred["qualified_owner_count"] == 1
    assert inferred["bus_factor"] == 1
    assert inferred["qualified_owner_count_cap"] == 3


def test_state_isolated_between_repos(tmp_path: Path, repo: Path) -> None:
    """Analyzing repo A must never leak ownership into repo B."""
    other = tmp_path / "other-repo"
    other.mkdir()
    write_state(repo, _make_ownership())
    assert load_ownership(other) is None
    assert load_ownership(repo) is not None


def test_load_ownership_roundtrip(repo: Path) -> None:
    original = _make_ownership()
    write_state(repo, original)
    loaded = load_ownership(repo)
    assert loaded is not None
    assert set(loaded.paths) == set(original.paths)
    loaded_owner = loaded.paths["src/auth.py"].owners[0]
    assert loaded_owner.handle == "@alice"
    assert loaded_owner.ownership_score == pytest.approx(0.85)
    assert loaded_owner.confidence == pytest.approx(0.85)
    assert loaded_owner.evidence_quality == pytest.approx(1.0)
    assert loaded_owner.commits == 12
    assert loaded_owner.last_commit == _NOW
    assert loaded_owner.score_breakdown is not None
    assert loaded_owner.score_breakdown.recency.score == pytest.approx(0.9)
    assert loaded_owner.score_breakdown.recency.available is True
    decay = loaded.paths["src/auth.py"].decay_warnings[0]
    assert decay.handle == "@bob"
    assert decay.days_since_last_commit == 200


def test_load_ownership_missing_returns_none(repo: Path) -> None:
    assert load_ownership(repo) is None


def test_load_ownership_invalid_returns_none(repo: Path) -> None:
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "inferred": "not a dict",
        },
    )
    assert load_ownership(repo) is None


def test_write_state_owner_without_breakdown(repo: Path) -> None:
    owner = OwnerEntry(handle="@x", ownership_score=0.2, last_commit=None, commits=1)
    write_state(
        repo,
        OwnershipMap(
            paths={"a.py": PathOwnership(owners=(owner,), qualified_owner_count=1)},
            last_analyzed=_NOW,
        ),
    )
    loaded = load_ownership(repo)
    assert loaded is not None
    entry = loaded.paths["a.py"].owners[0]
    assert entry.last_commit is None
    assert entry.score_breakdown is None
    raw = read_state(repo)
    assert raw is not None
    serialized = raw["inferred"]["a.py"]["owners"][0]
    assert serialized["last_commit"] is None
    assert serialized["ownership_score"] == 0.2
    assert "signals" not in serialized


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
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "inferred": {
                "p.py": {
                    "owners": owners,
                    "qualified_owner_count": 1,
                    "decay_warnings": [],
                }
            },
            "last_analyzed": _NOW.isoformat(),
        },
    )
    loaded = load_ownership(repo)
    assert loaded is not None
    assert [o.handle for o in loaded.paths["p.py"].owners] == ["@ok"]
    assert loaded.paths["p.py"].owners[0].last_commit is None


def test_load_ownership_signal_and_timestamp_edges(repo: Path) -> None:
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "inferred": {
                "p.py": {
                    "owners": [
                        {
                            "handle": "@invalid-date",
                            "ownership_score": 0.6,
                            "commits": 2,
                            "last_commit": "not-a-date",
                            "signals": {
                                "recency": {"available": True, "score": 0.9},
                                "frequency": {"available": True, "score": False},
                                "blame": {"available": False},
                                "review": {"available": False},
                            },
                        },
                        {
                            "handle": "@partial-signals",
                            "ownership_score": 0.5,
                            "commits": 1,
                            "last_commit": 123,
                            "signals": {
                                "recency": {"available": True, "score": 1.0},
                                "frequency": {"available": "yes"},
                                "blame": "nope",
                                "review": {"available": False},
                            },
                        },
                        {
                            "handle": "@ok",
                            "ownership_score": 0.5,
                            "commits": 1,
                            "signals": {
                                "recency": {"available": True, "score": 1.0},
                                "frequency": {"available": True},
                                "blame": {"available": False},
                                "review": {"available": False},
                            },
                        },
                    ],
                    "qualified_owner_count": 1,
                    "decay_warnings": [],
                }
            },
            "last_analyzed": _NOW.isoformat(),
        },
    )
    loaded = load_ownership(repo)
    assert loaded is not None
    by_handle = {o.handle: o for o in loaded.paths["p.py"].owners}
    assert by_handle["@invalid-date"].last_commit is None
    assert by_handle["@invalid-date"].score_breakdown is None
    assert by_handle["@partial-signals"].last_commit is None
    assert by_handle["@partial-signals"].score_breakdown is None
    ok = by_handle["@ok"]
    assert ok.score_breakdown is not None
    assert ok.score_breakdown.frequency.available is True
    assert ok.score_breakdown.frequency.score == 0.0
    assert ok.score_breakdown.blame.available is False
    assert "score" not in ok.score_breakdown.review.as_payload()


def test_load_ownership_skips_malformed_path(repo: Path) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo.resolve()),
        "inferred": {
            "src/good.py": {
                "owners": [
                    {
                        "handle": "@alice",
                        "confidence": 0.5,
                        "last_commit": _NOW.isoformat(),
                        "commits": 3,
                    }
                ],
                "qualified_owner_count": 1,
                "bus_factor": 1,
                "qualified_owner_count_cap": 3,
                "decay_warnings": [],
            },
            "src/bad.py": "garbage",
        },
        "last_analyzed": _NOW.isoformat(),
        "drift_detected": False,
    }
    _write_raw_state(repo, payload)
    loaded = load_ownership(repo)
    assert loaded is not None
    assert set(loaded.paths) == {"src/good.py"}


def test_write_state_creates_parent_dirs(tmp_path: Path, repo: Path) -> None:
    nested = tmp_path / "nested" / "dir"
    with patch.dict("os.environ", {"CHECKOWNERS_STATE_DIR": str(nested)}):
        target = write_state(repo, _make_ownership())
    assert target.exists()
    assert target.is_relative_to(nested)


def test_bus_factor_summary_empty(repo: Path) -> None:
    ownership = _make_ownership()
    target = write_state(repo, ownership)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bus_factor_summary"]["critical_paths"] == []
    assert data["bus_factor_summary"]["repo_average"] == 0.0


def test_handle_cache_roundtrip() -> None:
    write_handle_cache({"alice@example.com": "@alice", "gone@example.com": ""})
    cache = read_handle_cache()
    assert cache["alice@example.com"] == "@alice"
    assert cache["gone@example.com"] == ""


def test_handle_cache_merges_on_write() -> None:
    write_handle_cache({"alice@example.com": "@alice"})
    write_handle_cache({"bob@example.com": "@bob"})
    cache = read_handle_cache()
    assert cache == {"alice@example.com": "@alice", "bob@example.com": "@bob"}


def test_handle_cache_missing_returns_empty() -> None:
    assert read_handle_cache() == {}


def test_graph_cache_roundtrip(tmp_path: Path) -> None:
    graph_data = {"nodes": [{"id": "contrib::a"}], "edges": []}
    target = write_graph_cache(tmp_path, _NOW, graph_data)
    assert target.exists()
    assert read_graph_cache(tmp_path, _NOW) == graph_data


def test_graph_cache_stale_timestamp_ignored(tmp_path: Path) -> None:
    write_graph_cache(tmp_path, _NOW, {"nodes": [], "edges": []})
    newer = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    assert read_graph_cache(tmp_path, newer) is None


def test_graph_cache_missing_returns_none(tmp_path: Path) -> None:
    assert read_graph_cache(tmp_path, _NOW) is None


def test_graph_cache_keyed_by_repo(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    write_graph_cache(repo_a, _NOW, {"nodes": [{"id": "a"}], "edges": []})
    assert read_graph_cache(repo_b, _NOW) is None
    assert read_graph_cache(repo_a, _NOW) == {"nodes": [{"id": "a"}], "edges": []}

"""Tests for checkowners.state module."""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from checkowners.models import (
    OWNERSHIP_MODEL_VERSION,
    AnalysisGap,
    BusFactor,
    ConfidenceScore,
    Config,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    ScoringConfig,
    SignalScore,
    TeamCluster,
    models_payload,
)
from checkowners.privacy import email_token
from checkowners.state import (
    SCHEMA_VERSION,
    _as_gap_code,
    _evict,
    _file_size,
    _state_path,
    cache_clear,
    cache_directory,
    cache_info,
    cache_purge,
    config_hash,
    load_hysteresis,
    load_ownership,
    read_graph_cache,
    read_handle_cache,
    read_state,
    repository_identity,
    reusable_ownership,
    write_graph_cache,
    write_handle_cache,
    write_state,
)
from tests.conftest import git_commit, init_git_repo

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _readable_state(payload: dict[str, object]) -> dict[str, object]:
    return {
        **payload,
        "model_version": OWNERSHIP_MODEL_VERSION,
        "models": models_payload(),
    }


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
    return OwnershipMap(paths={"src/auth.py": po}, last_analyzed=_NOW, analysis_ref="deadbeef")


def _write_raw_state(repo_root: Path, payload: object) -> None:
    body = payload
    if isinstance(payload, dict) and "repo_id" not in payload:
        body = {**payload, "repo_id": repository_identity(repo_root)}
    target = _state_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        body if isinstance(body, str) else json.dumps(body),
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
            "models": models_payload(),
            "repo": str(repo.resolve()),
        },
    )
    assert read_state(repo) is None
    current = models_payload()
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "model_version": OWNERSHIP_MODEL_VERSION,
            "models": {**current, "risk": "risk-v0"},
            "repo": str(repo.resolve()),
        },
    )
    assert read_state(repo) is None
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "model_version": OWNERSHIP_MODEL_VERSION,
            "models": {**current, "topology": "topology-v0"},
            "repo": str(repo.resolve()),
        },
    )
    assert read_state(repo) is None
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "model_version": OWNERSHIP_MODEL_VERSION,
            "repo": str(repo.resolve()),
        },
    )
    assert read_state(repo) is None


def test_read_state_non_dict_returns_none(repo: Path) -> None:
    _write_raw_state(repo, ["not", "a", "dict"])
    assert read_state(repo) is None


def test_read_state_repo_mismatch_returns_none(repo: Path) -> None:
    """State whose repo_id does not match this checkout is rejected."""
    _write_raw_state(
        repo,
        _readable_state(
            {
                "schema_version": SCHEMA_VERSION,
                "repo": str(repo.resolve()),
                "repo_id": "origin:example.com/other",
            }
        ),
    )
    assert read_state(repo) is None
    _write_raw_state(
        repo,
        _readable_state(
            {
                "schema_version": SCHEMA_VERSION,
                "repo": "/somewhere/else",
                "repo_id": repository_identity(repo),
                "inferred": {},
                "last_analyzed": _NOW.isoformat(),
            }
        ),
    )
    loaded = read_state(repo)
    assert loaded is not None
    assert loaded["repo"] == "/somewhere/else"


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
    assert data["models"] == models_payload()
    assert data["deprecated_keys"] == ["bus_factor", "confidence"]
    assert data["analysis_ref"] == "deadbeef"
    assert data["analysis_completeness"] == {
        "ignore_revs_applied": False,
        "ignore_revs_file": "",
        "mailmap_applied": False,
        "mailmap_file": "",
        "excluded_gitattributes": 0,
        "excluded_static": 0,
        "score": None,
        "gaps": [],
    }
    assert data["drift_reported_severity"] is None
    assert data["drift_pending_severity"] is None
    assert data["drift_pending_streak"] == 0
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


def test_load_ownership_keeps_a_numeric_score_and_known_gaps(repo: Path) -> None:
    write_state(repo, _make_ownership())
    data = read_state(repo)
    assert data is not None
    completeness = data["analysis_completeness"]
    assert isinstance(completeness, dict)
    completeness["score"] = 1
    completeness["gaps"] = [
        "skip",
        {"code": 1, "reason": "nope"},
        {"code": "missing_mailmap", "reason": 2},
        {"code": "not-a-gap", "reason": "nope"},
        {"code": "missing_mailmap", "reason": "Missing .mailmap."},
    ]
    _write_raw_state(repo, data)
    loaded = load_ownership(repo)
    assert loaded is not None
    assert loaded.analysis_completeness.score == 1.0
    assert loaded.analysis_completeness.gaps == (
        AnalysisGap("missing_mailmap", "Missing .mailmap."),
    )

    completeness["score"] = 0.25
    completeness["gaps"] = "nope"
    _write_raw_state(repo, data)
    reloaded = load_ownership(repo)
    assert reloaded is not None
    assert reloaded.analysis_completeness.score == 0.25
    assert reloaded.analysis_completeness.gaps == ()

    completeness["score"] = True
    _write_raw_state(repo, data)
    flagged = load_ownership(repo)
    assert flagged is not None
    assert flagged.analysis_completeness.score is None

    completeness["score"] = "nope"
    _write_raw_state(repo, data)
    dropped = load_ownership(repo)
    assert dropped is not None
    assert dropped.analysis_completeness.score is None


def test_unknown_gap_code_is_rejected() -> None:
    assert _as_gap_code("shallow_history") == "shallow_history"
    with pytest.raises(ValueError, match="unknown gap code"):
        _as_gap_code("not-a-gap")


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
    assert loaded.analysis_ref == "deadbeef"
    assert loaded.analysis_completeness.ignore_revs_applied is False
    assert loaded.analysis_completeness.ignore_revs_file == ""
    assert loaded.analysis_completeness.mailmap_applied is False
    assert loaded.analysis_completeness.mailmap_file == ""
    assert loaded.analysis_completeness.excluded_gitattributes == 0
    assert loaded.analysis_completeness.excluded_static == 0


def test_load_ownership_missing_returns_none(repo: Path) -> None:
    assert load_ownership(repo) is None


def test_load_ownership_invalid_returns_none(repo: Path) -> None:
    _write_raw_state(
        repo,
        _readable_state(
            {
                "schema_version": SCHEMA_VERSION,
                "repo": str(repo.resolve()),
                "inferred": "not a dict",
            }
        ),
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
        _readable_state(
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
            }
        ),
    )
    loaded = load_ownership(repo)
    assert loaded is not None
    assert [o.handle for o in loaded.paths["p.py"].owners] == ["@ok"]
    assert loaded.paths["p.py"].owners[0].last_commit is None


def test_load_ownership_signal_and_timestamp_edges(repo: Path) -> None:
    _write_raw_state(
        repo,
        _readable_state(
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
            }
        ),
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
    _write_raw_state(repo, _readable_state(payload))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert set(loaded.paths) == {"src/good.py"}


def test_hysteresis_roundtrip_and_preserve(repo: Path) -> None:
    write_state(
        repo,
        _make_ownership(),
        drift_reported_severity="low",
        drift_pending_severity="medium",
        drift_pending_streak=2,
    )
    assert load_hysteresis(repo) == ("low", "medium", 2)
    write_state(repo, _make_ownership())
    assert load_hysteresis(repo) == ("low", "medium", 2)


def test_load_hysteresis_missing_and_invalid_fields(repo: Path) -> None:
    assert load_hysteresis(repo) == (None, None, 0)
    write_state(
        repo,
        _make_ownership(),
        drift_reported_severity="high",
        drift_pending_severity="critical",
        drift_pending_streak=1,
    )
    assert load_hysteresis(repo) == ("high", "critical", 1)
    data = read_state(repo)
    assert data is not None
    data["drift_reported_severity"] = 1
    data["drift_pending_severity"] = True
    data["drift_pending_streak"] = True
    _write_raw_state(repo, data)
    write_state(repo, _make_ownership())
    assert load_hysteresis(repo) == (None, None, 0)
    data = read_state(repo)
    assert data is not None
    data["drift_pending_streak"] = "nope"
    _write_raw_state(repo, data)
    assert load_hysteresis(repo) == (None, None, 0)


def test_load_ownership_analysis_ref_and_timestamp_edges(repo: Path) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo.resolve()),
        "inferred": {
            "src/good.py": {
                "owners": [
                    {
                        "handle": "@alice",
                        "ownership_score": 0.5,
                        "last_commit": _NOW.isoformat(),
                        "commits": 3,
                    }
                ],
                "qualified_owner_count": 1,
                "decay_warnings": "not-a-list",
            },
            "src/skip.py": {"owners": "not-a-list"},
            "src/decay.py": {
                "owners": [
                    {
                        "handle": "@bob",
                        "ownership_score": 0.4,
                        "commits": 1,
                    }
                ],
                "qualified_owner_count": True,
                "decay_warnings": [
                    "not-a-dict",
                    {
                        "handle": "@bob",
                        "path": "src/decay.py",
                        "last_commit": "not-a-date",
                        "days_since_last_commit": 10,
                        "historical_confidence": 0.4,
                    },
                    {
                        "handle": "@bob",
                        "path": "src/decay.py",
                        "last_commit": _NOW.isoformat(),
                        "days_since_last_commit": 10,
                        "historical_confidence": 0.4,
                    },
                ],
            },
        },
        "last_analyzed": _NOW.isoformat(),
        "analysis_ref": 12,
    }
    _write_raw_state(repo, _readable_state(payload))
    loaded = load_ownership(repo)
    assert loaded is not None
    assert loaded.analysis_ref == ""
    assert set(loaded.paths) == {"src/good.py", "src/decay.py"}
    assert loaded.paths["src/decay.py"].qualified_owner_count == 0
    assert len(loaded.paths["src/decay.py"].decay_warnings) == 1
    payload["last_analyzed"] = "not-a-date"
    _write_raw_state(repo, _readable_state(payload))
    assert load_ownership(repo) is None


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
    assert "alice@example.com" not in cache
    assert cache[email_token("alice@example.com")] == "@alice"
    assert cache[email_token("gone@example.com")] == ""


def test_handle_cache_merges_on_write() -> None:
    write_handle_cache({"alice@example.com": "@alice"})
    write_handle_cache({"bob@example.com": "@bob"})
    cache = read_handle_cache()
    assert cache == {
        email_token("alice@example.com"): "@alice",
        email_token("bob@example.com"): "@bob",
    }


def test_handle_cache_missing_returns_empty() -> None:
    assert read_handle_cache() == {}
    target = write_handle_cache({"x@example.com": "@x"})
    target.write_text("{not json", encoding="utf-8")
    assert read_handle_cache() == {}
    target.write_text("[]", encoding="utf-8")
    assert read_handle_cache() == {}


def test_graph_cache_roundtrip(tmp_path: Path) -> None:
    graph_data = {"nodes": [{"id": "contrib::a"}], "edges": []}
    target = write_graph_cache(tmp_path, _NOW, graph_data)
    assert target.exists()
    assert read_graph_cache(tmp_path, _NOW) == graph_data
    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["models"] = {**models_payload(), "topology": "topology-v0"}
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW) is None
    stored.pop("models")
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW) is None


def test_graph_cache_stale_timestamp_ignored(tmp_path: Path) -> None:
    write_graph_cache(tmp_path, _NOW, {"nodes": [], "edges": []})
    newer = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    assert read_graph_cache(tmp_path, newer) is None


def test_graph_cache_missing_returns_none(tmp_path: Path) -> None:
    assert read_graph_cache(tmp_path, _NOW) is None
    target = write_graph_cache(tmp_path, _NOW, {"nodes": [], "edges": []})
    target.write_text("{not json", encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW) is None
    target.write_text("[]", encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW) is None


def test_graph_cache_keyed_by_repo(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    write_graph_cache(repo_a, _NOW, {"nodes": [{"id": "a"}], "edges": []})
    assert read_graph_cache(repo_b, _NOW) is None
    assert read_graph_cache(repo_a, _NOW) == {"nodes": [{"id": "a"}], "edges": []}


def test_parallel_handle_writes_do_not_corrupt() -> None:
    errors: list[BaseException] = []

    def write_one(index: int) -> None:
        try:
            write_handle_cache({f"user{index}@example.com": f"@user{index}"})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_one, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    cache = read_handle_cache()
    assert len(cache) == 8
    assert cache[email_token("user3@example.com")] == "@user3"


def test_reusable_ownership_refuses_advanced_head(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    first = git_commit(
        repo, "first", author="Alice", email="alice@example.com", date="2024-01-01T00:00:00Z"
    )
    write_state(
        repo,
        OwnershipMap(paths={}, last_analyzed=_NOW, analysis_ref=first),
        config=Config(),
    )
    assert reusable_ownership(repo, Config(), head=first) is not None
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    second = git_commit(
        repo, "second", author="Alice", email="alice@example.com", date="2024-01-02T00:00:00Z"
    )
    assert reusable_ownership(repo, Config(), head=second) is None
    reused = reusable_ownership(repo, Config(), head=second, allow_stale=True)
    assert reused is not None
    assert reused.analysis_ref == first
    assert reusable_ownership(repo, Config(), head=first, max_age=0) is None


def test_reusable_ownership_rejects_config_hash_mismatch(repo: Path) -> None:
    write_state(repo, _make_ownership(), config=Config())
    changed = replace(Config(), scoring=replace(ScoringConfig(), recency_weight=0.5))
    assert reusable_ownership(repo, changed, head="deadbeef") is None
    assert reusable_ownership(repo, Config(), head="deadbeef") is not None


def test_same_origin_shares_state(tmp_path: Path) -> None:
    first = init_git_repo(tmp_path / "first")
    second = init_git_repo(tmp_path / "second")
    for root, url in (
        (first, "git@github.com:acme/widget.git"),
        (second, "https://github.com/acme/widget.git"),
    ):
        subprocess.run(  # noqa: S603
            ["git", "remote", "add", "origin", url],  # noqa: S607
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    write_state(first, _make_ownership(), config=Config())
    loaded = load_ownership(second)
    assert loaded is not None
    assert loaded.analysis_ref == "deadbeef"
    assert repository_identity(first) == repository_identity(second)


def test_cache_evicts_oldest_state_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("checkowners.state.CACHE_LIMIT_BYTES", 1)
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    older.mkdir()
    newer.mkdir()
    first = write_state(older, _make_ownership())
    os.utime(first, (1, 1))
    second = write_state(newer, _make_ownership())
    assert not first.exists()
    assert second.exists()
    info = cache_info()
    assert info["state_files"] == 1
    assert info["limit_bytes"] == 1


def test_cache_clear_keeps_handles_and_purge_removes_them(repo: Path) -> None:
    write_state(repo, _make_ownership())
    handles = write_handle_cache({"alice@example.com": "@alice"})
    outside = repo.parent / "outside-dir"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    linked_dir = cache_directory() / "linked"
    linked_dir.symlink_to(outside, target_is_directory=True)
    alias = cache_directory() / "alias"
    alias.symlink_to(handles.name)
    assert cache_info()["handles"] is True
    assert cache_clear() >= 1
    assert cache_info()["state_files"] == 0
    assert cache_info()["handles"] is True
    assert cache_purge() >= 1
    assert cache_info()["handles"] is False
    assert not linked_dir.exists()
    assert not alias.exists()
    assert (outside / "keep.txt").is_file()


def test_cache_purge_missing_directory_removes_nothing() -> None:
    assert cache_purge() == 0


def test_repository_identity_ignores_unusable_origin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / ".git" / "config"
    config.parent.mkdir()
    expected = f"path:{repo.resolve()}"
    for body in (
        '[core]\n\tbare = false\n[remote "origin"]\n\turl =\n',
        '[remote "origin"]\n\turl = https://github.com\n',
        '[remote "origin"]\n\turl = not-a-url\n',
        '[remote "origin"]\n\turl = github.com:/\n',
    ):
        config.write_text(body, encoding="utf-8")
        assert repository_identity(repo) == expected
    config.write_text(
        '[remote "origin"]\n\tfetch = +refs/heads/*\n\turl = https://github.com/acme/widget\n',
        encoding="utf-8",
    )
    assert repository_identity(repo) == "origin:github.com/acme/widget"


def test_repository_identity_follows_worktree_gitdir(tmp_path: Path) -> None:
    common = tmp_path / "common"
    common.mkdir()
    (common / "config").write_text(
        '[remote "origin"]\n\turl = https://user:token@GitHub.com/acme/widget.git\n',
        encoding="utf-8",
    )
    git_dir = tmp_path / "wt.git"
    git_dir.mkdir()
    (git_dir / "commondir").write_text("../common\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: ../wt.git\n", encoding="utf-8")
    assert repository_identity(checkout) == "origin:github.com/acme/widget"

    (git_dir / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (checkout / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    assert repository_identity(checkout) == "origin:github.com/acme/widget"

    (git_dir / "commondir").unlink()
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:acme/other\n',
        encoding="utf-8",
    )
    assert repository_identity(checkout) == "origin:github.com/acme/other"

    (checkout / ".git").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert repository_identity(checkout) == f"path:{checkout.resolve()}"


def test_origin_url_ignores_unreadable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    config = repo / ".git" / "config"
    real = Path.read_text

    def flaky(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if self == config:
            raise OSError("denied")
        return real(self, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert repository_identity(repo).startswith("path:")


def test_state_write_succeeds_without_fcntl(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def guarded(name: str, *args: object, **kwargs: object) -> object:
        if name == "fcntl":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    target = write_state(repo, _make_ownership())
    assert target.is_file()
    assert load_ownership(repo) is not None


def test_atomic_write_removes_temp_on_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.write_text

    def fail_temp(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.name.endswith(".tmp"):
            raise OSError("disk full")
        return real(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", fail_temp)
    with pytest.raises(OSError, match="disk full"):
        write_state(repo, _make_ownership())
    assert list(_state_path(repo).parent.glob("*.tmp")) == []


def test_file_size_is_zero_when_stat_fails(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")
    with patch.object(Path, "stat", side_effect=OSError("stat")):
        assert _file_size(target) == 0


def test_evict_stops_once_under_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[Path] = []
    for name in ("oldest", "middle", "newest"):
        root = tmp_path / name
        root.mkdir()
        paths.append(write_state(root, _make_ownership()))
    for index, path in enumerate(paths):
        os.utime(path, (index + 1, index + 1))
    monkeypatch.setattr(
        "checkowners.state.CACHE_LIMIT_BYTES",
        paths[1].stat().st_size + paths[2].stat().st_size,
    )
    _evict(paths[2])
    assert not paths[0].exists()
    assert paths[1].exists()
    assert paths[2].exists()


def test_cache_info_blanks_missing_or_non_string_fields(repo: Path) -> None:
    target = write_state(repo, _make_ownership())
    target.write_text("[]", encoding="utf-8")
    assert cache_info()["entries"][0]["repo_id"] == ""
    target.write_text(
        json.dumps({"repo_id": 1, "analysis_ref": None, "last_analyzed": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    entry = cache_info()["entries"][0]
    assert entry["repo_id"] == ""
    assert entry["analysis_ref"] == ""
    assert entry["last_analyzed"] == "2026-01-01T00:00:00Z"


def test_graph_cache_rejects_mismatched_identity_config_and_ref(tmp_path: Path) -> None:
    graph = {"nodes": [], "edges": []}
    target = write_graph_cache(tmp_path, _NOW, graph, config=Config(), analysis_ref="abc")
    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["repo_id"] = "origin:elsewhere"
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW, config=Config(), analysis_ref="abc") is None

    stored["repo_id"] = repository_identity(tmp_path)
    stored["config_hash"] = "0" * 64
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW, config=Config(), analysis_ref="abc") is None

    stored["config_hash"] = config_hash(Config())
    stored["analysis_ref"] = "other"
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW, config=Config(), analysis_ref="abc") is None

    stored["analysis_ref"] = "abc"
    stored["graph"] = []
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW, config=Config(), analysis_ref="abc") is None

    stored["graph"] = graph
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, _NOW, config=Config(), analysis_ref="abc") == graph


def test_reusable_ownership_rejects_incomplete_payload(repo: Path) -> None:
    target = write_state(repo, _make_ownership(), config=Config())
    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["inferred"] = []
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_state(repo) is not None
    assert reusable_ownership(repo, Config(), head="deadbeef") is None


def test_reusable_ownership_honors_positive_max_age(repo: Path) -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    target = write_state(
        repo,
        OwnershipMap(paths={}, last_analyzed=old, analysis_ref="deadbeef"),
        config=Config(),
    )
    assert reusable_ownership(repo, Config(), head="deadbeef", max_age=60) is None
    fresh = reusable_ownership(repo, Config(), head="deadbeef", max_age=10**12)
    assert fresh is not None
    assert fresh.analysis_ref == "deadbeef"

    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["last_analyzed"] = "2020-01-01T00:00:00"
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert reusable_ownership(repo, Config(), head="deadbeef", max_age=60) is None
    assert read_handle_cache() == {}

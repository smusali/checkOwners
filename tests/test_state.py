"""Tests for checkowners.state module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from checkowners.models import (
    AnalysisGap,
    BusFactor,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    TeamCluster,
)
from checkowners.state import (
    load_ownership,
    read_state,
    write_state,
)
from checkowners.storage import (
    cache_directory,
    repository_identity,
)
from tests.conftest import (
    NOW,
    readable_state,
    stored_ownership,
    write_raw_state,
)


def _json_map(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {key: item for key, item in value.items() if isinstance(key, str)}


def test_read_state_missing_returns_none(repo: Path) -> None:
    assert read_state(repo) is None


def test_read_state_invalid_json_returns_none(repo: Path) -> None:
    write_raw_state(repo, "not json")
    assert read_state(repo) is None


def test_read_state_from_another_version_returns_none(repo: Path) -> None:
    write_raw_state(repo, {"package_version": "0.0.0", "repo": str(repo.resolve())})
    assert read_state(repo) is None
    write_raw_state(repo, {"repo": str(repo.resolve())})
    assert read_state(repo) is None


def test_read_state_non_dict_returns_none(repo: Path) -> None:
    write_raw_state(repo, ["not", "a", "dict"])
    assert read_state(repo) is None


def test_read_state_repo_mismatch_returns_none(repo: Path) -> None:
    """State whose repo_id does not match this checkout is rejected."""
    write_raw_state(
        repo,
        readable_state(
            {
                "repo": str(repo.resolve()),
                "repo_id": "origin:example.com/other",
            }
        ),
    )
    assert read_state(repo) is None
    write_raw_state(
        repo,
        readable_state(
            {
                "repo": "/somewhere/else",
                "repo_id": repository_identity(repo),
                "inferred": {},
                "last_analyzed": NOW.isoformat(),
            }
        ),
    )
    loaded = read_state(repo)
    assert loaded is not None
    assert loaded["repo"] == "/somewhere/else"


def test_write_and_read_roundtrip(repo: Path) -> None:
    ownership = stored_ownership()
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
    assert data["repo"] == str(repo.resolve())
    assert data["drift_detected"] is True
    clusters = _json_map(data["topology"])["clusters"]
    assert isinstance(clusters, list)
    assert _json_map(clusters[0])["name"] == "backend"
    summary = _json_map(data["bus_factor_summary"])
    assert summary["critical_paths"] == ["src/auth.py"]
    distribution = _json_map(summary["distribution"])
    assert distribution["critical_path_risk"] == 1.0
    assert distribution["criticality_incomplete"] is True
    assert summary["qualified_owner_count_cap"] == 3
    assert data["analysis_ref"] == "deadbeef"
    assert data["reviews_sampled"] == 0
    assert data["reviews_window_days"] == 0
    assert data["analysis_completeness"] == {
        "ignore_revs_applied": False,
        "ignore_revs_file": "",
        "mailmap_applied": False,
        "mailmap_file": "",
        "excluded_gitattributes": 0,
        "excluded_static": 0,
        "merge_strategy": "rebase",
        "co_author_count": 0,
        "score": None,
        "gaps": [],
    }
    assert data["drift_reported_severity"] is None
    assert data["drift_pending_severity"] is None
    assert data["drift_pending_streak"] == 0
    inferred = _json_map(data["inferred"])
    assert "src/auth.py" in inferred
    path = _json_map(inferred["src/auth.py"])
    assert path["qualified_owner_count"] == 1
    assert path["qualified_owner_count_cap"] == 3


def test_load_ownership_keeps_a_numeric_score_and_known_gaps(repo: Path) -> None:
    write_state(repo, stored_ownership())
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
    write_raw_state(repo, data)
    loaded = load_ownership(repo)
    assert loaded is not None
    assert loaded.analysis_completeness.score == 1.0
    assert loaded.analysis_completeness.gaps == (
        AnalysisGap("missing_mailmap", "Missing .mailmap."),
    )

    completeness["score"] = 0.25
    completeness["gaps"] = "nope"
    write_raw_state(repo, data)
    reloaded = load_ownership(repo)
    assert reloaded is not None
    assert reloaded.analysis_completeness.score == 0.25
    assert reloaded.analysis_completeness.gaps == ()

    completeness["score"] = True
    write_raw_state(repo, data)
    flagged = load_ownership(repo)
    assert flagged is not None
    assert flagged.analysis_completeness.score is None

    completeness["score"] = "nope"
    write_raw_state(repo, data)
    dropped = load_ownership(repo)
    assert dropped is not None
    assert dropped.analysis_completeness.score is None

    completeness["merge_strategy"] = "fast-forward"
    completeness["co_author_count"] = True
    write_raw_state(repo, data)
    invalid_history = load_ownership(repo)
    assert invalid_history is not None
    assert invalid_history.analysis_completeness.merge_strategy == "rebase"
    assert invalid_history.analysis_completeness.co_author_count == 0

    completeness["merge_strategy"] = 1
    completeness["co_author_count"] = -3
    write_raw_state(repo, data)
    negative_count = load_ownership(repo)
    assert negative_count is not None
    assert negative_count.analysis_completeness.merge_strategy == "rebase"
    assert negative_count.analysis_completeness.co_author_count == 0

    completeness["merge_strategy"] = "squash"
    completeness["co_author_count"] = "4"
    write_raw_state(repo, data)
    text_count = load_ownership(repo)
    assert text_count is not None
    assert text_count.analysis_completeness.merge_strategy == "squash"
    assert text_count.analysis_completeness.co_author_count == 0

    completeness["co_author_count"] = 4
    write_raw_state(repo, data)
    counted = load_ownership(repo)
    assert counted is not None
    assert counted.analysis_completeness.co_author_count == 4


def test_load_ownership_roundtrip(repo: Path) -> None:
    original = stored_ownership()
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
    assert loaded_owner.last_commit == NOW
    assert loaded_owner.score_breakdown is not None
    assert loaded_owner.score_breakdown.recency.score == pytest.approx(0.9)
    assert loaded_owner.score_breakdown.recency.available is True
    assert loaded_owner.freshness is not None
    assert loaded_owner.freshness.status == "stable"
    assert loaded_owner.freshness.active_expertise == pytest.approx(0.9)
    assert loaded_owner.freshness.half_life_days == pytest.approx(400.0)
    decay = loaded.paths["src/auth.py"].decay_warnings[0]
    assert decay.handle == "@bob"
    assert decay.days_since_last_commit == 200
    assert decay.status == "inactive"
    assert loaded.analysis_ref == "deadbeef"
    assert loaded.analysis_completeness.ignore_revs_applied is False
    assert loaded.analysis_completeness.ignore_revs_file == ""
    assert loaded.analysis_completeness.mailmap_applied is False
    assert loaded.analysis_completeness.mailmap_file == ""
    assert loaded.analysis_completeness.excluded_gitattributes == 0
    assert loaded.analysis_completeness.excluded_static == 0
    assert loaded.analysis_completeness.merge_strategy == "rebase"
    assert loaded.analysis_completeness.co_author_count == 0


def test_load_ownership_missing_returns_none(repo: Path) -> None:
    assert load_ownership(repo) is None


def test_load_ownership_invalid_returns_none(repo: Path) -> None:
    write_raw_state(
        repo,
        readable_state(
            {
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
            last_analyzed=NOW,
        ),
    )
    loaded = load_ownership(repo)
    assert loaded is not None
    entry = loaded.paths["a.py"].owners[0]
    assert entry.last_commit is None
    assert entry.score_breakdown is None
    raw = read_state(repo)
    assert raw is not None
    owners = _json_map(_json_map(raw["inferred"])["a.py"])["owners"]
    assert isinstance(owners, list)
    serialized = _json_map(owners[0])
    assert serialized["last_commit"] is None
    assert serialized["ownership_score"] == 0.2
    assert "signals" not in serialized


def test_path_activity_roundtrip_rejects_bools(repo: Path) -> None:
    owner = OwnerEntry(handle="@x", ownership_score=0.4, last_commit=NOW, commits=2)
    write_state(
        repo,
        OwnershipMap(
            paths={
                "a.py": PathOwnership(
                    owners=(owner,),
                    qualified_owner_count=1,
                    change_events=4,
                    cadence_days=2.5,
                    cochange_degree=0.25,
                )
            },
            last_analyzed=NOW,
        ),
    )
    loaded = load_ownership(repo)
    assert loaded is not None
    stored = loaded.paths["a.py"]
    assert stored.change_events == 4
    assert stored.cadence_days == pytest.approx(2.5)
    assert stored.cochange_degree == pytest.approx(0.25)
    raw = read_state(repo)
    assert raw is not None
    path = _json_map(_json_map(raw["inferred"])["a.py"])
    path["change_events"] = True
    path["cadence_days"] = True
    inferred = _json_map(raw["inferred"])
    inferred["a.py"] = path
    raw["inferred"] = inferred
    write_raw_state(repo, raw)
    reloaded = load_ownership(repo)
    assert reloaded is not None
    assert reloaded.paths["a.py"].change_events is None
    assert reloaded.paths["a.py"].cadence_days is None


def test_write_state_creates_parent_dirs(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested" / "dir"
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(nested))
    target = write_state(repo, stored_ownership())
    assert target.exists()
    assert target.is_relative_to(nested)
    monkeypatch.delenv("CHECKOWNERS_STATE_DIR")
    assert cache_directory() == Path.home() / ".checkowners"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", "~/checkowners-state")
    assert cache_directory() == home / "checkowners-state"


def test_bus_factor_summary_empty(repo: Path) -> None:
    ownership = stored_ownership()
    target = write_state(repo, ownership)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bus_factor_summary"]["critical_paths"] == []
    assert data["bus_factor_summary"]["distribution"]["knowledge_at_risk"] == 0.0
    assert data["bus_factor_summary"]["distribution"]["criticality_incomplete"] is True


def _stored(**overrides: object) -> dict[str, object]:
    """Return a well-formed stored state, with `overrides` merged into one path."""
    owner: dict[str, object] = {
        "handle": "@alice",
        "ownership_score": 1,
        "evidence_quality": 1,
        "commits": 2,
        "last_commit": NOW.isoformat(),
        "signals": {
            "recency": {"available": True, "score": 1.0},
            "frequency": {"available": True},
            "blame": {"available": False},
            "review": {"available": False},
        },
    }
    path: dict[str, object] = {
        "owners": [owner],
        "qualified_owner_count": 1,
        "decay_warnings": [],
    }
    path.update(overrides)
    return {
        "repo": "/repo",
        "inferred": {"p.py": path},
        "last_analyzed": NOW.isoformat(),
        "analysis_ref": "a" * 40,
    }

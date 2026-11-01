"""Reusing a stored analysis: freshness, config match, hysteresis and the history baseline."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from checkowners.config import (
    Config,
    ScoringConfig,
)
from checkowners.models import (
    OwnershipMap,
)
from checkowners.state import (
    BlameLines,
    HistoryBaseline,
    _history_payload,
    _parse_blame_map,
    _parse_count_map,
    _parse_history,
    _parse_moves,
    _parse_path_lists,
    _parse_string_set,
    load_hysteresis,
    read_handle_cache,
    read_history_baseline,
    read_state,
    reusable_ownership,
    write_state,
)
from checkowners.storage import (
    config_hash,
)
from tests.conftest import (
    NOW,
    git_commit,
    init_git_repo,
    stored_ownership,
    write_raw_state,
)


def test_reusable_ownership_refuses_advanced_head(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    first = git_commit(
        repo, "first", author="Alice", email="alice@example.com", date="2024-01-01T00:00:00Z"
    )
    write_state(
        repo,
        OwnershipMap(paths={}, last_analyzed=NOW, analysis_ref=first),
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
    write_state(repo, stored_ownership(), config=Config())
    changed = replace(Config(), scoring=replace(ScoringConfig(), recency_weight=0.5))
    assert reusable_ownership(repo, changed, head="deadbeef") is None
    assert reusable_ownership(repo, Config(), head="deadbeef") is not None


def test_reusable_ownership_rejects_incomplete_payload(repo: Path) -> None:
    target = write_state(repo, stored_ownership(), config=Config())
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
    assert read_handle_cache(repo) == {}


def test_hysteresis_roundtrip_and_preserve(repo: Path) -> None:
    write_state(
        repo,
        stored_ownership(),
        drift_reported_severity="low",
        drift_pending_severity="medium",
        drift_pending_streak=2,
    )
    assert load_hysteresis(repo) == ("low", "medium", 2)
    write_state(repo, stored_ownership())
    assert load_hysteresis(repo) == ("low", "medium", 2)


def test_load_hysteresis_missing_and_invalid_fields(repo: Path) -> None:
    assert load_hysteresis(repo) == (None, None, 0)
    write_state(
        repo,
        stored_ownership(),
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
    write_raw_state(repo, data)
    write_state(repo, stored_ownership())
    assert load_hysteresis(repo) == (None, None, 0)
    data = read_state(repo)
    assert data is not None
    data["drift_pending_streak"] = "nope"
    write_raw_state(repo, data)
    assert load_hysteresis(repo) == (None, None, 0)


def test_history_baseline_round_trips_without_emails(repo: Path) -> None:
    baseline = HistoryBaseline(
        analysis_ref="a" * 40,
        files={("c" * 40): ("src/app.py",)},
        modified={("c" * 40): 1},
        renames=frozenset({"c" * 40}),
        blame={"src/app.py": BlameLines(blob_sha="b" * 40, lines={("c" * 40): 3})},
        blame_inputs="inputs",
        rename_detection="true",
        truncated=False,
    )
    target = write_state(repo, stored_ownership(), config=Config(), history=baseline)
    loaded, reason = read_history_baseline(repo, Config())
    assert reason == ""
    assert loaded == baseline
    history = json.loads(target.read_text(encoding="utf-8"))["history"]
    assert "@" not in json.dumps(history)
    write_state(repo, stored_ownership(), config=Config())
    kept, kept_reason = read_history_baseline(repo, Config())
    assert kept_reason == ""
    assert kept == baseline
    stored = json.loads(target.read_text(encoding="utf-8"))
    package_version = stored["package_version"]
    stored["config_hash"] = "0" * 64
    target.write_text(json.dumps(stored), encoding="utf-8")
    rejected, why = read_history_baseline(repo, Config())
    assert rejected is None
    assert why == "scoring configuration changed"
    stored["config_hash"] = config_hash(Config())
    stored["package_version"] = "0.0.0"
    target.write_text(json.dumps(stored), encoding="utf-8")
    versioned, version_reason = read_history_baseline(repo, Config())
    assert versioned is None
    assert version_reason == "package version changed"
    stored["package_version"] = package_version
    stored["history"] = {"analysis_ref": "x"}
    target.write_text(json.dumps(stored), encoding="utf-8")
    corrupt, corrupt_reason = read_history_baseline(repo, Config())
    assert corrupt is None
    assert corrupt_reason == "incremental baseline is corrupt"


def test_history_baseline_rejects_bad_fields(repo: Path) -> None:
    sha = "c" * 40
    baseline = HistoryBaseline(
        analysis_ref="a" * 40,
        files={sha: ("src/app.py",)},
        modified={sha: 1},
        renames=frozenset({sha}),
        blame={"src/app.py": BlameLines(blob_sha="b" * 40, lines={sha: 3})},
        blame_inputs="inputs",
        rename_detection="true",
        truncated=False,
    )
    target = write_state(repo, stored_ownership(), config=Config(), history=baseline)
    original = json.loads(target.read_text(encoding="utf-8"))

    def reason_after(mutate: Callable[[dict[str, object]], None]) -> str:
        payload = json.loads(json.dumps(original))
        mutate(payload)
        target.write_text(json.dumps(payload), encoding="utf-8")
        _loaded, reason = read_history_baseline(repo, Config())
        return reason

    assert reason_after(lambda payload: payload.__setitem__("repo_id", "origin:nope")) == (
        "repository identity changed"
    )
    valid = {
        "analysis_ref": "a" * 40,
        "files": {sha: ["src/app.py"]},
        "modified": {sha: 1},
        "renames": [sha],
        "blame": {"src/app.py": {"blob_sha": "b" * 40, "lines": {sha: 3}}},
        "blame_inputs": "inputs",
        "rename_detection": "true",
        "truncated": False,
    }
    legacy = _parse_history(valid)
    assert legacy is not None
    assert legacy.rename_similarity == -1
    rejected = (
        {"follow_renames": "yes", "rename_similarity": 50, "moves": {}},
        {"follow_renames": True, "rename_similarity": True, "moves": {}},
        {"follow_renames": True, "rename_similarity": 0, "moves": {}},
        {"follow_renames": True, "rename_similarity": 101, "moves": {}},
        {"follow_renames": True, "rename_similarity": 50, "moves": []},
    )
    for extra in rejected:
        assert _parse_history({**valid, **extra}) is None
    kept = _parse_history(
        {**valid, "follow_renames": False, "rename_similarity": 80, "moves": {}},
    )
    assert kept is not None
    assert kept.follow_renames is False
    assert kept.rename_similarity == 80
    assert _parse_moves([]) is None
    assert _parse_moves({1: []}) is None
    assert _parse_moves({"abc": "nope"}) is None
    assert _parse_moves({"abc": ["nope"]}) is None
    assert _parse_moves({"abc": [{"kind": "X", "score": 1, "source": "a", "dest": "b"}]}) is None
    assert _parse_moves({"abc": [{"kind": "R", "score": True, "source": "a", "dest": "b"}]}) is None
    assert _parse_moves({"abc": [{"kind": "R", "score": 1, "source": 1, "dest": "b"}]}) is None
    assert _parse_moves({"abc": [{"kind": "R", "score": 101, "source": "a", "dest": "b"}]}) is None
    parsed_moves = _parse_moves({"abc": [{"kind": "C", "score": 80, "source": "a", "dest": "b"}]})
    assert parsed_moves is not None
    assert parsed_moves["abc"][0].dest == "b"
    assert _parse_history([]) is None
    assert _parse_history({**valid, "analysis_ref": 1}) is None
    assert _parse_history({**valid, "files": []}) is None
    assert _parse_history({**valid, "modified": []}) is None
    assert _parse_history({**valid, "renames": {}}) is None
    assert _parse_history({**valid, "blame": []}) is None
    assert _parse_history({**valid, "blame_inputs": 1}) is None
    assert _parse_history({**valid, "rename_detection": 1}) is None
    assert _parse_history({**valid, "truncated": "no"}) is None
    assert _parse_path_lists({1: ["src/app.py"]}) is None
    assert _parse_path_lists({sha: "src/app.py"}) is None
    assert _parse_path_lists({sha: [1]}) is None
    assert _parse_count_map({sha: True}) is None
    assert _parse_count_map({sha: -1}) is None
    assert _parse_string_set("nope") is None
    assert _parse_string_set([1]) is None
    assert _parse_blame_map({"src/app.py": []}) is None
    assert _parse_blame_map({"src/app.py": {"blob_sha": "", "lines": {}}}) is None
    assert _parse_blame_map({"src/app.py": {"blob_sha": "abc", "lines": {sha: -1}}}) is None
    assert _history_payload(None, None) is None
    assert _history_payload(None, {"history": "nope"}) is None

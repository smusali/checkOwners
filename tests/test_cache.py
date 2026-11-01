"""The cache directory: handle, graph and blame caches, eviction and locking."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from checkowners.config import (
    Config,
)
from checkowners.privacy import email_token
from checkowners.state import (
    HandleCacheEntry,
    _blame_line_counts,
    _evict,
    blame_cache_path,
    cache_clear,
    cache_info,
    cache_purge,
    handle_cache_path,
    read_blame_cache,
    read_graph_cache,
    read_handle_cache,
    write_blame_cache,
    write_graph_cache,
    write_handle_cache,
    write_state,
)
from checkowners.storage import (
    cache_directory,
    config_hash,
    file_size,
    read_json_file,
    repository_identity,
    write_json_file,
)
from tests.conftest import (
    NOW,
    stored_ownership,
)


def test_handle_cache_roundtrip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_handle_cache(
        repo,
        {
            "alice@example.com": HandleCacheEntry(handle="@alice", source="commit"),
            "gone@example.com": HandleCacheEntry(
                handle="",
                expires_at="2099-01-01T00:00:00+00:00",
            ),
        },
    )
    cache = read_handle_cache(repo)
    assert "alice@example.com" not in cache
    assert cache[email_token("alice@example.com")].handle == "@alice"
    assert cache[email_token("gone@example.com")].handle == ""


def test_handle_cache_merges_on_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_handle_cache(repo, {"alice@example.com": HandleCacheEntry(handle="@alice")})
    write_handle_cache(repo, {"bob@example.com": HandleCacheEntry(handle="@bob")})
    cache = read_handle_cache(repo)
    assert cache[email_token("alice@example.com")].handle == "@alice"
    assert cache[email_token("bob@example.com")].handle == "@bob"


def test_handle_cache_missing_returns_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert read_handle_cache(repo) == {}
    target = write_handle_cache(repo, {"x@example.com": HandleCacheEntry(handle="@x")})
    target.write_text("{not json", encoding="utf-8")
    assert read_handle_cache(repo) == {}
    target.write_text("[]", encoding="utf-8")
    assert read_handle_cache(repo) == {}


def test_parallel_handle_writes_do_not_corrupt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    errors: list[BaseException] = []

    def write_one(index: int) -> None:
        try:
            write_handle_cache(
                repo,
                {f"user{index}@example.com": HandleCacheEntry(handle=f"@user{index}")},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_one, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    cache = read_handle_cache(repo)
    assert len(cache) == 8
    assert cache[email_token("user3@example.com")].handle == "@user3"


def test_handle_cache_and_json_files_drop_malformed_values(tmp_path: Path) -> None:
    path = handle_cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "alice@example.com": "@alice",
                "email:dead": "",
                "n": 1,
                "bad-handle": {"handle": 1},
                "bad-source": {"handle": "@a", "source": 1, "account_type": 2, "expires_at": 3},
                "empty": {"handle": "", "expires_at": ""},
                "miss": {"handle": "", "expires_at": "9999-01-01T00:00:00+00:00"},
            }
        ),
        encoding="utf-8",
    )
    loaded = read_handle_cache(tmp_path)
    handles = {entry.handle: entry for entry in loaded.values()}
    assert handles["@alice"].source == ""
    assert handles["@a"].source == ""
    assert handles["@a"].account_type == ""
    assert any(entry.expires_at.startswith("9999") for entry in loaded.values())
    missing = tmp_path / "missing.json"
    assert read_json_file(missing) is None
    write_json_file(missing, {"ok": True})
    assert read_json_file(missing) == {"ok": True}
    missing.write_text("[1]", encoding="utf-8")
    assert read_json_file(missing) is None
    legacy = handle_cache_path(tmp_path).parents[1] / "handles.json"
    legacy.write_text("{}", encoding="utf-8")
    assert cache_info()["handles"] is True


def test_graph_cache_roundtrip(tmp_path: Path) -> None:
    graph_data = {"nodes": [{"id": "contrib::a"}], "edges": []}
    target = write_graph_cache(tmp_path, NOW, graph_data)
    assert target.exists()
    assert read_graph_cache(tmp_path, NOW) == graph_data
    stored = json.loads(target.read_text(encoding="utf-8"))
    target.write_text(json.dumps({**stored, "package_version": "0.0.0"}), encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW) is None
    target.write_text(json.dumps({**stored, "repo_id": "origin:nope"}), encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW) is None


def test_graph_cache_stale_timestamp_ignored(tmp_path: Path) -> None:
    write_graph_cache(tmp_path, NOW, {"nodes": [], "edges": []})
    newer = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    assert read_graph_cache(tmp_path, newer) is None


def test_graph_cache_missing_returns_none(tmp_path: Path) -> None:
    assert read_graph_cache(tmp_path, NOW) is None
    target = write_graph_cache(tmp_path, NOW, {"nodes": [], "edges": []})
    target.write_text("{not json", encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW) is None
    target.write_text("[]", encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW) is None


def test_graph_cache_keyed_by_repo(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    write_graph_cache(repo_a, NOW, {"nodes": [{"id": "a"}], "edges": []})
    assert read_graph_cache(repo_b, NOW) is None
    assert read_graph_cache(repo_a, NOW) == {"nodes": [{"id": "a"}], "edges": []}


def test_graph_cache_rejects_mismatched_identity_config_and_ref(tmp_path: Path) -> None:
    graph: dict[str, list[object]] = {"nodes": [], "edges": []}
    target = write_graph_cache(tmp_path, NOW, graph, config=Config(), analysis_ref="abc")
    stored = json.loads(target.read_text(encoding="utf-8"))
    stored["repo_id"] = "origin:elsewhere"
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW, config=Config(), analysis_ref="abc") is None

    stored["repo_id"] = repository_identity(tmp_path)
    stored["config_hash"] = "0" * 64
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW, config=Config(), analysis_ref="abc") is None

    stored["config_hash"] = config_hash(Config())
    stored["analysis_ref"] = "other"
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW, config=Config(), analysis_ref="abc") is None

    stored["analysis_ref"] = "abc"
    stored["graph"] = []
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW, config=Config(), analysis_ref="abc") is None

    stored["graph"] = graph
    target.write_text(json.dumps(stored), encoding="utf-8")
    assert read_graph_cache(tmp_path, NOW, config=Config(), analysis_ref="abc") == graph


def test_blame_cache_rejects_a_bad_digest_and_a_bad_payload() -> None:
    with pytest.raises(ValueError, match="digest"):
        blame_cache_path("nope")
    assert _blame_line_counts([]) is None
    assert _blame_line_counts({}) is None
    assert _blame_line_counts({"lines": []}) is None
    assert _blame_line_counts({"lines": {1: 1}}) is None
    assert _blame_line_counts({"lines": {"a": True}}) is None
    assert _blame_line_counts({"lines": {"a": -1}}) is None
    assert _blame_line_counts({"lines": {"a": 2}}) == {"a": 2}
    digest = "ab" + ("cd" * 31)
    write_blame_cache(digest, {"a": 1})
    blame_cache_path(digest).write_text("{", encoding="utf-8")
    assert read_blame_cache(digest) is None
    blame_cache_path(digest).write_text("[]", encoding="utf-8")
    assert read_blame_cache(digest) is None


def test_concurrent_blame_cache_writes_stay_intact() -> None:
    digest = "ab" + ("cd" * 31)
    lines = {"alice@example.com": 4, "bob@example.com": 2}

    def write() -> None:
        for _ in range(30):
            write_blame_cache(digest, lines)

    threads = [threading.Thread(target=write) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert cache_info()["blame_files"] == 1
    assert read_blame_cache(digest) == lines
    assert cache_clear() >= 1
    assert read_blame_cache(digest) is None
    assert cache_info()["blame_files"] == 0


def test_cache_evicts_oldest_state_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("checkowners.state.CACHE_LIMIT_BYTES", 1)
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    older.mkdir()
    newer.mkdir()
    first = write_state(older, stored_ownership())
    os.utime(first, (1, 1))
    second = write_state(newer, stored_ownership())
    assert not first.exists()
    assert second.exists()
    info = cache_info()
    assert info["state_files"] == 1
    assert info["limit_bytes"] == 1


def test_cache_clear_keeps_handles_and_purge_removes_them(repo: Path) -> None:
    write_state(repo, stored_ownership())
    handles = write_handle_cache(repo, {"alice@example.com": HandleCacheEntry(handle="@alice")})
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


def test_evict_stops_once_under_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[Path] = []
    for name in ("oldest", "middle", "newest"):
        root = tmp_path / name
        root.mkdir()
        paths.append(write_state(root, stored_ownership()))
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


def test_cache_info_reports_the_current_repository(repo: Path) -> None:
    absent = cache_info(repo)
    assert absent["current_repo_id"] == repository_identity(repo)
    assert absent["current_state_exists"] is False
    assert absent["current_state_path"].endswith(".json")
    write_state(repo, stored_ownership())
    present = cache_info(repo)
    assert present["current_state_exists"] is True
    assert Path(present["current_state_path"]).is_file()


def test_cache_info_blanks_missing_or_non_string_fields(repo: Path) -> None:
    target = write_state(repo, stored_ownership())
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


def test_file_size_is_zero_when_stat_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")

    def boom(_self: Path) -> object:
        raise OSError("stat")

    monkeypatch.setattr(Path, "stat", boom)
    assert file_size(target) == 0

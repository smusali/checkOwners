"""Identity resolution, the contributor graph, and contributor status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from checkowners.github import resolve_handles
from checkowners.identity import (
    MASS_COMMIT_FLOOR,
    ContributorRecord,
    DuplicateGroup,
    HandleHit,
    IdentityGraph,
    apply_merge,
    assign_status,
    audit_payload,
    former_knowledge_share,
    is_bot_identity,
    mechanical_authors,
    merge_owners,
    probable_duplicates,
    revert_merge,
    without_bot_authors,
)
from checkowners.models import OwnerEntry
from checkowners.state import HandleCacheEntry, read_handle_cache, write_handle_cache

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _owner(handle: str, score: float = 0.5, commits: int = 1) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=None,
        commits=commits,
    )


def test_commit_login_resolves_without_search(tmp_path: Path) -> None:
    commit = MagicMock()
    commit.author.login = "alice"
    commit.author.type = "User"
    commit.committer.type = "User"
    client = MagicMock()
    client.get_repo.return_value.get_commit.return_value = commit
    with (
        patch("checkowners.github.get_github_client", return_value=client),
        patch("checkowners.github.github_repository", return_value="acme/app"),
    ):
        result = resolve_handles(
            {"alice@example.com"},
            "ghp_test",
            repo_root=tmp_path,
            author_shas={"alice@example.com": "a" * 40},
        )
    assert result == {"alice@example.com": "@alice"}
    client.search_users.assert_not_called()


def test_handle_cache_is_per_repository(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    write_handle_cache(first, {"alice@example.com": HandleCacheEntry(handle="@alice")})
    assert read_handle_cache(second) == {}
    stored = read_handle_cache(first)
    assert any(entry.handle == "@alice" for entry in stored.values())


def test_negative_cache_skips_until_it_expires(tmp_path: Path) -> None:
    client = MagicMock()
    user = MagicMock()
    user.login = "alice"
    client.search_users.return_value = [user]
    future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    write_handle_cache(
        tmp_path,
        {"alice@example.com": HandleCacheEntry(handle="", expires_at=future)},
    )
    with patch("checkowners.github.get_github_client", return_value=client):
        skipped = resolve_handles({"alice@example.com"}, "ghp_test", repo_root=tmp_path)
    assert skipped == {}
    client.search_users.assert_not_called()
    past = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
    write_handle_cache(
        tmp_path,
        {"alice@example.com": HandleCacheEntry(handle="", expires_at=past)},
    )
    with patch("checkowners.github.get_github_client", return_value=client):
        found = resolve_handles({"alice@example.com"}, "ghp_test", repo_root=tmp_path)
    assert found == {"alice@example.com": "@alice"}
    client.search_users.assert_called_once()


def test_override_beats_search_and_separations_stay_split(tmp_path: Path) -> None:
    client = MagicMock()
    user = MagicMock()
    user.login = "bob"
    client.search_users.return_value = [user]
    with patch("checkowners.github.get_github_client", return_value=client):
        result = resolve_handles(
            {"alice@example.com"},
            "ghp_test",
            repo_root=tmp_path,
            overrides={"alice@example.com": "@alice"},
        )
    assert result == {"alice@example.com": "@alice"}
    client.search_users.assert_not_called()
    owners = (_owner("a@x.com", 0.4), _owner("b@x.com", 0.6))
    hits = {
        "a@x.com": HandleHit("@alice", "commit"),
        "b@x.com": HandleHit("@alice", "commit"),
    }
    assert len(merge_owners(owners, hits).owners) == 1
    kept = merge_owners(owners, hits, (("a@x.com", "b@x.com"),))
    assert len(kept.owners) == 2
    search_hits = {
        "a@x.com": HandleHit("@alice", "search"),
        "b@x.com": HandleHit("@alice", "search"),
    }
    assert len(merge_owners(owners, search_hits).owners) == 2


def test_merge_then_unmerge_restores_records() -> None:
    left = ContributorRecord(handle="a@x.com", emails=("a@x.com",), names=("Ada",))
    right = ContributorRecord(handle="b@x.com", emails=("b@x.com",), names=("Ada B",))
    graph = IdentityGraph(contributors=(left, right))
    merged = apply_merge(graph, "a@x.com", "b@x.com", "@ada")
    restored = revert_merge(merged, "a@x.com", "b@x.com")
    assert {record.handle: record for record in restored.contributors} == {
        left.handle: left,
        right.handle: right,
    }
    assert any(event.action == "unmerge" for event in restored.audit)
    assert ("a@x.com", "b@x.com") in restored.separations


def test_audit_reports_a_shared_name_without_writing() -> None:
    records = (
        ContributorRecord(handle="a@x.com", names=("Ada",)),
        ContributorRecord(handle="b@x.com", names=("Ada",)),
    )
    graph = IdentityGraph(contributors=records)
    found = probable_duplicates(graph.contributors)
    assert len(found) == 1
    assert found[0].reason == "same author name"
    assert graph.contributors == records


def test_former_share_and_unknown_status() -> None:
    owners = (
        _owner("a@x.com", 0.8),
        _owner("b@x.com", 0.2),
    )
    former = (
        OwnerEntry(
            handle="a@x.com",
            ownership_score=0.8,
            last_commit=None,
            commits=1,
            contributor_status="former",
        ),
        OwnerEntry(
            handle="b@x.com",
            ownership_score=0.2,
            last_commit=None,
            commits=1,
            contributor_status="active",
        ),
    )
    assert former_knowledge_share(former) == 0.8
    assert former_knowledge_share(owners) == 0.0
    assert (
        assign_status(
            "a@x.com",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=None,
            last_commit=None,
            as_of=_NOW,
            threshold_days=90,
        )
        == "unknown"
    )


def test_additional_bot_addresses_are_excluded() -> None:
    assert is_bot_identity("release@company.com", ("release@company.com",))
    assert not is_bot_identity("release@company.com")
    kept = without_bot_authors(
        {"release@company.com": 1, "ada@x.com": 2},
        ("release@company.com",),
    )
    assert list(kept) == ["ada@x.com"]


def test_repeated_mechanical_subjects_are_flagged() -> None:
    identical = tuple(("release@company.com", "bump deps") for _ in range(1000))
    phrases = tuple(f"rewrite the {name} parser" for name in ("alpha", "beta", "gamma", "delta"))
    varied = tuple(("ada@x.com", phrase) for phrase in phrases * 5)
    assert "release@company.com" in mechanical_authors(identical)
    assert "ada@x.com" not in mechanical_authors(varied[:MASS_COMMIT_FLOOR])


def test_aggregate_audit_hides_identities() -> None:
    groups = (DuplicateGroup(("a@x.com", "b@x.com"), "same author name"),)
    hidden = audit_payload(groups, aggregate_only=True)
    shown = audit_payload(groups, aggregate_only=False)
    assert hidden == {"duplicate_count": 1}
    assert "duplicates" not in hidden
    duplicates = shown["duplicates"]
    assert isinstance(duplicates, list)
    assert duplicates[0]["identities"] == ["a@x.com", "b@x.com"]

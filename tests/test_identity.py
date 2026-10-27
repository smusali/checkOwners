from __future__ import annotations

import json
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
    apply_contributor_status,
    apply_merge,
    assign_status,
    audit_payload,
    audit_records,
    bot_identities,
    former_knowledge_share,
    is_bot_identity,
    load_identity_graph,
    mechanical_authors,
    merge_owners,
    override_handles,
    probable_duplicates,
    record_certain_merges,
    repository_former_share,
    revert_merge,
    save_identity_graph,
    service_account_identities,
)
from checkowners.models import Config, IdentityOverride, OwnerEntry, OwnershipMap, PathOwnership
from checkowners.state import (
    HandleCacheEntry,
    identity_store_path,
    read_handle_cache,
    write_handle_cache,
)

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


def test_repeated_mechanical_subjects_are_flagged() -> None:
    identical = tuple(("release@company.com", "bump deps") for _ in range(1000))
    phrases = tuple(f"rewrite the {name} parser" for name in ("alpha", "beta", "gamma", "delta"))
    varied = tuple(("ada@x.com", phrase) for phrase in phrases * 5)
    assert "release@company.com" in mechanical_authors(identical)
    assert "ada@x.com" not in mechanical_authors(varied[:MASS_COMMIT_FLOOR])


def test_overrides_roster_and_org_membership_set_status() -> None:
    mapped = override_handles(
        (
            ("alice", IdentityOverride(emails=("Ada@example.com",))),
            ("@bob", IdentityOverride()),
        )
    )
    assert mapped["alice"] == "@alice"
    assert mapped["ada@example.com"] == "@alice"
    assert mapped["@bob"] == "@bob"
    config = Config(
        bot_addresses=("release@company.com",),
        identity_overrides=(
            ("svc", IdentityOverride(emails=("svc@company.com",), service_account=True)),
            ("human", IdentityOverride(service_account=False)),
        ),
    )
    assert service_account_identities(config.identity_overrides) == ("@svc", "svc@company.com")
    assert "release@company.com" in bot_identities(config)
    assert is_bot_identity("actions@github.com")
    assert is_bot_identity("bot@company.com")
    assert is_bot_identity("Dependabot")
    recent = _NOW - timedelta(days=2)
    stale = _NOW - timedelta(days=200)
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert (
        assign_status(
            "Ada@example.com",
            roster={"zzz": "active", "ada@example.com": "former"},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=None,
            last_commit=None,
            as_of=_NOW,
            threshold_days=90,
        )
        == "former"
    )
    assert (
        assign_status(
            "@Ada",
            roster={"ada": "inactive"},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=None,
            last_commit=None,
            as_of=_NOW,
            threshold_days=90,
        )
        == "inactive"
    )
    assert (
        assign_status(
            "person@x.com",
            roster={},
            bot_identities=(),
            account_type="Bot",
            committer_type="",
            org_members=None,
            last_commit=None,
            as_of=_NOW,
            threshold_days=90,
        )
        == "bot"
    )
    assert (
        assign_status(
            "person@x.com",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="Bot",
            org_members=None,
            last_commit=None,
            as_of=_NOW,
            threshold_days=90,
        )
        == "bot"
    )
    assert (
        assign_status(
            "@dependabot[bot]",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=None,
            last_commit=None,
            as_of=_NOW,
            threshold_days=90,
        )
        == "bot"
    )
    members = frozenset({"Alice"})
    assert (
        assign_status(
            "@alice",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=members,
            last_commit=recent,
            as_of=_NOW,
            threshold_days=90,
        )
        == "active"
    )
    assert (
        assign_status(
            "@alice",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=members,
            last_commit=stale,
            as_of=_NOW,
            threshold_days=90,
        )
        == "inactive"
    )
    assert (
        assign_status(
            "@alice",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=members,
            last_commit=None,
            as_of=_NOW,
            threshold_days=90,
        )
        == "inactive"
    )
    assert (
        assign_status(
            "@carol",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=members,
            last_commit=recent,
            as_of=_NOW,
            threshold_days=90,
        )
        == "external"
    )
    assert (
        assign_status(
            "carol@x.com",
            roster={},
            bot_identities=(),
            account_type="",
            committer_type="",
            org_members=members,
            last_commit=naive,
            as_of=_NOW,
            threshold_days=90,
        )
        == "inactive"
    )
    owners = apply_contributor_status(
        (_owner("ada@x.com"),),
        config=Config(),
        hits={"ada@x.com": HandleHit("@ada", "commit", account_type="User", committer_type="User")},
        org_members=None,
        as_of=_NOW,
    )
    assert owners[0].contributor_status == "unknown"
    indexed = apply_contributor_status(
        (_owner("@ada"),),
        config=Config(),
        hits={"other": HandleHit("", "search")},
        org_members=None,
        as_of=_NOW,
    )
    assert indexed[0].handle == "@ada"


def test_merges_are_idempotent_and_groups_of_three_are_recorded() -> None:
    covered = ContributorRecord(handle="@ada", aliases=("a@x.com",), emails=("b@x.com",))
    graph = IdentityGraph(contributors=(covered,))
    assert apply_merge(graph, "a@x.com", "b@x.com", "@ada") is graph
    partial = IdentityGraph(contributors=(ContributorRecord(handle="@ada", emails=("a@x.com",)),))
    widened = apply_merge(partial, "a@x.com", "b@x.com", "@ada")
    assert any(record.handle == "@ada" for record in widened.contributors)
    empty = IdentityGraph()
    assert revert_merge(empty, "a@x.com", "b@x.com") is empty
    recorded = record_certain_merges(
        IdentityGraph(),
        (("only",), ("a@x.com", "b@x.com", "c@x.com")),
        {"a@x.com": "@ada"},
    )
    assert any(event.action == "merge" for event in recorded.audit)
    assert record_certain_merges(IdentityGraph(), (), {}).audit == ()
    left = _owner("a@x.com", 0.2)
    right = OwnerEntry(
        handle="b@x.com",
        ownership_score=0.8,
        last_commit=_NOW,
        commits=3,
    )
    merged = merge_owners(
        (left, right),
        {"a@x.com": HandleHit("@ada", "commit"), "b@x.com": HandleHit("@ada", "noreply")},
    )
    assert merged.owners[0].handle == "@ada"
    assert merged.owners[0].last_commit == _NOW
    assert merged.groups


def test_duplicate_reasons_skip_separated_and_already_merged_pairs() -> None:
    named = probable_duplicates(
        (
            ContributorRecord(handle="@ada", names=("Ada",)),
            ContributorRecord(handle="ada@x.com", names=("Ada",)),
        )
    )
    assert named[0].reason == "same author name"
    assert (
        probable_duplicates(
            (
                ContributorRecord(handle="@ada", names=("Ada",)),
                ContributorRecord(handle="@ada", names=("Ada",)),
            )
        )
        == ()
    )
    local = probable_duplicates(
        (
            ContributorRecord(handle="@ada"),
            ContributorRecord(handle="other@x.com", emails=("nope@x.com", "ada@x.com")),
        )
    )
    assert local[0].reason == "email local-part"
    other_way = probable_duplicates(
        (
            ContributorRecord(handle="ada@x.com", emails=("nope@x.com", "ada@x.com")),
            ContributorRecord(handle="@ada"),
        )
    )
    assert other_way[0].reason == "email local-part"
    alias = probable_duplicates(
        (
            ContributorRecord(handle="@a", aliases=("shared",)),
            ContributorRecord(handle="@b", names=("Shared",)),
        )
    )
    assert alias[0].reason == "shared alias"
    assert (
        probable_duplicates(
            (
                ContributorRecord(handle="@ada", names=("Ada",)),
                ContributorRecord(handle="ada@x.com", names=("Ada",)),
            ),
            separations=(("@ada", "ada@x.com"),),
        )
        == ()
    )
    assert (
        probable_duplicates(
            (
                ContributorRecord(handle="@ada", aliases=("ada@x.com",)),
                ContributorRecord(handle="ada@x.com"),
            )
        )
        == ()
    )
    assert (
        probable_duplicates(
            (
                ContributorRecord(handle="@c", names=("Cara",)),
                ContributorRecord(handle="@d", names=("Dan",)),
            )
        )
        == ()
    )


def test_audit_records_and_graph_round_trip(tmp_path: Path) -> None:
    records = audit_records(
        ("ada@x.com", "@ada"),
        {"ada@x.com": ("Ada",)},
        (
            (
                "ada",
                IdentityOverride(emails=("ada@x.com",), aliases=("Ada L",), service_account=True),
            ),
        ),
    )
    stored = {record.handle: record for record in records}
    assert stored["@ada"].service_account is True
    assert "ada@x.com" in stored["@ada"].emails
    graph = apply_merge(
        IdentityGraph(
            contributors=(*records, ContributorRecord(handle="b@x.com")),
            separations=(("x", "y"),),
        ),
        "b@x.com",
        "c@x.com",
        "@merged",
    )
    save_identity_graph(tmp_path, graph)
    loaded = load_identity_graph(tmp_path)
    assert any(record.handle == "@ada" for record in loaded.contributors)
    assert loaded.separations
    assert loaded.audit
    path = identity_store_path(tmp_path)
    path.write_text(
        json.dumps(
            {
                "contributors": [
                    "nope",
                    {"handle": ""},
                    {"handle": "@kept", "emails": "nope", "source": 1, "service_account": "yes"},
                    {
                        "handle": "@named",
                        "emails": ["ada@x.com", 4],
                        "names": ["Ada"],
                        "aliases": ["A"],
                    },
                ],
                "separations": ["nope", ["only"], [1, 2], ["", "b"], ["a", "b"]],
                "audit": [
                    "nope",
                    {"action": "drop"},
                    {"action": "merge", "left": "", "right": "b", "into": "c", "at": "t"},
                    {"action": "merge", "left": 1, "right": "b", "into": "c", "at": "t"},
                    {
                        "action": "unmerge",
                        "left": "a",
                        "right": "b",
                        "into": "c",
                        "at": "t",
                        "prior": [{"handle": "@prior"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    repaired = load_identity_graph(tmp_path)
    assert {record.handle for record in repaired.contributors} >= {"@kept", "@named"}
    assert repaired.separations == (("a", "b"),)
    assert repaired.audit[0].action == "unmerge"
    path.write_text(
        json.dumps({"contributors": {}, "separations": {}, "audit": {}}),
        encoding="utf-8",
    )
    assert load_identity_graph(tmp_path).contributors == ()
    path.write_text("{", encoding="utf-8")
    assert load_identity_graph(tmp_path).contributors == ()
    ownership = OwnershipMap(
        paths={
            "src/a.py": PathOwnership(
                owners=(
                    OwnerEntry(
                        handle="@ada",
                        ownership_score=1.0,
                        last_commit=_NOW,
                        commits=1,
                        contributor_status="former",
                    ),
                ),
                qualified_owner_count=1,
            )
        },
        last_analyzed=_NOW,
    )
    assert repository_former_share(ownership, (("src/*", 2.0), ("missing/*", 9.0))) == 1.0


def test_aggregate_audit_hides_identities() -> None:
    groups = (DuplicateGroup(("a@x.com", "b@x.com"), "same author name"),)
    hidden = audit_payload(groups, aggregate_only=True)
    shown = audit_payload(groups, aggregate_only=False)
    assert hidden == {"duplicate_count": 1}
    assert "duplicates" not in hidden
    duplicates = shown["duplicates"]
    assert isinstance(duplicates, list)
    assert duplicates[0]["identities"] == ["a@x.com", "b@x.com"]

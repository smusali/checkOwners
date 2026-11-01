"""Contributor status from the roster, bots, and organization membership."""

from __future__ import annotations

from datetime import datetime, timedelta

from checkowners.config import (
    Config,
    IdentityOverride,
)
from checkowners.identity import (
    MASS_COMMIT_FLOOR,
    HandleHit,
    apply_contributor_status,
    assign_status,
    bot_identities,
    former_knowledge_share,
    is_bot_identity,
    mechanical_authors,
    override_handles,
    service_account_identities,
)
from checkowners.models import (
    OwnerEntry,
)
from tests.test_identity import (
    _NOW,
    _owner,
)


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

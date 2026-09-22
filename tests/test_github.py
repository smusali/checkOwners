"""Tests for checkowners.github module."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from checkowners.github import (
    build_review_coverage,
    clear_api_evidence,
    collection_timestamp,
    create_team_resolver,
    external_evidence_payload,
    get_github_client,
    get_github_token,
    note_api_call,
    resolve_handles,
    resolve_noreply_handle,
    team_snapshot_hash,
)
from checkowners.state import read_handle_cache, write_handle_cache


def test_get_github_token_present() -> None:
    with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"}):
        assert get_github_token() == "ghp_test123"


def test_get_github_token_missing() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert get_github_token() == ""


def _fake_pull(reviewer_logins: list[str], filenames: list[str]) -> MagicMock:
    pull = MagicMock()
    reviews = []
    for login in reviewer_logins:
        review = MagicMock()
        review.user.login = login
        reviews.append(review)
    pull.get_reviews.return_value = reviews
    files = []
    for name in filenames:
        changed = MagicMock()
        changed.filename = name
        files.append(changed)
    pull.get_files.return_value = files
    return pull


def test_build_review_coverage_maps_logins_to_emails() -> None:
    client = MagicMock()
    repo = MagicMock()
    repo.get_pulls.return_value = [
        _fake_pull(["alice", "bob"], ["src/main.py"]),
        _fake_pull(["alice"], ["src/main.py", "src/util.py"]),
    ]
    client.get_repo.return_value = repo
    email_to_handle = {"alice@example.com": "@alice", "bob@example.com": "@bob"}
    with (
        patch("checkowners.github.get_github_client", return_value=client),
        patch("checkowners.github.resolve_handles", return_value=email_to_handle),
    ):
        coverage = build_review_coverage(
            "tok", "org/repo", {"alice@example.com", "bob@example.com"}
        )
    # src/main.py: alice reviewed in 2 pulls, bob in 1 -> total 3.
    assert coverage["src/main.py"]["alice@example.com"] == 2 / 3
    assert coverage["src/main.py"]["bob@example.com"] == 1 / 3
    # src/util.py: only alice, single review -> full coverage.
    assert coverage["src/util.py"]["alice@example.com"] == 1.0


def test_build_review_coverage_empty_without_client() -> None:
    with patch("checkowners.github.get_github_client", return_value=None):
        assert build_review_coverage("tok", "org/repo", {"a@example.com"}) == {}


def test_build_review_coverage_empty_when_no_handles_resolve() -> None:
    client = MagicMock()
    with (
        patch("checkowners.github.get_github_client", return_value=client),
        patch("checkowners.github.resolve_handles", return_value={}),
    ):
        assert build_review_coverage("tok", "org/repo", {"a@example.com"}) == {}


def test_get_github_client_with_token() -> None:
    with patch("github.Github") as mock_cls:
        client = get_github_client("ghp_test")
    mock_cls.assert_called_once_with("ghp_test")
    assert client is not None


def test_get_github_client_empty_token() -> None:
    assert get_github_client("") is None


def test_resolve_handles_no_token() -> None:
    result = resolve_handles({"alice@example.com"}, "")
    assert result == {}


def test_resolve_handles_success() -> None:
    mock_user = MagicMock()
    mock_user.login = "alice"
    mock_client = MagicMock()
    mock_client.search_users.return_value = [mock_user]
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"alice@example.com"}, "ghp_test")
    assert result == {"alice@example.com": "@alice"}


def test_resolve_handles_not_found() -> None:
    mock_client = MagicMock()
    mock_client.search_users.return_value = []
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"unknown@example.com"}, "ghp_test")
    assert result == {}


def test_resolve_handles_api_error() -> None:
    mock_client = MagicMock()
    mock_client.search_users.side_effect = Exception("rate limit")
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"alice@example.com"}, "ghp_test")
    assert result == {}


def test_create_team_resolver_all_in_one_team() -> None:
    mock_team = MagicMock()
    mock_team.slug = "backend"
    mock_team.get_members.return_value = [
        MagicMock(login="alice"),
        MagicMock(login="bob"),
    ]
    mock_org = MagicMock()
    mock_org.get_teams.return_value = [mock_team]
    mock_client = MagicMock()
    mock_client.get_organization.return_value = mock_org

    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolver = create_team_resolver("ghp_test", "myorg")
    assert resolver is not None
    assert resolver(("@alice", "@bob")) == "@myorg/backend"


def test_create_team_resolver_no_matching_team() -> None:
    mock_team = MagicMock()
    mock_team.slug = "backend"
    mock_team.get_members.return_value = [MagicMock(login="alice")]
    mock_org = MagicMock()
    mock_org.get_teams.return_value = [mock_team]
    mock_client = MagicMock()
    mock_client.get_organization.return_value = mock_org

    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolver = create_team_resolver("ghp_test", "myorg")
    assert resolver is not None
    assert resolver(("@alice", "@carol")) is None


def test_create_team_resolver_prefers_subteam() -> None:
    parent = MagicMock()
    parent.slug = "platform"
    parent.get_members.return_value = [
        MagicMock(login="alice"),
        MagicMock(login="bob"),
        MagicMock(login="carol"),
    ]
    child = MagicMock()
    child.slug = "platform/backend"
    child.get_members.return_value = [
        MagicMock(login="alice"),
        MagicMock(login="bob"),
    ]
    mock_org = MagicMock()
    mock_org.get_teams.return_value = [parent, child]
    mock_client = MagicMock()
    mock_client.get_organization.return_value = mock_org

    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolver = create_team_resolver("ghp_test", "myorg")
    assert resolver is not None
    assert resolver(("@alice", "@bob")) == "@myorg/platform/backend"


def test_create_team_resolver_no_token() -> None:
    assert create_team_resolver("", "myorg") is None


def test_create_team_resolver_no_org() -> None:
    assert create_team_resolver("ghp_test", "") is None


def test_resolve_noreply_handle_current_form() -> None:
    assert resolve_noreply_handle("12345+octo-cat@users.noreply.github.com") == "@octo-cat"


def test_resolve_noreply_handle_legacy_form() -> None:
    assert resolve_noreply_handle("octocat@users.noreply.github.com") == "@octocat"


def test_resolve_noreply_handle_rejects_other_emails() -> None:
    assert resolve_noreply_handle("alice@example.com") is None
    assert resolve_noreply_handle("x@users.noreply.github.com.evil.com") is None


def test_resolve_handles_noreply_without_token() -> None:
    """Noreply emails resolve locally: no token, no API, no client."""
    result = resolve_handles({"99+alice@users.noreply.github.com"}, "")
    assert result == {"99+alice@users.noreply.github.com": "@alice"}


def test_resolve_handles_uses_disk_cache_before_api() -> None:
    write_handle_cache({"alice@example.com": "@alice"})
    mock_client = MagicMock()
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"alice@example.com"}, "ghp_test")
    assert result == {"alice@example.com": "@alice"}
    mock_client.search_users.assert_not_called()


def test_collection_timestamp_uses_source_date_epoch() -> None:
    with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
        assert collection_timestamp() == datetime.fromtimestamp(1700000000, tz=UTC).isoformat()


def test_collection_timestamp_ignores_invalid_source_date_epoch() -> None:
    with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "not-a-timestamp"}):
        stamp = collection_timestamp()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None


def test_external_evidence_after_api_calls() -> None:
    clear_api_evidence()
    try:
        assert external_evidence_payload("abc") == {}
        with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            note_api_call()
        payload = external_evidence_payload("abc")
        assert payload == {
            "github_evidence_collected_at": datetime.fromtimestamp(1700000000, tz=UTC).isoformat(),
            "repository_head": "abc",
        }
        note_api_call({"backend": ["bob", "alice"]})
        with_team = external_evidence_payload("abc")
        assert with_team["github_evidence_collected_at"] == payload["github_evidence_collected_at"]
        assert with_team["team_snapshot"] == team_snapshot_hash({"backend": ["alice", "bob"]})
        note_api_call()
        kept = external_evidence_payload("def")
        assert kept["team_snapshot"] == with_team["team_snapshot"]
        assert kept["repository_head"] == "def"
    finally:
        clear_api_evidence()


def test_team_fetch_failure_records_api_evidence_without_snapshot() -> None:
    clear_api_evidence()
    try:
        mock_client = MagicMock()
        mock_client.get_organization.side_effect = RuntimeError("teams unavailable")
        with patch("checkowners.github.get_github_client", return_value=mock_client):
            assert create_team_resolver("ghp_test", "myorg") is None
        payload = external_evidence_payload("head")
        assert payload["repository_head"] == "head"
        assert "github_evidence_collected_at" in payload
        assert "team_snapshot" not in payload
    finally:
        clear_api_evidence()


def test_resolve_handles_remembers_misses() -> None:
    mock_client = MagicMock()
    mock_client.search_users.return_value = []
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolve_handles({"ghost@example.com"}, "ghp_test")
    assert read_handle_cache()["ghost@example.com"] == ""
    # Second run: the remembered miss short-circuits the API.
    mock_client.search_users.reset_mock()
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"ghost@example.com"}, "ghp_test")
    assert result == {}
    mock_client.search_users.assert_not_called()

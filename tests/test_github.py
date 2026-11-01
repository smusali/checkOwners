"""GitHub payload parsing, driven through the one faked HTTP seam."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from checkowners.github import (
    api_status,
    clear_api_evidence,
    code_owner_review_required,
    collection_timestamp,
    create_team_resolver,
    external_evidence_payload,
    fetch_codeowners_errors,
    get_github_token,
    github_repository,
    load_team_directory,
    organization_members,
    owner_access,
    recent_pulls,
    repository_visibility,
    resolve_handle_details,
    resolve_handles,
    resolve_noreply_handle,
    set_offline,
)
from checkowners.providers import begin_api_budget, clear_api_budget
from checkowners.state import HandleCacheEntry, write_handle_cache
from tests.conftest import GitRepo, Route, fake_api

_TOKEN = "ghp_token"
_ORG = "acme"
_REPO = "acme/app"


def _user(login: str, account_type: str = "User") -> dict[str, object]:
    return {"login": login, "type": account_type}


def _rate(bucket: str, remaining: int) -> Route:
    return Route("GET", "/rate_limit", body={"resources": {bucket: {"remaining": remaining}}})


@pytest.fixture(autouse=True)
def _reset_api_state() -> None:
    clear_api_evidence()
    clear_api_budget()
    set_offline(False)


def test_token_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
    assert get_github_token() == _TOKEN
    monkeypatch.delenv("GITHUB_TOKEN")
    assert get_github_token() == ""


@pytest.mark.parametrize(
    ("email", "handle"),
    [
        ("12345+ada@users.noreply.github.com", "@ada"),
        ("ada@users.noreply.github.com", "@ada"),
        ("ada@example.com", None),
    ],
)
def test_noreply_addresses_resolve_locally(email: str, handle: str | None) -> None:
    assert resolve_noreply_handle(email) == handle


def test_an_override_wins_over_every_lookup() -> None:
    with fake_api() as api:
        resolved = resolve_handles(
            {"ada@example.com"}, _TOKEN, overrides={"ada@example.com": "@ada"}
        )
    assert resolved == {"ada@example.com": "@ada"}
    assert api.calls == []


def test_a_commit_lookup_names_the_author_and_account_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", _REPO)
    commit = {"author": _user("ada", "Bot"), "committer": _user("eve", "User")}
    with fake_api(_rate("core", 100), Route("GET", "/repos/acme/app/commits/", body=commit)):
        details = resolve_handle_details(
            {"ada@example.com"},
            _TOKEN,
            repo_root=tmp_path,
            author_shas={"ada@example.com": "abc123"},
        )
    hit = details["ada@example.com"]
    assert hit.handle == "@ada"
    assert hit.source == "commit"
    assert hit.account_type == "Bot"
    assert hit.committer_type == "User"


def test_a_failed_commit_lookup_does_not_fall_through_to_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", _REPO)
    routes = (
        _rate("core", 100),
        Route("GET", "/repos/acme/app/commits/", status=404),
        Route("GET", "/search/users", body={"items": [_user("ada")]}),
    )
    with fake_api(*routes) as api:
        resolved = resolve_handles(
            {"ada@example.com"},
            _TOKEN,
            repo_root=tmp_path,
            author_shas={"ada@example.com": "abc123"},
        )
    assert resolved == {}
    assert not [path for path in api.paths() if path.startswith("/search/users")]


def test_a_commit_without_a_login_falls_through_to_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", _REPO)
    routes = (
        _rate("core", 100),
        _rate("search", 100),
        Route("GET", "/repos/acme/app/commits/", body={"author": None, "committer": None}),
        Route("GET", "/search/users", body={"items": [_user("ada")]}),
    )
    with fake_api(*routes):
        resolved = resolve_handles(
            {"ada@example.com"},
            _TOKEN,
            repo_root=tmp_path,
            author_shas={"ada@example.com": "abc123"},
        )
    assert resolved == {"ada@example.com": "@ada"}


@pytest.mark.parametrize(
    "body",
    [
        {"resources": {"search": {"remaining": 1}}},
        {"resources": {}},
        {"resources": {"search": {}}},
        {},
    ],
    ids=["one-left", "no-bucket", "no-count", "no-resources"],
)
def test_search_runs_unless_the_quota_says_it_is_spent(body: dict[str, object]) -> None:
    routes = (
        Route("GET", "/rate_limit", body=body),
        Route("GET", "/search/users", body={"items": [_user("ada")]}),
    )
    with fake_api(*routes):
        assert resolve_handles({"ada@example.com"}, _TOKEN) == {"ada@example.com": "@ada"}


def test_an_exhausted_search_quota_skips_the_lookup() -> None:
    with fake_api(_rate("search", 0)) as api:
        assert resolve_handles({"ada@example.com"}, _TOKEN) == {}
    assert not [path for path in api.paths() if path.startswith("/search/users")]


def test_a_search_miss_is_remembered_for_the_configured_days(tmp_path: Path) -> None:
    routes = (_rate("search", 10), Route("GET", "/search/users", body={"items": []}))
    with fake_api(*routes):
        assert resolve_handles({"ada@example.com"}, _TOKEN, repo_root=tmp_path, ttl=7) == {}
    with fake_api(*routes) as again:
        assert resolve_handles({"ada@example.com"}, _TOKEN, repo_root=tmp_path, ttl=7) == {}
    assert again.calls == []


def test_a_cached_handle_is_used_before_the_api(tmp_path: Path) -> None:
    write_handle_cache(tmp_path, {"ada@example.com": HandleCacheEntry(handle="@ada")})
    with fake_api() as api:
        assert resolve_handles({"ada@example.com"}, _TOKEN, repo_root=tmp_path) == {
            "ada@example.com": "@ada"
        }
    assert api.calls == []


def test_an_offline_run_resolves_nothing_over_the_api() -> None:
    set_offline(True)
    with fake_api() as api:
        assert resolve_handles({"ada@example.com"}, _TOKEN) == {}
    assert api.calls == []


def test_a_spent_request_budget_stops_the_lookup() -> None:
    begin_api_budget(0)
    with fake_api(_rate("search", 10)) as api:
        assert resolve_handles({"ada@example.com"}, _TOKEN) == {}
    assert api.calls == []


def test_organization_members_pages_until_the_list_ends() -> None:
    first = [_user(f"user{index}") for index in range(100)]
    routes = (
        Route("GET", "/orgs/acme/members?per_page=100&page=2", body=[_user("ada")]),
        Route("GET", "/orgs/acme/members", body=first),
    )
    with fake_api(*routes) as api:
        members = organization_members(_TOKEN, _ORG)
    assert members is not None
    assert "ada" in members
    assert len(members) == 101
    assert len(api.calls) == 2


@pytest.mark.parametrize(
    ("org", "offline", "status"),
    [("", False, 200), (_ORG, True, 200), (_ORG, False, 500)],
    ids=["no-org", "offline", "api-error"],
)
def test_organization_members_is_none_when_unavailable(
    org: str, offline: bool, status: int
) -> None:
    set_offline(offline)
    with fake_api(Route("GET", "/orgs/", status=status)):
        assert organization_members(_TOKEN, org) is None


def _team_routes(teams: dict[str, list[str]]) -> tuple[Route, ...]:
    listed = [{"slug": slug, "parent": None} for slug in teams]
    routes = [Route("GET", "/orgs/acme/teams?", body=listed)]
    routes.extend(
        Route("GET", f"/orgs/acme/teams/{slug}/members", body=[_user(login) for login in logins])
        for slug, logins in teams.items()
    )
    return tuple(routes)


@pytest.mark.parametrize(
    ("teams", "owners", "expected"),
    [
        ({"platform": ["ada", "eve"]}, ("@ada", "@eve"), "@acme/platform"),
        ({"platform": ["ada", "eve"]}, ("@ada", "@zoe"), None),
        (
            {"platform": ["ada", "eve", "zoe"], "core": ["ada", "eve"]},
            ("@ada", "@eve"),
            "@acme/core",
        ),
    ],
    ids=["all-in-one", "no-match", "prefers-the-narrower-team"],
)
def test_the_team_resolver_names_the_narrowest_matching_team(
    teams: dict[str, list[str]],
    owners: tuple[str, ...],
    expected: str | None,
) -> None:
    with fake_api(*_team_routes(teams)):
        resolve = create_team_resolver(_TOKEN, _ORG)
    assert resolve is not None
    assert resolve(owners) == expected


@pytest.mark.parametrize(("token", "org"), [("", _ORG), (_TOKEN, "")], ids=["no-token", "no-org"])
def test_the_team_resolver_needs_a_token_and_an_org(token: str, org: str) -> None:
    with fake_api():
        assert create_team_resolver(token, org) is None


def test_the_team_directory_links_children_and_hides_refused_members(tmp_path: Path) -> None:
    listed = [
        {"slug": "platform", "parent": None},
        {"slug": "core", "parent": {"slug": "platform"}},
        {"slug": "secret", "parent": None},
    ]
    routes = (
        Route("GET", "/orgs/acme/teams?", body=listed),
        Route("GET", "/orgs/acme/teams/platform/members", body=[_user("ada")]),
        Route("GET", "/orgs/acme/teams/core/members", body=[_user("eve")]),
        Route("GET", "/orgs/acme/teams/secret/members", status=403),
    )
    os.environ["CHECKOWNERS_STATE_DIR"] = str(tmp_path)
    with fake_api(*routes):
        directory = load_team_directory(_TOKEN, _ORG)
    assert directory is not None
    by_slug = {team.slug: team for team in directory.teams}
    assert by_slug["platform"].children == ("core",)
    assert by_slug["core"].members == frozenset({"eve"})
    assert by_slug["secret"].visible is False
    assert by_slug["secret"].members == frozenset()


def test_the_team_directory_is_none_offline_and_on_failure() -> None:
    set_offline(True)
    with fake_api():
        assert load_team_directory(_TOKEN, _ORG) is None
    set_offline(False)
    with fake_api(Route("GET", "/orgs/acme/teams", status=500)):
        assert load_team_directory(_TOKEN, _ORG) is None


def test_a_failed_team_fetch_records_api_evidence_without_a_snapshot() -> None:
    with fake_api(Route("GET", "/orgs/acme/teams", status=500)):
        assert load_team_directory(_TOKEN, _ORG) is None
    payload = external_evidence_payload("deadbeef")
    assert payload["github_evidence_collected_at"]
    assert "team_snapshot" not in payload


def test_api_evidence_records_a_team_snapshot_on_success(tmp_path: Path) -> None:
    os.environ["CHECKOWNERS_STATE_DIR"] = str(tmp_path)
    with fake_api(*_team_routes({"platform": ["ada"]})):
        assert load_team_directory(_TOKEN, _ORG) is not None
    assert external_evidence_payload("deadbeef")["team_snapshot"]


def test_recent_pulls_parses_the_fields_the_load_scan_needs() -> None:
    pulls = [
        {
            "number": 7,
            "created_at": "2026-01-01T00:00:00Z",
            "additions": 10,
            "deletions": 5,
            "requested_reviewers": [_user("eve")],
        },
        {"number": 8, "created_at": "nope", "requested_reviewers": []},
    ]
    with fake_api(Route("GET", "/repos/acme/app/pulls", body=pulls)):
        parsed = recent_pulls(_TOKEN, _REPO, limit=10)
    assert parsed is not None
    assert parsed[0].number == 7
    assert parsed[0].changed_lines == 15
    assert parsed[0].created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert parsed[0].requested_reviewers == ("@eve",)
    assert parsed[1].changed_lines is None
    assert parsed[1].created_at is None


@pytest.mark.parametrize("repo", ["", "not-a-slug"])
def test_recent_pulls_rejects_a_repository_that_is_not_a_slug(repo: str) -> None:
    with fake_api():
        assert recent_pulls(_TOKEN, repo) is None


def test_codeowners_errors_parse_and_reject_malformed_rows() -> None:
    good = {
        "errors": [
            {
                "line": 2,
                "column": 1,
                "kind": "Invalid owner",
                "message": "no",
                "source": "* @ghost",
                "path": "CODEOWNERS",
            }
        ]
    }
    with fake_api(Route("GET", "/repos/acme/app/codeowners/errors", body=good)):
        errors = fetch_codeowners_errors(_TOKEN, _REPO)
    assert errors is not None
    assert errors[0].line == 2
    assert errors[0].kind == "Invalid owner"
    for body in ({"errors": "nope"}, {"errors": [{"line": "two"}]}, {}):
        with fake_api(Route("GET", "/repos/acme/app/codeowners/errors", body=body)):
            assert fetch_codeowners_errors(_TOKEN, _REPO) is None


def test_repository_visibility_needs_a_default_branch() -> None:
    with fake_api(
        Route("GET", "/repos/acme/app/", body={"private": True, "default_branch": "main"})
    ):
        assert repository_visibility(_TOKEN, _REPO) == (True, "main")
    with fake_api(Route("GET", "/repos/acme/app/", body={"private": True})):
        assert repository_visibility(_TOKEN, _REPO) is None


@pytest.mark.parametrize(
    ("permission", "expected"),
    [("admin", "write"), ("write", "write"), ("read", "read")],
)
def test_owner_access_maps_the_reported_permission(permission: str, expected: str) -> None:
    route = Route("GET", "/repos/acme/app/collaborators/", body={"permission": permission})
    with fake_api(route):
        assert owner_access(_TOKEN, _REPO, "@ada") == expected


def test_owner_access_is_unknown_without_a_token_or_a_slug() -> None:
    with fake_api():
        assert owner_access("", _REPO, "@ada") == "unknown"
        assert owner_access(_TOKEN, "nope", "@ada") == "unknown"
        assert owner_access(_TOKEN, _REPO, "") == "unknown"


def test_code_owner_review_is_required_when_a_ruleset_says_so() -> None:
    rulesets = [{"id": 1, "target": "branch"}]
    detail = {
        "rules": [{"type": "code_owner_review"}],
    }
    routes = (
        Route("GET", "/repos/acme/app/rulesets/1", body=detail),
        Route("GET", "/repos/acme/app/rulesets", body=rulesets),
        Route("GET", "/repos/acme/app/branches/main/protection", status=404),
    )
    with fake_api(*routes):
        assert code_owner_review_required(_TOKEN, _REPO, "main") == "required"


def test_code_owner_review_is_unknown_offline() -> None:
    set_offline(True)
    with fake_api():
        assert code_owner_review_required(_TOKEN, _REPO, "main") == "unknown"


def test_api_status_reports_the_scopes_header() -> None:
    headers = {"x-oauth-scopes": "repo, read:org"}
    with fake_api(Route("GET", "/user", body={"login": "ada"}, headers=headers)):
        assert api_status(_TOKEN) == (True, ("repo", "read:org"))
    with fake_api(Route("GET", "/user", status=0, reason="refused")):
        assert api_status(_TOKEN) == (False, ())
    set_offline(True)
    with fake_api():
        assert api_status(_TOKEN) == (False, ())


@pytest.mark.parametrize(
    ("env", "origin", "expected"),
    [
        ("acme/app", "", "acme/app"),
        ("", "https://github.com/acme/app.git", "acme/app"),
        ("not a slug", "https://github.com/acme/app.git", "acme/app"),
    ],
    ids=["from-env", "from-origin", "env-is-unusable"],
)
def test_github_repository_prefers_the_environment_then_origin(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
    origin: str,
    expected: str,
) -> None:
    git_repo.commit_files({"a.py": "x = 1\n"}, "init")
    if origin:
        git_repo.set_origin(origin)
    monkeypatch.setenv("GITHUB_REPOSITORY", env)
    assert github_repository(git_repo.path) == expected


def test_collection_timestamp_uses_a_valid_source_date_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert collection_timestamp().startswith("2023-11-14")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "later")
    assert collection_timestamp()

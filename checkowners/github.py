"""GitHub API integration for owner resolution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from checkowners.models import (
    API_BUDGET_REASON,
    API_RATE_LIMIT_REASON,
    SEARCH_RATE_LIMIT_REASON,
    AnalysisGap,
    GapCode,
    review_window_truncated_reason,
)
from checkowners.privacy import email_token, is_email
from checkowners.state import read_handle_cache, repository_identity, write_handle_cache

if TYPE_CHECKING:
    from collections.abc import Iterator

    from github import Github
    from github.PullRequest import PullRequest

logger = logging.getLogger(__name__)

RECONCILE_BRANCH = "checkowners/reconcile-codeowners"
RECONCILE_TITLE = "chore: reconcile CODEOWNERS ownership drift"
RECONCILE_MARKER = "<!-- checkowners-reconcile -->"
_API_VERSION = "2022-11-28"
_RECONCILE_DENIED = (
    "The token cannot open pull requests. Grant contents: write and pull-requests: write."
)
_RECONCILE_FAILED = "Could not open a pull request. Grant contents: write and pull-requests: write."


@dataclass(frozen=True)
class _ApiEvidence:
    collected_at: str
    team_snapshot: str = ""


_API_EVIDENCE: ContextVar[_ApiEvidence | None] = ContextVar(
    "checkowners_api_evidence",
    default=None,
)
_OFFLINE: ContextVar[bool] = ContextVar("checkowners_offline", default=False)


def set_offline(enabled: bool) -> None:
    """Record whether this process may open a network connection."""
    _OFFLINE.set(enabled)


def offline_enabled() -> bool:
    """Return whether network access is disabled for this context."""
    return _OFFLINE.get()


def collection_timestamp() -> str:
    """Return `SOURCE_DATE_EPOCH` as UTC ISO-8601, or the current UTC time."""
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if raw:
        try:
            seconds = int(raw)
        except ValueError:
            seconds = None
        if seconds is not None:
            return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    return datetime.now(tz=UTC).isoformat()


def team_snapshot_hash(teams: Mapping[str, Iterable[str]]) -> str:
    """Return a stable sha256 of `teams` (sorted slugs and members)."""
    canonical = {team: sorted(members) for team, members in sorted(teams.items())}
    raw = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def note_api_call(teams: Mapping[str, Iterable[str]] | None = None) -> None:
    """Record that a GitHub API call ran, keeping the first collection time."""
    snapshot = team_snapshot_hash(teams) if teams is not None else ""
    current = _API_EVIDENCE.get()
    if current is None:
        _API_EVIDENCE.set(_ApiEvidence(collected_at=collection_timestamp(), team_snapshot=snapshot))
        return
    _API_EVIDENCE.set(
        _ApiEvidence(
            collected_at=current.collected_at,
            team_snapshot=snapshot or current.team_snapshot,
        )
    )


@dataclass
class _ApiBudget:
    limit: int
    used: int = 0
    review_omitted: bool = False
    gaps: dict[GapCode, AnalysisGap] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewWindow:
    sampled: int = 0
    window_days: int = 0


_REVIEW_WINDOW: ContextVar[ReviewWindow | None] = ContextVar(
    "checkowners_review_window",
    default=None,
)


def reset_review_window() -> None:
    _REVIEW_WINDOW.set(None)


def review_window() -> ReviewWindow:
    current = _REVIEW_WINDOW.get()
    return ReviewWindow() if current is None else current


def _record_review_window(sampled: int, window_days: int) -> None:
    _REVIEW_WINDOW.set(ReviewWindow(sampled=sampled, window_days=window_days))


_API_BUDGET: ContextVar[_ApiBudget | None] = ContextVar("checkowners_api_budget", default=None)


def begin_api_budget(limit: int) -> None:
    """Start a request budget of `limit` calls for the current context."""
    _API_BUDGET.set(_ApiBudget(limit=limit))


def clear_api_budget() -> None:
    """Drop the request budget and any gaps recorded for the current context."""
    _API_BUDGET.set(None)


def _budget() -> _ApiBudget:
    current = _API_BUDGET.get()
    if current is None:
        current = _ApiBudget(limit=2**31 - 1)
        _API_BUDGET.set(current)
    return current


def take_api_request() -> bool:
    """Consume one request. Return false when the budget is already spent."""
    budget = _API_BUDGET.get()
    if budget is None:
        return True
    if budget.used >= budget.limit:
        budget.gaps.setdefault("api_budget", AnalysisGap("api_budget", API_BUDGET_REASON))
        return False
    budget.used += 1
    return True


def note_collection_gap(gap: AnalysisGap, *, replace: bool = False) -> None:
    """Record `gap`. A later call replaces the reason only when `replace` is true."""
    budget = _budget()
    if replace or gap.code not in budget.gaps:
        budget.gaps[gap.code] = gap


def mark_review_omitted() -> None:
    """Mark review collection as skipped so scores do not treat it as a measured zero."""
    _budget().review_omitted = True


def review_was_omitted() -> bool:
    """Return whether review collection was skipped for this context."""
    budget = _API_BUDGET.get()
    return budget is not None and budget.review_omitted


def collection_gaps() -> tuple[AnalysisGap, ...]:
    """Return API gaps recorded for the current context, in catalog order."""
    budget = _API_BUDGET.get()
    if budget is None:
        return ()
    return tuple(budget.gaps.values())


def clear_api_evidence() -> None:
    """Drop recorded API evidence for the current context."""
    _API_EVIDENCE.set(None)
    clear_api_budget()
    reset_review_window()


def external_evidence_payload(repository_head: str) -> dict[str, str]:
    """Return external-evidence fields when an API call ran, otherwise `{}`."""
    record = _API_EVIDENCE.get()
    if record is None:
        return {}
    payload = {
        "github_evidence_collected_at": record.collected_at,
        "repository_head": repository_head,
    }
    if record.team_snapshot:
        payload["team_snapshot"] = record.team_snapshot
    return payload


#: GitHub noreply commit emails embed the login: "12345+login@users.noreply.
#: github.com" (current form) or "login@users.noreply.github.com" (legacy).
#: These resolve to @handles locally, with zero API calls.
_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?([a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38})@users\.noreply\.github\.com$",
    re.IGNORECASE,
)


def get_github_token() -> str:
    """Read GITHUB_TOKEN from environment.

    The token is intentionally never read from checkowners.yml: that file is
    typically committed to git, so a token there would leak the secret.
    """
    return os.environ.get("GITHUB_TOKEN", "")


def get_github_client(token: str) -> Github | None:
    """Create a PyGithub client, or None if offline or the token is empty.

    PyGithub ships in the optional ``github`` extra; when it is not installed
    the API-backed features degrade gracefully instead of crashing.
    """
    if offline_enabled() or not token:
        return None
    try:
        from github import Github as GithubClient  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "PyGithub is not installed; GitHub API features are disabled. "
            'Install with: pip install "checkowners[github]"'
        )
        return None

    return GithubClient(token)


def resolve_noreply_handle(email: str) -> str | None:
    """Extract the @handle from a GitHub noreply email, or None."""
    match = _NOREPLY_RE.match(email.strip())
    if match is None:
        return None
    return f"@{match.group(1)}"


def resolve_handles(
    emails: set[str],
    token: str,
) -> dict[str, str]:
    """Map git commit emails to GitHub @handles.

    Incoming emails are already mailmap-canonical when that mapping ran.
    Resolution order: noreply-email parsing (local, free), then the on-disk
    cache (which also remembers misses as empty strings so unresolvable
    emails are not re-queried), then the rate-limited user-search API.
    """
    resolved: dict[str, str] = {}
    remaining: list[str] = []
    for email in sorted(emails):
        noreply = resolve_noreply_handle(email)
        if noreply is not None:
            resolved[email] = noreply
        else:
            remaining.append(email)
    if not remaining:
        return resolved

    cached = read_handle_cache()
    api_queue: list[str] = []
    for email in remaining:
        hit = _cached_handle(cached, email)
        if hit:
            resolved[email] = hit
        elif hit is None:
            api_queue.append(email)
        # hit == "": remembered miss; skip the API.
    if not api_queue:
        return resolved

    client = get_github_client(token)
    if client is None:
        return resolved
    if not _search_budget_allows(client):
        return resolved
    fresh: dict[str, str] = {}
    for email in api_queue:
        if not take_api_request():
            break
        handle = _lookup_handle(client, email)
        fresh[email] = handle if handle is not None else ""
        if handle is not None:
            resolved[email] = handle
    write_handle_cache(fresh)
    return resolved


def _cached_handle(cache: dict[str, str], email: str) -> str | None:
    token = email_token(email) if is_email(email) else email
    hit = cache.get(token)
    if hit is None and token != email:
        return cache.get(email)
    return hit


def _rate_remaining(rate: object, bucket: str) -> int | None:
    section = getattr(rate, bucket, None)
    remaining = getattr(section, "remaining", None)
    if isinstance(remaining, bool) or not isinstance(remaining, int):
        return None
    return remaining


def _search_budget_allows(client: Github) -> bool:
    if not take_api_request():
        return False
    try:
        rate = client.get_rate_limit()
    except Exception:
        logger.warning("Failed to read the GitHub search rate limit")
        return True
    remaining = _rate_remaining(rate, "search")
    if remaining is None or remaining >= 1:
        return True
    note_collection_gap(AnalysisGap("api_rate_limit", SEARCH_RATE_LIMIT_REASON))
    return False


def _lookup_handle(client: Github, email: str) -> str | None:
    """Look up a single email via GitHub user search API."""
    note_api_call()
    try:
        users = client.search_users(f"{email} in:email")
        for user in users:
            if user.login:
                return f"@{user.login}"
        return None
    except Exception:
        logger.warning("Failed to resolve GitHub handle for %s", email)
        return None


def build_review_coverage(
    token: str,
    repo_full_name: str,
    emails: set[str],
    *,
    as_of: datetime,
    since: datetime | None,
) -> dict[str, dict[str, float]]:
    """Return `path -> {email: fraction}` for pulls closed from `since` through `as_of`.

    `since` None keeps reading until the pull list ends. Each fraction is one
    login's share of included pulls that touch the path. Returns an empty
    mapping when the API is unavailable or nothing maps to `emails`.
    """
    reset_review_window()
    client = get_github_client(token)
    if client is None or not repo_full_name or not emails:
        return {}
    email_to_handle = resolve_handles(emails, token)
    login_to_email = {handle.lstrip("@"): email for email, handle in email_to_handle.items()}
    if not login_to_email:
        return {}
    if not _is_slug(repo_full_name):
        return {}
    owner, name = _split_repository(repo_full_name)
    scan = _scan_review_pulls(token, owner, name, since=since, as_of=as_of)
    return _coverage_from_scan(
        scan,
        login_to_email,
        repo_full_name,
        as_of=as_of,
        since=since,
    )


#: Cap for the suggest closed-pull scan. Ownership review uses a time window.
REVIEW_SCAN_PR_LIMIT = 200


def iter_recent_closed_pulls(
    client: Github,
    repo_full_name: str,
    limit: int = REVIEW_SCAN_PR_LIMIT,
    *,
    state: str = "closed",
) -> Iterator[PullRequest]:
    """Yield at most `limit` pull requests for `repo_full_name` in `state`."""
    repo = client.get_repo(repo_full_name)
    pulls = repo.get_pulls(state=state, sort="updated", direction="desc")
    return islice(pulls, limit)


_REVIEW_PAGE_SIZE = 20
_REVIEW_NESTED_PAGE_SIZE = 100
_REVIEW_PAGE_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: [CLOSED, MERGED]
      orderBy: {field: UPDATED_AT, direction: DESC}
      first: PAGE_SIZE
      after: $cursor
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        updatedAt
        closedAt
        reviews(first: NESTED_SIZE) {
          pageInfo { hasNextPage endCursor }
          nodes { author { login } }
        }
        files(first: NESTED_SIZE) {
          pageInfo { hasNextPage endCursor }
          nodes { path }
        }
      }
    }
  }
}
""".replace("PAGE_SIZE", str(_REVIEW_PAGE_SIZE)).replace(
    "NESTED_SIZE", str(_REVIEW_NESTED_PAGE_SIZE)
)


@dataclass(frozen=True)
class _GraphqlResult:
    data: dict[str, object] | None
    rate_limited: bool


@dataclass
class _PullBuild:
    number: int
    closed_at: datetime
    reviewers: set[str]
    paths: set[str]
    review_cursor: str | None
    file_cursor: str | None


@dataclass(frozen=True)
class _ParsedPage:
    pulls: tuple[_PullBuild, ...]
    stop: bool
    has_next: bool
    cursor: str | None


@dataclass(frozen=True)
class _ClosedPull:
    closed_at: datetime
    reviewers: frozenset[str]
    paths: tuple[str, ...]


@dataclass(frozen=True)
class _ReviewScan:
    pulls: tuple[_ClosedPull, ...]
    finished: bool
    rate_limited: bool
    budget_stopped: bool


def _coverage_from_scan(
    scan: _ReviewScan,
    login_to_email: dict[str, str],
    repo_full_name: str,
    *,
    as_of: datetime,
    since: datetime | None,
) -> dict[str, dict[str, float]]:
    if not scan.finished and not scan.pulls and (scan.budget_stopped or scan.rate_limited):
        mark_review_omitted()
        if scan.rate_limited:
            note_collection_gap(AnalysisGap("api_rate_limit", API_RATE_LIMIT_REASON), replace=True)
        return {}
    if not scan.finished and not scan.pulls:
        logger.warning("Failed to gather review coverage for %s", repo_full_name)
        return {}
    oldest = min((pull.closed_at for pull in scan.pulls), default=None)
    days = _span_days(as_of, oldest)
    _record_review_window(len(scan.pulls), days)
    if not scan.finished:
        code: GapCode = "api_rate_limit" if scan.rate_limited else "api_budget"
        note_collection_gap(
            AnalysisGap(
                code,
                review_window_truncated_reason(
                    days, None if since is None else _span_days(as_of, since)
                ),
            ),
            replace=True,
        )
    return _coverage_fractions(scan.pulls, login_to_email)


def _coverage_fractions(
    pulls: tuple[_ClosedPull, ...],
    login_to_email: dict[str, str],
) -> dict[str, dict[str, float]]:
    raw: dict[str, dict[str, int]] = {}
    for pull in pulls:
        if not pull.reviewers:
            continue
        for path in pull.paths:
            per_path = raw.setdefault(path, {})
            for login in pull.reviewers:
                per_path[login] = per_path.get(login, 0) + 1
    coverage: dict[str, dict[str, float]] = {}
    for path, counts in raw.items():
        total = sum(counts.values())
        mapped = {
            login_to_email[login]: count / total
            for login, count in counts.items()
            if login in login_to_email
        }
        if mapped:
            coverage[path] = mapped
    return coverage


def _scan_review_pulls(
    token: str,
    owner: str,
    name: str,
    *,
    since: datetime | None,
    as_of: datetime,
) -> _ReviewScan:
    pulls: list[_ClosedPull] = []
    cursor: str | None = None
    seen: set[str] = set()
    finished = False
    rate_limited = False
    budget_stopped = False
    while True:
        if not take_api_request():
            budget_stopped = True
            break
        result = _graphql(
            token, _REVIEW_PAGE_QUERY, {"owner": owner, "name": name, "cursor": cursor}
        )
        if result.rate_limited:
            rate_limited = True
        if result.data is None:
            break
        parsed = _parse_pull_page(result.data, since, as_of)
        if parsed is None:
            break
        nested_truncated, nested_limited = _complete_pulls(token, owner, name, parsed.pulls)
        if nested_limited:
            rate_limited = True
        pulls.extend(
            _ClosedPull(pull.closed_at, frozenset(pull.reviewers), tuple(sorted(pull.paths)))
            for pull in parsed.pulls
        )
        if parsed.stop or not parsed.has_next:
            finished = True
            break
        if nested_truncated or result.rate_limited or nested_limited:
            break
        next_cursor = parsed.cursor
        if next_cursor is None or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    return _ReviewScan(tuple(pulls), finished, rate_limited, budget_stopped)


def _complete_pulls(
    token: str,
    owner: str,
    name: str,
    pulls: tuple[_PullBuild, ...],
) -> tuple[bool, bool]:
    truncated = False
    rate_limited = False
    pending = [pull for pull in pulls if pull.review_cursor or pull.file_cursor]
    seen: set[tuple[int, str, str]] = set()
    while pending:
        if not take_api_request():
            return True, rate_limited
        fresh: list[_PullBuild] = []
        for pull in pending:
            key = (pull.number, pull.review_cursor or "", pull.file_cursor or "")
            if key in seen:
                pull.review_cursor = None
                pull.file_cursor = None
                truncated = True
                continue
            seen.add(key)
            fresh.append(pull)
        pending = fresh
        if not pending:
            break
        query, variables = _follow_query(pending)
        variables["owner"] = owner
        variables["name"] = name
        result = _graphql(token, query, variables)
        if result.rate_limited:
            rate_limited = True
        repo = _mapping(result.data.get("repository")) if result.data is not None else None
        if repo is None:
            return True, rate_limited
        next_pending: list[_PullBuild] = []
        for index, pull in enumerate(pending):
            node = _mapping(repo.get(f"p{index}"))
            if node is None or not _absorb_pull(pull, node):
                pull.review_cursor = None
                pull.file_cursor = None
                truncated = True
                continue
            if pull.review_cursor or pull.file_cursor:
                next_pending.append(pull)
        pending = next_pending
        if rate_limited and pending:
            return True, True
    return truncated, rate_limited


def _follow_query(pending: list[_PullBuild]) -> tuple[str, dict[str, object]]:
    var_defs = ["$owner: String!", "$name: String!"]
    variables: dict[str, object] = {}
    fields: list[str] = []
    nested = _REVIEW_NESTED_PAGE_SIZE
    for index, pull in enumerate(pending):
        parts: list[str] = []
        if pull.review_cursor is not None:
            var_defs.append(f"$r{index}: String")
            variables[f"r{index}"] = pull.review_cursor
            parts.append(
                f"reviews(first: {nested}, after: $r{index}) "
                "{ pageInfo { hasNextPage endCursor } nodes { author { login } } }"
            )
        if pull.file_cursor is not None:
            var_defs.append(f"$f{index}: String")
            variables[f"f{index}"] = pull.file_cursor
            parts.append(
                f"files(first: {nested}, after: $f{index}) "
                "{ pageInfo { hasNextPage endCursor } nodes { path } }"
            )
        fields.append(f"p{index}: pullRequest(number: {pull.number}) {{ {' '.join(parts)} }}")
    header = ", ".join(var_defs)
    body = " ".join(fields)
    return f"query({header}) {{ repository(owner: $owner, name: $name) {{ {body} }} }}", variables


def _absorb_pull(pull: _PullBuild, node: dict[str, object]) -> bool:
    applied = True
    if pull.review_cursor is not None:
        reviews = _mapping(node.get("reviews"))
        if reviews is None:
            applied = False
            pull.review_cursor = None
        else:
            pull.reviewers.update(_logins(_object_list(reviews.get("nodes"))))
            pull.review_cursor = _next_cursor(reviews)
    if pull.file_cursor is not None:
        files = _mapping(node.get("files"))
        if files is None:
            applied = False
            pull.file_cursor = None
        else:
            pull.paths.update(_paths(_object_list(files.get("nodes"))))
            pull.file_cursor = _next_cursor(files)
    return applied


def _parse_pull_page(
    data: dict[str, object],
    since: datetime | None,
    as_of: datetime,
) -> _ParsedPage | None:
    repo = _mapping(data.get("repository"))
    connection = _mapping(repo.get("pullRequests")) if repo is not None else None
    if connection is None:
        return None
    page = _mapping(connection.get("pageInfo"))
    has_next = page is not None and page.get("hasNextPage") is True
    end = page.get("endCursor") if page is not None else None
    cursor = end if isinstance(end, str) and end else None
    included: list[_PullBuild] = []
    stop = False
    for node in _object_list(connection.get("nodes")):
        updated = _parse_github_time(node.get("updatedAt"))
        if updated is None:
            continue
        if since is not None and updated < _as_utc(since):
            stop = True
            continue
        closed = _parse_github_time(node.get("closedAt"))
        closed_at = closed if closed is not None else updated
        if closed_at > _as_utc(as_of):
            continue
        if since is not None and closed_at < _as_utc(since):
            continue
        number = node.get("number")
        pull_number = number if isinstance(number, int) and not isinstance(number, bool) else 0
        reviews = _mapping(node.get("reviews"))
        files = _mapping(node.get("files"))
        review_cursor = _next_cursor(reviews) if reviews is not None else None
        file_cursor = _next_cursor(files) if files is not None else None
        reviewers = _logins(_object_list(None if reviews is None else reviews.get("nodes")))
        if pull_number <= 0 or (not reviewers and review_cursor is None):
            review_cursor = None
            file_cursor = None
        included.append(
            _PullBuild(
                number=pull_number,
                closed_at=closed_at,
                reviewers=reviewers,
                paths=_paths(_object_list(None if files is None else files.get("nodes"))),
                review_cursor=review_cursor,
                file_cursor=file_cursor,
            )
        )
    return _ParsedPage(tuple(included), stop, has_next, cursor)


def _graphql(token: str, query: str, variables: Mapping[str, object]) -> _GraphqlResult:
    root = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    url = f"{root}/graphql"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _GraphqlResult(None, False)
    body = json.dumps({"query": query, "variables": dict(variables)}).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310  # scheme restricted to http/https above
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "Content-Type": "application/json",
        },
    )
    header: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
            found = resp.headers.get("x-ratelimit-remaining")
            header = found if isinstance(found, str) else None
    except urllib.error.HTTPError as exc:
        exc.read()
        logger.warning("GitHub review query failed: HTTP %s", exc.code)
        note_api_call()
        return _GraphqlResult(None, exc.code in {403, 429})
    except urllib.error.URLError as exc:
        logger.warning("GitHub review query failed: %s", exc.reason)
        return _GraphqlResult(None, False)
    except TimeoutError:
        logger.warning("GitHub review query failed")
        return _GraphqlResult(None, False)
    note_api_call()
    try:
        loaded: object = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        logger.warning("GitHub review query failed")
        return _GraphqlResult(None, False)
    payload = _mapping(loaded)
    if payload is None:
        return _GraphqlResult(None, False)
    limited = _graphql_rate_limited(payload, header)
    data = _mapping(payload.get("data"))
    if data is None:
        if not limited:
            logger.warning("GitHub review query returned no data")
        return _GraphqlResult(None, limited)
    return _GraphqlResult(data, limited)


def _graphql_rate_limited(payload: dict[str, object], remaining: str | None) -> bool:
    if remaining == "0":
        return True
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return False
    for item in errors:
        mapped = _mapping(item)
        if mapped is None:
            continue
        if mapped.get("type") == "RATE_LIMITED":
            return True
        message = mapped.get("message")
        if isinstance(message, str) and "rate limit" in message.casefold():
            return True
    return False


def _object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, object]] = []
    for item in value:
        mapped = _mapping(item)
        if mapped is not None:
            items.append(mapped)
    return items


def _next_cursor(connection: dict[str, object]) -> str | None:
    page = _mapping(connection.get("pageInfo"))
    if page is None or page.get("hasNextPage") is not True:
        return None
    cursor = page.get("endCursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _logins(nodes: list[dict[str, object]]) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        author = _mapping(node.get("author"))
        if author is None:
            continue
        login = author.get("login")
        if isinstance(login, str) and login:
            found.add(login)
    return found


def _paths(nodes: list[dict[str, object]]) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        path = node.get("path")
        if isinstance(path, str) and path:
            found.add(path)
    return found


def _parse_github_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _span_days(as_of: datetime, oldest: datetime | None) -> int:
    if oldest is None:
        return 0
    span = _as_utc(as_of) - _as_utc(oldest)
    if span.total_seconds() < 0:
        return 0
    return span.days


def create_team_resolver(
    token: str,
    org: str,
) -> Callable[[tuple[str, ...]], str | None] | None:
    """Create a team resolver with pre-fetched team data.

    Returns None if token/org are empty or API fails.
    """
    if not token or not org:
        return None
    client = get_github_client(token)
    if client is None:
        return None
    team_data = _get_org_teams(client, org)
    if not team_data:
        return None

    def _resolve(owners: tuple[str, ...]) -> str | None:
        owner_logins = {owner.lstrip("@") for owner in owners}
        matching_teams: list[str] = []
        for team_slug, members in team_data.items():
            if owner_logins.issubset(members):
                matching_teams.append(team_slug)
        if not matching_teams:
            return None
        best = max(matching_teams, key=lambda t: t.count("/"))
        return f"@{org}/{best}"

    return _resolve


def _get_org_teams(
    client: Github,
    org: str,
) -> dict[str, set[str]]:
    """Fetch all teams in an org with their member login sets."""
    try:
        gh_org = client.get_organization(org)
        teams: dict[str, set[str]] = {}
        for team in gh_org.get_teams():
            members = {m.login for m in team.get_members()}
            teams[team.slug] = members
        note_api_call(teams)
        return teams
    except Exception:
        note_api_call()
        logger.warning("Failed to fetch teams for org %s", org)
        return {}


class ReconciliationError(Exception):
    """Raised when a reconciliation pull request cannot be opened or updated."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ReconciliationResult(TypedDict):
    number: int
    url: str
    updated: bool


def github_repository(repo_root: Path) -> str:
    """Return `owner/name` for `repo_root` from GITHUB_REPOSITORY or origin.

    Raises ReconciliationError when neither source names a GitHub repository.
    """
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if _is_slug(slug):
        return slug
    identity = repository_identity(repo_root)
    prefix = "origin:github.com/"
    if identity.startswith(prefix) and _is_slug(identity[len(prefix) :]):
        return identity[len(prefix) :]
    raise ReconciliationError(_RECONCILE_DENIED)


def open_or_update_reconciliation(
    *,
    token: str,
    repository: str,
    path: str,
    content: str,
    body: str,
) -> ReconciliationResult:
    """Open or update the reconciliation pull request for `repository`.

    `token` authenticates the GitHub API. `repository` is `owner/name`.
    `path` is the repo-relative CODEOWNERS path and `content` is its text.
    `body` is the pull-request text. Returns the pull request number, URL,
    and whether an existing open pull request was updated.
    """
    if offline_enabled() or not token:
        raise ReconciliationError(_RECONCILE_DENIED)
    owner, name = _split_repository(repository)
    base = f"/repos/{_quote(owner)}/{_quote(name)}"
    try:
        default_branch = _default_branch(base, token)
        parent = _branch_tip(base, token, default_branch)
        commit_sha = _replacement_commit(base, token, parent, path, content)
        existing = _open_pull(base, token, owner)
        _point_branch(base, token, commit_sha)
        if existing is None:
            opened = _create_pull(base, token, default_branch, body)
            return {"number": opened[0], "url": opened[1], "updated": False}
        updated = _update_pull(base, token, existing[0], body)
        return {"number": updated[0], "url": updated[1], "updated": True}
    except ReconciliationError:
        raise
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise ReconciliationError(f"{_RECONCILE_FAILED} ({exc})") from None


def _is_slug(value: str) -> bool:
    owner, sep, name = value.partition("/")
    return bool(sep and owner and name and "/" not in name)


def _split_repository(repository: str) -> tuple[str, str]:
    owner, sep, name = repository.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ReconciliationError(_RECONCILE_DENIED)
    return owner, name


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _default_branch(base: str, token: str) -> str:
    repo = _require_mapping(_github_request("GET", base, token))
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ReconciliationError(_RECONCILE_FAILED)
    return branch


def _branch_tip(base: str, token: str, branch: str) -> tuple[str, str]:
    ref = _require_mapping(_github_request("GET", f"{base}/git/ref/heads/{_quote(branch)}", token))
    commit_sha = _require_sha(_require_mapping(ref.get("object")))
    commit = _require_mapping(
        _github_request("GET", f"{base}/git/commits/{_quote(commit_sha)}", token)
    )
    tree_sha = _require_sha(_require_mapping(commit.get("tree")))
    return commit_sha, tree_sha


def _replacement_commit(
    base: str,
    token: str,
    parent: tuple[str, str],
    path: str,
    content: str,
) -> str:
    parent_sha, tree_sha = parent
    blob = _require_mapping(
        _github_request(
            "POST",
            f"{base}/git/blobs",
            token,
            {"content": content, "encoding": "utf-8"},
        )
    )
    blob_sha = _require_sha(blob)
    tree = _require_mapping(
        _github_request(
            "POST",
            f"{base}/git/trees",
            token,
            {
                "base_tree": tree_sha,
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha},
                ],
            },
        )
    )
    created = _require_mapping(
        _github_request(
            "POST",
            f"{base}/git/commits",
            token,
            {"message": RECONCILE_TITLE, "tree": _require_sha(tree), "parents": [parent_sha]},
        )
    )
    return _require_sha(created)


def _open_pull(base: str, token: str, owner: str) -> tuple[int, str] | None:
    head = _quote(f"{owner}:{RECONCILE_BRANCH}")
    payload = _github_request("GET", f"{base}/pulls?state=open&head={head}&per_page=20", token)
    if not isinstance(payload, list):
        raise ReconciliationError(_RECONCILE_FAILED)
    for item in payload:
        mapped = _mapping(item)
        if mapped is None:
            continue
        number = mapped.get("number")
        url = mapped.get("html_url")
        if isinstance(number, int) and isinstance(url, str) and url:
            return number, url
    return None


def _point_branch(base: str, token: str, sha: str) -> None:
    ref = f"{base}/git/refs/heads/{_quote(RECONCILE_BRANCH)}"
    try:
        _github_request("PATCH", ref, token, {"sha": sha, "force": True})
    except ReconciliationError as exc:
        if exc.status != 404:
            raise
        _github_request(
            "POST",
            f"{base}/git/refs",
            token,
            {"ref": f"refs/heads/{RECONCILE_BRANCH}", "sha": sha},
        )


def _create_pull(base: str, token: str, default_branch: str, body: str) -> tuple[int, str]:
    created = _require_mapping(
        _github_request(
            "POST",
            f"{base}/pulls",
            token,
            {
                "title": RECONCILE_TITLE,
                "body": body,
                "head": RECONCILE_BRANCH,
                "base": default_branch,
            },
        )
    )
    return _pull_ref(created)


def _update_pull(base: str, token: str, number: int, body: str) -> tuple[int, str]:
    updated = _require_mapping(
        _github_request(
            "PATCH",
            f"{base}/pulls/{number}",
            token,
            {"title": RECONCILE_TITLE, "body": body},
        )
    )
    return _pull_ref(updated)


def _pull_ref(payload: dict[str, object]) -> tuple[int, str]:
    number = payload.get("number")
    url = payload.get("html_url")
    if not isinstance(number, int) or not isinstance(url, str) or not url:
        raise ReconciliationError(_RECONCILE_FAILED)
    return number, url


def _require_sha(payload: dict[str, object]) -> str:
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ReconciliationError(_RECONCILE_FAILED)
    return sha


def _require_mapping(value: object) -> dict[str, object]:
    mapped = _mapping(value)
    if mapped is None:
        raise ReconciliationError(_RECONCILE_FAILED)
    return mapped


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        mapped[key] = item
    return mapped


def _github_request(
    method: str,
    path: str,
    token: str,
    payload: Mapping[str, object] | None = None,
) -> object:
    root = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    url = f"{root}{path}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ReconciliationError(_RECONCILE_FAILED)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310  # scheme restricted to http/https above
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # scheme restricted to http/https above
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        exc.read()
        if exc.code in {401, 403, 404}:
            raise ReconciliationError(_RECONCILE_DENIED, exc.code) from None
        raise ReconciliationError(
            f"Could not open a pull request (HTTP {exc.code}). "
            "Grant contents: write and pull-requests: write.",
            exc.code,
        ) from None
    except urllib.error.URLError as exc:
        raise ReconciliationError(f"{_RECONCILE_FAILED} ({exc.reason})") from None
    if not raw:
        return None
    loaded: object = json.loads(raw.decode("utf-8"))
    return loaded

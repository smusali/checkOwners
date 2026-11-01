"""GitHub API integration for owner resolution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.message import Message as EmailMessage
from pathlib import Path
from typing import Literal, TypedDict, TypeGuard

import checkowners.providers as _evidence
from checkowners.config import (
    Config,
    GithubConfig,
)
from checkowners.errors import IntegrationError
from checkowners.identity import HandleHit
from checkowners.models import (
    API_RATE_LIMIT_REASON,
    SEARCH_RATE_LIMIT_REASON,
    AnalysisGap,
    GapCode,
    review_window_truncated_reason,
)
from checkowners.privacy import email_token, is_email
from checkowners.state import (
    HandleCacheEntry,
    read_handle_cache,
    write_handle_cache,
)
from checkowners.storage import (
    cache_directory,
    repository_identity,
    write_json_file,
)
from checkowners.validate import RemoteCodeownersError

begin_api_budget = _evidence.begin_api_budget
clear_api_budget = _evidence.clear_api_budget
collection_gaps = _evidence.collection_gaps
mark_review_omitted = _evidence.mark_review_omitted
note_collection_gap = _evidence.note_collection_gap
record_review_window = _evidence.record_review_window
reset_review_window = _evidence.reset_review_window
review_was_omitted = _evidence.review_was_omitted
review_window = _evidence.review_window
take_api_request = _evidence.take_api_request

_HandleCacheValue = HandleCacheEntry | str

logger = logging.getLogger(__name__)

RECONCILE_BRANCH = "checkowners/reconcile-codeowners"
RECONCILE_TITLE = "chore: reconcile CODEOWNERS ownership drift"
RECONCILE_MARKER = "<!-- checkowners-reconcile -->"
_API_VERSION = "2022-11-28"
_TIMEOUT_SECONDS = 30
_PAGE_SIZE = 100
#: Caps on list endpoints, so one bad org cannot page forever.
_MEMBER_LIMIT = 1000
_TEAM_LIMIT = 500
_RECONCILE_DENIED = (
    "The token cannot open pull requests. Grant contents: write and pull-requests: write."
)
_RECONCILE_FAILED = "Could not open a pull request. Grant contents: write and pull-requests: write."


@dataclass(frozen=True)
class ApiResponse:
    """One GitHub reply.

    `status` is the HTTP status, or 0 when the request was never sent or the
    transport failed. `reason` describes a status of 0. `headers` holds the
    reply headers with their original casing folded away.
    """

    status: int
    body: bytes
    headers: Mapping[str, str]
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Return whether the status is in the 2xx range."""
        return 200 <= self.status < 300

    def header(self, name: str) -> str:
        """Return the reply header `name`, or an empty string."""
        return self.headers.get(name.casefold(), "")

    def json(self) -> object | None:
        """Return the parsed JSON body, or None when it is empty or malformed."""
        if not self.body:
            return None
        try:
            loaded: object = json.loads(self.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            return None
        return loaded


def api_root() -> str:
    """Return the REST API root, honoring GITHUB_API_URL."""
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def request(
    method: str,
    path: str,
    token: str,
    *,
    payload: Mapping[str, object] | None = None,
) -> ApiResponse:
    """Send one request to `path` under the API root. This never raises.

    `path` is appended to the API root, so it must start with a slash.
    `payload` is serialized as a JSON body. The reply carries the status, the
    body bytes and the headers.
    """
    url = f"{api_root()}{path}"
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        return ApiResponse(0, b"", {}, "unsupported API scheme")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    prepared = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(prepared, timeout=_TIMEOUT_SECONDS) as reply:  # noqa: S310
            return ApiResponse(reply.status, reply.read(), _folded(reply.headers))
    except urllib.error.HTTPError as exc:
        return ApiResponse(exc.code, exc.read(), _folded(exc.headers))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return ApiResponse(0, b"", {}, str(reason))


def _folded(headers: EmailMessage) -> dict[str, str]:
    return {name.casefold(): value for name, value in headers.items()}


def _object_list_from(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [mapped for item in value if (mapped := _mapping(item)) is not None]


def _login_of(value: object) -> str:
    mapped = _mapping(value)
    if mapped is None:
        return ""
    login = mapped.get("login")
    return login if isinstance(login, str) else ""


def _paged(
    path: str,
    token: str,
    *,
    limit: int,
    budget: Callable[[], bool] | None = None,
) -> list[dict[str, object]] | None:
    """Return up to `limit` objects from the paginated list at `path`.

    `budget` is consulted before each page. Returns None when a page fails.
    """
    joiner = "&" if "?" in path else "?"
    collected: list[dict[str, object]] = []
    page = 1
    while len(collected) < limit:
        if budget is not None and not budget():
            break
        size = min(_PAGE_SIZE, limit - len(collected))
        reply = request("GET", f"{path}{joiner}per_page={size}&page={page}", token)
        note_api_call()
        if not reply.ok:
            return collected or None
        items = _object_list_from(reply.json())
        collected.extend(items)
        if len(items) < size:
            break
        page += 1
    return collected


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


def api_available(token: str) -> bool:
    """Return whether this context may call the GitHub API with `token`."""
    return bool(token) and not offline_enabled()


def resolve_noreply_handle(email: str) -> str | None:
    """Extract the @handle from a GitHub noreply email, or None."""
    match = _NOREPLY_RE.match(email.strip())
    if match is None:
        return None
    return f"@{match.group(1)}"


def resolve_handles(
    emails: set[str],
    token: str,
    *,
    repo_root: Path | None = None,
    author_shas: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
    ttl: int | None = None,
) -> dict[str, str]:
    """Return @handles for the `emails` that resolve."""
    details = resolve_handle_details(
        emails,
        token,
        repo_root=repo_root,
        author_shas=author_shas,
        overrides=overrides,
        ttl=ttl,
    )
    return {email: hit.handle for email, hit in details.items() if hit.handle}


def resolve_handle_details(
    emails: set[str],
    token: str,
    *,
    repo_root: Path | None = None,
    author_shas: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
    ttl: int | None = None,
) -> dict[str, HandleHit]:
    """Return the handle hit for each resolved email. `repo_root` selects the cache."""
    configured = overrides or {}
    shas = author_shas or {}
    resolved: dict[str, HandleHit] = {}
    remaining: list[str] = []
    for email in sorted(emails):
        override = configured.get(email) or configured.get(email.casefold())
        if override:
            resolved[email] = HandleHit(handle=override, source="override")
            continue
        noreply = resolve_noreply_handle(email)
        if noreply is not None:
            resolved[email] = HandleHit(handle=noreply, source="noreply")
            continue
        remaining.append(email)
    if not remaining:
        return resolved

    cached: Mapping[str, _HandleCacheValue] = (
        read_handle_cache(repo_root) if repo_root is not None else {}
    )
    api_queue: list[str] = []
    now = datetime.now(tz=UTC)
    for email in remaining:
        hit = _cached_handle(cached, email)
        if hit is None:
            api_queue.append(email)
            continue
        if hit.handle:
            resolved[email] = HandleHit(
                handle=hit.handle,
                source=hit.source or "commit",
                account_type=hit.account_type,
            )
            continue
        if _miss_is_active(hit, now):
            continue
        api_queue.append(email)
    if not api_queue:
        return resolved

    if not api_available(token):
        return resolved
    fresh: dict[str, HandleCacheEntry] = {}
    search_queue = _resolve_from_commits(
        token,
        api_queue,
        shas,
        repo_root,
        resolved,
        fresh,
    )
    if search_queue and _search_budget_allows(token):
        _resolve_from_search(token, search_queue, resolved, fresh, ttl if ttl is not None else 30)
    if repo_root is not None and fresh:
        write_handle_cache(repo_root, fresh)
    return resolved


def organization_members(token: str, org: str) -> frozenset[str] | None:
    """Return member logins for `org`, or None when the list was not fetched."""
    if not org or not api_available(token):
        return None
    members = _paged(
        f"/orgs/{_quote(org)}/members", token, limit=_MEMBER_LIMIT, budget=take_api_request
    )
    if members is None:
        logger.warning("Failed to read members of %s", org)
        return None
    return frozenset(login for member in members if (login := _login_of(member)))


def _cached_handle(cache: Mapping[str, _HandleCacheValue], email: str) -> HandleCacheEntry | None:
    token = email_token(email) if is_email(email) else email
    hit = cache.get(token)
    if hit is None and token != email:
        hit = cache.get(email)
    return _cache_entry(hit)


def _cache_entry(value: _HandleCacheValue | None) -> HandleCacheEntry | None:
    if isinstance(value, HandleCacheEntry):
        return value
    if isinstance(value, str):
        if not value:
            return HandleCacheEntry(handle="", expires_at="9999-01-01T00:00:00+00:00")
        return HandleCacheEntry(handle=value)
    return None


def _miss_is_active(entry: HandleCacheEntry, now: datetime) -> bool:
    if not entry.expires_at:
        return False
    try:
        expires = datetime.fromisoformat(entry.expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > now


def _resolve_from_commits(
    token: str,
    emails: list[str],
    shas: Mapping[str, str],
    repo_root: Path | None,
    resolved: dict[str, HandleHit],
    fresh: dict[str, HandleCacheEntry],
) -> list[str]:
    needing_commit = [email for email in emails if shas.get(email)]
    if not needing_commit:
        return list(emails)
    if not _core_budget_allows(token):
        return [email for email in emails if email not in set(needing_commit)]
    slug = _repository_slug(repo_root) if repo_root is not None else ""
    if not slug:
        return list(emails)
    search_queue = [email for email in emails if email not in set(needing_commit)]
    for email in needing_commit:
        if not take_api_request():
            break
        looked_up = _lookup_commit_login(token, slug, shas[email])
        if looked_up is None:
            continue
        login, author_type, committer_type = looked_up
        if not login:
            search_queue.append(email)
            continue
        handle = login if login.startswith("@") else f"@{login}"
        resolved[email] = HandleHit(
            handle=handle,
            source="commit",
            account_type=author_type,
            committer_type=committer_type,
        )
        fresh[email] = HandleCacheEntry(
            handle=handle,
            source="commit",
            account_type=author_type,
        )
    return search_queue


def _resolve_from_search(
    token: str,
    emails: list[str],
    resolved: dict[str, HandleHit],
    fresh: dict[str, HandleCacheEntry],
    ttl: int,
) -> None:
    for email in emails:
        if not take_api_request():
            break
        handle = _lookup_handle(token, email)
        if handle is None:
            if ttl > 0:
                expires = datetime.now(tz=UTC) + timedelta(days=ttl)
                fresh[email] = HandleCacheEntry(handle="", expires_at=expires.isoformat())
            continue
        resolved[email] = HandleHit(handle=handle, source="search")
        fresh[email] = HandleCacheEntry(handle=handle, source="search")


def _repository_slug(repo_root: Path) -> str:
    try:
        return github_repository(repo_root)
    except ReconciliationError:
        return ""


def _lookup_commit_login(token: str, repo: str, sha: str) -> tuple[str, str, str] | None:
    """Return `(login, author type, committer type)` for `sha`, or None when the read fails."""
    note_api_call()
    owner, name = _split_repository(repo)
    reply = request("GET", f"/repos/{_quote(owner)}/{_quote(name)}/commits/{_quote(sha)}", token)
    commit = _mapping(reply.json()) if reply.ok else None
    if commit is None:
        logger.warning("Failed to read commit %s", sha)
        return None
    author = _mapping(commit.get("author")) or {}
    committer = _mapping(commit.get("committer")) or {}
    return _login_of(author), _text_field(author, "type"), _text_field(committer, "type")


def _text_field(mapped: Mapping[str, object], key: str) -> str:
    value = mapped.get(key)
    return value if isinstance(value, str) else ""


def _remaining(token: str, bucket: str) -> int | None:
    """Return the remaining calls in `bucket`, or None when the read fails."""
    reply = request("GET", "/rate_limit", token)
    note_api_call()
    if not reply.ok:
        return None
    resources = _mapping(_mapping(reply.json()) or {})
    section = _mapping((resources or {}).get("resources"))
    if section is None:
        return None
    counts = _mapping(section.get(bucket))
    if counts is None:
        return None
    value = counts.get("remaining")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _budget_allows(token: str, bucket: str) -> bool:
    if not take_api_request():
        return False
    remaining = _remaining(token, bucket)
    if remaining is None:
        logger.warning("Failed to read the GitHub %s rate limit", bucket)
        return True
    if remaining >= 1:
        return True
    note_collection_gap(AnalysisGap("api_rate_limit", SEARCH_RATE_LIMIT_REASON))
    return False


def _core_budget_allows(token: str) -> bool:
    return _budget_allows(token, "core")


def _search_budget_allows(token: str) -> bool:
    return _budget_allows(token, "search")


def _lookup_handle(token: str, email: str) -> str | None:
    """Return the `@handle` whose account carries `email`, or None."""
    note_api_call()
    query = urllib.parse.quote(f"{email} in:email", safe="")
    reply = request("GET", f"/search/users?q={query}", token)
    if not reply.ok:
        logger.warning("Failed to resolve GitHub handle for %s", email)
        return None
    payload = _mapping(reply.json())
    if payload is None:
        return None
    for item in _object_list_from(payload.get("items")):
        login = _login_of(item)
        if login:
            return f"@{login}"
    return None


def build_review_coverage(
    token: str,
    repo_full_name: str,
    emails: set[str],
    *,
    as_of: datetime,
    since: datetime | None,
    repo_root: Path | None = None,
) -> dict[str, dict[str, float]]:
    """Return `path -> {email: fraction}` for pulls closed from `since` through `as_of`.

    `since` None keeps reading until the pull list ends. Each fraction is one
    login's share of included pulls that touch the path. Returns an empty
    mapping when the API is unavailable or nothing maps to `emails`.
    """
    reset_review_window()
    if not api_available(token) or not repo_full_name or not emails:
        return {}
    email_to_handle = resolve_handles(emails, token, repo_root=repo_root)
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


@dataclass(frozen=True)
class PullSummary:
    """One pull request, as the review-load scan needs it."""

    number: int
    created_at: datetime | None
    changed_lines: int | None
    requested_reviewers: tuple[str, ...]


@dataclass(frozen=True)
class ReviewRecord:
    """One submitted review on a pull request."""

    handle: str
    submitted_at: datetime | None


def recent_pulls(
    token: str,
    repo_full_name: str,
    limit: int = REVIEW_SCAN_PR_LIMIT,
    *,
    state: str = "closed",
) -> tuple[PullSummary, ...] | None:
    """Return up to `limit` pulls in `state`, newest update first, or None on failure."""
    if not _is_slug(repo_full_name):
        return None
    owner, name = _split_repository(repo_full_name)
    path = (
        f"/repos/{_quote(owner)}/{_quote(name)}/pulls"
        f"?state={_quote(state)}&sort=updated&direction=desc"
    )
    listed = _paged(path, token, limit=limit)
    if listed is None:
        return None
    return tuple(_pull_summary(item) for item in listed)


def _pull_summary(payload: Mapping[str, object]) -> PullSummary:
    number = payload.get("number")
    additions = payload.get("additions")
    deletions = payload.get("deletions")
    changed = additions + deletions if _whole(additions) and _whole(deletions) else None
    return PullSummary(
        number=number if _whole(number) else 0,
        created_at=_parse_github_time(payload.get("created_at")),
        changed_lines=changed,
        requested_reviewers=tuple(
            f"@{login}"
            for item in _object_list_from(payload.get("requested_reviewers"))
            if (login := _login_of(item))
        ),
    )


def _whole(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def pull_reviews(token: str, repo_full_name: str, number: int) -> tuple[ReviewRecord, ...] | None:
    """Return the submitted reviews on one pull, or None when the read fails."""
    owner, name = _split_repository(repo_full_name)
    path = f"/repos/{_quote(owner)}/{_quote(name)}/pulls/{number}/reviews"
    listed = _paged(path, token, limit=_PAGE_SIZE)
    if listed is None:
        return None
    records: list[ReviewRecord] = []
    for item in listed:
        login = _login_of(item.get("user"))
        if not login:
            continue
        records.append(
            ReviewRecord(
                handle=f"@{login}",
                submitted_at=_parse_github_time(item.get("submitted_at")),
            )
        )
    return tuple(records)


def pull_filenames(token: str, repo_full_name: str, number: int) -> tuple[str, ...]:
    """Return the paths changed by one pull, or an empty tuple when unavailable."""
    owner, name = _split_repository(repo_full_name)
    path = f"/repos/{_quote(owner)}/{_quote(name)}/pulls/{number}/files"
    listed = _paged(path, token, limit=_PAGE_SIZE)
    if listed is None:
        return ()
    return tuple(name for item in listed if (name := _text_field(item, "filename")))


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
    record_review_window(len(scan.pulls), days)
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
    reply = request(
        "POST",
        "/graphql",
        token,
        payload={"query": query, "variables": dict(variables)},
    )
    if reply.status == 0:
        logger.warning("GitHub review query failed: %s", reply.reason)
        return _GraphqlResult(None, False)
    note_api_call()
    if not reply.ok:
        logger.warning("GitHub review query failed: HTTP %s", reply.status)
        return _GraphqlResult(None, reply.status in {403, 429})
    payload = _mapping(reply.json())
    if payload is None:
        logger.warning("GitHub review query failed")
        return _GraphqlResult(None, False)
    limited = _graphql_rate_limited(payload, reply.header("x-ratelimit-remaining") or None)
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
    if not api_available(token) or not org:
        return None
    team_data = _get_org_teams(token, org)
    if not team_data:
        return None

    def _resolve(owners: tuple[str, ...]) -> str | None:
        owner_logins = {owner.lstrip("@") for owner in owners}
        matching = [slug for slug, members in team_data.items() if owner_logins <= members]
        if not matching:
            return None
        narrowest = min(matching, key=lambda slug: (len(team_data[slug]), slug))
        return f"@{org}/{narrowest}"

    return _resolve


def _org_teams(token: str, org: str) -> list[dict[str, object]] | None:
    """Return the raw team objects for `org`, or None when the list fails."""
    return _paged(f"/orgs/{_quote(org)}/teams", token, limit=_TEAM_LIMIT)


def _team_member_logins(token: str, org: str, slug: str) -> tuple[frozenset[str], bool]:
    """Return `(member logins, visible)` for one team.

    A team whose member list is refused is reported as not visible.
    """
    path = f"/orgs/{_quote(org)}/teams/{_quote(slug)}/members"
    members = _paged(path, token, limit=_MEMBER_LIMIT)
    if members is None:
        return frozenset(), False
    return frozenset(login for member in members if (login := _login_of(member))), True


def _get_org_teams(token: str, org: str) -> dict[str, set[str]] | None:
    """Return `slug -> member logins` for every team in `org`, or None on failure."""
    listed = _org_teams(token, org)
    if listed is None:
        note_api_call()
        logger.warning("Failed to fetch teams for org %s", org)
        return None
    teams: dict[str, set[str]] = {}
    for team in listed:
        slug = _text_field(team, "slug")
        if not slug:
            continue
        members, _visible = _team_member_logins(token, org, slug)
        teams[slug] = set(members)
    note_api_call(teams)
    return teams


def declared_teams_from_github(config: Config) -> dict[str, frozenset[str]] | None:
    if not config.github.api_enabled:
        return None
    return _fetch_declared_teams(config.github)


def _fetch_declared_teams(github: GithubConfig) -> dict[str, frozenset[str]] | None:
    if not github.org:
        return None
    token = get_github_token()
    if not api_available(token):
        return None
    raw = _get_org_teams(token, github.org)
    if raw is None:
        return None
    return {team: frozenset(members) for team, members in raw.items()}


@dataclass(frozen=True)
class TeamRecord:
    slug: str
    members: frozenset[str]
    parent: str
    children: tuple[str, ...]
    visible: bool


@dataclass(frozen=True)
class TeamMembership:
    members: frozenset[str]
    nested: bool
    visible: bool
    found: bool


@dataclass(frozen=True)
class TeamDirectory:
    org: str
    fetched_at: str
    teams: tuple[TeamRecord, ...]

    def membership_for(self, owner: str) -> TeamMembership | None:
        slug = team_slug(owner, self.org)
        if slug is None:
            return None
        return self.membership(slug)

    def membership(self, slug: str) -> TeamMembership:
        index = {team.slug.casefold(): team for team in self.teams}
        seen: set[str] = set()

        def walk(key: str) -> tuple[set[str], bool, bool, bool]:
            folded = key.casefold()
            if folded in seen:
                return set(), False, True, True
            record = index.get(folded)
            if record is None:
                return set(), False, False, False
            if not record.visible:
                return set(), False, False, True
            seen.add(folded)
            members = set(record.members)
            nested = bool(record.children)
            visible = True
            for child in record.children:
                child_members, child_nested, child_visible, child_found = walk(child)
                if not child_found or not child_visible:
                    visible = False
                members.update(child_members)
                nested = nested or child_nested
            return members, nested, visible, True

        members, nested, visible, found = walk(slug)
        return TeamMembership(frozenset(members), nested, visible, found)


def team_slug(owner: str, org: str) -> str | None:
    if not owner.startswith("@") or "/" not in owner:
        return None
    team_org, sep, slug = owner[1:].partition("/")
    if not sep or not slug or not team_org:
        return None
    if org and team_org.casefold() != org.casefold():
        return None
    return slug


def load_team_directory(token: str, org: str) -> TeamDirectory | None:
    if not api_available(token) or not org:
        return None
    listed = _list_team_records(token, org)
    if listed is None:
        note_api_call()
        logger.warning("Failed to fetch teams for org %s", org)
        return None
    directory = TeamDirectory(
        org=org,
        fetched_at=collection_timestamp(),
        teams=tuple(_with_children(listed)),
    )
    _store_team_directory(directory)
    note_api_call({team.slug: team.members for team in directory.teams if team.visible})
    return directory


def _list_team_records(token: str, org: str) -> list[TeamRecord] | None:
    """Return one record per team in `org`, or None when the team list fails."""
    listed = _org_teams(token, org)
    if listed is None:
        return None
    records: list[TeamRecord] = []
    for team in listed:
        slug = _text_field(team, "slug")
        if not slug:
            continue
        members, visible = _team_member_logins(token, org, slug)
        records.append(
            TeamRecord(
                slug=slug,
                members=members,
                parent=_text_field(_mapping(team.get("parent")) or {}, "slug"),
                children=(),
                visible=visible,
            )
        )
    return records


def _with_children(records: list[TeamRecord]) -> list[TeamRecord]:
    grouped: dict[str, list[str]] = {}
    for record in records:
        if record.parent:
            grouped.setdefault(record.parent.casefold(), []).append(record.slug)
    built: list[TeamRecord] = []
    for record in records:
        children = tuple(grouped.get(record.slug.casefold(), ()))
        built.append(replace(record, children=children))
    return built


def _store_team_directory(directory: TeamDirectory) -> None:
    payload = {
        "org": directory.org,
        "fetched_at": directory.fetched_at,
        "teams": [
            {
                "slug": team.slug,
                "members": sorted(team.members),
                "parent": team.parent,
                "children": list(team.children),
                "visible": team.visible,
            }
            for team in directory.teams
        ],
    }
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in directory.org)
    try:
        write_json_file(cache_directory() / "teams" / f"{safe}.json", payload)
    except OSError:
        logger.warning("Failed to cache team snapshot for org %s", directory.org)


class ReconciliationError(IntegrationError):
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
    """Return the parsed reply for `path`, raising ReconciliationError on failure."""
    reply = request(method, path, token, payload=payload)
    if reply.status == 0:
        raise ReconciliationError(f"{_RECONCILE_FAILED} ({reply.reason})")
    if reply.ok:
        return reply.json()
    if reply.status in {401, 403, 404}:
        raise ReconciliationError(_RECONCILE_DENIED, reply.status)
    raise ReconciliationError(
        f"Could not open a pull request (HTTP {reply.status}). "
        "Grant contents: write and pull-requests: write.",
        reply.status,
    )


class PullRequestFilesError(IntegrationError):
    pass


_CHECK_SUMMARY_LIMIT = 65535
_CHECK_ANNOTATION_LIMIT = 50
_PR_PAGE = 100
_PR_STATUSES = frozenset({"added", "modified", "renamed", "copied", "changed"})
_PULL_READ = "Could not read pull request files. Grant pull-requests: read."


def _require_pull_read(repository: str, token: str) -> None:
    if offline_enabled():
        raise PullRequestFilesError("Offline mode cannot read pull request files.")
    if not token or not repository:
        raise PullRequestFilesError("A token with pull-requests: read is required.")


def _pull_read_error(detail: str | None = None) -> PullRequestFilesError:
    if detail is None:
        return PullRequestFilesError(_PULL_READ)
    return PullRequestFilesError(
        f"Could not read pull request files ({detail}). Grant pull-requests: read."
    )


def pull_request_paths(repository: str, number: int, token: str) -> tuple[str, ...]:
    _require_pull_read(repository, token)
    paths: list[str] = []
    page = 1
    while True:
        body = _read_json(
            "GET",
            f"/repos/{repository}/pulls/{number}/files?per_page={_PR_PAGE}&page={page}",
            token,
        )
        if not isinstance(body, list):
            raise _pull_read_error()
        for item in body:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            filename = item.get("filename")
            if status not in _PR_STATUSES or not isinstance(filename, str) or not filename:
                continue
            paths.append(filename)
        if len(body) < _PR_PAGE:
            break
        page += 1
    return tuple(paths)


def pull_request_base(repository: str, number: int, token: str) -> str:
    _require_pull_read(repository, token)
    body = _read_json("GET", f"/repos/{repository}/pulls/{number}", token)
    base = body.get("base") if isinstance(body, dict) else None
    sha = base.get("sha") if isinstance(base, dict) else None
    if not isinstance(sha, str) or not sha:
        raise _pull_read_error()
    return sha


def publish_check_run(
    *,
    conclusion: Literal["success", "failure"],
    summary: str,
    annotations: list[dict[str, object]],
) -> bool:
    if offline_enabled():
        return False
    token = get_github_token()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if not token or not repository or not sha:
        return False
    text = summary.strip() or "checkOwners"
    payload: dict[str, object] = {
        "name": "checkOwners",
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": "checkOwners",
            "summary": text[:_CHECK_SUMMARY_LIMIT],
            "annotations": annotations[:_CHECK_ANNOTATION_LIMIT],
        },
    }
    try:
        status, _body = _exchange("POST", f"/repos/{repository}/check-runs", token, payload)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        _notice_check_skipped()
        return False
    if status in {200, 201}:
        return True
    if status in {401, 403, 404}:
        _notice_check_skipped()
    return False


def _read_json(method: str, path: str, token: str) -> object:
    try:
        status, raw = _exchange(method, path, token, None)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _pull_read_error() from exc
    if status in {401, 403, 404}:
        raise _pull_read_error()
    if status < 200 or status >= 300:
        raise _pull_read_error(f"HTTP {status}")
    if not raw:
        return []
    try:
        loaded: object = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise _pull_read_error() from exc
    return loaded


def _exchange(
    method: str,
    path: str,
    token: str,
    payload: Mapping[str, object] | None,
) -> tuple[int, bytes]:
    """Return `(status, body)` for `path`, or `(403, b"")` with no budget left."""
    if not take_api_request():
        return 403, b""
    reply = request(method, path, token, payload=payload)
    note_api_call()
    return reply.status, reply.body


def _notice_check_skipped() -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice::checkOwners check run was not created.", file=sys.stderr)


def fetch_codeowners_errors(
    token: str,
    repository: str,
    ref: str = "",
) -> tuple[RemoteCodeownersError, ...] | None:
    payload = _codeowners_api(
        token,
        repository,
        f"codeowners/errors?ref={_quote(ref)}" if ref else "codeowners/errors",
    )
    mapped = _mapping(payload)
    if mapped is None:
        return None
    raw = mapped.get("errors")
    if not isinstance(raw, list):
        return None
    errors: list[RemoteCodeownersError] = []
    for item in raw:
        row = _mapping(item)
        if row is None:
            return None
        line = row.get("line")
        if isinstance(line, bool) or not isinstance(line, int):
            return None
        errors.append(
            RemoteCodeownersError(
                line=line,
                column=_int_field(row.get("column")),
                kind=_str_field(row.get("kind")),
                message=_str_field(row.get("message")),
                source=_str_field(row.get("source")),
                path=_str_field(row.get("path")),
            )
        )
    return tuple(errors)


def _codeowners_api(token: str, repository: str, suffix: str) -> object | None:
    if offline_enabled() or not token or not _is_slug(repository):
        return None
    try:
        owner, name = _split_repository(repository)
        return _github_request("GET", f"/repos/{_quote(owner)}/{_quote(name)}/{suffix}", token)
    except ReconciliationError:
        return None


def _int_field(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _str_field(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


AccessState = Literal["write", "read", "invisible", "unknown"]
ReviewState = Literal["required", "disabled", "unknown"]
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})


def owner_access(token: str, repository: str, owner: str) -> AccessState:
    if offline_enabled() or not token or not _is_slug(repository) or not owner:
        return "unknown"
    if "/" in owner:
        return _team_repo_access(token, repository, owner)
    return _user_access(token, repository, owner.lstrip("@"))


def code_owner_review_required(token: str, repository: str, branch: str) -> ReviewState:
    if offline_enabled() or not token or not _is_slug(repository):
        return "unknown"
    ruleset = _ruleset_review(token, repository)
    protection = _protection_review(token, repository, branch)
    if ruleset == "required" or protection == "required":
        return "required"
    if ruleset == "unknown" or protection == "unknown":
        return "unknown"
    return "disabled"


def repository_visibility(token: str, repository: str) -> tuple[bool, str] | None:
    """Return `(private, default_branch)` for `repository`, or None when unavailable."""
    mapped = _mapping(_codeowners_api(token, repository, ""))
    if mapped is None:
        return None
    branch = mapped.get("default_branch")
    if not isinstance(branch, str) or not branch:
        return None
    return mapped.get("private") is True, branch


def api_status(token: str) -> tuple[bool, tuple[str, ...]]:
    """Return whether the token authenticates, and the scopes it reports."""
    if offline_enabled() or not token:
        return False, ()
    reply = request("GET", "/user", token)
    if reply.status == 0:
        return False, ()
    header = reply.header("x-oauth-scopes")
    return True, tuple(part.strip() for part in header.split(",") if part.strip())


def _user_access(token: str, repository: str, login: str) -> AccessState:
    if not login:
        return "unknown"
    try:
        owner, name = _split_repository(repository)
        payload = _github_request(
            "GET",
            f"/repos/{_quote(owner)}/{_quote(name)}/collaborators/{_quote(login)}/permission",
            token,
        )
    except ReconciliationError as exc:
        if exc.status == 404:
            return "invisible"
        return "unknown"
    mapped = _mapping(payload)
    if mapped is None:
        return "unknown"
    permission = mapped.get("permission")
    if permission in _WRITE_PERMISSIONS:
        return "write"
    if isinstance(permission, str):
        return "read"
    return "unknown"


def _team_repo_access(token: str, repository: str, owner: str) -> AccessState:
    org, _, slug = owner.lstrip("@").partition("/")
    if not org or not slug:
        return "unknown"
    try:
        repo_owner, name = _split_repository(repository)
        payload = _github_request(
            "GET",
            f"/orgs/{_quote(org)}/teams/{_quote(slug)}/repos/{_quote(repo_owner)}/{_quote(name)}",
            token,
        )
    except ReconciliationError as exc:
        if exc.status == 404:
            return "invisible"
        return "unknown"
    mapped = _mapping(payload)
    if mapped is None:
        return "unknown"
    permissions = mapped.get("permissions")
    if not isinstance(permissions, dict):
        return "unknown"
    if any(permissions.get(name) is True for name in ("admin", "maintain", "push")):
        return "write"
    return "read"


def _ruleset_review(token: str, repository: str) -> ReviewState:
    try:
        owner, name = _split_repository(repository)
        payload = _github_request("GET", f"/repos/{_quote(owner)}/{_quote(name)}/rulesets", token)
    except ReconciliationError:
        return "unknown"
    if not isinstance(payload, list):
        return "unknown"
    for item in payload:
        row = _mapping(item)
        if row is None:
            continue
        ruleset_id = row.get("id")
        if isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int):
            continue
        if _ruleset_has_codeowners(token, repository, ruleset_id):
            return "required"
    return "disabled"


def _ruleset_has_codeowners(token: str, repository: str, ruleset_id: int) -> bool:
    try:
        owner, name = _split_repository(repository)
        payload = _github_request(
            "GET",
            f"/repos/{_quote(owner)}/{_quote(name)}/rulesets/{ruleset_id}",
            token,
        )
    except ReconciliationError:
        return False
    mapped = _mapping(payload)
    if mapped is None:
        return False
    rules = mapped.get("rules")
    if not isinstance(rules, list):
        return False
    for rule in rules:
        row = _mapping(rule)
        if row is not None and row.get("type") == "code_owner_review":
            return True
    return False


def _protection_review(token: str, repository: str, branch: str) -> ReviewState:
    if not branch:
        return "unknown"
    try:
        owner, name = _split_repository(repository)
        payload = _github_request(
            "GET",
            f"/repos/{_quote(owner)}/{_quote(name)}/branches/{_quote(branch)}/protection",
            token,
        )
    except ReconciliationError as exc:
        if exc.status == 404:
            return "disabled"
        return "unknown"
    mapped = _mapping(payload)
    if mapped is None:
        return "unknown"
    reviews = mapped.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        return "disabled"
    if reviews.get("require_code_owner_reviews") is True:
        return "required"
    return "disabled"

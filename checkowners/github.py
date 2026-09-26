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
    API_RATE_LIMIT_TRUNCATED_REASON,
    SEARCH_RATE_LIMIT_REASON,
    AnalysisGap,
    GapCode,
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


def _review_pull_limit(client: Github, planned: int) -> int:
    """Return how many pull requests review collection can afford, or 0 to skip."""
    if not take_api_request():
        mark_review_omitted()
        return 0
    try:
        rate = client.get_rate_limit()
    except Exception:
        logger.warning("Failed to read the GitHub core rate limit")
        return planned
    remaining = _rate_remaining(rate, "core")
    if remaining is None:
        return planned
    if remaining < 2:
        note_collection_gap(AnalysisGap("api_rate_limit", API_RATE_LIMIT_REASON), replace=True)
        mark_review_omitted()
        return 0
    affordable = remaining // 2
    if affordable < planned:
        note_collection_gap(
            AnalysisGap("api_rate_limit", API_RATE_LIMIT_TRUNCATED_REASON),
            replace=True,
        )
        return affordable
    return planned


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
) -> dict[str, dict[str, float]]:
    """Build per-path, per-email PR-review coverage for the given repo.

    Returns ``path -> {email: fraction}`` where the fraction is a reviewer's
    share of all reviews touching that path. Reviewer GitHub logins are mapped
    back to commit emails (via the same handle resolution used elsewhere) so the
    coverage keys line up with the contribution emails used for scoring.
    Returns an empty mapping when the API is unavailable or nothing maps.
    """
    client = get_github_client(token)
    if client is None or not repo_full_name or not emails:
        return {}
    email_to_handle = resolve_handles(emails, token)
    login_to_email = {handle.lstrip("@"): email for email, handle in email_to_handle.items()}
    if not login_to_email:
        return {}
    limit = _review_pull_limit(client, REVIEW_SCAN_PR_LIMIT)
    if limit == 0:
        return {}
    raw = _gather_review_counts_by_path(client, repo_full_name, limit=limit)
    coverage: dict[str, dict[str, float]] = {}
    for path, counts in raw.items():
        total = sum(counts.values())
        if total == 0:
            continue
        mapped = {
            login_to_email[login]: count / total
            for login, count in counts.items()
            if login in login_to_email
        }
        if mapped:
            coverage[path] = mapped
    return coverage


#: PR scans cover the most recently updated closed PRs only. Each PR costs
#: 2+ API calls (reviews + files); without a bound a mature repo with tens
#: of thousands of PRs would exhaust the rate limit in a single run.
REVIEW_SCAN_PR_LIMIT = 200


def iter_recent_closed_pulls(
    client: Github,
    repo_full_name: str,
    limit: int = REVIEW_SCAN_PR_LIMIT,
) -> Iterator[PullRequest]:
    """Yield the most recently updated closed PRs of a repo, bounded by limit."""
    repo = client.get_repo(repo_full_name)
    pulls = repo.get_pulls(state="closed", sort="updated", direction="desc")
    return islice(pulls, limit)


def _gather_review_counts_by_path(
    client: Github,
    repo_full_name: str,
    *,
    limit: int = REVIEW_SCAN_PR_LIMIT,
) -> dict[str, dict[str, int]]:
    """Count, per file path, how many reviews each reviewer login contributed."""
    note_api_call()
    result: dict[str, dict[str, int]] = {}
    try:
        for pull in iter_recent_closed_pulls(client, repo_full_name, limit):
            if not take_api_request():
                break
            reviewers = {review.user.login for review in pull.get_reviews() if review.user}
            if not reviewers:
                continue
            if not take_api_request():
                break
            for changed in pull.get_files():
                per_path = result.setdefault(changed.filename, {})
                for login in reviewers:
                    per_path[login] = per_path.get(login, 0) + 1
    except Exception:
        logger.warning("Failed to gather review coverage for %s", repo_full_name)
        return {}
    return result


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

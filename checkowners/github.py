"""GitHub API integration for owner resolution."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from itertools import islice
from typing import TYPE_CHECKING

from checkowners.state import read_handle_cache, write_handle_cache

if TYPE_CHECKING:
    from collections.abc import Iterator

    from github import Github
    from github.PullRequest import PullRequest

logger = logging.getLogger(__name__)

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
    """Create a PyGithub client, or None if token is empty.

    PyGithub ships in the optional ``github`` extra; when it is not installed
    the API-backed features degrade gracefully instead of crashing.
    """
    if not token:
        return None
    try:
        from github import Github as GithubClient
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
        hit = cached.get(email)
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
    fresh: dict[str, str] = {}
    for email in api_queue:
        handle = _lookup_handle(client, email)
        fresh[email] = handle if handle is not None else ""
        if handle is not None:
            resolved[email] = handle
    write_handle_cache(fresh)
    return resolved


def _lookup_handle(client: Github, email: str) -> str | None:
    """Look up a single email via GitHub user search API."""
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
    raw = _gather_review_counts_by_path(client, repo_full_name)
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
) -> dict[str, dict[str, int]]:
    """Count, per file path, how many reviews each reviewer login contributed."""
    result: dict[str, dict[str, int]] = {}
    try:
        for pull in iter_recent_closed_pulls(client, repo_full_name):
            reviewers = {review.user.login for review in pull.get_reviews() if review.user}
            if not reviewers:
                continue
            for changed in pull.get_files():
                per_path = result.setdefault(changed.filename, {})
                for login in reviewers:
                    per_path[login] = per_path.get(login, 0) + 1
    except Exception:  # noqa: BLE001
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
        return teams
    except Exception:
        logger.warning("Failed to fetch teams for org %s", org)
        return {}

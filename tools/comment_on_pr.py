#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from render_job_summary import DIAGNOSTIC, has_actionable_findings, load

MARKER = "<!-- checkowners-drift-report -->"
RESOLVED = (
    f"{MARKER}\n### CheckOwners: no drift detected\n\nPreviously reported drift has been resolved."
)
API_VERSION = "2022-11-28"
COMMENTS_PAGE = 100


def _notice(message: str) -> None:
    print(f"::notice::{message}")


def _warning(message: str) -> None:
    print(f"::warning::{message}")


def _api_root() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, str] | None = None,
) -> object:
    url = f"{_api_root()}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read()
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _existing_id(token: str, owner: str, repo: str, number: str) -> int | None:
    owner_q = urllib.parse.quote(owner, safe="")
    repo_q = urllib.parse.quote(repo, safe="")
    page = 1
    while True:
        comments = _request(
            "GET",
            f"/repos/{owner_q}/{repo_q}/issues/{number}/comments"
            f"?per_page={COMMENTS_PAGE}&page={page}",
            token,
        )
        if not isinstance(comments, list):
            return None
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            comment_id = comment.get("id")
            if isinstance(body, str) and MARKER in body and isinstance(comment_id, int):
                return comment_id
        if len(comments) < COMMENTS_PAGE:
            return None
        page += 1


def _comment(token: str, owner: str, repo: str, number: str) -> None:
    try:
        report = Path("checkowners-report.md").read_text(encoding="utf-8")
    except OSError:
        report = DIAGNOSTIC

    drift = load("drift.json")
    bus = load("bus_factor.json")
    decay = load("decay.json")
    analysis_failed = drift is None
    has_findings = has_actionable_findings(drift, bus, decay)
    existing = _existing_id(token, owner, repo, number)
    owner_q = urllib.parse.quote(owner, safe="")
    repo_q = urllib.parse.quote(repo, safe="")

    if not has_findings and not analysis_failed:
        if existing is not None:
            _request(
                "PATCH",
                f"/repos/{owner_q}/{repo_q}/issues/comments/{existing}",
                token,
                {"body": RESOLVED},
            )
        return

    body = f"{MARKER}\n{report}"
    if existing is not None:
        _request(
            "PATCH",
            f"/repos/{owner_q}/{repo_q}/issues/comments/{existing}",
            token,
            {"body": body},
        )
        return
    _request(
        "POST",
        f"/repos/{owner_q}/{repo_q}/issues/{number}/comments",
        token,
        {"body": body},
    )


def main() -> int:
    head_repo = os.environ.get("HEAD_REPO", "")
    this_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not head_repo or head_repo != this_repo:
        _notice("Skipping PR comment: fork pull request. Full report is in the job summary.")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    number = os.environ.get("PR_NUMBER", "")
    if not token or not number.isdigit() or "/" not in this_repo:
        _warning(
            "Could not post PR comment (missing token, repository, or PR number). "
            "Grant 'pull-requests: write' or set comment_on_pr: false. "
            "Full report is in the job summary."
        )
        return 0

    owner, repo = this_repo.split("/", 1)
    try:
        _comment(token, owner, repo, number)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        _warning(
            f"Could not post PR comment ({exc}). "
            "Grant 'pull-requests: write' or set comment_on_pr: false. "
            "Full report is in the job summary."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

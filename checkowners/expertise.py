"""Per-path expertise ranking from a confidence-scored ownership map."""

from __future__ import annotations

import fnmatch
from datetime import datetime

from checkowners.models import ExpertiseRank, OwnershipMap


def rank_expertise(
    ownership: OwnershipMap,
    target: str,
) -> tuple[ExpertiseRank, ...]:
    """Rank contributors who have expertise in `target`.

    `target` may be a concrete path or a fnmatch-style glob. Multiple
    PathOwnerships matching the target are merged: a contributor's
    rank is the max confidence across the matched paths, with commits
    summed and last_commit set to the most recent.
    """
    aggregated: dict[str, ExpertiseRank] = {}
    for path, po in ownership.paths.items():
        if not path_matches_glob(path, target):
            continue
        for owner in po.owners:
            existing = aggregated.get(owner.handle)
            if existing is None:
                aggregated[owner.handle] = ExpertiseRank(
                    handle=owner.handle,
                    confidence=owner.confidence,
                    commits=owner.commits,
                    last_commit=owner.last_commit,
                )
                continue
            new_confidence = max(existing.confidence, owner.confidence)
            new_commits = existing.commits + owner.commits
            new_last = _max_dt(existing.last_commit, owner.last_commit)
            aggregated[owner.handle] = ExpertiseRank(
                handle=owner.handle,
                confidence=new_confidence,
                commits=new_commits,
                last_commit=new_last,
            )
    ranked = sorted(aggregated.values(), key=lambda r: (-r.confidence, -r.commits, r.handle))
    return tuple(ranked)


def path_matches_glob(path: str, pattern: str) -> bool:
    """Match a repo path against a user-supplied path, glob, or directory prefix.

    Shared matching semantics for expertise, bus factor, and onboarding
    targets: fnmatch (where ``*`` may cross ``/``), PLUS a bare
    directory-prefix convenience, so a non-glob pattern like ``controllers``
    or ``controllers/`` matches any path under that directory
    (segment-aware prefix match).
    """
    normalized_path = path.lstrip("/")
    normalized_pattern = pattern.lstrip("/")
    if normalized_path == normalized_pattern:
        return True
    if fnmatch.fnmatch(normalized_path, normalized_pattern):
        return True
    prefix = normalized_pattern.rstrip("/")
    if prefix and not _is_glob(prefix):
        return normalized_path.startswith(prefix + "/")
    return False


def _is_glob(value: str) -> bool:
    return any(ch in value for ch in ("*", "?", "["))


def common_prefix_depth(a: str, b: str) -> int:
    """Number of leading directory components shared between two paths."""
    a_parts = a.lstrip("/").split("/")[:-1]
    b_parts = b.lstrip("/").split("/")[:-1]
    common = 0
    for x, y in zip(a_parts, b_parts, strict=False):
        if x != y:
            break
        common += 1
    return common


def _max_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b

"""Presentation and storage rules for contributor identities."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from checkowners.models import Config, OwnerEntry, OwnershipMap, PathOwnership

KNOWLEDGE_RISK_NOTICE = (
    "This is a knowledge-risk tool, not a performance-measurement tool. "
    "Using it for individual evaluation is unsupported and harmful."
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9][A-Za-z0-9/-]*")
_PERSON_KEYS = frozenset(
    {
        "handle",
        "identity",
        "members",
        "owners",
        "loads",
        "overloaded",
        "suggestions",
        "recommended_transfer",
        "recommended_backups",
        "contributors_above_threshold",
        "candidate",
        "reviewer",
    }
)


def is_email(value: str) -> bool:
    """Return whether `value` is an address rather than a handle or stored token."""
    if value.startswith("email:"):
        return False
    at = value.find("@")
    return at > 0 and "." in value[at + 1 :]


def email_token(email: str) -> str:
    """Return the stable at-rest token for `email`."""
    if email.startswith("email:") and "@" not in email:
        return email
    digest = hashlib.sha256(email.casefold().encode("utf-8")).hexdigest()[:16]
    return f"email:{digest}"


def stored_identity(value: str) -> str:
    """Return `value` with an email replaced by its at-rest token."""
    if is_email(value):
        return email_token(value)
    return value


def pseudonym(identity: str, repo_id: str) -> str:
    """Return a stable person token for `identity` in `repo_id`."""
    key = stored_identity(identity)
    digest = hashlib.sha256(f"{repo_id}\n{key.casefold()}".encode()).hexdigest()[:12]
    return f"person-{digest}"


def rekey_handle_cache(cache: Mapping[str, str]) -> dict[str, str]:
    """Return `cache` with email keys replaced by at-rest tokens."""
    stored: dict[str, str] = {}
    for key, value in cache.items():
        token = stored_identity(key)
        current = stored.get(token)
        if current is None or (current == "" and value):
            stored[token] = value
    return stored


def label(identity: str, config: Config, repo_id: str) -> str:
    """Return the identity string that may appear in output."""
    if config.output.anonymize:
        return pseudonym(identity, repo_id)
    if is_email(identity) and (config.identity_mode == "hashed" or config.privacy.redact_emails):
        return email_token(identity)
    return identity


def named_codeowners_allowed(config: Config) -> bool:
    """Return whether generate may write GitHub owner tokens."""
    return not (
        config.output.anonymize or config.output.aggregate_only or config.identity_mode == "hashed"
    )


def is_excluded(identity: str, excluded: tuple[str, ...]) -> bool:
    """Return whether `identity` matches an entry in `excluded`."""
    if not excluded:
        return False
    return _matches(identity, _excluded_keys(excluded))


def exclude_contributors(
    ownership: OwnershipMap,
    excluded: tuple[str, ...],
    *,
    confidence_threshold: float,
) -> OwnershipMap:
    """Return `ownership` without contributors listed in `excluded`."""
    if not excluded:
        return ownership
    keys = _excluded_keys(excluded)
    paths: dict[str, PathOwnership] = {}
    for path, path_ownership in ownership.paths.items():
        owners = tuple(owner for owner in path_ownership.owners if not _matches(owner.handle, keys))
        candidates = tuple(
            owner for owner in path_ownership.candidates if not _matches(owner.handle, keys)
        )
        warnings = tuple(
            warning
            for warning in path_ownership.decay_warnings
            if not _matches(warning.handle, keys)
        )
        paths[path] = replace(
            path_ownership,
            owners=owners,
            candidates=candidates,
            decay_warnings=warnings,
            qualified_owner_count=_qualified_count(owners, confidence_threshold),
        )
    return replace(ownership, paths=paths)


def without_emails(
    ownership: OwnershipMap,
    *,
    confidence_threshold: float,
) -> OwnershipMap:
    """Return `ownership` without owners whose identity is still an email address."""
    paths: dict[str, PathOwnership] = {}
    for path, path_ownership in ownership.paths.items():
        owners = tuple(owner for owner in path_ownership.owners if not is_email(owner.handle))
        candidates = tuple(
            owner for owner in path_ownership.candidates if not is_email(owner.handle)
        )
        warnings = tuple(
            warning for warning in path_ownership.decay_warnings if not is_email(warning.handle)
        )
        paths[path] = replace(
            path_ownership,
            owners=owners,
            candidates=candidates,
            decay_warnings=warnings,
            qualified_owner_count=_qualified_count(owners, confidence_threshold),
        )
    return replace(ownership, paths=paths)


def sanitize(payload: Mapping[str, object], config: Config, repo_id: str) -> dict[str, object]:
    """Return `payload` with identities labeled or removed."""
    return _walk_dict(dict(payload), config, repo_id)


def scrub_text(value: str, config: Config, repo_id: str) -> str:
    """Return `value` with identities labeled or removed."""
    return _scrub(value, config, repo_id)


def _excluded_keys(excluded: tuple[str, ...]) -> frozenset[str]:
    keys: set[str] = set()
    for item in excluded:
        text = item.strip()
        if not text:
            continue
        keys.add(text.casefold())
        keys.add(stored_identity(text).casefold())
        if text.startswith("@"):
            keys.add(text[1:].casefold())
        else:
            keys.add(f"@{text}".casefold())
    return frozenset(keys)


def _matches(identity: str, keys: frozenset[str]) -> bool:
    folded = identity.casefold()
    bare = identity[1:].casefold() if identity.startswith("@") else ""
    return folded in keys or stored_identity(identity).casefold() in keys or bare in keys


def _qualified_count(owners: tuple[OwnerEntry, ...], threshold: float) -> int:
    return sum(1 for owner in owners if owner.confidence >= threshold)


def _walk_dict(value: Mapping[Any, object], config: Config, repo_id: str) -> dict[str, object]:
    walked: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if config.output.aggregate_only and key in _PERSON_KEYS:
            continue
        walked[key] = _walk(item, config, repo_id)
    return walked


def _walk(value: object, config: Config, repo_id: str) -> object:
    if isinstance(value, dict):
        return _walk_dict(value, config, repo_id)
    if isinstance(value, list):
        return [_walk(item, config, repo_id) for item in value]
    if isinstance(value, str):
        return _scrub(value, config, repo_id)
    return value


def _scrub(value: str, config: Config, repo_id: str) -> str:
    if config.output.aggregate_only:
        text = _EMAIL.sub("", value)
        return _HANDLE.sub("", text)

    def replace_email(match: re.Match[str]) -> str:
        return label(match.group(0), config, repo_id)

    text = _EMAIL.sub(replace_email, value)
    if not config.output.anonymize:
        return text

    def replace_handle(match: re.Match[str]) -> str:
        return label(match.group(0), config, repo_id)

    return _HANDLE.sub(replace_handle, text)

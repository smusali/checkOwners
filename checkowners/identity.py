from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from checkowners.expertise import path_matches_glob
from checkowners.models import (
    Config,
    ContributorStatus,
    IdentityOverride,
    OwnerEntry,
    OwnershipMap,
)
from checkowners.privacy import is_email, stored_identity

CERTAIN_FACTOR = 1.0
UNRESOLVED_FACTOR = 0.75
SEARCH_FACTOR = 0.5
MASS_COMMIT_FLOOR = 20
MASS_TEMPLATE_FRACTION = 0.8

_MECHANICAL_PREFIXES = ("bump", "chore(deps)", "dependabot", "automated")
_BOT_LOCAL_PARTS = frozenset({"dependabot", "renovate", "github-actions", "release-please"})
_SHA_RE = re.compile(r"\b[0-9a-f]{7,}\b", re.IGNORECASE)
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


@dataclass(frozen=True)
class HandleHit:
    handle: str
    source: str
    account_type: str = ""
    committer_type: str = ""


@dataclass(frozen=True)
class ContributorRecord:
    handle: str
    emails: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    service_account: bool = False
    source: str = ""


@dataclass(frozen=True)
class AuditEvent:
    action: str
    left: str
    right: str
    into: str
    at: str
    prior: tuple[ContributorRecord, ...] = ()


@dataclass(frozen=True)
class IdentityGraph:
    contributors: tuple[ContributorRecord, ...] = ()
    separations: tuple[tuple[str, str], ...] = ()
    audit: tuple[AuditEvent, ...] = ()


@dataclass(frozen=True)
class DuplicateGroup:
    identities: tuple[str, str]
    reason: str


@dataclass(frozen=True)
class OwnerMerge:
    owners: tuple[OwnerEntry, ...]
    groups: tuple[tuple[str, ...], ...]


def identity_evidence_factor(handle: str, hits: Mapping[str, HandleHit]) -> float:
    """Return the evidence scale for `handle` given the resolution `hits`."""
    hit = _index_hits(hits).get(handle.casefold())
    if hit is not None and hit.handle:
        if hit.source == "search":
            return SEARCH_FACTOR
        return CERTAIN_FACTOR
    if is_email(handle):
        return UNRESOLVED_FACTOR
    return CERTAIN_FACTOR


def override_handles(
    overrides: tuple[tuple[str, IdentityOverride], ...],
) -> dict[str, str]:
    """Return configured emails and handles mapped to their canonical handle."""
    mapped: dict[str, str] = {}
    for handle, override in overrides:
        target = handle if handle.startswith("@") else f"@{handle}"
        mapped[handle.casefold()] = target
        mapped[target.casefold()] = target
        for email in override.emails:
            mapped[email.casefold()] = target
    return mapped


def service_account_identities(
    overrides: tuple[tuple[str, IdentityOverride], ...],
) -> tuple[str, ...]:
    """Return handles and emails marked as service accounts."""
    found: list[str] = []
    for handle, override in overrides:
        if not override.service_account:
            continue
        found.append(handle if handle.startswith("@") else f"@{handle}")
        found.extend(override.emails)
    return tuple(found)


def bot_identities(config: Config) -> tuple[str, ...]:
    """Return configured bot addresses and service-account identities from `config`."""
    return (*config.bot_addresses, *service_account_identities(config.identity_overrides))


def is_bot_identity(identity: str, extra: Sequence[str] = ()) -> bool:
    """Return whether `identity` is a bot address, bot account, or `extra` entry."""
    lowered = identity.casefold()
    if lowered in {item.casefold() for item in extra}:
        return True
    if "[bot]" in lowered or lowered == "actions@github.com" or lowered.startswith("bot@"):
        return True
    local = lowered.split("@", 1)[0]
    return local in _BOT_LOCAL_PARTS


def subject_template(subject: str) -> str:
    """Return `subject` with commit SHAs and version numbers removed."""
    stripped = _SHA_RE.sub("", subject.casefold())
    return " ".join(_VERSION_RE.sub("", stripped).split())


def mechanical_authors(commits: Sequence[tuple[str, str]]) -> frozenset[str]:
    """Return authors in `commits` whose subjects repeat one template or a mechanical prefix."""
    grouped: dict[str, list[str]] = {}
    for author, subject in commits:
        grouped.setdefault(author, []).append(subject)
    found: set[str] = set()
    for author, subjects in grouped.items():
        if len(subjects) < MASS_COMMIT_FLOOR:
            continue
        templates = [subject_template(subject) for subject in subjects]
        top = max(templates.count(template) for template in set(templates))
        prefixes = sum(1 for subject in subjects if _mechanical_subject(subject))
        count = len(subjects)
        if top / count >= MASS_TEMPLATE_FRACTION or prefixes / count >= MASS_TEMPLATE_FRACTION:
            found.add(author)
    return frozenset(found)


def former_knowledge_share(owners: Sequence[OwnerEntry]) -> float:
    """Return the share of positive ownership score held by former owners."""
    total = sum(owner.ownership_score for owner in owners if owner.ownership_score > 0)
    if total <= 0:
        return 0.0
    former = sum(
        owner.ownership_score
        for owner in owners
        if owner.ownership_score > 0 and owner.contributor_status == "former"
    )
    return former / total


def repository_former_share(
    ownership: OwnershipMap,
    criticality: tuple[tuple[str, float], ...],
) -> float:
    """Return the criticality-weighted former-knowledge share across `ownership`."""
    total = 0.0
    weighted = 0.0
    for path, path_ownership in ownership.paths.items():
        weight = _criticality_weight(path, criticality)
        total += weight
        weighted += weight * former_knowledge_share(path_ownership.owners)
    if total <= 0:
        return 0.0
    return round(weighted / total, 4)


def merge_owners(
    owners: tuple[OwnerEntry, ...],
    hits: Mapping[str, HandleHit],
    separations: tuple[tuple[str, str], ...] = (),
) -> OwnerMerge:
    """Return `owners` collapsed by certain `hits`, plus the identity groups that merged."""
    tagged = [_tag_owner(owner, hits.get(owner.handle)) for owner in owners]
    counts: dict[str, int] = {}
    for tag in tagged:
        key = tag.resolved.casefold()
        counts[key] = counts.get(key, 0) + 1
    for tag in tagged:
        if not tag.certain and counts.get(tag.resolved.casefold(), 0) > 1:
            tag.display = tag.original
    groups = _group_tags(tagged, separations)
    combined: list[OwnerEntry] = []
    recorded: list[tuple[str, ...]] = []
    for group in groups:
        combined.append(_combine_group(group))
        if group[0].certain and len(group) > 1:
            recorded.append(tuple(tag.original for tag in group))
    combined.sort(key=lambda owner: (-owner.ownership_score, owner.handle))
    return OwnerMerge(tuple(combined), tuple(recorded))


def assign_status(
    identity: str,
    *,
    roster: Mapping[str, ContributorStatus],
    bot_identities: Sequence[str],
    account_type: str,
    committer_type: str,
    org_members: frozenset[str] | None,
    last_commit: datetime | None,
    as_of: datetime,
    threshold_days: int,
) -> ContributorStatus:
    """Return the status for `identity` from roster, bots, org membership, then activity."""
    rostered = _roster_status(identity, roster)
    if rostered is not None:
        return rostered
    if (
        is_bot_identity(identity, bot_identities)
        or account_type == "Bot"
        or committer_type == "Bot"
    ):
        return "bot"
    if org_members is not None and identity.startswith("@"):
        login = identity[1:].casefold()
        members = {member.casefold() for member in org_members}
        if login in members:
            return _activity_status(last_commit, as_of, threshold_days) or "inactive"
        return "external"
    activity = _activity_status(last_commit, as_of, threshold_days)
    if activity is not None:
        return activity
    return "unknown"


def apply_contributor_status(
    owners: tuple[OwnerEntry, ...],
    *,
    config: Config,
    hits: Mapping[str, HandleHit],
    org_members: frozenset[str] | None,
    as_of: datetime,
) -> tuple[OwnerEntry, ...]:
    """Return `owners` with `contributor_status` filled in."""
    roster = dict(config.contributor_roster)
    bots = bot_identities(config)
    indexed = _index_hits(hits)
    updated: list[OwnerEntry] = []
    for owner in owners:
        hit = indexed.get(owner.handle.casefold())
        status = assign_status(
            owner.handle,
            roster=roster,
            bot_identities=bots,
            account_type=hit.account_type if hit is not None else "",
            committer_type=hit.committer_type if hit is not None else "",
            org_members=org_members,
            last_commit=owner.last_commit,
            as_of=as_of,
            threshold_days=config.decay.threshold_days,
        )
        updated.append(replace(owner, contributor_status=status))
    return tuple(updated)


def apply_merge(graph: IdentityGraph, left: str, right: str, into: str) -> IdentityGraph:
    """Return `graph` with `left` and `right` combined into `into`, plus an audit event."""
    records = {record.handle: record for record in graph.contributors}
    prior_left = records.get(left, ContributorRecord(handle=left))
    prior_right = records.get(right, ContributorRecord(handle=right))
    existing = records.get(into)
    if existing is not None and _covers(existing, left, right):
        return graph
    combined = ContributorRecord(
        handle=into,
        emails=_unique((*prior_left.emails, *prior_right.emails)),
        names=_unique((*prior_left.names, *prior_right.names)),
        aliases=_unique((*prior_left.aliases, *prior_right.aliases)),
        service_account=prior_left.service_account or prior_right.service_account,
        source="merge",
    )
    kept = tuple(record for handle, record in records.items() if handle not in {left, right, into})
    event = AuditEvent(
        action="merge",
        left=left,
        right=right,
        into=into,
        at=_timestamp(),
        prior=(prior_left, prior_right),
    )
    return replace(graph, contributors=(*kept, combined), audit=(*graph.audit, event))


def revert_merge(graph: IdentityGraph, left: str, right: str) -> IdentityGraph:
    """Return `graph` restored from the latest merge of `left` and `right`."""
    event = _latest_merge(graph, left, right)
    if event is None:
        return graph
    removed = {event.into, event.left, event.right}
    kept = tuple(record for record in graph.contributors if record.handle not in removed)
    undo = AuditEvent(
        action="unmerge",
        left=left,
        right=right,
        into=event.into,
        at=_timestamp(),
        prior=event.prior,
    )
    return replace(
        graph,
        contributors=(*kept, *event.prior),
        separations=(*graph.separations, (left, right)),
        audit=(*graph.audit, undo),
    )


def record_certain_merges(
    graph: IdentityGraph,
    groups: Sequence[tuple[str, ...]],
    into: Mapping[str, str],
) -> IdentityGraph:
    """Return `graph` after recording each certain group as a merge."""
    current = graph
    for group in groups:
        if len(group) < 2:
            continue
        target = into.get(group[0], group[0])
        current = apply_merge(current, group[0], group[1], target)
        for extra in group[2:]:
            current = apply_merge(current, target, extra, target)
    return current


def probable_duplicates(
    records: Sequence[ContributorRecord],
    separations: tuple[tuple[str, str], ...] = (),
) -> tuple[DuplicateGroup, ...]:
    """Return probable duplicate pairs among `records`, skipping `separations`."""
    found: list[DuplicateGroup] = []
    items = list(records)
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if _separated(left.handle, right.handle, separations):
                continue
            if _same_record(left, right):
                continue
            reason = _duplicate_reason(left, right)
            if reason:
                found.append(DuplicateGroup((left.handle, right.handle), reason))
    return tuple(found)


def audit_records(
    identities: Iterable[str],
    names_by_identity: Mapping[str, tuple[str, ...]],
    overrides: tuple[tuple[str, IdentityOverride], ...],
) -> tuple[ContributorRecord, ...]:
    """Return audit records for `identities`, including configured aliases and emails."""
    built: dict[str, ContributorRecord] = {}
    for identity in identities:
        emails = (identity,) if is_email(identity) else ()
        built[identity] = ContributorRecord(
            handle=identity,
            emails=emails,
            names=names_by_identity.get(identity, ()),
        )
    for handle, override in overrides:
        target = handle if handle.startswith("@") else f"@{handle}"
        current = built.get(target, ContributorRecord(handle=target))
        built[target] = replace(
            current,
            emails=_unique((*current.emails, *override.emails)),
            aliases=_unique((*current.aliases, *override.aliases)),
            names=_unique((*current.names, *override.aliases)),
            service_account=override.service_account or current.service_account,
        )
    return tuple(built.values())


def audit_payload(
    groups: Sequence[DuplicateGroup],
    *,
    aggregate_only: bool,
) -> dict[str, int | list[dict[str, list[str] | str]]]:
    """Return the audit body. Aggregate-only output is the duplicate count."""
    if aggregate_only:
        return {"duplicate_count": len(groups)}
    duplicates: list[dict[str, list[str] | str]] = [
        {"identities": [group.identities[0], group.identities[1]], "reason": group.reason}
        for group in groups
    ]
    return {"duplicate_count": len(groups), "duplicates": duplicates}


def load_identity_graph(repo_root: Path) -> IdentityGraph:
    """Return the identity graph stored for `repo_root`, or an empty graph."""
    from checkowners.state import identity_store_path, read_json_file  # noqa: PLC0415

    raw = read_json_file(identity_store_path(repo_root))
    if raw is None:
        return IdentityGraph()
    return _graph_from_payload(raw)


def save_identity_graph(repo_root: Path, graph: IdentityGraph) -> None:
    """Write `graph` for `repo_root`."""
    from checkowners.state import identity_store_path, write_json_file  # noqa: PLC0415

    write_json_file(identity_store_path(repo_root), _graph_payload(graph))


@dataclass
class _Tag:
    owner: OwnerEntry
    original: str
    resolved: str
    certain: bool
    quality: float
    display: str


def _tag_owner(owner: OwnerEntry, hit: HandleHit | None) -> _Tag:
    if hit is None or not hit.handle:
        return _Tag(
            owner=owner,
            original=owner.handle,
            resolved=owner.handle,
            certain=False,
            quality=owner.evidence_quality * identity_evidence_factor(owner.handle, {}),
            display=owner.handle,
        )
    certain = hit.source != "search"
    factor = identity_evidence_factor(owner.handle, {owner.handle: hit})
    return _Tag(
        owner=owner,
        original=owner.handle,
        resolved=hit.handle,
        certain=certain,
        quality=owner.evidence_quality * factor,
        display=hit.handle,
    )


def _group_tags(
    tagged: list[_Tag],
    separations: tuple[tuple[str, str], ...],
) -> list[list[_Tag]]:
    groups: list[list[_Tag]] = []
    for tag in tagged:
        if not tag.certain:
            groups.append([tag])
            continue
        placed = False
        for group in groups:
            if not group[0].certain or group[0].resolved.casefold() != tag.resolved.casefold():
                continue
            if any(_separated(tag.original, member.original, separations) for member in group):
                continue
            group.append(tag)
            placed = True
            break
        if placed:
            continue
        blocked = any(
            other.certain
            and other is not tag
            and other.resolved.casefold() == tag.resolved.casefold()
            and _separated(tag.original, other.original, separations)
            for other in tagged
        )
        if blocked:
            tag.display = tag.original
        groups.append([tag])
    return groups


def _combine_group(group: list[_Tag]) -> OwnerEntry:
    best = max(group, key=lambda tag: tag.owner.ownership_score)
    last_dates = [tag.owner.last_commit for tag in group if tag.owner.last_commit is not None]
    last_commit = max(last_dates) if last_dates else None
    handle = group[0].resolved if len(group) > 1 else group[0].display
    return replace(
        best.owner,
        handle=handle,
        commits=sum(tag.owner.commits for tag in group),
        last_commit=last_commit,
        evidence_quality=min(tag.quality for tag in group),
    )


def _activity_status(
    last_commit: datetime | None,
    as_of: datetime,
    threshold_days: int,
) -> ContributorStatus | None:
    if last_commit is None:
        return None
    moment = last_commit if last_commit.tzinfo is not None else last_commit.replace(tzinfo=UTC)
    age = (as_of - moment.astimezone(UTC)).days
    if age <= threshold_days:
        return "active"
    return "inactive"


def _roster_status(
    identity: str,
    roster: Mapping[str, ContributorStatus],
) -> ContributorStatus | None:
    for name, status in roster.items():
        if _same_identity(identity, name):
            return status
    return None


def _same_identity(left: str, right: str) -> bool:
    folded = left.casefold().lstrip("@")
    return bool(folded) and folded == right.casefold().lstrip("@")


def _index_hits(hits: Mapping[str, HandleHit]) -> dict[str, HandleHit]:
    indexed: dict[str, HandleHit] = {}
    for key, hit in hits.items():
        indexed[key.casefold()] = hit
        if hit.handle:
            indexed.setdefault(hit.handle.casefold(), hit)
    return indexed


def _separated(left: str, right: str, pairs: tuple[tuple[str, str], ...]) -> bool:
    wanted = {left.casefold(), right.casefold()}
    return any({one.casefold(), two.casefold()} == wanted for one, two in pairs)


def _covers(record: ContributorRecord, left: str, right: str) -> bool:
    names = {record.handle, *record.aliases, *record.emails}
    return left in names and right in names


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _timestamp() -> str:
    return datetime.now(tz=UTC).isoformat()


def _latest_merge(graph: IdentityGraph, left: str, right: str) -> AuditEvent | None:
    wanted = {left, right}
    for event in reversed(graph.audit):
        if event.action == "merge" and {event.left, event.right} == wanted:
            return event
    return None


def _same_record(left: ContributorRecord, right: ContributorRecord) -> bool:
    if left.handle == right.handle:
        return True
    return left.handle in right.aliases or right.handle in left.aliases


def _duplicate_reason(left: ContributorRecord, right: ContributorRecord) -> str:
    left_names = {name.casefold() for name in left.names if name}
    right_names = {name.casefold() for name in right.names if name}
    if left_names & right_names:
        return "same author name"
    left_handle = left.handle.casefold().lstrip("@")
    right_handle = right.handle.casefold().lstrip("@")
    for email in right.emails:
        if email.split("@", 1)[0].casefold() == left_handle:
            return "email local-part"
    for email in left.emails:
        if email.split("@", 1)[0].casefold() == right_handle:
            return "email local-part"
    left_aliases = {alias.casefold() for alias in left.aliases if alias}
    right_aliases = {alias.casefold() for alias in right.aliases if alias}
    if left_aliases & right_aliases or left_aliases & right_names or right_aliases & left_names:
        return "shared alias"
    return ""


def _mechanical_subject(subject: str) -> bool:
    lowered = subject.casefold().strip()
    return any(lowered.startswith(prefix) for prefix in _MECHANICAL_PREFIXES)


def _criticality_weight(path: str, rules: tuple[tuple[str, float], ...]) -> float:
    for pattern, weight in rules:
        if path_matches_glob(path, pattern):
            return weight
    return 1.0


def _graph_payload(graph: IdentityGraph) -> dict[str, object]:
    return {
        "contributors": [_record_payload(record) for record in graph.contributors],
        "separations": [list(pair) for pair in graph.separations],
        "audit": [_event_payload(event) for event in graph.audit],
    }


def _record_payload(record: ContributorRecord) -> dict[str, object]:
    return {
        "handle": record.handle,
        "emails": [stored_identity(email) for email in record.emails],
        "names": list(record.names),
        "aliases": list(record.aliases),
        "service_account": record.service_account,
        "source": record.source,
    }


def _event_payload(event: AuditEvent) -> dict[str, object]:
    return {
        "action": event.action,
        "left": event.left,
        "right": event.right,
        "into": event.into,
        "at": event.at,
        "prior": [_record_payload(record) for record in event.prior],
    }


def _graph_from_payload(raw: Mapping[str, object]) -> IdentityGraph:
    contributors = raw.get("contributors")
    separations = raw.get("separations")
    audit = raw.get("audit")
    return IdentityGraph(
        contributors=_records(contributors),
        separations=_pairs(separations),
        audit=_events(audit),
    )


def _records(value: object) -> tuple[ContributorRecord, ...]:
    if not isinstance(value, list):
        return ()
    records: list[ContributorRecord] = []
    for item in value:
        record = _record(item)
        if record is not None:
            records.append(record)
    return tuple(records)


def _record(value: object) -> ContributorRecord | None:
    if not isinstance(value, dict):
        return None
    handle = value.get("handle")
    if not isinstance(handle, str) or not handle:
        return None
    service = value.get("service_account", False)
    source = value.get("source", "")
    return ContributorRecord(
        handle=handle,
        emails=_strings(value.get("emails")),
        names=_strings(value.get("names")),
        aliases=_strings(value.get("aliases")),
        service_account=service is True,
        source=source if isinstance(source, str) else "",
    )


def _events(value: object) -> tuple[AuditEvent, ...]:
    if not isinstance(value, list):
        return ()
    events: list[AuditEvent] = []
    for item in value:
        event = _event(item)
        if event is not None:
            events.append(event)
    return tuple(events)


def _event(value: object) -> AuditEvent | None:
    if not isinstance(value, dict):
        return None
    action = value.get("action")
    left = value.get("left")
    right = value.get("right")
    into = value.get("into")
    at = value.get("at")
    if not isinstance(action, str) or action not in {"merge", "unmerge"}:
        return None
    if not isinstance(left, str) or not isinstance(right, str) or not isinstance(into, str):
        return None
    if not isinstance(at, str) or not left or not right or not into or not at:
        return None
    return AuditEvent(
        action=action,
        left=left,
        right=right,
        into=into,
        at=at,
        prior=_records(value.get("prior")),
    )


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            continue
        left, right = item
        if isinstance(left, str) and isinstance(right, str) and left and right:
            pairs.append((left, right))
    return tuple(pairs)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))

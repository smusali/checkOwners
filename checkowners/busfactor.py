"""Qualified owner count per path with backup-reviewer recommendations."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from checkowners.expertise import common_prefix_depth, path_matches_glob
from checkowners.models import (
    BusFactor,
    BusFactorConfig,
    Config,
    OwnerDistribution,
    OwnerDistributionJson,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    RiskJson,
    knowledge_concentration,
    risk_from_scores,
)
from checkowners.privacy import is_excluded

Tier = Literal["critical", "warning", "ok"]


EMPTY_DISTRIBUTION = OwnerDistribution(
    minimum=0.0,
    p10=0.0,
    median=0.0,
    p90=0.0,
    critical_path_risk=0.0,
    knowledge_at_risk=0.0,
    criticality_incomplete=True,
)


@dataclass(frozen=True)
class BusFactorReport:
    entries: tuple[BusFactor, ...]
    qualified_owner_count_cap: int
    distribution: OwnerDistribution = EMPTY_DISTRIBUTION
    config: BusFactorConfig = field(default_factory=BusFactorConfig)

    @property
    def critical_paths(self) -> tuple[str, ...]:
        return tuple(
            e.path for e in self.entries if self.tier_for(e.qualified_owner_count) == "critical"
        )

    def tier_for(self, qualified_owner_count: int) -> Tier:
        """Tier a qualified owner count against this report's configured thresholds."""
        return classify(qualified_owner_count, self.config)


def format_qualified_owner_count(count: int, cap: int) -> str:
    return f"{count} (capped by top_n_owners={cap})"


def qualified_owner_count_fields(count: int, cap: int) -> dict[str, int]:
    return {
        "qualified_owner_count": count,
        "qualified_owner_count_cap": cap,
    }


def compute_qualified_owners(
    ownership: OwnershipMap,
    config: Config,
    *,
    target: str | None = None,
) -> BusFactorReport:
    """Compute qualified-owner entries for every path matching `target` (or all paths)."""
    threshold = config.analysis.confidence_threshold
    cap = config.analysis.top_n_owners
    entries: list[BusFactor] = []
    for path, po in ownership.paths.items():
        if target is not None and not path_matches_glob(path, target):
            continue
        qualified = tuple(o.handle for o in po.owners if o.confidence >= threshold)
        backups = _recommend_backups(ownership, path, qualified, threshold)
        entries.append(
            BusFactor(
                path=path,
                qualified_owner_count=po.qualified_owner_count,
                contributors_above_threshold=qualified,
                recommended_backups=backups,
            )
        )
    entries.sort(key=lambda e: (e.qualified_owner_count, e.path))
    return BusFactorReport(
        entries=tuple(entries),
        qualified_owner_count_cap=cap,
        distribution=owner_distribution(tuple(entries), ownership, config),
        config=config.bus_factor,
    )


def distribution_json(distribution: OwnerDistribution) -> OwnerDistributionJson:
    """Return the JSON object for `distribution`."""
    return {
        "minimum": distribution.minimum,
        "p10": distribution.p10,
        "median": distribution.median,
        "p90": distribution.p90,
        "critical_path_risk": distribution.critical_path_risk,
        "knowledge_at_risk": distribution.knowledge_at_risk,
        "criticality_incomplete": distribution.criticality_incomplete,
    }


def owner_distribution(
    entries: tuple[BusFactor, ...],
    ownership: OwnershipMap,
    config: Config,
) -> OwnerDistribution:
    """Return the criticality-weighted share distribution of `entries`."""
    incomplete = not config.criticality
    if not entries:
        return OwnerDistribution(
            minimum=0.0,
            p10=0.0,
            median=0.0,
            p90=0.0,
            critical_path_risk=0.0,
            knowledge_at_risk=0.0,
            criticality_incomplete=incomplete,
        )
    weighted: list[tuple[float, float]] = []
    risk_weight = 0.0
    total_weight = 0.0
    weighted_share = 0.0
    threshold = config.bus_factor.critical_threshold
    for entry in entries:
        path_ownership = ownership.paths.get(entry.path)
        share = _top_owner_share(path_ownership)
        weight = _criticality_weight(entry.path, config.criticality)
        weighted.append((share, weight))
        total_weight += weight
        weighted_share += weight * share
        if entry.qualified_owner_count <= threshold:
            risk_weight += weight
    ordered = sorted(weighted, key=lambda item: item[0])
    return OwnerDistribution(
        minimum=_weighted_percentile(ordered, total_weight, 0.0),
        p10=_weighted_percentile(ordered, total_weight, 0.10),
        median=_weighted_percentile(ordered, total_weight, 0.50),
        p90=_weighted_percentile(ordered, total_weight, 0.90),
        critical_path_risk=round(weighted_share / total_weight, 4),
        knowledge_at_risk=round(risk_weight / total_weight, 4),
        criticality_incomplete=incomplete,
    )


def _top_owner_share(path_ownership: PathOwnership | None) -> float:
    if path_ownership is None:
        return 0.0
    scored = path_ownership.scored_owners or path_ownership.owners
    scores = tuple(owner.ownership_score for owner in scored)
    return knowledge_concentration(scores).top_owner_share


def _criticality_weight(path: str, rules: tuple[tuple[str, float], ...]) -> float:
    for pattern, weight in rules:
        if path_matches_glob(path, pattern):
            return weight
    return 1.0


def _weighted_percentile(
    ordered: list[tuple[float, float]],
    total_weight: float,
    quantile: float,
) -> float:
    target = quantile * total_weight
    cumulative = 0.0
    share = ordered[-1][0]
    for item_share, weight in ordered:
        cumulative += weight
        share = item_share
        if cumulative >= target:
            break
    return share


def classify(qualified_owner_count: int, config: BusFactorConfig) -> Tier:
    """Map a qualified owner count to a severity tier."""
    return _classify(
        qualified_owner_count,
        critical_threshold=config.critical_threshold,
        warn_threshold=config.warn_threshold,
    )


def _classify(
    qualified_owner_count: int,
    *,
    critical_threshold: int,
    warn_threshold: int,
) -> Tier:
    if qualified_owner_count <= critical_threshold:
        return "critical"
    if qualified_owner_count <= warn_threshold:
        return "warning"
    return "ok"


def _recommend_backups(
    ownership: OwnershipMap,
    path: str,
    qualified: tuple[str, ...],
    threshold: float,
) -> tuple[str, ...]:
    """Suggest contributors who could build up backup expertise.

    Prefers contributors working in adjacent paths (shared leading
    directory); when that heuristic yields nothing (e.g. root-level files),
    falls back to the repo-wide top owners by confidence, excluding the
    path's own owners.
    """
    qualified_set = set(qualified)
    candidates = _adjacent_candidates(ownership, path, qualified_set, threshold)
    if not candidates:
        candidates = _repo_wide_candidates(ownership, path)
    ordered = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(handle for handle, _ in ordered[:3])


def _adjacent_candidates(
    ownership: OwnershipMap,
    path: str,
    qualified_set: set[str],
    threshold: float,
) -> dict[str, float]:
    """Backup candidates from paths sharing a leading directory with `path`."""
    candidates: dict[str, float] = {}
    for other_path, po in ownership.paths.items():
        if other_path == path:
            continue
        if common_prefix_depth(path, other_path) < 1:
            continue
        for owner in po.owners:
            if owner.handle in qualified_set:
                continue
            if owner.confidence < threshold:
                continue
            current = candidates.get(owner.handle, 0.0)
            if owner.confidence > current:
                candidates[owner.handle] = owner.confidence
    return candidates


def _repo_wide_candidates(
    ownership: OwnershipMap,
    path: str,
) -> dict[str, float]:
    """Repo-wide top owners by confidence, excluding the path's own owners."""
    own_handles = {owner.handle for owner in ownership.paths[path].owners}
    candidates: dict[str, float] = {}
    for other_path, po in ownership.paths.items():
        if other_path == path:
            continue
        for owner in po.owners:
            if owner.handle in own_handles:
                continue
            current = candidates.get(owner.handle, 0.0)
            if owner.confidence > current:
                candidates[owner.handle] = owner.confidence
    return candidates


class UnknownIdentityError(ValueError):
    def __init__(self, identity: str) -> None:
        self.identity = identity
        super().__init__(identity)


@dataclass(frozen=True)
class TransferCandidate:
    identity: str
    confidence: float


@dataclass(frozen=True)
class OrphanedDirectory:
    path: str
    files: int


@dataclass(frozen=True)
class AffectedPath:
    path: str
    authors_before: tuple[str, ...]
    authors_after: tuple[str, ...]
    risk_before: RiskJson
    risk_after: RiskJson


@dataclass(frozen=True)
class DirectoryTransfer:
    path: str
    candidates: tuple[TransferCandidate, ...]


@dataclass(frozen=True)
class SimulationReport:
    removed: tuple[str, ...]
    contributor_count: int
    files_losing_only_owner: int
    files_losing_only_owner_ratio: float
    files_dropping_to_one: int
    orphaned_directories: tuple[OrphanedDirectory, ...]
    repo_truck_factor_before: int
    repo_truck_factor_after: int
    risk_before: OwnerDistribution
    risk_after: OwnerDistribution
    affected: tuple[AffectedPath, ...]
    transfers: tuple[DirectoryTransfer, ...]


def simulate_removal(
    ownership: OwnershipMap,
    config: Config,
    removed: tuple[str, ...],
) -> SimulationReport:
    """Return the departure report for `removed` on `ownership` under `config`.

    `removed` is one or more identities. The result counts files that lose
    their only author, files left with one author, fully orphaned directories,
    and the repository truck factor before and after. Raises ValueError when
    `removed` is empty, and UnknownIdentityError when an identity is absent.
    """
    identities = _unique_identities(removed)
    if not identities:
        msg = "at least one identity is required"
        raise ValueError(msg)
    for identity in identities:
        if not _identity_known(ownership, identity):
            raise UnknownIdentityError(identity)

    threshold = config.analysis.confidence_threshold
    thresholds = config.risk.truck_factor_thresholds
    before: dict[str, tuple[OwnerEntry, ...]] = {}
    after: dict[str, tuple[OwnerEntry, ...]] = {}
    only_owner = 0
    drop_to_one = 0
    affected: list[AffectedPath] = []
    for path, path_ownership in ownership.paths.items():
        authors = _authors(path_ownership, threshold)
        remaining = tuple(
            author for author in authors if not is_excluded(author.handle, identities)
        )
        before[path] = authors
        after[path] = remaining
        if len(authors) == 1 and not remaining:
            only_owner += 1
        if len(authors) >= 2 and len(remaining) == 1:
            drop_to_one += 1
        if _handles(authors) != _handles(remaining):
            affected.append(
                AffectedPath(
                    path=path,
                    authors_before=_handles(authors),
                    authors_after=_handles(remaining),
                    risk_before=_path_risk(authors, thresholds),
                    risk_after=_path_risk(remaining, thresholds),
                )
            )
    orphaned = tuple(path for path, authors in after.items() if not authors)
    directories = _orphaned_directories(tuple(ownership.paths), orphaned)
    transfers = tuple(
        DirectoryTransfer(
            path=directory.path,
            candidates=_residual(ownership, directory.path, identities),
        )
        for directory in directories
    )
    total = len(ownership.paths)
    before_sets = {path: frozenset(_handles(authors)) for path, authors in before.items()}
    after_sets = {path: frozenset(_handles(authors)) for path, authors in after.items()}
    ratio = round(only_owner / total, 4) if total else 0.0
    return SimulationReport(
        removed=identities,
        contributor_count=_contributor_count(ownership),
        files_losing_only_owner=only_owner,
        files_losing_only_owner_ratio=ratio,
        files_dropping_to_one=drop_to_one,
        orphaned_directories=directories,
        repo_truck_factor_before=_repo_truck_factor(before_sets, thresholds[0]),
        repo_truck_factor_after=_repo_truck_factor(after_sets, thresholds[0]),
        risk_before=_distribution(ownership, before, config),
        risk_after=_distribution(ownership, after, config),
        affected=tuple(affected),
        transfers=transfers,
    )


def simulation_payload(report: SimulationReport) -> dict[str, object]:
    """Return the machine-readable body for `report`."""
    return {
        "removed": list(report.removed),
        "contributor_count": report.contributor_count,
        "files_losing_only_owner": report.files_losing_only_owner,
        "files_losing_only_owner_ratio": report.files_losing_only_owner_ratio,
        "files_dropping_to_one": report.files_dropping_to_one,
        "orphaned_directories": [
            {"path": directory.path, "files": directory.files}
            for directory in report.orphaned_directories
        ],
        "repo_truck_factor": {
            "before": report.repo_truck_factor_before,
            "after": report.repo_truck_factor_after,
        },
        "risk": {
            "before": distribution_json(report.risk_before),
            "after": distribution_json(report.risk_after),
        },
        "affected": [
            {
                "path": item.path,
                "authors_before": list(item.authors_before),
                "authors_after": list(item.authors_after),
                "risk_before": dict(item.risk_before),
                "risk_after": dict(item.risk_after),
            }
            for item in report.affected
        ],
        "transfers": [
            {
                "path": transfer.path,
                "candidates": [
                    {
                        "identity": candidate.identity,
                        "confidence": round(candidate.confidence, 4),
                    }
                    for candidate in transfer.candidates
                ],
            }
            for transfer in report.transfers
        ],
    }


def _unique_identities(removed: tuple[str, ...]) -> tuple[str, ...]:
    kept: list[str] = []
    for item in removed:
        text = item.strip()
        if not text or any(is_excluded(text, (earlier,)) for earlier in kept):
            continue
        kept.append(text)
    return tuple(kept)


def _identity_known(ownership: OwnershipMap, identity: str) -> bool:
    for path_ownership in ownership.paths.values():
        for person in _stored_people(path_ownership):
            if is_excluded(person.handle, (identity,)):
                return True
    return False


def _contributor_count(ownership: OwnershipMap) -> int:
    handles = {
        person.handle
        for path_ownership in ownership.paths.values()
        for person in _stored_people(path_ownership)
    }
    return len(handles)


def _stored_people(path_ownership: PathOwnership) -> tuple[OwnerEntry, ...]:
    best: dict[str, OwnerEntry] = {}
    stored = (
        *path_ownership.scored_owners,
        *path_ownership.owners,
        *path_ownership.candidates,
    )
    for person in stored:
        current = best.get(person.handle)
        if current is None or person.ownership_score > current.ownership_score:
            best[person.handle] = person
    return tuple(best.values())


def _authors(path_ownership: PathOwnership, threshold: float) -> tuple[OwnerEntry, ...]:
    source = path_ownership.scored_owners or path_ownership.owners
    return tuple(person for person in source if person.ownership_score >= threshold)


def _handles(people: tuple[OwnerEntry, ...]) -> tuple[str, ...]:
    return tuple(sorted(person.handle for person in people))


def _path_risk(
    people: tuple[OwnerEntry, ...],
    thresholds: tuple[float, float, float],
) -> RiskJson:
    scores = tuple(person.ownership_score for person in people)
    return risk_from_scores(scores, thresholds)


def _distribution(
    ownership: OwnershipMap,
    authors: dict[str, tuple[OwnerEntry, ...]],
    config: Config,
) -> OwnerDistribution:
    paths: dict[str, PathOwnership] = {}
    entries: list[BusFactor] = []
    for path, path_ownership in ownership.paths.items():
        people = authors[path]
        paths[path] = replace(
            path_ownership,
            owners=people,
            scored_owners=people,
            qualified_owner_count=len(people),
        )
        entries.append(
            BusFactor(
                path=path,
                qualified_owner_count=len(people),
                contributors_above_threshold=_handles(people),
                recommended_backups=(),
            )
        )
    mapped = replace(ownership, paths=paths)
    return owner_distribution(tuple(entries), mapped, config)


def _orphaned_directories(
    paths: tuple[str, ...],
    orphaned: tuple[str, ...],
) -> tuple[OrphanedDirectory, ...]:
    orphaned_set = set(orphaned)
    prefixes: set[str] = set()
    for path in orphaned:
        parts = path.split("/")
        for index in range(1, len(parts)):
            prefixes.add("/".join(parts[:index]))
    fully: set[str] = set()
    counts: dict[str, int] = {}
    for prefix in prefixes:
        under = [path for path in paths if path.startswith(f"{prefix}/")]
        if under and all(path in orphaned_set for path in under):
            fully.add(prefix)
            counts[prefix] = len(under)
    roots: list[str] = []
    for prefix in fully:
        parent = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        if parent in fully:
            continue
        roots.append(prefix)
    ordered = sorted(roots, key=lambda prefix: (-counts[prefix], prefix))
    return tuple(OrphanedDirectory(path=f"{prefix}/", files=counts[prefix]) for prefix in ordered)


def _residual(
    ownership: OwnershipMap,
    directory: str,
    removed: tuple[str, ...],
) -> tuple[TransferCandidate, ...]:
    prefix = directory if directory.endswith("/") else f"{directory}/"
    best: dict[str, float] = {}
    for path, path_ownership in ownership.paths.items():
        if not path.startswith(prefix):
            continue
        for person in _stored_people(path_ownership):
            if person.ownership_score <= 0 or is_excluded(person.handle, removed):
                continue
            current = best.get(person.handle, 0.0)
            if person.ownership_score > current:
                best[person.handle] = person.ownership_score
    ordered = sorted(best.items(), key=lambda item: (-item[1], item[0]))
    return tuple(
        TransferCandidate(identity=handle, confidence=score) for handle, score in ordered[:2]
    )


def _repo_truck_factor(authors: dict[str, frozenset[str]], coverage: float) -> int:
    total = len(authors)
    if total == 0:
        return 0
    remaining = {path: set(handles) for path, handles in authors.items()}
    removed_count = 0
    while True:
        covered = sum(1 for handles in remaining.values() if handles)
        if covered / total < coverage:
            return removed_count
        counts: dict[str, int] = {}
        for handles in remaining.values():
            for handle in handles:
                counts[handle] = counts.get(handle, 0) + 1
        if not counts:
            return removed_count
        top = min(counts, key=lambda handle: (-counts[handle], handle))
        for handles in remaining.values():
            handles.discard(top)
        removed_count += 1

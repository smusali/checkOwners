"""Evidence providers, rescoring, and declared-versus-observed reconciliation."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from checkowners.analyze import Contribution as _CommitFacts

import yaml

from checkowners.config import find_codeowners_path
from checkowners.models import (
    API_BUDGET_REASON,
    AnalysisGap,
    Config,
    EvidenceType,
    GapCode,
    OwnerEntry,
    OwnershipEvidence,
    OwnershipMap,
    ScoringConfig,
)
from checkowners.patterns import CodeownersRule, match_path, parse_rules, pattern_matches

ImplementedDialect = Literal["github", "gitlab"]
ComponentLevel = Literal["file", "directory", "package", "service", "repository"]

_PACKAGE_FILES = frozenset({"pyproject.toml", "package.json", "go.mod", "Cargo.toml"})
_SERVICE_FILE = "catalog-info.yaml"


class DialectError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewWindow:
    sampled: int = 0
    window_days: int = 0


@dataclass
class _ApiBudget:
    limit: int
    used: int = 0
    review_omitted: bool = False
    gaps: dict[GapCode, AnalysisGap] = field(default_factory=dict)


_REVIEW_WINDOW: ContextVar[ReviewWindow | None] = ContextVar(
    "checkowners_review_window",
    default=None,
)
_API_BUDGET: ContextVar[_ApiBudget | None] = ContextVar("checkowners_api_budget", default=None)


def reset_review_window() -> None:
    _REVIEW_WINDOW.set(None)


def review_window() -> ReviewWindow:
    current = _REVIEW_WINDOW.get()
    return ReviewWindow() if current is None else current


def record_review_window(sampled: int, window_days: int) -> None:
    _REVIEW_WINDOW.set(ReviewWindow(sampled=sampled, window_days=window_days))


def note_metric(kind: str) -> None:
    path = os.environ.get("CHECKOWNERS_METRICS_PATH")
    if not path:
        return
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, f"{kind}\n".encode())
    finally:
        os.close(fd)


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
    note_metric("api")
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


def require_dialect(name: str) -> ImplementedDialect:
    if name == "github":
        return "github"
    if name == "gitlab":
        return "gitlab"
    if name in {"bitbucket", "gerrit"}:
        raise DialectError(f"{name} dialect is not implemented.")
    raise DialectError(f"Unknown dialect: {name}.")


class GitHubTeamProvider:
    def __init__(self, path: str, teams: Mapping[str, frozenset[str]]) -> None:
        self._path = path
        self._teams = teams

    def collect(self) -> Iterable[OwnershipEvidence]:
        for team, members in self._teams.items():
            if not members:
                continue
            yield OwnershipEvidence(
                subject=team,
                path=self._path,
                type="review",
                share=0.0,
            )


def path_evidence(
    path: str,
    authors: Mapping[str, _CommitFacts],
    blame: Mapping[str, float],
    review: Mapping[str, float],
    *,
    blame_available: bool,
    review_available: bool,
    survival: Mapping[str, float | None] | None,
) -> tuple[OwnershipEvidence, ...]:
    records: list[OwnershipEvidence] = []
    for subject, contrib in authors.items():
        ratio = None if survival is None else survival.get(subject)
        records.append(
            OwnershipEvidence(
                subject=subject,
                path=path,
                type="commit",
                timestamp=contrib.last_commit,
                weight=contrib.credit,
                commits=contrib.commits,
                credit=contrib.credit,
                cadence_days=contrib.cadence_days,
                later_commits=contrib.later_commits,
                survival=ratio,
            )
        )
    if blame_available:
        records.extend(
            OwnershipEvidence(
                subject=subject,
                path=path,
                type="blame",
                share=blame.get(subject, 0.0),
            )
            for subject in authors
        )
    if review_available:
        records.extend(
            OwnershipEvidence(
                subject=subject,
                path=path,
                type="review",
                share=review.get(subject, 0.0),
            )
            for subject in authors
        )
    return tuple(records)


def replace_reviews(
    evidence: Iterable[OwnershipEvidence],
    reviews: Iterable[OwnershipEvidence],
) -> tuple[OwnershipEvidence, ...]:
    kept = tuple(record for record in evidence if record.type != "review")
    return kept + tuple(reviews)


def rescore(
    evidence: Iterable[OwnershipEvidence],
    *,
    scoring: ScoringConfig,
    now: datetime,
    frequency_prior: float = 0.0,
    threshold_days: int = 180,
) -> dict[str, tuple[OwnerEntry, ...]]:
    from checkowners.analyze import Contribution, score_owners  # noqa: PLC0415

    grouped: dict[str, list[OwnershipEvidence]] = {}
    for record in evidence:
        grouped.setdefault(record.path, []).append(record)
    scored: dict[str, tuple[OwnerEntry, ...]] = {}
    for path, records in grouped.items():
        qualified: dict[str, Contribution] = {}
        blame: dict[str, float] = {}
        review: dict[str, float] = {}
        survival: dict[str, float | None] = {}
        blame_available = False
        review_available = False
        saw_survival = False
        for record in records:
            if record.type == "commit":
                credit = record.credit if record.credit is not None else (record.weight or 0.0)
                qualified[record.subject] = Contribution(
                    commits=record.commits or 0,
                    last_commit=record.timestamp if record.timestamp is not None else now,
                    credit=credit,
                    later_commits=record.later_commits or 0,
                    cadence_days=record.cadence_days,
                )
                if record.survival is not None:
                    survival[record.subject] = record.survival
                    saw_survival = True
            elif record.type == "blame":
                blame_available = True
                blame[record.subject] = 0.0 if record.share is None else record.share
            elif record.type == "review":
                review_available = True
                review[record.subject] = 0.0 if record.share is None else record.share
        if not qualified:
            continue
        max_credit = max(item.credit for item in qualified.values())
        scored[path] = score_owners(
            qualified,
            blame,
            review,
            max_commits=max_credit,
            scoring=scoring,
            now=now,
            blame_available=blame_available,
            review_available=review_available,
            frequency_prior=frequency_prior,
            threshold_days=threshold_days,
            survival=survival if saw_survival else None,
        )
    return scored


def rescore_map(
    ownership: OwnershipMap,
    config: Config,
) -> dict[str, tuple[OwnerEntry, ...]]:
    from checkowners.analyze import frequency_prior_for  # noqa: PLC0415

    evidence = [
        record for path_ownership in ownership.paths.values() for record in path_ownership.evidence
    ]
    return rescore(
        evidence,
        scoring=config.scoring,
        now=ownership.last_analyzed,
        frequency_prior=frequency_prior_for(config.qualification.strategy),
        threshold_days=config.decay.threshold_days,
    )


def evidence_payload(record: OwnershipEvidence) -> dict[str, str | float | int]:
    payload: dict[str, str | float | int] = {
        "type": record.type,
        "subject": record.subject,
    }
    if record.timestamp is not None:
        payload["timestamp"] = record.timestamp.isoformat()
    if record.weight is not None:
        payload["weight"] = record.weight
    if record.share is not None:
        payload["share"] = record.share
    if record.commits is not None:
        payload["commits"] = record.commits
    if record.credit is not None:
        payload["credit"] = record.credit
    if record.cadence_days is not None:
        payload["cadence_days"] = record.cadence_days
    if record.later_commits is not None:
        payload["later_commits"] = record.later_commits
    if record.survival is not None:
        payload["survival"] = record.survival
    return payload


def evidence_from_payload(path: str, raw: object) -> OwnershipEvidence | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    subject = raw.get("subject")
    if not isinstance(kind, str) or not isinstance(subject, str):
        return None
    evidence_type = _evidence_type(kind)
    if evidence_type is None:
        return None
    stored_path = raw.get("path")
    record_path = stored_path if isinstance(stored_path, str) and stored_path else path
    return OwnershipEvidence(
        subject=subject,
        path=record_path,
        type=evidence_type,
        timestamp=_optional_datetime(raw.get("timestamp")),
        weight=_optional_float(raw.get("weight")),
        share=_optional_float(raw.get("share")),
        commits=_optional_int(raw.get("commits")),
        credit=_optional_float(raw.get("credit")),
        cadence_days=_optional_float(raw.get("cadence_days")),
        later_commits=_optional_int(raw.get("later_commits")),
        survival=_optional_float(raw.get("survival")),
    )


def _evidence_type(kind: str) -> EvidenceType | None:
    if kind == "commit":
        return "commit"
    if kind == "blame":
        return "blame"
    if kind == "review":
        return "review"
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class LayeredOwnership:
    component: str
    level: ComponentLevel
    declared_local: tuple[str, ...]
    declared_global: tuple[str, ...]
    observed: tuple[str, ...]


def reconcile_layers(
    root: Path,
    ownership: OwnershipMap,
    config: Config,
) -> tuple[LayeredOwnership, ...]:
    threshold = config.analysis.confidence_threshold
    global_path = find_codeowners_path(root)
    global_rules = parse_rules(_read_text(global_path)) if global_path.is_file() else ()
    owners_files = _owners_files(root)
    nested = _nested_codeowners(root, global_path)
    observed_by_dir: dict[str, list[str]] = {}
    global_by_dir: dict[str, list[str]] = {}
    files: list[LayeredOwnership] = []
    for path in sorted(ownership.paths):
        observed = _observed(ownership.paths[path].owners, threshold)
        declared_global = _global_owners(global_rules, path)
        declared_local = _merge(
            _nearest_owners(path, owners_files),
            _nested_owners(path, nested),
        )
        files.append(
            LayeredOwnership(
                component=path,
                level="file",
                declared_local=declared_local,
                declared_global=declared_global,
                observed=observed,
            )
        )
        directory = Path(path).parent.as_posix()
        if directory == ".":
            continue
        _extend_unique(observed_by_dir.setdefault(directory, []), observed)
        _extend_unique(global_by_dir.setdefault(directory, []), declared_global)

    directories = [
        _layer("directory", directory, owners_files, nested, observed_by_dir, global_by_dir, "")
        for directory in sorted(observed_by_dir)
    ]
    packages = [
        _layer("package", directory, owners_files, nested, observed_by_dir, global_by_dir, "")
        for directory in _package_dirs(root)
    ]
    services = [
        _layer("service", directory, owners_files, nested, observed_by_dir, global_by_dir, owner)
        for directory, owner in _service_dirs(root)
    ]
    repository = LayeredOwnership(
        component=".",
        level="repository",
        declared_local=_owners_in(".", owners_files),
        declared_global=_star_owners(global_rules),
        observed=_unique(
            owner
            for path_ownership in ownership.paths.values()
            for owner in _observed(path_ownership.owners, threshold)
        ),
    )
    return (*files, *directories, *packages, *services, repository)


def _layer(
    level: ComponentLevel,
    directory: str,
    owners_files: tuple[tuple[str, tuple[str, ...]], ...],
    nested: tuple[tuple[str, tuple[CodeownersRule, ...]], ...],
    observed_by_dir: Mapping[str, list[str]],
    global_by_dir: Mapping[str, list[str]],
    extra_local: str,
) -> LayeredOwnership:
    sample = "file" if directory == "." else f"{directory}/file"
    local = _merge(_owners_in(directory, owners_files), _nested_owners(sample, nested))
    if extra_local:
        local = _merge(local, (extra_local,))
    return LayeredOwnership(
        component=directory,
        level=level,
        declared_local=local,
        declared_global=tuple(global_by_dir.get(directory, ())),
        observed=tuple(observed_by_dir.get(directory, ())),
    )


def _observed(owners: tuple[OwnerEntry, ...], threshold: float) -> tuple[str, ...]:
    return tuple(owner.handle for owner in owners if owner.confidence >= threshold)


def _global_owners(rules: tuple[CodeownersRule, ...], path: str) -> tuple[str, ...]:
    winner = match_path(rules, path)
    if winner is None:
        return ()
    return winner.owners


def _star_owners(rules: tuple[CodeownersRule, ...]) -> tuple[str, ...]:
    for rule in reversed(rules):
        if rule.pattern in {"*", "/"}:
            return rule.owners
    return ()


def _nearest_owners(
    path: str,
    owners_files: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[str, ...]:
    parent = Path(path).parent
    while True:
        key = "." if parent.as_posix() == "." else parent.as_posix()
        found = _owners_in(key, owners_files)
        if found or parent.as_posix() == ".":
            return found
        parent = parent.parent


def _nested_owners(
    path: str,
    nested: tuple[tuple[str, tuple[CodeownersRule, ...]], ...],
) -> tuple[str, ...]:
    best: tuple[str, ...] = ()
    for directory, rules in nested:
        if directory != "." and not path.startswith(f"{directory}/"):
            continue
        relative = path[len(directory) + 1 :] if directory != "." else path
        winner: CodeownersRule | None = None
        for rule in rules:
            if pattern_matches(rule.pattern, relative) or pattern_matches(rule.pattern, path):
                winner = rule
        if winner is not None:
            best = winner.owners
    return best


def _owners_in(
    directory: str,
    owners_files: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[str, ...]:
    for name, owners in owners_files:
        if name == directory:
            return owners
    return ()


def _nested_codeowners(
    root: Path,
    global_path: Path,
) -> tuple[tuple[str, tuple[CodeownersRule, ...]], ...]:
    rows: list[tuple[str, tuple[CodeownersRule, ...]]] = []
    global_resolved = global_path.resolve() if global_path.exists() else None
    for path in _named_files(root, "CODEOWNERS"):
        if global_resolved is not None and path.resolve() == global_resolved:
            continue
        directory = "." if path.parent == root else path.parent.relative_to(root).as_posix()
        rows.append((directory, parse_rules(_read_text(path))))
    return tuple(rows)


def _package_dirs(root: Path) -> tuple[str, ...]:
    found: list[str] = []
    for name in sorted(_PACKAGE_FILES):
        for path in _named_files(root, name):
            directory = "." if path.parent == root else path.parent.relative_to(root).as_posix()
            if directory not in found:
                found.append(directory)
    return tuple(sorted(found))


def _service_dirs(root: Path) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for path in _named_files(root, _SERVICE_FILE):
        directory = "." if path.parent == root else path.parent.relative_to(root).as_posix()
        found.append((directory, _catalog_owner(path)))
    return tuple(sorted(found))


def _catalog_owner(path: Path) -> str:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    spec = loaded.get("spec")
    if not isinstance(spec, dict):
        return ""
    owner = spec.get("owner")
    if isinstance(owner, str):
        return owner.strip()
    return ""


def _owners_files(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rows: list[tuple[str, tuple[str, ...]]] = []
    for path in _named_files(root, "OWNERS"):
        directory = "." if path.parent == root else path.parent.relative_to(root).as_posix()
        rows.append((directory, _read_owners_file(path)))
    return tuple(rows)


def _named_files(root: Path, name: str) -> tuple[Path, ...]:
    found = [
        path
        for path in root.rglob(name)
        if path.is_file() and ".git" not in path.relative_to(root).parts
    ]
    return tuple(sorted(found))


def _read_owners_file(path: Path) -> tuple[str, ...]:
    owners: list[str] = []
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0]
        if token.startswith("@") or "@" in token:
            owners.append(token)
    return tuple(owners)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _merge(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return _unique((*left, *right))


def _extend_unique(target: list[str], values: tuple[str, ...]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)

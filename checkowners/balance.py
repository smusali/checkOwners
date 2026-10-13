from __future__ import annotations

import os
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal, TypeGuard

from checkowners.expertise import path_matches_glob
from checkowners.models import Config, OwnerEntry, OwnershipMap
from checkowners.patterns import CodeownersRule, match_path

if TYPE_CHECKING:
    from github import Github

LoadSource = Literal["github_api", "git_authorship", "external"]
LoadSignal = Literal["github_completed_reviews", "git_authorship_proxy", "external"]
LoadBand = Literal["high", "low", "typical"]

RECOMMENDATION_REASON = "qualified co-owner with lower current load"

_MAX_INCREASE_FRACTION: float = 0.30
_OVERLOAD_FACTOR: float = 2.0
_MAX_CLOSED_PRS: int = 200
_REVIEW_MONOPOLY_SHARE: float = 0.72


def signal_for(source: LoadSource) -> LoadSignal:
    if source == "github_api":
        return "github_completed_reviews"
    if source == "external":
        return "external"
    return "git_authorship_proxy"


@dataclass(frozen=True)
class ReviewLoad:
    handle: str
    reviews: int
    reviews_requested: int | None = None
    reviews_completed: int | None = None
    prs_waiting: int | None = None
    median_response_time_hours: float | None = None
    reviewed_lines: int | None = None
    pr_complexity: float | None = None
    ownership_criticality: float | None = None
    expertise: float = 0.0
    load_band: LoadBand | None = None


@dataclass(frozen=True)
class RebalanceSuggestion:
    overloaded: str
    candidate: str
    confidence: float
    proposed_shift: int


@dataclass(frozen=True)
class ReviewConcentration:
    path: str
    share: float
    reviewer: str
    knowledgeable_owners: int


@dataclass(frozen=True)
class RoleDivergence:
    path: str
    writer: str
    reviewer: str
    declared: str


@dataclass(frozen=True)
class BalanceReport:
    loads: tuple[ReviewLoad, ...]
    average: float
    overloaded: tuple[ReviewLoad, ...]
    suggestions: tuple[RebalanceSuggestion, ...]
    source: LoadSource
    fallback_reason: str = ""
    review_concentration: tuple[ReviewConcentration, ...] = ()
    divergence: tuple[RoleDivergence, ...] = ()

    @property
    def load_signal(self) -> LoadSignal:
        return signal_for(self.source)


@dataclass(frozen=True)
class _PersonMetrics:
    reviews_requested: int | None = None
    reviews_completed: int | None = None
    prs_waiting: int | None = None
    median_response_time_hours: float | None = None
    reviewed_lines: int | None = None
    pr_complexity: float | None = None


@dataclass(frozen=True)
class _Gathered:
    source: LoadSource
    fallback_reason: str
    counts: dict[str, int]
    metrics: dict[str, _PersonMetrics]
    path_reviews: dict[str, dict[str, int]]


@dataclass
class _Activity:
    completed: dict[str, int] = field(default_factory=dict)
    requested: dict[str, int] = field(default_factory=dict)
    waiting: dict[str, int] = field(default_factory=dict)
    waiting_known: bool = False
    hours: dict[str, list[float]] = field(default_factory=dict)
    lines: dict[str, int] = field(default_factory=dict)
    sizes: dict[str, list[int]] = field(default_factory=dict)
    by_path: dict[str, dict[str, int]] = field(default_factory=dict)


def analyze_balance(
    ownership: OwnershipMap,
    config: Config,
    *,
    review_counts: dict[str, int] | None = None,
    path_reviews: dict[str, dict[str, int]] | None = None,
    rules: tuple[CodeownersRule, ...] = (),
) -> BalanceReport:
    if review_counts is None:
        gathered = _gather(ownership, config)
    else:
        gathered = _external(review_counts, path_reviews)
    real = gathered.source != "git_authorship"
    loads = _review_loads(
        gathered.counts,
        gathered.metrics,
        ownership,
        config,
        profile=real,
        criticality=gathered.source == "github_api",
    )
    average = _average(loads)
    if real:
        loads = tuple(_with_bands(loads, average))
    overloaded = tuple(load for load in loads if load.load_band == "high")
    suggestions: tuple[RebalanceSuggestion, ...] = ()
    concentration: tuple[ReviewConcentration, ...] = ()
    divergence: tuple[RoleDivergence, ...] = ()
    if real:
        suggestions = _suggest(
            ownership,
            loads,
            overloaded,
            config.analysis.confidence_threshold,
        )
        threshold = config.analysis.confidence_threshold
        concentration = _concentration(ownership, gathered.path_reviews, threshold)
        divergence = _role_divergence(ownership, gathered.path_reviews, rules)
    return BalanceReport(
        loads=loads,
        average=average,
        overloaded=overloaded,
        suggestions=suggestions,
        source=gathered.source,
        fallback_reason=gathered.fallback_reason,
        review_concentration=concentration,
        divergence=divergence,
    )


def _external(
    review_counts: Mapping[str, int],
    path_reviews: Mapping[str, Mapping[str, int]] | None,
) -> _Gathered:
    counts = dict(review_counts)
    paths = (
        {}
        if path_reviews is None
        else {path: dict(per_path) for path, per_path in path_reviews.items()}
    )
    return _Gathered(
        source="external",
        fallback_reason="",
        counts=counts,
        metrics={
            handle: _PersonMetrics(reviews_completed=count) for handle, count in counts.items()
        },
        path_reviews=paths,
    )


def _gather(ownership: OwnershipMap, config: Config) -> _Gathered:
    reason = ""
    if config.github.api_enabled:
        activity, reason = _gather_from_github()
        if activity is not None:
            return _from_activity(activity)
    return _Gathered(
        source="git_authorship",
        fallback_reason=reason,
        counts=_commit_counts(ownership),
        metrics={},
        path_reviews={},
    )


def _from_activity(activity: _Activity) -> _Gathered:
    counts = dict(activity.completed)
    if activity.waiting_known:
        for handle in activity.waiting:
            counts.setdefault(handle, 0)
    metrics = {handle: _metrics_for(handle, activity) for handle in counts}
    return _Gathered(
        source="github_api",
        fallback_reason="",
        counts=counts,
        metrics=metrics,
        path_reviews=activity.by_path,
    )


def _metrics_for(handle: str, activity: _Activity) -> _PersonMetrics:
    waiting = activity.waiting.get(handle, 0) if activity.waiting_known else None
    lines = activity.lines.get(handle)
    return _PersonMetrics(
        reviews_requested=activity.requested.get(handle, 0),
        reviews_completed=activity.completed.get(handle, 0),
        prs_waiting=waiting,
        median_response_time_hours=_median(activity.hours.get(handle, [])),
        reviewed_lines=lines,
        pr_complexity=_median(activity.sizes.get(handle, [])),
    )


def _commit_counts(ownership: OwnershipMap) -> dict[str, int]:
    counts: dict[str, int] = {}
    for po in ownership.paths.values():
        for owner in po.owners:
            counts[owner.handle] = counts.get(owner.handle, 0) + owner.commits
    return counts


def _review_loads(
    counts: Mapping[str, int],
    metrics: Mapping[str, _PersonMetrics],
    ownership: OwnershipMap,
    config: Config,
    *,
    profile: bool,
    criticality: bool,
) -> tuple[ReviewLoad, ...]:
    threshold = config.analysis.confidence_threshold
    handles = sorted(set(counts) | set(metrics), key=str.casefold)
    loads: list[ReviewLoad] = []
    for handle in handles:
        metric = metrics.get(handle)
        weight = _criticality(ownership, config, handle, threshold) if criticality else None
        loads.append(_load(handle, counts.get(handle, 0), metric, ownership, profile, weight))
    return tuple(loads)


def _load(
    handle: str,
    reviews: int,
    metric: _PersonMetrics | None,
    ownership: OwnershipMap,
    profile: bool,
    criticality: float | None,
) -> ReviewLoad:
    return ReviewLoad(
        handle=handle,
        reviews=reviews,
        reviews_requested=None if metric is None else metric.reviews_requested,
        reviews_completed=None if metric is None else metric.reviews_completed,
        prs_waiting=None if metric is None else metric.prs_waiting,
        median_response_time_hours=None if metric is None else metric.median_response_time_hours,
        reviewed_lines=None if metric is None else metric.reviewed_lines,
        pr_complexity=None if metric is None else metric.pr_complexity,
        ownership_criticality=criticality,
        expertise=_expertise(ownership, handle) if profile else 0.0,
    )


def _average(loads: tuple[ReviewLoad, ...]) -> float:
    if not loads:
        return 0.0
    return round(sum(load.reviews for load in loads) / len(loads), 2)


def _with_bands(loads: tuple[ReviewLoad, ...], average: float) -> list[ReviewLoad]:
    known = [load.prs_waiting for load in loads if load.prs_waiting is not None]
    waiting_average = round(sum(known) / len(known), 2) if known else None
    return [replace(load, load_band=_band(load, average, waiting_average)) for load in loads]


def _band(load: ReviewLoad, average: float, waiting_average: float | None) -> LoadBand:
    high = average > 0 and load.reviews >= _OVERLOAD_FACTOR * average
    waiting = load.prs_waiting
    if (
        not high
        and waiting_average is not None
        and waiting_average > 0
        and waiting is not None
        and waiting >= _OVERLOAD_FACTOR * waiting_average
    ):
        high = True
    if high:
        return "high"
    if load.reviews <= average and _waiting_is_low(waiting, waiting_average):
        return "low"
    return "typical"


def _waiting_is_low(waiting: int | None, waiting_average: float | None) -> bool:
    if waiting is None or waiting_average is None:
        return True
    return waiting <= waiting_average


def _suggest(
    ownership: OwnershipMap,
    loads: tuple[ReviewLoad, ...],
    overloaded: tuple[ReviewLoad, ...],
    confidence_threshold: float,
) -> tuple[RebalanceSuggestion, ...]:
    by_handle = {load.handle: load for load in loads}
    high = {load.handle for load in overloaded}
    pairs = _qualified_pairs(ownership, confidence_threshold)
    suggestions: list[RebalanceSuggestion] = []
    for overloaded_load in overloaded:
        chosen = _best_candidate(overloaded_load.handle, pairs, by_handle, high, ownership)
        if chosen is None:
            continue
        candidate, confidence, candidate_reviews = chosen
        allowed = max(1, int(candidate_reviews * _MAX_INCREASE_FRACTION) or 1)
        proposed = min(allowed, max(1, overloaded_load.reviews // 4))
        suggestions.append(
            RebalanceSuggestion(
                overloaded=overloaded_load.handle,
                candidate=candidate,
                confidence=confidence,
                proposed_shift=proposed,
            )
        )
    return tuple(suggestions)


def _best_candidate(
    handle: str,
    pairs: Mapping[str, list[tuple[str, float]]],
    by_handle: Mapping[str, ReviewLoad],
    high: set[str],
    ownership: OwnershipMap,
) -> tuple[str, float, int] | None:
    best_key: tuple[int, float, int, str] | None = None
    best: tuple[str, float, int] | None = None
    for candidate, confidence in pairs.get(handle, []):
        if candidate in high:
            continue
        load = by_handle.get(candidate)
        rank = 0 if load is None or load.load_band == "low" else 1
        expertise = _expertise(ownership, candidate) if load is None else load.expertise
        reviews = 0 if load is None else load.reviews
        key = (rank, -expertise, reviews, candidate.casefold())
        if best_key is None or key < best_key:
            best_key = key
            best = (candidate, confidence, reviews)
    return best


def _qualified_pairs(
    ownership: OwnershipMap,
    confidence_threshold: float,
) -> dict[str, list[tuple[str, float]]]:
    pairs: dict[str, dict[str, float]] = {}
    for po in ownership.paths.values():
        qualified = [
            (owner.handle, owner.confidence)
            for owner in po.owners
            if owner.confidence >= confidence_threshold
        ]
        for handle_a, _ in qualified:
            pairs.setdefault(handle_a, {})
            for handle_b, confidence_b in qualified:
                if handle_a == handle_b:
                    continue
                current = pairs[handle_a].get(handle_b, 0.0)
                if confidence_b > current:
                    pairs[handle_a][handle_b] = confidence_b
    return {
        handle: sorted(neighbors.items(), key=lambda item: -item[1])
        for handle, neighbors in pairs.items()
    }


def _concentration(
    ownership: OwnershipMap,
    path_reviews: Mapping[str, Mapping[str, int]],
    threshold: float,
) -> tuple[ReviewConcentration, ...]:
    found: list[ReviewConcentration] = []
    for path in sorted(path_reviews):
        if path not in ownership.paths:
            continue
        counts = path_reviews[path]
        total = sum(counts.values())
        if total <= 0:
            continue
        reviewer, top = max(counts.items(), key=lambda item: (item[1], item[0]))
        share = top / total
        if share < _REVIEW_MONOPOLY_SHARE:
            continue
        knowledgeable = sum(
            1 for owner in ownership.paths[path].owners if owner.ownership_score >= threshold
        )
        if knowledgeable < 2:
            continue
        found.append(
            ReviewConcentration(
                path=path,
                share=round(share, 4),
                reviewer=reviewer,
                knowledgeable_owners=knowledgeable,
            )
        )
    return tuple(found)


def _role_divergence(
    ownership: OwnershipMap,
    path_reviews: Mapping[str, Mapping[str, int]],
    rules: tuple[CodeownersRule, ...],
) -> tuple[RoleDivergence, ...]:
    found: list[RoleDivergence] = []
    for path in sorted(ownership.paths):
        counts = path_reviews.get(path)
        if counts is None or sum(counts.values()) <= 0:
            continue
        writer = _writer(ownership.paths[path].owners)
        if writer is None:
            continue
        reviewer = max(counts.items(), key=lambda item: (item[1], item[0]))[0]
        if writer.casefold() == reviewer.casefold():
            continue
        rule = match_path(rules, path)
        if rule is None:
            continue
        declared = _other_declared(rule.owners, writer, reviewer)
        if declared is None:
            continue
        found.append(RoleDivergence(path=path, writer=writer, reviewer=reviewer, declared=declared))
    return tuple(found)


def _writer(owners: tuple[OwnerEntry, ...]) -> str | None:
    credited = tuple(owner for owner in owners if owner.commits > 0)
    if not credited:
        return None
    best = max(credited, key=lambda owner: (owner.commits, owner.ownership_score, owner.handle))
    return best.handle


def _other_declared(owners: tuple[str, ...], writer: str, reviewer: str) -> str | None:
    blocked = {writer.casefold(), reviewer.casefold()}
    for name in owners:
        if name.casefold() in blocked:
            continue
        return name
    return None


def _expertise(ownership: OwnershipMap, handle: str) -> float:
    folded = handle.casefold()
    best: float | None = None
    for po in ownership.paths.values():
        for owner in po.owners:
            if owner.handle.casefold() != folded:
                continue
            if best is None or owner.ownership_score > best:
                best = owner.ownership_score
    if best is None:
        return 0.0
    return round(best, 2)


def _criticality(
    ownership: OwnershipMap,
    config: Config,
    handle: str,
    threshold: float,
) -> float | None:
    folded = handle.casefold()
    best: float | None = None
    for path, po in ownership.paths.items():
        qualified = any(
            owner.handle.casefold() == folded and owner.ownership_score >= threshold
            for owner in po.owners
        )
        if not qualified:
            continue
        for pattern, weight in config.criticality:
            if not path_matches_glob(path, pattern):
                continue
            best = weight if best is None else max(best, weight)
    if best is None:
        return None
    return round(best, 4)


def _median(values: list[int] | list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 2)


def _gather_from_github() -> tuple[_Activity | None, str]:
    try:
        from checkowners.github import (  # noqa: PLC0415
            get_github_client,
            get_github_token,
        )
    except ImportError:
        return None, "PyGithub is not installed"
    token = get_github_token()
    if not token:
        return None, "GITHUB_TOKEN is not set"
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo_full_name:
        return None, "GITHUB_REPOSITORY is not set"
    client = get_github_client(token)
    if client is None:
        return None, "GitHub client could not be created"
    return _fetch_review_activity(client, repo_full_name)


def _fetch_review_activity(client: Github, repo_full_name: str) -> tuple[_Activity | None, str]:
    from checkowners.github import (  # noqa: PLC0415
        iter_recent_closed_pulls,
        take_api_request,
    )

    try:
        closed = list(iter_recent_closed_pulls(client, repo_full_name, _MAX_CLOSED_PRS))
    except Exception as exc:
        return None, f"GitHub API error: {exc}"
    activity = _Activity()
    stopped = False
    for pull in closed:
        if not take_api_request():
            stopped = True
            break
        try:
            reviews = list(pull.get_reviews())
        except Exception as exc:
            if activity.completed:
                break
            return None, f"GitHub API error: {exc}"
        _record_closed(activity, pull, reviews)
        if not take_api_request():
            stopped = True
            break
        _record_files(activity, pull, reviews)
    if not activity.completed:
        if stopped:
            return None, "GitHub API budget insufficient"
        return None, f"no PR reviews found in the last {_MAX_CLOSED_PRS} closed PRs"
    _record_open_queue(activity, client, repo_full_name, take_api_request)
    return activity, ""


def _record_open_queue(
    activity: _Activity,
    client: Github,
    repo_full_name: str,
    take: Callable[[], bool],
) -> None:
    from checkowners.github import iter_recent_closed_pulls  # noqa: PLC0415

    pending_waiting: dict[str, int] = {}
    pending_requested: dict[str, int] = {}
    try:
        opened = iter_recent_closed_pulls(
            client,
            repo_full_name,
            _MAX_CLOSED_PRS,
            state="open",
        )
        for pull in opened:
            if not take():
                break
            _record_open(pending_waiting, pending_requested, pull)
    except Exception:
        return
    for handle, count in pending_waiting.items():
        activity.waiting[handle] = activity.waiting.get(handle, 0) + count
    for handle, count in pending_requested.items():
        activity.requested[handle] = activity.requested.get(handle, 0) + count
    activity.waiting_known = True


def _record_closed(activity: _Activity, pull: object, reviews: Iterable[object]) -> None:
    reviewers: set[str] = set()
    earliest: dict[str, datetime] = {}
    for review in reviews:
        handle = _review_handle(review)
        if handle is None:
            continue
        activity.completed[handle] = activity.completed.get(handle, 0) + 1
        reviewers.add(handle)
        submitted = getattr(review, "submitted_at", None)
        if isinstance(submitted, datetime):
            current = earliest.get(handle)
            if current is None or submitted < current:
                earliest[handle] = submitted
    for handle in reviewers:
        activity.requested[handle] = activity.requested.get(handle, 0) + 1
    changed = _changed_lines(pull)
    if changed is not None:
        for handle in reviewers:
            activity.lines[handle] = activity.lines.get(handle, 0) + changed
            activity.sizes.setdefault(handle, []).append(changed)
    created = getattr(pull, "created_at", None)
    if not isinstance(created, datetime):
        return
    for handle, submitted in earliest.items():
        try:
            hours = (submitted - created).total_seconds() / 3600
        except TypeError:
            continue
        if hours >= 0:
            activity.hours.setdefault(handle, []).append(hours)


def _record_files(activity: _Activity, pull: object, reviews: Iterable[object]) -> None:
    names = _filenames(pull)
    if not names:
        return
    reviewers: set[str] = set()
    for review in reviews:
        handle = _review_handle(review)
        if handle is not None:
            reviewers.add(handle)
    for name in names:
        per_path = activity.by_path.setdefault(name, {})
        for handle in reviewers:
            per_path[handle] = per_path.get(handle, 0) + 1


def _record_open(
    waiting: dict[str, int],
    requested: dict[str, int],
    pull: object,
) -> None:
    getter = getattr(pull, "get_reviews", None)
    if not callable(getter):
        return
    try:
        reviews = list(getter())
    except Exception:
        return
    reviewed: set[str] = set()
    for review in reviews:
        handle = _review_handle(review)
        if handle is not None:
            reviewed.add(handle)
    asked = _requested_handles(pull)
    if asked is None:
        return
    for handle in asked:
        if handle in reviewed:
            continue
        waiting[handle] = waiting.get(handle, 0) + 1
        requested[handle] = requested.get(handle, 0) + 1


def _review_handle(review: object) -> str | None:
    user = getattr(review, "user", None)
    if user is None:
        return None
    login = getattr(user, "login", None)
    if not isinstance(login, str) or not login:
        return None
    return f"@{login}"


def _changed_lines(pull: object) -> int | None:
    additions = getattr(pull, "additions", None)
    deletions = getattr(pull, "deletions", None)
    if not _whole_number(additions) or not _whole_number(deletions):
        return None
    return additions + deletions


def _whole_number(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _filenames(pull: object) -> tuple[str, ...]:
    getter = getattr(pull, "get_files", None)
    if not callable(getter):
        return ()
    try:
        raw = getter()
        files = raw if isinstance(raw, list) else list(raw)
    except Exception:
        return ()
    names: list[str] = []
    for changed in files:
        filename = getattr(changed, "filename", None)
        if isinstance(filename, str):
            names.append(filename)
    return tuple(names)


def _requested_handles(pull: object) -> set[str] | None:
    try:
        raw = getattr(pull, "requested_reviewers", None)
    except Exception:
        raw = None
    direct = _handles_from(raw)
    if direct is not None:
        return direct
    getter = getattr(pull, "get_review_requests", None)
    if not callable(getter):
        return None
    try:
        requested = getter()
    except Exception:
        return None
    if isinstance(requested, tuple) and requested:
        return _handles_from(requested[0])
    return _handles_from(requested)


def _handles_from(raw: object) -> set[str] | None:
    if raw is None or isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return None
    handles: set[str] = set()
    for user in raw:
        login = getattr(user, "login", None)
        if isinstance(login, str) and login:
            handles.add(f"@{login}")
    return handles

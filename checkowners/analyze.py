"""Git history analysis for confidence-scored ownership inference."""

from __future__ import annotations

import fnmatch
import math
import os
import re
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from checkowners.expertise import path_matches_glob
from checkowners.github import collection_gaps, review_was_omitted
from checkowners.models import (
    AnalysisCompleteness,
    ConfidenceScore,
    Config,
    DecayWarning,
    FreshnessStatus,
    GitConfig,
    LookbackDays,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    QualificationStrategy,
    ScoringConfig,
    SignalScore,
    history_evidence_gaps,
    merge_gaps,
    with_gaps,
)

_COMMIT_SENTINEL = "COMMIT_START"
FREQUENCY_SHRINKAGE_PRIOR = 3.0
SOURCE_DATE_EPOCH_ENV = "SOURCE_DATE_EPOCH"
MIN_GIT_VERSION = (2, 23, 0)
_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class GitRequirementError(ValueError):
    pass


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: A review provider maps a set of contributor emails to per-path, per-email
#: review-coverage fractions (path -> {email: fraction in [0, 1]}). It is
#: injected so analyze.py itself stays free of network calls; the CLI supplies
#: a GitHub-backed implementation only when the API is enabled.
ReviewProvider = Callable[[set[str]], dict[str, dict[str, float]]]

#: Optional progress hook: called as on_progress(done, total) while blame runs.
#: Injected by the CLI to drive a progress bar; analyze.py stays console-free.
ProgressHook = Callable[[int, int], None]


@dataclass(frozen=True)
class Contribution:
    """Raw per-(path, author) signal aggregated from git log."""

    commits: int
    last_commit: datetime
    later_commits: int = 0
    cadence_days: float | None = None


@dataclass(frozen=True)
class _RawCommit:
    """A single commit's parsed metadata."""

    author: str
    timestamp: datetime
    files: tuple[str, ...]


@dataclass(frozen=True)
class _BlamePass:
    coverage: dict[str, dict[str, float]] = field(default_factory=dict)
    ignore_revs_file: str = ""
    ignore_revs_applied: bool = False
    truncated: bool = False


_BLAME_DEADLINE: ContextVar[float | None] = ContextVar("checkowners_blame_deadline", default=None)


@dataclass(frozen=True)
class _GitAttributeRule:
    pattern: str
    relative_to: str
    values: tuple[tuple[str, bool], ...]


_LINGUIST_ATTRS = frozenset({"linguist-generated", "linguist-vendored"})


def parse_as_of(value: str) -> datetime:
    """Parse an ISO 8601 instant. Naive values are treated as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"Invalid as-of value: {value!r}"
        raise ValueError(msg) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_source_date_epoch(raw: str) -> datetime:
    """Parse SOURCE_DATE_EPOCH as integer POSIX seconds in UTC."""
    try:
        seconds = int(raw)
    except ValueError as exc:
        msg = f"Invalid SOURCE_DATE_EPOCH: {raw!r}"
        raise ValueError(msg) from exc
    return datetime.fromtimestamp(seconds, tz=UTC)


def head_commit_datetime(repo_root: Path) -> datetime:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cI"],  # noqa: S607  # git from PATH; not user-supplied
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
    parsed = _parse_timestamp(result.stdout.strip())
    if parsed is None:
        msg = "HEAD commit has no parseable committer timestamp"
        raise ValueError(msg)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def head_commit_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607  # git from PATH; not user-supplied
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
    sha = result.stdout.strip()
    if not sha:
        msg = "Could not resolve HEAD commit SHA"
        raise ValueError(msg)
    return sha


def resolve_as_of(cli_value: str | None, repo_root: Path) -> datetime:
    """Resolve the analysis instant from CLI, SOURCE_DATE_EPOCH, or HEAD.

    Priority: ``cli_value`` (ISO 8601), then ``SOURCE_DATE_EPOCH``, then the
    HEAD committer timestamp. Never uses the wall clock.
    """
    if cli_value:
        return parse_as_of(cli_value)
    epoch = os.environ.get(SOURCE_DATE_EPOCH_ENV)
    if epoch:
        return parse_source_date_epoch(epoch)
    return head_commit_datetime(repo_root)


def analysis_epoch(when: datetime) -> str:
    aware = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def analyze_ownership(
    repo_root: Path,
    config: Config,
    *,
    review_provider: ReviewProvider | None = None,
    on_progress: ProgressHook | None = None,
    as_of: datetime | None = None,
    analysis_ref: str | None = None,
    max_workers: int | None = None,
    pathspec: tuple[str, ...] | None = None,
    retain_all: bool = False,
) -> OwnershipMap:
    """Analyze git history and return a confidence-scored ownership map.

    When ``review_provider`` is supplied, its per-path, per-email review
    coverage feeds the review signal; otherwise review is unavailable and
    remaining weights are renormalized. ``on_progress`` receives
    (done, total) updates while the per-file blame pass runs.

    ``as_of`` is the instant recency and decay are scored against. When
    omitted, ``resolve_as_of(None, repo_root)`` supplies it (SOURCE_DATE_EPOCH
    or HEAD, never the wall clock). ``analysis_ref`` is the commit SHA; when
    omitted, HEAD is used. ``pathspec`` limits git log and blame to those
    paths. ``retain_all`` stores every scored author on ``candidates``;
    inferred ``owners`` still apply the confidence threshold and top-N cap.
    """
    when = as_of if as_of is not None else resolve_as_of(None, repo_root)
    ref = analysis_ref if analysis_ref is not None else head_commit_sha(repo_root)
    deadline = time.monotonic() + config.analysis.max_runtime_seconds
    deadline_token = _BLAME_DEADLINE.set(deadline)
    try:
        return _analyze_ownership(
            repo_root,
            config,
            when=when,
            ref=ref,
            review_provider=review_provider,
            on_progress=on_progress,
            max_workers=max_workers,
            pathspec=pathspec,
            retain_all=retain_all,
            deadline=deadline,
        )
    finally:
        _BLAME_DEADLINE.reset(deadline_token)


def _analyze_ownership(
    repo_root: Path,
    config: Config,
    *,
    when: datetime,
    ref: str,
    review_provider: ReviewProvider | None,
    on_progress: ProgressHook | None,
    max_workers: int | None,
    pathspec: tuple[str, ...] | None,
    retain_all: bool,
    deadline: float,
) -> OwnershipMap:
    mailmap_path = _resolve_mailmap_file(repo_root)
    commits = _get_commit_history(
        repo_root,
        lookback_start(config.analysis.lookback_days, when),
        when,
        use_mailmap=config.git.use_mailmap,
        pathspec=pathspec,
    )
    contributions = _aggregate_contributions(commits)
    if pathspec:
        contributions = _filter_to_pathspec(contributions, pathspec)
    linguist = (
        _linguist_excluded_paths(repo_root, contributions)
        if config.analysis.respect_gitattributes
        else frozenset()
    )
    gitattributes_n = sum(1 for path in contributions if path in linguist)
    contributions = {
        path: authors for path, authors in contributions.items() if path not in linguist
    }
    before_static = len(contributions)
    contributions = _filter_excluded(contributions, config.paths.exclude)
    static_n = before_static - len(contributions)
    contributions = _filter_nonexistent(contributions, repo_root)
    if config.analysis.exclude_bots:
        contributions = _filter_bot_authors(contributions)
    if config.qualification.strategy == "threshold":
        contributions = _filter_unqualified(contributions, config.analysis.min_commits)
    if time.monotonic() >= deadline:
        blame_pass = _BlamePass(truncated=True)
    else:
        blame_pass = gather_blame_coverage(
            contributions.keys(),
            repo_root,
            git=config.git,
            on_progress=on_progress,
            max_workers=max_workers,
        )
    review_coverage = _gather_review_coverage(contributions, review_provider)
    review_omitted = review_was_omitted()
    paths = _build_path_ownerships(
        contributions,
        blame_pass.coverage,
        review_coverage,
        config,
        when,
        review_available=review_provider is not None and not review_omitted,
        retain_all=retain_all,
    )
    completeness = _run_completeness(
        repo_root,
        config,
        when=when,
        commits=commits,
        mailmap_path=mailmap_path,
        blame_pass=blame_pass,
        gitattributes_n=gitattributes_n,
        static_n=static_n,
        review_missing=config.scoring.review_weight > 0
        and review_provider is None
        and not review_omitted,
    )
    return apply_completeness(
        OwnershipMap(
            paths=dict(sorted(paths.items())),
            last_analyzed=when,
            analysis_ref=ref,
            analysis_completeness=completeness,
        ),
        completeness,
        config.scoring,
    )


def _run_completeness(
    repo_root: Path,
    config: Config,
    *,
    when: datetime,
    commits: list[_RawCommit],
    mailmap_path: Path | None,
    blame_pass: _BlamePass,
    gitattributes_n: int,
    static_n: int,
    review_missing: bool,
) -> AnalysisCompleteness:
    since = lookback_start(config.analysis.lookback_days, when)
    gaps = history_evidence_gaps(
        shallow=_is_shallow_repository(repo_root),
        insufficient=_insufficient_history(repo_root, commits),
        renamed=_window_has_renames(repo_root, since, when),
        mailmap_missing=config.git.use_mailmap and mailmap_path is None,
        ignore_revs_missing=_resolve_ignore_revs_file(repo_root, config.git.blame_ignore_revs_file)
        is None,
        excluded_gitattributes=gitattributes_n,
        excluded_static=static_n,
        review_missing=review_missing,
        runtime_truncated=blame_pass.truncated,
    )
    base = AnalysisCompleteness(
        ignore_revs_applied=blame_pass.ignore_revs_applied,
        ignore_revs_file=blame_pass.ignore_revs_file,
        mailmap_applied=mailmap_path is not None and config.git.use_mailmap,
        mailmap_file=(_display_ignore_revs_path(repo_root, mailmap_path) if mailmap_path else ""),
        excluded_gitattributes=gitattributes_n,
        excluded_static=static_n,
    )
    return with_gaps(base, merge_gaps(gaps, collection_gaps()))


def apply_completeness(
    ownership: OwnershipMap,
    completeness: AnalysisCompleteness,
    scoring: ScoringConfig,
) -> OwnershipMap:
    """Return `ownership` with evidence quality scaled by `completeness.score`."""
    factor = completeness.score if completeness.score is not None else 1.0
    weights = signal_weights(scoring)
    reliabilities = signal_reliabilities(scoring)

    def scale(owner: OwnerEntry) -> OwnerEntry:
        breakdown = owner.score_breakdown
        if breakdown is None:
            return owner
        _, quality = combine_available_signals(
            {
                "recency": (breakdown.recency.score, breakdown.recency.available),
                "frequency": (breakdown.frequency.score, breakdown.frequency.available),
                "blame": (breakdown.blame.score, breakdown.blame.available),
                "review": (breakdown.review.score, breakdown.review.available),
            },
            weights,
            reliabilities,
        )
        return replace(owner, evidence_quality=_clamp(quality * factor))

    paths = {
        path: replace(
            path_ownership,
            owners=tuple(scale(owner) for owner in path_ownership.owners),
            candidates=tuple(scale(owner) for owner in path_ownership.candidates),
            scored_owners=tuple(scale(owner) for owner in path_ownership.scored_owners),
        )
        for path, path_ownership in ownership.paths.items()
    }
    return replace(ownership, paths=paths, analysis_completeness=completeness)


def _git_stdout(repo_root: Path, argv: list[str]) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _is_shallow_repository(repo_root: Path) -> bool:
    stdout = _git_stdout(repo_root, ["git", "rev-parse", "--is-shallow-repository"])
    return stdout is not None and stdout.strip() == "true"


def _insufficient_history(repo_root: Path, commits: list[_RawCommit]) -> bool:
    if commits:
        return False
    stdout = _git_stdout(repo_root, ["git", "rev-parse", "--verify", "HEAD"])
    if stdout is None:
        return False
    return _FULL_SHA_RE.fullmatch(stdout.strip()) is not None


def _stdout_has_rename(stdout: str) -> bool:
    for line in stdout.splitlines():
        if line.startswith("R") and "\t" in line:
            status = line.split("\t", 1)[0]
            if status == "R" or (len(status) > 1 and status[1:].isdigit()):
                return True
    return False


def _window_has_renames(repo_root: Path, since: datetime | None, until: datetime) -> bool:
    argv = [
        "git",
        "log",
        "--diff-filter=R",
        "--name-status",
        "--pretty=format:",
    ]
    if since is not None:
        argv.append(f"--since={since.isoformat()}")
    argv.append(f"--until={until.isoformat()}")
    stdout = _git_stdout(
        repo_root,
        argv,
    )
    if not stdout:
        return False
    return _stdout_has_rename(stdout)


def _gather_review_coverage(
    contributions: dict[str, dict[str, Contribution]],
    review_provider: ReviewProvider | None,
) -> dict[str, dict[str, float]]:
    """Collect per-path, per-email review coverage from the injected provider."""
    if review_provider is None:
        return {}
    emails = {author for authors in contributions.values() for author in authors}
    if not emails:
        return {}
    return review_provider(emails)


def _build_path_ownerships(
    contributions: dict[str, dict[str, Contribution]],
    blame_coverage: dict[str, dict[str, float]],
    review_coverage: dict[str, dict[str, float]],
    config: Config,
    now: datetime,
    *,
    review_available: bool,
    retain_all: bool = False,
) -> dict[str, PathOwnership]:
    """Compute scored owners + qualified owner count + decay per path."""
    result: dict[str, PathOwnership] = {}
    frequency_prior = frequency_prior_for(config.qualification.strategy)
    for path, authors in contributions.items():
        path_blame = blame_coverage.get(path, {})
        qualified = _qualify_authors(
            authors,
            min_commits=config.analysis.min_commits,
            strategy=config.qualification.strategy,
            path_blame=path_blame,
            blame_override=config.qualification.strong_blame_override,
        )
        if not qualified:
            continue
        max_commits = max(c.commits for c in qualified.values())
        path_review = review_coverage.get(path, {})
        entries = score_owners(
            qualified,
            path_blame,
            path_review,
            max_commits=max_commits,
            scoring=config.scoring,
            now=now,
            blame_available=path in blame_coverage,
            review_available=review_available,
            frequency_prior=frequency_prior,
            threshold_days=config.decay.threshold_days,
        )
        filtered = tuple(e for e in entries if e.confidence >= config.analysis.confidence_threshold)
        if not filtered and not retain_all:
            continue
        top = filtered[: config.analysis.top_n_owners]
        decay = _detect_decay(path, qualified, top, config.decay.threshold_days, now)
        qualified_owner_count = _count_qualified_owners(top, config.analysis.confidence_threshold)
        result[path] = PathOwnership(
            owners=top,
            qualified_owner_count=qualified_owner_count,
            decay_warnings=decay,
            candidates=entries if retain_all else (),
            scored_owners=filtered,
        )
    return dict(sorted(result.items()))


def combine_available_signals(
    signals: Mapping[str, tuple[float, bool]],
    weights: Mapping[str, float],
    reliabilities: Mapping[str, float],
) -> tuple[float, float]:
    """Return ``(ownership_score, evidence_quality)`` over available signals.

    ``ownership_score`` is the weighted mean of available scores so the range
    is always ``[0, 1]``. ``evidence_quality`` is the share of configured
    weight that was observed, scaled by each signal's reliability.
    """
    score_num = 0.0
    score_den = 0.0
    quality_num = 0.0
    quality_den = 0.0
    for name, (score, available) in signals.items():
        weight = weights.get(name, 0.0)
        quality_den += weight
        if not available:
            continue
        score_num += weight * score
        score_den += weight
        quality_num += weight * reliabilities.get(name, 1.0)
    ownership_score = _clamp(score_num / score_den) if score_den else 0.0
    evidence_quality = _clamp(quality_num / quality_den) if quality_den else 0.0
    return ownership_score, evidence_quality


def signal_weights(scoring: ScoringConfig) -> dict[str, float]:
    return {
        "recency": scoring.recency_weight,
        "frequency": scoring.frequency_weight,
        "blame": scoring.blame_weight,
        "review": scoring.review_weight,
    }


def signal_reliabilities(scoring: ScoringConfig) -> dict[str, float]:
    return {
        "recency": scoring.recency_reliability,
        "frequency": scoring.frequency_reliability,
        "blame": scoring.blame_reliability,
        "review": scoring.review_reliability,
    }


def frequency_prior_for(strategy: QualificationStrategy) -> float:
    return FREQUENCY_SHRINKAGE_PRIOR if strategy == "adaptive" else 0.0


def _qualify_authors(
    authors: Mapping[str, Contribution],
    *,
    min_commits: int,
    strategy: QualificationStrategy,
    path_blame: Mapping[str, float],
    blame_override: float,
) -> dict[str, Contribution]:
    if strategy == "threshold":
        return {
            author: contrib for author, contrib in authors.items() if contrib.commits >= min_commits
        }
    return {
        author: contrib
        for author, contrib in authors.items()
        if contrib.commits >= min_commits or path_blame.get(author, 0.0) >= blame_override
    }


def score_owners(
    qualified: dict[str, Contribution],
    path_blame: dict[str, float],
    path_review: dict[str, float],
    *,
    max_commits: int,
    scoring: ScoringConfig,
    now: datetime,
    blame_available: bool,
    review_available: bool,
    frequency_prior: float = 0.0,
    threshold_days: int = 180,
) -> tuple[OwnerEntry, ...]:
    """Return owners of `qualified` scored at `now`."""
    if not qualified:
        return ()
    scored: list[OwnerEntry] = []
    weights = signal_weights(scoring)
    reliabilities = signal_reliabilities(scoring)
    path_last = max(contrib.last_commit for contrib in qualified.values())
    for author, contrib in qualified.items():
        half_life = effective_half_life(contrib.cadence_days, scoring)
        recency = _recency_score(contrib.last_commit, now, half_life)
        frequency = _frequency_score(contrib.commits, max_commits, frequency_prior)
        blame = path_blame.get(author, 0.0) if blame_available else 0.0
        review = _clamp(path_review.get(author, 0.0)) if review_available else 0.0
        signals = {
            "recency": (recency, True),
            "frequency": (frequency, True),
            "blame": (blame, blame_available),
            "review": (review, review_available),
        }
        total, quality = combine_available_signals(signals, weights, reliabilities)
        breakdown = ConfidenceScore(
            total=total,
            recency=SignalScore(available=True, score=recency),
            frequency=SignalScore(available=True, score=frequency),
            blame=SignalScore(available=blame_available, score=blame),
            review=SignalScore(available=review_available, score=review),
        )
        days = _elapsed_days(contrib.last_commit, now)
        scored.append(
            OwnerEntry(
                handle=author,
                ownership_score=total,
                last_commit=contrib.last_commit,
                commits=contrib.commits,
                evidence_quality=quality,
                score_breakdown=breakdown,
                freshness=OwnershipFreshness(
                    active_expertise=recency,
                    historical_expertise=_historical_expertise(
                        contrib.commits, contrib.later_commits
                    ),
                    maintenance_recency=_recency_score(path_last, now, half_life),
                    status=_classify_freshness(
                        contrib,
                        days=days,
                        half_life_days=half_life,
                        path_last=path_last,
                        ceiling_days=scoring.recency_half_life_ceiling_days,
                        threshold_days=threshold_days,
                    ),
                    half_life_days=half_life,
                ),
            )
        )
    scored.sort(key=lambda e: (-e.confidence, e.handle))
    return tuple(scored)


def lookback_start(lookback: LookbackDays, when: datetime) -> datetime | None:
    """Return the window start for `lookback` at `when`, or None when adaptive."""
    if lookback == "adaptive":
        return None
    return when - timedelta(days=lookback)


def effective_half_life(cadence_days: float | None, scoring: ScoringConfig) -> float:
    """Return the recency half-life in days for `cadence_days` under `scoring`."""
    if scoring.recency_strategy == "fixed" or cadence_days is None:
        return _as_days(scoring.recency_half_life_days)
    return _bounded_half_life(
        cadence_days,
        scoring.recency_half_life_floor_days,
        scoring.recency_half_life_ceiling_days,
    )


def _as_days(value: int) -> float:
    return value + 0.0


def _bounded_half_life(cadence_days: float, floor_days: int, ceiling_days: int) -> float:
    low = _as_days(floor_days)
    high = _as_days(ceiling_days)
    if cadence_days < low:
        return low
    if cadence_days > high:
        return high
    return cadence_days


def _historical_expertise(commits: int, later_commits: int) -> float:
    total = commits + later_commits
    if total <= 0:
        return 0.0
    return _clamp(commits / total)


def _classify_freshness(
    contrib: Contribution,
    *,
    days: int,
    half_life_days: float,
    path_last: datetime,
    ceiling_days: int,
    threshold_days: int,
) -> FreshnessStatus:
    if contrib.later_commits > contrib.commits:
        return "superseded"
    if contrib.cadence_days is None:
        if days > threshold_days:
            return "inactive"
        return "stable"
    if days <= half_life_days:
        return "stable"
    if contrib.last_commit == path_last and days <= ceiling_days:
        return "stable"
    return "inactive"


def _recency_score(last_commit: datetime, now: datetime, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    delta_days = max(0.0, (now - last_commit).total_seconds() / 86400.0)
    return _clamp(math.pow(0.5, delta_days / half_life_days))


def _elapsed_days(last_commit: datetime, now: datetime) -> int:
    seconds = (now - last_commit).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 86400)


def _frequency_score(commits: int, max_commits: int, prior: float = 0.0) -> float:
    if max_commits <= 0:
        return 0.0
    return _clamp(commits / (max_commits + prior))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _detect_decay(
    path: str,
    qualified: dict[str, Contribution],
    top: tuple[OwnerEntry, ...],
    threshold_days: int,
    now: datetime,
) -> tuple[DecayWarning, ...]:
    warnings: list[DecayWarning] = []
    for entry in top:
        contrib = qualified.get(entry.handle)
        if contrib is None:
            continue
        status = _warning_status(entry, contrib, threshold_days, now)
        if status is None:
            continue
        warnings.append(
            DecayWarning(
                handle=entry.handle,
                path=path,
                last_commit=contrib.last_commit,
                days_since_last_commit=_elapsed_days(contrib.last_commit, now),
                historical_confidence=entry.confidence,
                status=status,
            )
        )
    return tuple(warnings)


def _warning_status(
    entry: OwnerEntry,
    contrib: Contribution,
    threshold_days: int,
    now: datetime,
) -> FreshnessStatus | None:
    freshness = entry.freshness
    if freshness is not None:
        if freshness.status == "stable":
            return None
        return freshness.status
    if _elapsed_days(contrib.last_commit, now) <= threshold_days:
        return None
    return "inactive"


def _count_qualified_owners(top: tuple[OwnerEntry, ...], threshold: float) -> int:
    return sum(1 for entry in top if entry.confidence >= threshold)


_MAILMAP_FLAGS = frozenset({"--use-mailmap", "--no-use-mailmap"})


def mailmap_flag(enabled: bool) -> str:
    return "--use-mailmap" if enabled else "--no-use-mailmap"


@contextmanager
def _suppress_workdir_mailmap(repo_root: Path, *, enabled: bool) -> Iterator[None]:
    visible = repo_root / ".mailmap"
    if enabled or not visible.is_file():
        yield
        return
    hidden = repo_root / ".mailmap.checkowners-hidden"
    visible.replace(hidden)
    try:
        yield
    finally:
        hidden.replace(visible)


def _filter_to_pathspec(
    contributions: dict[str, dict[str, Contribution]],
    pathspec: tuple[str, ...],
) -> dict[str, dict[str, Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if any(path_matches_glob(path, spec) for spec in pathspec)
    }


def _get_commit_history(
    repo_root: Path,
    since: datetime | None,
    as_of: datetime,
    *,
    use_mailmap: bool = True,
    pathspec: tuple[str, ...] | None = None,
) -> list[_RawCommit]:
    """Return parsed commits from `since` until `as_of`. `since` None omits the lower bound."""
    email_fmt = "%aE" if use_mailmap else "%ae"
    argv = [  # git from PATH; not user-supplied
        "git",
        "log",
        mailmap_flag(use_mailmap),
        f"--format={_COMMIT_SENTINEL}%n{email_fmt}%n%cI",
        "--name-only",
    ]
    if since is not None:
        argv.append(f"--since={since.isoformat()}")
    argv.append(f"--until={as_of.isoformat()}")
    if pathspec:
        argv.append("--")
        argv.extend(pathspec)
    result = subprocess.run(  # noqa: S603  # literal git argv, no shell
        argv,
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
    return _parse_log_output(result.stdout)


def _parse_log_output(stdout: str) -> list[_RawCommit]:
    if not stdout.strip():
        return []
    chunks = stdout.split(_COMMIT_SENTINEL)
    commits: list[_RawCommit] = []
    for chunk in chunks:
        lines = [line for line in chunk.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        author = lines[0].strip()
        timestamp = _parse_timestamp(lines[1].strip())
        if timestamp is None:
            continue
        files = tuple(line for line in lines[2:] if line)
        if files:
            commits.append(_RawCommit(author=author, timestamp=timestamp, files=files))
    return commits


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _aggregate_contributions(
    commits: list[_RawCommit],
) -> dict[str, dict[str, Contribution]]:
    """Aggregate per-(path, author) counts, cadence, and commits that followed each author."""
    events: dict[str, list[tuple[str, datetime]]] = {}
    for commit in commits:
        for file_path in commit.files:
            events.setdefault(file_path, []).append((commit.author, commit.timestamp))
    result: dict[str, dict[str, Contribution]] = {}
    for path in sorted(events):
        path_events = events[path]
        counts: dict[str, int] = {}
        latest: dict[str, datetime] = {}
        for author, timestamp in path_events:
            counts[author] = counts.get(author, 0) + 1
            prior = latest.get(author)
            if prior is None or timestamp > prior:
                latest[author] = timestamp
        cadence = _cadence_days([timestamp for _author, timestamp in path_events])
        result[path] = {
            author: Contribution(
                commits=commits_n,
                last_commit=latest[author],
                later_commits=sum(
                    1
                    for other, timestamp in path_events
                    if other != author and timestamp > latest[author]
                ),
                cadence_days=cadence,
            )
            for author, commits_n in sorted(counts.items())
        }
    return result


def _cadence_days(timestamps: list[datetime]) -> float | None:
    unique = sorted(set(timestamps))
    if len(unique) < 2:
        return None
    gaps = [(later - earlier).total_seconds() / 86400.0 for earlier, later in pairwise(unique)]
    return statistics.median(gaps)


def _filter_excluded(
    contributions: dict[str, dict[str, Contribution]],
    exclude_patterns: tuple[str, ...],
) -> dict[str, dict[str, Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if not _is_excluded(path, exclude_patterns)
    }


def _filter_nonexistent(
    contributions: dict[str, dict[str, Contribution]],
    repo_root: Path,
) -> dict[str, dict[str, Contribution]]:
    return {path: authors for path, authors in contributions.items() if (repo_root / path).exists()}


def _is_bot_email(email: str) -> bool:
    """True for automation authors (GitHub Apps sign as `name[bot]@...`)."""
    lowered = email.lower()
    return "[bot]" in lowered or lowered == "actions@github.com" or lowered.startswith("bot@")


def _filter_bot_authors(
    contributions: dict[str, dict[str, Contribution]],
) -> dict[str, dict[str, Contribution]]:
    result: dict[str, dict[str, Contribution]] = {}
    for path, authors in contributions.items():
        humans = {a: c for a, c in authors.items() if not _is_bot_email(a)}
        if humans:
            result[path] = humans
    return result


def _filter_unqualified(
    contributions: dict[str, dict[str, Contribution]],
    min_commits: int,
) -> dict[str, dict[str, Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if any(contrib.commits >= min_commits for contrib in authors.values())
    }


def _is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _parse_attribute_token(token: str) -> tuple[str, bool] | None:
    if token.startswith("-"):
        name = token[1:]
        return (name, False) if name in _LINGUIST_ATTRS else None
    if "=" in token:
        name, _, value = token.partition("=")
        if name not in _LINGUIST_ATTRS:
            return None
        return (name, value.lower() != "false")
    if token in _LINGUIST_ATTRS:
        return (token, True)
    return None


def _parse_gitattributes(content: str, relative_to: str) -> tuple[_GitAttributeRule, ...]:
    rules: list[_GitAttributeRule] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        values: list[tuple[str, bool]] = []
        for token in parts[1:]:
            parsed = _parse_attribute_token(token)
            if parsed is not None:
                values.append(parsed)
        if values:
            rules.append(
                _GitAttributeRule(
                    pattern=parts[0],
                    relative_to=relative_to,
                    values=tuple(values),
                )
            )
    return tuple(rules)


def _translate_gitattributes_segment(segment: str) -> str:
    out: list[str] = []
    for char in segment:
        if char == "*":
            out.append(r"[^/]*")
        elif char == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(char))
    return "".join(out)


def _translate_gitattributes_pattern(pattern: str) -> str:
    p = pattern.rstrip("/") if pattern.endswith("/") else pattern
    anchored = p.startswith("/")
    p = p.lstrip("/")
    if "/" in p:
        anchored = True
    if not p:
        return r"\A\Z"
    segments = p.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        if segment == "**":
            parts.append(r"(?:[^/]+/)*" if index < len(segments) - 1 else r".*")
            continue
        escaped = _translate_gitattributes_segment(segment)
        parts.append(escaped + ("/" if index < len(segments) - 1 else ""))
    body = "".join(parts)
    prefix = r"" if anchored else r"(?:[^/]+/)*"
    return prefix + body + r"\Z"


@lru_cache(maxsize=4096)
def _compile_gitattributes_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(_translate_gitattributes_pattern(pattern))


def _gitattributes_pattern_matches(pattern: str, path: str, relative_to: str) -> bool:
    if relative_to:
        prefix = f"{relative_to}/"
        if not path.startswith(prefix):
            return False
        local = path[len(prefix) :]
    else:
        local = path
    if not local:
        return False
    return _compile_gitattributes_pattern(pattern).match(local) is not None


def _ancestor_dirs(path: str) -> tuple[str, ...]:
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if not parent:
        return ("",)
    dirs = [""]
    current = ""
    for part in parent.split("/"):
        current = f"{current}/{part}" if current else part
        dirs.append(current)
    return tuple(dirs)


def _gitattributes_rules_for(
    repo_root: Path,
    rel_dir: str,
    cache: dict[str, tuple[_GitAttributeRule, ...]],
) -> tuple[_GitAttributeRule, ...]:
    if rel_dir in cache:
        return cache[rel_dir]
    attrs_path = repo_root / rel_dir / ".gitattributes" if rel_dir else repo_root / ".gitattributes"
    try:
        rules = (
            _parse_gitattributes(attrs_path.read_text(encoding="utf-8"), rel_dir)
            if attrs_path.is_file()
            else ()
        )
    except OSError:
        rules = ()
    cache[rel_dir] = rules
    return rules


def _path_has_linguist_attr(
    path: str,
    repo_root: Path,
    cache: dict[str, tuple[_GitAttributeRule, ...]],
) -> bool:
    flags: dict[str, bool] = {}
    for rel_dir in _ancestor_dirs(path):
        for rule in _gitattributes_rules_for(repo_root, rel_dir, cache):
            if not _gitattributes_pattern_matches(rule.pattern, path, rule.relative_to):
                continue
            for name, is_set in rule.values:
                flags[name] = is_set
    return flags.get("linguist-generated") is True or flags.get("linguist-vendored") is True


def _linguist_excluded_paths(repo_root: Path, paths: Iterable[str]) -> frozenset[str]:
    pending = tuple(paths)
    if not pending:
        return frozenset()
    cache: dict[str, tuple[_GitAttributeRule, ...]] = {}
    return frozenset(path for path in pending if _path_has_linguist_attr(path, repo_root, cache))


def parse_git_version(raw: str) -> tuple[int, int, int]:
    """Parse `git version` stdout into (major, minor, patch)."""
    match = _GIT_VERSION_RE.search(raw)
    if match is None:
        msg = f"Could not parse git version from {raw!r}"
        raise ValueError(msg)
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def _ensure_git_version(repo_root: Path) -> None:
    result = subprocess.run(
        ["git", "version"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
    try:
        found = parse_git_version(result.stdout)
    except ValueError as exc:
        raise GitRequirementError(str(exc)) from exc
    if found < MIN_GIT_VERSION:
        pretty = ".".join(str(part) for part in found)
        required = ".".join(str(part) for part in MIN_GIT_VERSION)
        msg = f"checkOwners requires Git {required} or newer; found {pretty}"
        raise GitRequirementError(msg)


def _git_config_value(repo_root: Path, key: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "config", "--get", key],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=True,
        )
    except subprocess.CalledProcessError:
        return ""
    return result.stdout.strip()


def _existing_file(repo_root: Path, raw: str) -> Path | None:
    if not raw or "\n" in raw or "\x00" in raw:
        return None
    try:
        candidate = Path(raw)
        resolved = candidate if candidate.is_absolute() else repo_root / candidate
        if resolved.is_file():
            return resolved
    except OSError:
        return None
    return None


def _resolve_ignore_revs_file(repo_root: Path, configured: str) -> Path | None:
    found = _existing_file(repo_root, configured)
    if found is not None:
        return found
    return _existing_file(repo_root, _git_config_value(repo_root, "blame.ignoreRevsFile"))


def _display_ignore_revs_path(repo_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


def _resolve_mailmap_file(repo_root: Path) -> Path | None:
    candidate = repo_root / ".mailmap"
    if candidate.is_file():
        return candidate
    return None


def _tracked_file_count(repo_root: Path) -> int:
    result = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
    return sum(1 for line in result.stdout.splitlines() if line)


def _parse_mass_refactor_revs(stdout: str, tracked: int, fraction: float) -> tuple[str, ...]:
    found: list[str] = []
    current_sha = ""
    modified = 0

    def flush() -> None:
        nonlocal current_sha, modified
        if current_sha and tracked > 0 and modified / tracked >= fraction:
            found.append(current_sha)
        current_sha = ""
        modified = 0

    for line in stdout.splitlines():
        if not line:
            flush()
            continue
        if _FULL_SHA_RE.fullmatch(line):
            flush()
            current_sha = line
            continue
        if line.startswith("M\t") or line.startswith("M "):
            modified += 1
    flush()
    return tuple(found)


def _mass_refactor_revs(repo_root: Path, fraction: float) -> tuple[str, ...]:
    if fraction <= 0.0:
        return ()
    tracked = _tracked_file_count(repo_root)
    if tracked == 0:
        return ()
    result = subprocess.run(
        ["git", "log", "--name-status", "--pretty=format:%H"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
    return _parse_mass_refactor_revs(result.stdout, tracked, fraction)


def _write_ignore_revs(shas: tuple[str, ...]) -> Path:
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        encoding="utf-8",
        suffix=".revs",
        delete=False,
    )
    with handle:
        handle.write("\n".join(shas) + "\n")
    return Path(handle.name)


def _blame_argv(
    git: GitConfig,
    ignore_revs: Path | None,
    extra_revs: Path | None,
) -> list[str]:
    argv = ["git", "blame", "--line-porcelain", "-w", mailmap_flag(git.use_mailmap)]
    if git.detect_moves:
        argv.extend(["-M", "-C"])
    if ignore_revs is not None:
        argv.append(f"--ignore-revs-file={ignore_revs}")
    if extra_revs is not None:
        argv.append(f"--ignore-revs-file={extra_revs}")
    return argv


def gather_blame_coverage(
    paths: Iterable[str],
    repo_root: Path,
    *,
    git: GitConfig | None = None,
    on_progress: ProgressHook | None = None,
    max_workers: int | None = None,
) -> _BlamePass:
    """Run git blame per path (in parallel) and return author coverage plus ignore-revs status."""
    git_config = git if git is not None else GitConfig()
    path_list = sorted(paths)
    total = len(path_list)
    if total == 0:
        return _BlamePass()
    _ensure_git_version(repo_root)
    ignore_revs = _resolve_ignore_revs_file(repo_root, git_config.blame_ignore_revs_file)
    extra_path: Path | None = None
    try:
        mass = _mass_refactor_revs(repo_root, git_config.mass_refactor_file_fraction)
        if mass:
            extra_path = _write_ignore_revs(mass)
        argv = _blame_argv(git_config, ignore_revs, extra_path)
        if not _blame_accepts_mailmap_flags(repo_root):
            argv = _drop_mailmap_flags(argv)
        coverage: dict[str, dict[str, float]] = {}
        workers = max_workers if max_workers is not None else min(32, os.cpu_count() or 4)
        workers = max(workers, 1)
        done = 0
        truncated = False
        deadline = _BLAME_DEADLINE.get()
        with (
            _suppress_workdir_mailmap(repo_root, enabled=git_config.use_mailmap),
            ThreadPoolExecutor(max_workers=workers) as pool,
        ):
            pending: list[tuple[str, Future[dict[str, float]]]] = []
            for path in path_list:
                if deadline is not None and time.monotonic() >= deadline:
                    truncated = True
                    break
                pending.append((path, pool.submit(_blame_for_path, repo_root, path, argv)))
            for path, future in pending:
                per_author = future.result()
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
                if per_author:
                    coverage[path] = per_author
        display = _display_ignore_revs_path(repo_root, ignore_revs) if ignore_revs else ""
        return _BlamePass(
            coverage=dict(sorted(coverage.items())),
            ignore_revs_file=display,
            ignore_revs_applied=ignore_revs is not None,
            truncated=truncated,
        )
    finally:
        if extra_path is not None:
            extra_path.unlink(missing_ok=True)


def _drop_mailmap_flags(argv: list[str]) -> list[str]:
    return [part for part in argv if part not in _MAILMAP_FLAGS]


def _blame_accepts_mailmap_flags(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "blame", "--use-mailmap"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    return "unknown option" not in f"{result.stdout}{result.stderr}"


def _blame_for_path(repo_root: Path, path: str, argv: list[str]) -> dict[str, float]:
    """Run git blame with the prebuilt argv on one path; return coverage fractions."""
    try:
        result = subprocess.run(  # noqa: S603
            [*argv, "--", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
            check=True,
        )
    except subprocess.CalledProcessError:
        return {}
    return _parse_blame_output(result.stdout)


def _parse_blame_output(stdout: str) -> dict[str, float]:
    counts: dict[str, int] = {}
    total = 0
    for line in stdout.splitlines():
        if line.startswith("author-mail "):
            email = line[len("author-mail ") :].strip().strip("<>")
            counts[email] = counts.get(email, 0) + 1
            total += 1
    if total == 0:
        return {}
    return dict(sorted((author, count / total) for author, count in counts.items()))

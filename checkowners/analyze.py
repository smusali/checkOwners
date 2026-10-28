"""Git history analysis for confidence-scored ownership inference."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Literal

from checkowners.expertise import fnmatch_posix, path_matches_glob
from checkowners.github import (
    collection_gaps,
    note_metric,
    reset_review_window,
    review_was_omitted,
    review_window,
)
from checkowners.identity import bot_identities, is_bot_identity, mechanical_authors
from checkowners.models import (
    AnalysisCompleteness,
    ConfidenceScore,
    Config,
    ConfiguredMergeStrategy,
    DecayWarning,
    FreshnessStatus,
    GitConfig,
    LookbackDays,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    QualificationStrategy,
    ReportedMergeStrategy,
    ScoringConfig,
    SignalScore,
    history_evidence_gaps,
    merge_gaps,
    with_gaps,
)
from checkowners.state import (
    BlameLines,
    HistoryBaseline,
    PathMove,
    read_blame_cache,
    read_history_baseline,
    write_blame_cache,
)

_COMMIT_SENTINEL = "COMMIT_START"
_BODY_START = "BODY_START"
_BODY_END = "BODY_END"
_MERGE_COMMIT_MIN_PERCENT = 5
_SQUASH_SUBJECT_MIN_PERCENT = 50
_COAUTHOR_LINE = re.compile(r"(?i)^co-authored-by:\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*$")
_CONTACT_EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")
_SQUASH_SUBJECT = re.compile(r"\(#\d+\)\s*$")
FREQUENCY_SHRINKAGE_PRIOR = 3.0
SOURCE_DATE_EPOCH_ENV = "SOURCE_DATE_EPOCH"
MIN_GIT_VERSION = (2, 23, 0)
_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_PATH_GIT_CONFIG = ("-c", "core.quotePath=false", "-c", "core.longpaths=true")


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
    credit: float
    later_commits: int = 0
    cadence_days: float | None = None
    last_sha: str = ""


@dataclass(frozen=True)
class _RawCommit:
    """A single commit's parsed metadata."""

    author: str
    timestamp: datetime
    files: tuple[str, ...]
    coauthors: tuple[str, ...] = ()
    sha: str = ""
    subject: str = ""
    author_name: str = ""


@dataclass(frozen=True)
class _ParsedCommit:
    sha: str
    author: str
    timestamp: datetime
    files: tuple[str, ...]
    contacts: tuple[tuple[str, str], ...]
    parent_count: int
    squash_subject: bool
    author_name: str = ""
    subject: str = ""
    moves: tuple[PathMove, ...] = ()


@dataclass(frozen=True)
class AuthorIndex:
    shas: dict[str, str] = field(default_factory=dict)
    names: dict[str, tuple[str, ...]] = field(default_factory=dict)


_AUTHOR_INDEX: ContextVar[AuthorIndex | None] = ContextVar(
    "checkowners_author_index",
    default=None,
)


@dataclass(frozen=True)
class _CommitHistory:
    commits: list[_RawCommit]
    merge_strategy: ReportedMergeStrategy
    co_author_count: int
    moves: dict[str, tuple[PathMove, ...]] = field(default_factory=dict)
    id_to_path: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    priors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    logical_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _BlamePass:
    coverage: dict[str, dict[str, float]] = field(default_factory=dict)
    ignore_revs_file: str = ""
    ignore_revs_applied: bool = False
    truncated: bool = False
    lines: dict[str, dict[str, int]] = field(default_factory=dict)
    mass: tuple[str, ...] = ()


@dataclass(frozen=True)
class _BlameDetail:
    fractions: dict[str, float]
    commits: dict[str, int]
    reusable: bool
    lines: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisOutcome:
    ownership: OwnershipMap
    baseline: HistoryBaseline
    mode: Literal["full", "incremental"]
    fallback: str
    prior_ref: str


_PENDING_OUTCOME: ContextVar[AnalysisOutcome | None] = ContextVar(
    "checkowners_analysis_outcome",
    default=None,
)
_BLAME_SINK_LOCK = threading.Lock()
_SHA_BATCH = 40


_BLAME_DEADLINE: ContextVar[float | None] = ContextVar("checkowners_blame_deadline", default=None)
_USE_BLAME_CACHE: ContextVar[bool] = ContextVar("checkowners_blame_cache", default=True)
_BLAME_VERBOSE: ContextVar[bool] = ContextVar("checkowners_blame_verbose", default=False)


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
    result = run_git(repo_root, ["git", "log", "-1", "--format=%cI"], check=True)
    parsed = _parse_timestamp(result.stdout.strip())
    if parsed is None:
        msg = "HEAD commit has no parseable committer timestamp"
        raise ValueError(msg)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def head_commit_sha(repo_root: Path) -> str:
    result = run_git(repo_root, ["git", "rev-parse", "HEAD"], check=True)
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


def consume_analysis_outcome() -> AnalysisOutcome | None:
    """Return the captured outcome, or None, and clear it."""
    outcome = _PENDING_OUTCOME.get()
    _PENDING_OUTCOME.set(None)
    return outcome


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
    incremental: bool = False,
    capture_baseline: bool = False,
    revision: str | None = None,
    mass_revs: tuple[str, ...] | None = None,
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
    ``incremental`` replays a stored baseline when one is usable.
    ``capture_baseline`` stores the run for ``consume_analysis_outcome``.
    ``revision`` logs and blames that commit instead of HEAD.
    ``mass_revs`` is passed through to blame; an empty tuple skips the scan.
    """
    if incremental or capture_baseline:
        outcome = collect_analysis(
            repo_root,
            config,
            review_provider=review_provider,
            on_progress=on_progress,
            as_of=as_of,
            analysis_ref=analysis_ref,
            max_workers=max_workers,
            incremental=incremental,
        )
        if capture_baseline:
            _PENDING_OUTCOME.set(outcome)
        return outcome.ownership
    when = as_of if as_of is not None else resolve_as_of(None, repo_root)
    ref = analysis_ref if analysis_ref is not None else head_commit_sha(repo_root)
    reset_review_window()
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
            revision=revision,
            mass_revs=mass_revs,
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
    prepared_history: _CommitHistory | None = None,
    blame_runner: Callable[[Iterable[str]], _BlamePass] | None = None,
    renamed: bool | None = None,
    revision: str | None = None,
    mass_revs: tuple[str, ...] | None = None,
) -> OwnershipMap:
    mailmap_path = _resolve_mailmap_file(repo_root)
    history = prepared_history
    if history is None:
        history = _get_commit_history(
            repo_root,
            lookback_start(config.analysis.lookback_days, when),
            when,
            use_mailmap=config.git.use_mailmap,
            pathspec=pathspec,
            count_co_authors=config.git.count_co_authors,
            merge_strategy=config.git.merge_strategy,
            tip=revision,
            follow_renames=config.analysis.follow_renames,
            rename_similarity=config.analysis.rename_similarity,
        )
    commits = history.commits
    _AUTHOR_INDEX.set(_author_index(commits))
    contributions = _remap_contributions(
        _aggregate_contributions(
            commits,
            co_author_weight=config.git.co_author_weight,
        ),
        history.id_to_path,
    )
    commits = _current_commits(history)
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
    contributions = _cap_mechanical_authors(contributions, commits)
    if config.analysis.exclude_bots:
        contributions = _filter_bot_authors(contributions, bot_identities(config))
    if config.qualification.strategy == "threshold":
        contributions = _filter_unqualified(contributions, config.analysis.min_commits)
    if blame_runner is not None:
        blame_pass = blame_runner(contributions.keys())
    elif time.monotonic() >= deadline:
        blame_pass = _BlamePass(truncated=True)
    elif mass_revs is None and revision is None:
        blame_pass = gather_blame_coverage(
            contributions.keys(),
            repo_root,
            git=config.git,
            on_progress=on_progress,
            max_workers=max_workers,
        )
    else:
        blame_pass = gather_blame_coverage(
            contributions.keys(),
            repo_root,
            git=config.git,
            on_progress=on_progress,
            max_workers=max_workers,
            mass_revs=mass_revs,
            revision=revision,
        )
    review_coverage = _gather_review_coverage(contributions, review_provider)
    review_omitted = review_was_omitted()
    activity = _path_activity(commits, frozenset(contributions))
    paths = _build_path_ownerships(
        contributions,
        blame_pass.coverage,
        review_coverage,
        config,
        when,
        activity=activity,
        review_available=review_provider is not None and not review_omitted,
        retain_all=retain_all,
        blame_lines=blame_pass.lines,
        priors=history.priors,
        repo_root=repo_root,
        mass_revs=blame_pass.mass,
    )
    if renamed is None and config.analysis.follow_renames:
        detected = _rename_commit_shas(
            repo_root,
            lookback_start(config.analysis.lookback_days, when),
            when,
        )
        renamed = any(sha not in history.moves for sha in detected)
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
        merge_strategy=history.merge_strategy,
        co_author_count=history.co_author_count,
        renamed=renamed,
    )
    sample = review_window()
    ownership = apply_completeness(
        OwnershipMap(
            paths=dict(sorted(paths.items())),
            last_analyzed=when,
            analysis_ref=ref,
            analysis_completeness=completeness,
            reviews_sampled=sample.sampled,
            reviews_window_days=sample.window_days,
        ),
        completeness,
        config.scoring,
    )
    return _scale_lineage_confidence(ownership, history.confidence)


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
    merge_strategy: ReportedMergeStrategy,
    co_author_count: int,
    renamed: bool | None = None,
) -> AnalysisCompleteness:
    since = lookback_start(config.analysis.lookback_days, when)
    gaps = history_evidence_gaps(
        shallow=_is_shallow_repository(repo_root),
        insufficient=_insufficient_history(repo_root, commits),
        renamed=(_window_has_renames(repo_root, since, when) if renamed is None else renamed),
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
        merge_strategy=merge_strategy,
        co_author_count=co_author_count,
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
                "change_survival": (
                    breakdown.change_survival.score,
                    breakdown.change_survival.available,
                ),
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


def run_git(
    repo_root: Path,
    args: list[str],
    *,
    check: bool,
    paths: bool = False,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    note_metric("git")
    argv = args
    if paths and args and args[0] == "git":
        argv = ["git", *_PATH_GIT_CONFIG, *args[1:]]
    return subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        cwd=repo_root,
        check=check,
        input=stdin,
    )


def _git_stdout(repo_root: Path, argv: list[str], *, paths: bool = False) -> str | None:
    try:
        result = run_git(repo_root, argv, check=False, paths=paths)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _is_shallow_repository(repo_root: Path) -> bool:
    stdout = _git_stdout(repo_root, ["git", "rev-parse", "--is-shallow-repository"])
    if stdout is not None:
        text = stdout.strip()
        if text == "true":
            return True
        if text == "false":
            return False
    shallow = _git_stdout(repo_root, ["git", "rev-parse", "--git-path", "shallow"])
    if shallow is None:
        return False
    text = shallow.strip()
    if not text or "\n" in text:
        return False
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        return candidate.is_file()
    except OSError:
        return False


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


def _rename_commit_shas(
    repo_root: Path,
    since: datetime | None,
    until: datetime,
    revision: str | None = None,
) -> frozenset[str]:
    argv = ["git", "log", "--diff-filter=R", "--name-status", "--pretty=format:%H"]
    if revision:
        argv.append(revision)
    if since is not None:
        argv.append(f"--since={since.isoformat()}")
    argv.append(f"--until={until.isoformat()}")
    stdout = _git_stdout(repo_root, argv, paths=True)
    if not stdout:
        return frozenset()
    found: set[str] = set()
    current = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if _FULL_SHA_RE.fullmatch(stripped):
            current = stripped
            continue
        if current and _stdout_has_rename(line):
            found.add(current)
    return frozenset(found)


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
    stdout = _git_stdout(repo_root, argv, paths=True)
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
    activity: dict[str, tuple[int, float | None, float | None]],
    review_available: bool,
    retain_all: bool = False,
    blame_lines: Mapping[str, Mapping[str, int]] | None = None,
    priors: Mapping[str, tuple[str, ...]] | None = None,
    repo_root: Path | None = None,
    mass_revs: tuple[str, ...] = (),
) -> dict[str, PathOwnership]:
    """Compute scored owners + qualified owner count + decay per path."""
    result: dict[str, PathOwnership] = {}
    frequency_prior = frequency_prior_for(config.qualification.strategy)
    survival_by_path = _survival_by_path(
        contributions,
        set(blame_coverage),
        blame_lines or {},
        priors or {},
        repo_root,
        config.git,
        mass_revs,
    )
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
        max_credit = max(frequency_credit(c) for c in qualified.values())
        path_review = review_coverage.get(path, {})
        entries = score_owners(
            qualified,
            path_blame,
            path_review,
            max_commits=max_credit,
            scoring=config.scoring,
            now=now,
            blame_available=path in blame_coverage,
            review_available=review_available,
            frequency_prior=frequency_prior,
            threshold_days=config.decay.threshold_days,
            survival=survival_by_path.get(path),
        )
        filtered = tuple(e for e in entries if e.confidence >= config.analysis.confidence_threshold)
        if not filtered and not retain_all:
            continue
        top = filtered[: config.analysis.top_n_owners]
        decay = _detect_decay(path, qualified, top, config.decay.threshold_days, now)
        qualified_owner_count = _count_qualified_owners(top, config.analysis.confidence_threshold)
        events, cadence, coupling = activity.get(path, (None, None, None))
        result[path] = PathOwnership(
            owners=top,
            qualified_owner_count=qualified_owner_count,
            decay_warnings=decay,
            candidates=entries if retain_all else (),
            scored_owners=filtered,
            change_events=events,
            cadence_days=cadence,
            cochange_degree=coupling,
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
        "change_survival": scoring.change_survival_weight,
    }


def signal_reliabilities(scoring: ScoringConfig) -> dict[str, float]:
    return {
        "recency": scoring.recency_reliability,
        "frequency": scoring.frequency_reliability,
        "blame": scoring.blame_reliability,
        "review": scoring.review_reliability,
        "change_survival": scoring.change_survival_reliability,
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
    max_commits: float,
    scoring: ScoringConfig,
    now: datetime,
    blame_available: bool,
    review_available: bool,
    frequency_prior: float = 0.0,
    threshold_days: int = 180,
    survival: Mapping[str, float | None] | None = None,
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
        frequency = _frequency_score(frequency_credit(contrib), max_commits, frequency_prior)
        blame = path_blame.get(author, 0.0) if blame_available else 0.0
        review = _clamp(path_review.get(author, 0.0)) if review_available else 0.0
        ratio = None if survival is None else survival.get(author)
        survival_available = blame_available and ratio is not None
        survival_score = 0.0 if ratio is None else ratio
        signals = {
            "recency": (recency, True),
            "frequency": (frequency, True),
            "blame": (blame, blame_available),
            "review": (review, review_available),
            "change_survival": (survival_score, survival_available),
        }
        total, quality = combine_available_signals(signals, weights, reliabilities)
        breakdown = ConfidenceScore(
            total=total,
            recency=SignalScore(available=True, score=recency),
            frequency=SignalScore(available=True, score=frequency),
            blame=SignalScore(available=blame_available, score=blame),
            review=SignalScore(available=review_available, score=review),
            change_survival=SignalScore(available=survival_available, score=survival_score),
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
                        survival=ratio if survival_available else None,
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


def review_window_start(config: Config, when: datetime) -> datetime | None:
    """Return the review window start for `config` at `when`, or None when it is open-ended."""
    days = config.analysis.review_lookback_days
    if days is None:
        return lookback_start(config.analysis.lookback_days, when)
    return when - timedelta(days=days)


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


_SURVIVAL_SUPERSEDED = 0.2


def _classify_freshness(
    contrib: Contribution,
    *,
    days: int,
    half_life_days: float,
    path_last: datetime,
    ceiling_days: int,
    threshold_days: int,
    survival: float | None = None,
) -> FreshnessStatus:
    if survival is None:
        if contrib.later_commits > contrib.commits:
            return "superseded"
    elif survival < _SURVIVAL_SUPERSEDED and contrib.later_commits > 0:
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


def frequency_credit(contrib: Contribution) -> float:
    """Return the frequency numerator for `contrib`."""
    return contrib.credit


def _frequency_score(commits: float, max_commits: float, prior: float = 0.0) -> float:
    if max_commits <= 0:
        return 0.0
    return _clamp(commits / (max_commits + prior))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _survival_by_path(
    contributions: Mapping[str, Mapping[str, Contribution]],
    blamed: set[str],
    blame_lines: Mapping[str, Mapping[str, int]],
    priors: Mapping[str, tuple[str, ...]],
    repo_root: Path | None,
    git: GitConfig,
    mass_revs: tuple[str, ...],
) -> dict[str, dict[str, float | None]]:
    if repo_root is None or not blamed:
        return {}
    pending: list[tuple[str, str, str, str]] = []
    misses: list[tuple[str, str]] = []
    grouped: dict[str, dict[str, str]] = {}
    rows_by_rev: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    for path, authors in contributions.items():
        if path not in blamed:
            continue
        for author, contrib in authors.items():
            if not contrib.last_sha:
                misses.append((path, author))
                continue
            rows_by_rev.setdefault(contrib.last_sha, []).append(
                (path, author, priors.get(path, ()))
            )
    for revision, rows in rows_by_rev.items():
        candidates: list[str] = []
        for path, _author, prior in rows:
            for candidate in (path, *reversed(prior)):
                if candidate not in candidates:
                    candidates.append(candidate)
        blobs = _blobs_at_revision(repo_root, revision, candidates)
        for path, author, prior in rows:
            located: tuple[str, str] | None = None
            for candidate in (path, *reversed(prior)):
                blob = blobs.get(candidate)
                if blob:
                    located = (candidate, blob)
                    break
            if located is None:
                misses.append((path, author))
                continue
            historical, blob = located
            pending.append((path, author, revision, historical))
            grouped.setdefault(revision, {})[historical] = blob
    looked: dict[tuple[str, str], Mapping[str, int]] = {}
    for revision, blobs in grouped.items():
        passed = gather_blame_coverage(
            blobs,
            repo_root,
            git=git,
            mass_revs=mass_revs,
            revision=revision,
            blobs=blobs,
            report_cache=False,
        )
        for historical in blobs:
            looked[(revision, historical)] = passed.lines.get(historical, {})
    ratios: dict[str, dict[str, float | None]] = {}
    for path, author in misses:
        ratios.setdefault(path, {})[author] = None
    for path, author, revision, historical in pending:
        past = looked.get((revision, historical), {})
        denominator = past.get(author, 0)
        current = blame_lines.get(path, {}).get(author, 0)
        ratios.setdefault(path, {})[author] = (
            None if denominator <= 0 else _clamp(current / denominator)
        )
    return ratios


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


def coauthor_contacts(body: str) -> tuple[tuple[str, str], ...]:
    """Return unique `(name, email)` pairs from valid Co-authored-by lines in `body`."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in body.splitlines():
        match = _COAUTHOR_LINE.match(line.strip())
        if match is None:
            continue
        email = match.group(2)
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append((match.group(1).strip(), email))
    return tuple(found)


def canonical_emails(
    repo_root: Path,
    contacts: tuple[tuple[str, str], ...],
    *,
    enabled: bool,
) -> tuple[str, ...]:
    """Return one email per contact in `contacts`. `enabled` applies `.mailmap`."""
    raw = tuple(email for _name, email in contacts)
    if not enabled or not contacts:
        return raw
    argv = ["git", "check-mailmap"]
    for name, email in contacts:
        argv.append(f"{name} <{email}>" if name else f"<{email}>")
    result = run_git(repo_root, argv, check=False)
    if result.returncode != 0:
        return raw
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != len(contacts):
        return raw
    parsed: list[str] = []
    for line, email in zip(lines, raw, strict=True):
        match = _CONTACT_EMAIL.search(line)
        parsed.append(match.group(1) if match is not None else email)
    return tuple(parsed)


def assign_coauthors(
    repo_root: Path,
    authors: tuple[str, ...],
    contact_lists: tuple[tuple[tuple[str, str], ...], ...],
    *,
    use_mailmap: bool,
    enabled: bool,
) -> tuple[tuple[str, ...], ...]:
    """Return credited co-author emails for each author, excluding that author."""
    empty = tuple(() for _author in authors)
    if not enabled:
        return empty
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for contacts in contact_lists:
        for name, email in contacts:
            key = email.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append((name, email))
    canonical = canonical_emails(repo_root, tuple(unique), enabled=use_mailmap)
    mapped = {
        email.casefold(): canonical_email
        for (_name, email), canonical_email in zip(unique, canonical, strict=True)
    }
    credited: list[tuple[str, ...]] = []
    for author, contacts in zip(authors, contact_lists, strict=True):
        author_key = author.casefold()
        people: list[str] = []
        used: set[str] = set()
        for _name, email in contacts:
            person = mapped.get(email.casefold(), email)
            key = person.casefold()
            if key == author_key or key in used:
                continue
            used.add(key)
            people.append(person)
        credited.append(tuple(people))
    return tuple(credited)


def _get_commit_history(
    repo_root: Path,
    since: datetime | None,
    as_of: datetime,
    *,
    use_mailmap: bool = True,
    pathspec: tuple[str, ...] | None = None,
    count_co_authors: bool = True,
    merge_strategy: ConfiguredMergeStrategy = "auto",
    tip: str | None = None,
    follow_renames: bool = True,
    rename_similarity: int = 50,
) -> _CommitHistory:
    """Return parsed commits from `since` until `as_of`. `since` None omits the lower bound."""
    records = _parsed_commits(
        repo_root,
        since,
        as_of,
        use_mailmap=use_mailmap,
        name_only=not follow_renames,
        pathspec=pathspec,
        tip=tip,
        track_renames=follow_renames,
        rename_similarity=rename_similarity,
    )
    history = _history_from_records(
        repo_root,
        records,
        use_mailmap=use_mailmap,
        count_co_authors=count_co_authors,
        merge_strategy=merge_strategy,
    )
    return _finish_history(history, follow_renames=follow_renames)


def _history_log_format(use_mailmap: bool) -> str:
    author_fmt = "%aN%x09%aE" if use_mailmap else "%an%x09%ae"
    prefix = f"--format={_COMMIT_SENTINEL}%n%H%n{author_fmt}%n%cI%n%P%n%s"
    trailer = f"%n{_BODY_START}%n%b%n{_BODY_END}"
    return prefix + trailer


def consume_author_index() -> AuthorIndex:
    """Return the author index from the latest analysis and clear it."""
    current = _AUTHOR_INDEX.get()
    _AUTHOR_INDEX.set(None)
    if current is None:
        return AuthorIndex()
    return current


def _parsed_commits(
    repo_root: Path,
    since: datetime | None,
    as_of: datetime,
    *,
    use_mailmap: bool,
    name_only: bool,
    pathspec: tuple[str, ...] | None = None,
    revisions: tuple[str, ...] = (),
    tip: str | None = None,
    track_renames: bool = False,
    rename_similarity: int = 50,
) -> list[_ParsedCommit]:
    argv = ["git", "log", mailmap_flag(use_mailmap), _history_log_format(use_mailmap)]
    if track_renames and pathspec is not None and len(pathspec) == 1 and not revisions:
        argv.append("--follow")
    if track_renames:
        argv.extend(
            [
                "--name-status",
                "-M",
                "-C",
                f"--find-renames={rename_similarity}%",
                f"--find-copies={rename_similarity}%",
            ]
        )
    elif name_only:
        argv.append("--name-only")
    if revisions:
        argv.append("--no-walk")
        argv.extend(revisions)
    else:
        if tip is not None:
            argv.append(tip)
        if since is not None:
            argv.append(f"--since={since.isoformat()}")
        argv.append(f"--until={as_of.isoformat()}")
        if pathspec:
            argv.append("--")
            argv.extend(pathspec)
    result = run_git(repo_root, argv, check=True, paths=name_only or track_renames)
    return _parse_log(result.stdout)


def _history_from_records(
    repo_root: Path,
    records: list[_ParsedCommit],
    *,
    use_mailmap: bool,
    count_co_authors: bool,
    merge_strategy: ConfiguredMergeStrategy,
) -> _CommitHistory:
    file_records = [record for record in records if record.files]
    credited = assign_coauthors(
        repo_root,
        tuple(record.author for record in file_records),
        tuple(record.contacts for record in file_records),
        use_mailmap=use_mailmap,
        enabled=count_co_authors,
    )
    commits = [
        _RawCommit(
            author=record.author,
            timestamp=record.timestamp,
            files=record.files,
            coauthors=coauthors,
            sha=record.sha,
            subject=record.subject,
            author_name=record.author_name,
        )
        for record, coauthors in zip(file_records, credited, strict=True)
    ]
    moves = {record.sha: record.moves for record in records if record.sha and record.moves}
    return _CommitHistory(
        commits=commits,
        merge_strategy=_applied_strategy(merge_strategy, _detect_strategy(records)),
        co_author_count=sum(len(coauthors) for coauthors in credited),
        moves=moves,
    )


class _ReplayError(Exception):
    pass


def collect_analysis(
    repo_root: Path,
    config: Config,
    *,
    review_provider: ReviewProvider | None = None,
    on_progress: ProgressHook | None = None,
    as_of: datetime | None = None,
    analysis_ref: str | None = None,
    max_workers: int | None = None,
    incremental: bool = True,
) -> AnalysisOutcome:
    """Return scored ownership for `repo_root` and the baseline that reproduces it."""
    when = as_of if as_of is not None else resolve_as_of(None, repo_root)
    ref = analysis_ref if analysis_ref is not None else head_commit_sha(repo_root)
    reset_review_window()
    deadline = time.monotonic() + config.analysis.max_runtime_seconds
    token = _BLAME_DEADLINE.set(deadline)
    try:
        return _collect_analysis(
            repo_root,
            config,
            when=when,
            ref=ref,
            review_provider=review_provider,
            on_progress=on_progress,
            max_workers=max_workers,
            incremental=incremental,
            deadline=deadline,
        )
    finally:
        _BLAME_DEADLINE.reset(token)


def _collect_analysis(
    repo_root: Path,
    config: Config,
    *,
    when: datetime,
    ref: str,
    review_provider: ReviewProvider | None,
    on_progress: ProgressHook | None,
    max_workers: int | None,
    incremental: bool,
    deadline: float,
) -> AnalysisOutcome:
    since = lookback_start(config.analysis.lookback_days, when)
    mode: Literal["full", "incremental"] = "full"
    prior_ref = ""
    fallback = "" if incremental else "incremental analysis disabled"
    prior: HistoryBaseline | None = None
    if incremental:
        loaded, rejection = read_history_baseline(repo_root, config)
        if rejection:
            fallback = rejection
        elif loaded is not None:
            git_reason = _git_baseline_reason(repo_root, loaded, ref, config)
            if git_reason:
                fallback = git_reason
            else:
                prior = loaded
    window: tuple[
        _CommitHistory,
        dict[str, tuple[str, ...]],
        dict[str, int],
        frozenset[str],
    ]
    if prior is not None:
        try:
            window = _replay_window(repo_root, config, prior, since, when, ref)
        except _ReplayError as exc:
            fallback = str(exc)
            prior = None
            window = _full_window(repo_root, config, since, when)
        else:
            mode = "incremental"
            prior_ref = prior.analysis_ref
            fallback = ""
    else:
        window = _full_window(repo_root, config, since, when)
    history, files, modified, renames = window
    saved = _BlameSaved()

    def blame_runner(paths: Iterable[str]) -> _BlamePass:
        blame_pass, lines, blame_inputs, truncated = _coverage_with_reuse(
            paths,
            repo_root,
            config,
            baseline=prior if mode == "incremental" else None,
            modified=modified,
            on_progress=on_progress,
            max_workers=max_workers,
        )
        saved.lines = lines
        saved.inputs = blame_inputs
        saved.truncated = truncated
        return blame_pass

    ownership = _analyze_ownership(
        repo_root,
        config,
        when=when,
        ref=ref,
        review_provider=review_provider,
        on_progress=on_progress,
        max_workers=max_workers,
        pathspec=None,
        retain_all=False,
        deadline=deadline,
        prepared_history=history,
        blame_runner=blame_runner,
        renamed=(
            any(sha not in history.moves for sha in renames)
            if config.analysis.follow_renames
            else bool(renames)
        ),
    )
    baseline = HistoryBaseline(
        analysis_ref=ref,
        files=files,
        modified=modified,
        renames=renames,
        blame={} if saved.truncated else saved.lines,
        blame_inputs=saved.inputs,
        rename_detection=_rename_detection(repo_root),
        truncated=saved.truncated,
        follow_renames=config.analysis.follow_renames,
        rename_similarity=config.analysis.rename_similarity,
        moves=history.moves,
    )
    return AnalysisOutcome(
        ownership=ownership,
        baseline=baseline,
        mode=mode,
        fallback=fallback,
        prior_ref=prior_ref,
    )


@dataclass
class _BlameSaved:
    lines: dict[str, BlameLines] = field(default_factory=dict)
    inputs: str = ""
    truncated: bool = False


def _full_window(
    repo_root: Path,
    config: Config,
    since: datetime | None,
    when: datetime,
) -> tuple[_CommitHistory, dict[str, tuple[str, ...]], dict[str, int], frozenset[str]]:
    records = _parsed_commits(
        repo_root,
        since,
        when,
        use_mailmap=config.git.use_mailmap,
        name_only=not config.analysis.follow_renames,
        track_renames=config.analysis.follow_renames,
        rename_similarity=config.analysis.rename_similarity,
    )
    history = _finish_history(
        _history_from_records(
            repo_root,
            records,
            use_mailmap=config.git.use_mailmap,
            count_co_authors=config.git.count_co_authors,
            merge_strategy=config.git.merge_strategy,
        ),
        follow_renames=config.analysis.follow_renames,
    )
    files = {record.sha: record.files for record in records}
    return history, files, _modified_counts(repo_root), _rename_commit_shas(repo_root, since, when)


def _replay_window(
    repo_root: Path,
    config: Config,
    baseline: HistoryBaseline,
    since: datetime | None,
    when: datetime,
    head: str,
) -> tuple[_CommitHistory, dict[str, tuple[str, ...]], dict[str, int], frozenset[str]]:
    records = _parsed_commits(
        repo_root,
        since,
        when,
        use_mailmap=config.git.use_mailmap,
        name_only=False,
    )
    missing = [record.sha for record in records if record.sha not in baseline.files]
    fetched: dict[str, tuple[str, ...]] = {}
    fetched_moves: dict[str, tuple[PathMove, ...]] = {}
    if missing:
        ancestors = _ancestor_shas(repo_root, baseline.analysis_ref)
        if any(sha in ancestors for sha in missing):
            raise _ReplayError("incremental baseline does not cover this lookback window")
        fetched, fetched_moves = _fetch_commit_files(
            repo_root,
            missing,
            use_mailmap=config.git.use_mailmap,
            as_of=when,
            follow_renames=config.analysis.follow_renames,
            rename_similarity=config.analysis.rename_similarity,
        )
        if any(sha not in fetched for sha in missing):
            raise _ReplayError("incremental baseline does not cover this lookback window")
    files: dict[str, tuple[str, ...]] = {}
    attached: list[_ParsedCommit] = []
    for record in records:
        paths = baseline.files.get(record.sha, fetched.get(record.sha, ()))
        files[record.sha] = paths
        attached.append(replace(record, files=paths))
    combined_moves = dict(baseline.moves)
    combined_moves.update(fetched_moves)
    history = replace(
        _history_from_records(
            repo_root,
            attached,
            use_mailmap=config.git.use_mailmap,
            count_co_authors=config.git.count_co_authors,
            merge_strategy=config.git.merge_strategy,
        ),
        moves=combined_moves,
    )
    history = _finish_history(history, follow_renames=config.analysis.follow_renames)
    revision = f"{baseline.analysis_ref}..{head}"
    modified = dict(baseline.modified)
    modified.update(_modified_counts(repo_root, revision))
    delta_renames = _rename_commit_shas(repo_root, since, when, revision)
    window_shas = set(files)
    renames = frozenset(sha for sha in baseline.renames | delta_renames if sha in window_shas)
    return history, files, modified, renames


def _fetch_commit_files(
    repo_root: Path,
    shas: list[str],
    *,
    use_mailmap: bool,
    as_of: datetime,
    follow_renames: bool = False,
    rename_similarity: int = 50,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[PathMove, ...]]]:
    found: dict[str, tuple[str, ...]] = {}
    moves: dict[str, tuple[PathMove, ...]] = {}
    for start in range(0, len(shas), _SHA_BATCH):
        batch = tuple(shas[start : start + _SHA_BATCH])
        records = _parsed_commits(
            repo_root,
            None,
            as_of,
            use_mailmap=use_mailmap,
            name_only=not follow_renames,
            revisions=batch,
            track_renames=follow_renames,
            rename_similarity=rename_similarity,
        )
        for record in records:
            found[record.sha] = record.files
            if record.moves:
                moves[record.sha] = record.moves
    return found, moves


def _rename_detection(repo_root: Path) -> str:
    return _git_config_value(repo_root, "diff.renames") or "true"


def _commit_exists(repo_root: Path, ref: str) -> bool:
    result = run_git(repo_root, ["git", "cat-file", "-e", f"{ref}^{{commit}}"], check=False)
    return result.returncode == 0


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    if not ancestor or not descendant:
        return False
    result = run_git(
        repo_root,
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    )
    return result.returncode == 0


def _ancestor_shas(repo_root: Path, ref: str) -> set[str]:
    result = run_git(repo_root, ["git", "rev-list", ref], check=True)
    return set(result.stdout.split())


def _git_baseline_reason(
    repo_root: Path,
    baseline: HistoryBaseline,
    head: str,
    config: Config | None = None,
) -> str:
    if baseline.truncated:
        return "incremental baseline is incomplete"
    if not baseline.analysis_ref:
        return "incremental baseline has no analyzed ref"
    if not _commit_exists(repo_root, baseline.analysis_ref):
        return "cached ref is missing"
    if not _is_ancestor(repo_root, baseline.analysis_ref, head):
        return "cached ref is not an ancestor of HEAD"
    if baseline.rename_detection != _rename_detection(repo_root):
        return "rename detection changed"
    active = config if config is not None else Config()
    if (
        baseline.follow_renames != active.analysis.follow_renames
        or baseline.rename_similarity != active.analysis.rename_similarity
    ):
        return "lineage settings changed"
    return ""


def _mailmap_enabled(repo_root: Path, requested: bool) -> bool:
    return requested and _blame_accepts_mailmap_flags(repo_root)


def _blame_inputs_token(repo_root: Path, config: Config, mass: tuple[str, ...]) -> str:
    ignore = _resolve_ignore_revs_file(repo_root, config.git.blame_ignore_revs_file)
    text = ""
    if ignore is not None:
        try:
            text = ignore.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    payload = json.dumps(
        {"ignore": text, "mass": sorted(mass), "moves": config.git.detect_moves},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _blob_shas(repo_root: Path, paths: list[str]) -> dict[str, str]:
    safe = [path for path in paths if "\n" not in path and "\x00" not in path]
    if not safe:
        return {}
    result = run_git(
        repo_root,
        ["git", "hash-object", "--stdin-paths"],
        check=False,
        stdin="\n".join(safe) + "\n",
    )
    if result.returncode != 0:
        return {}
    hashes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(hashes) != len(safe):
        return {}
    return dict(zip(safe, hashes, strict=True))


def _blobs_at_revision(repo_root: Path, revision: str, paths: list[str]) -> dict[str, str]:
    safe = [path for path in paths if "\n" not in path and "\x00" not in path]
    if not safe or not revision:
        return {}
    result = run_git(
        repo_root,
        ["git", "cat-file", "--batch-check"],
        check=False,
        stdin="".join(f"{revision}:{path}\n" for path in safe),
    )
    if result.returncode != 0:
        return {}
    found: dict[str, str] = {}
    for path, line in zip(safe, result.stdout.splitlines(), strict=False):
        blob, _separator, kind = line.partition(" ")
        if kind.startswith("blob") and _FULL_SHA_RE.fullmatch(blob):
            found[path] = blob
    return found


def _author_counts(commit_lines: Mapping[str, int], emails: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sha, count in commit_lines.items():
        email = emails.get(sha)
        if email is None or count <= 0:
            continue
        counts[email] = counts.get(email, 0) + count
    return dict(sorted(counts.items()))


def _author_emails(repo_root: Path, shas: Iterable[str], *, use_mailmap: bool) -> dict[str, str]:
    unique = sorted({sha for sha in shas if _FULL_SHA_RE.fullmatch(sha)})
    found: dict[str, str] = {}
    for start in range(0, len(unique), _SHA_BATCH):
        batch = unique[start : start + _SHA_BATCH]
        result = run_git(
            repo_root,
            [
                "git",
                "log",
                "--no-walk",
                mailmap_flag(use_mailmap),
                "--format=%H%x00%aE",
                *batch,
            ],
            check=True,
        )
        for line in result.stdout.splitlines():
            sha, separator, email = line.partition("\x00")
            if separator and email and _FULL_SHA_RE.fullmatch(sha):
                found[sha] = email
    return found


def _reused_fraction(
    stored: BlameLines | None,
    blob: str,
    emails: Mapping[str, str],
) -> dict[str, float] | None:
    if stored is None or not blob or stored.blob_sha != blob or not stored.lines:
        return None
    total = 0
    counts: dict[str, int] = {}
    for sha, count in stored.lines.items():
        email = emails.get(sha)
        if not email:
            return None
        total += count
        counts[email] = counts.get(email, 0) + count
    if total <= 0:
        return None
    return dict(sorted((author, count / total) for author, count in counts.items()))


def _coverage_with_reuse(
    paths: Iterable[str],
    repo_root: Path,
    config: Config,
    *,
    baseline: HistoryBaseline | None,
    modified: Mapping[str, int],
    on_progress: ProgressHook | None,
    max_workers: int | None,
) -> tuple[_BlamePass, dict[str, BlameLines], str, bool]:
    tracked = _tracked_file_count(repo_root)
    mass = _mass_shas(modified, tracked, config.git.mass_refactor_file_fraction)
    inputs = _blame_inputs_token(repo_root, config, mass)
    path_list = sorted(paths)
    reuse_ok = baseline is not None and not baseline.truncated and baseline.blame_inputs == inputs
    blobs = _blob_shas(repo_root, path_list)
    emails: dict[str, str] = {}
    if reuse_ok and baseline is not None:
        wanted: list[str] = []
        for path in path_list:
            stored = baseline.blame.get(path)
            if stored is not None and stored.blob_sha == blobs.get(path, "") and stored.blob_sha:
                wanted.extend(stored.lines)
        emails = _author_emails(
            repo_root,
            wanted,
            use_mailmap=_mailmap_enabled(repo_root, config.git.use_mailmap),
        )
    reused: dict[str, dict[str, float]] = {}
    saved: dict[str, BlameLines] = {}
    dirty: list[str] = []
    for path in path_list:
        if reuse_ok and baseline is not None:
            fraction = _reused_fraction(
                baseline.blame.get(path),
                blobs.get(path, ""),
                emails,
            )
            if fraction is not None:
                reused[path] = fraction
                saved[path] = baseline.blame[path]
                continue
        dirty.append(path)
    ignore = _resolve_ignore_revs_file(repo_root, config.git.blame_ignore_revs_file)
    display = _display_ignore_revs_path(repo_root, ignore) if ignore else ""
    sink: dict[str, _BlameDetail] = {}
    if dirty:
        observed = len(reused)
        hook: ProgressHook | None
        if on_progress is None:
            hook = None
        else:
            callback = on_progress

            def report(done: int, _total: int) -> None:
                callback(observed + done, len(path_list))

            hook = report
        fresh = gather_blame_coverage(
            dirty,
            repo_root,
            git=config.git,
            on_progress=hook,
            max_workers=max_workers,
            mass_revs=mass,
            detail_sink=sink,
            blobs=blobs,
        )
    else:
        fresh = _BlamePass(ignore_revs_file=display, ignore_revs_applied=ignore is not None)
        if on_progress is not None and path_list:
            on_progress(len(path_list), len(path_list))
    coverage = {**reused, **fresh.coverage}
    lines = {path: dict(counts) for path, counts in fresh.lines.items()}
    for path in reused:
        stored = saved.get(path)
        if stored is not None:
            lines[path] = _author_counts(stored.lines, emails)
    passed = _BlamePass(
        coverage=dict(sorted(coverage.items())),
        ignore_revs_file=fresh.ignore_revs_file or display,
        ignore_revs_applied=fresh.ignore_revs_applied or ignore is not None,
        truncated=fresh.truncated,
        lines=lines,
        mass=fresh.mass,
    )
    if fresh.truncated:
        return passed, {}, inputs, True
    for path, detail in sink.items():
        blob = blobs.get(path) or _blob_shas(repo_root, [path]).get(path, "")
        if detail.reusable and blob and detail.commits:
            saved[path] = BlameLines(blob_sha=blob, lines=dict(detail.commits))
    return passed, saved, inputs, False


def _parse_log_output(stdout: str) -> list[_RawCommit]:
    return [
        _RawCommit(author=record.author, timestamp=record.timestamp, files=record.files)
        for record in _parse_log(stdout)
        if record.files
    ]


def _parse_log(stdout: str) -> list[_ParsedCommit]:
    if not stdout.strip():
        return []
    records: list[_ParsedCommit] = []
    for chunk in stdout.split(_COMMIT_SENTINEL):
        parsed = _parse_commit_chunk(chunk)
        if parsed is not None:
            records.append(parsed)
    return records


def _parse_commit_chunk(chunk: str) -> _ParsedCommit | None:
    marker = f"\n{_BODY_START}\n"
    end_marker = f"\n{_BODY_END}"
    start = chunk.find(marker)
    if start < 0:
        return None
    header = chunk[:start].lstrip("\n")
    rest = chunk[start + len(marker) :]
    end = rest.rfind(end_marker)
    if end < 0:
        return None
    body = rest[:end]
    files_blob = rest[end + len(end_marker) :]
    lines = header.splitlines()
    if len(lines) < 4:
        return None
    sha = lines[0].strip()
    author_name, author = _split_author(lines[1].strip())
    timestamp = _parse_timestamp(lines[2].strip())
    if timestamp is None or not author or _FULL_SHA_RE.fullmatch(sha) is None:
        return None
    subject = lines[4] if len(lines) > 4 else ""
    files, moves = _parse_status_paths(files_blob)
    return _ParsedCommit(
        sha=sha,
        author=author,
        timestamp=timestamp,
        files=files,
        contacts=coauthor_contacts(body),
        parent_count=len(lines[3].split()),
        squash_subject=_SQUASH_SUBJECT.search(subject) is not None,
        author_name=author_name,
        subject=subject,
        moves=moves,
    )


def _split_author(line: str) -> tuple[str, str]:
    if "\t" not in line:
        return "", line
    name, email = line.split("\t", 1)
    return name.strip(), email.strip()


def _author_index(commits: list[_RawCommit]) -> AuthorIndex:
    shas: dict[str, str] = {}
    names: dict[str, set[str]] = {}
    for commit in commits:
        if commit.sha:
            shas.setdefault(commit.author, commit.sha)
        if commit.author_name:
            names.setdefault(commit.author, set()).add(commit.author_name)
    return AuthorIndex(
        shas=shas,
        names={author: tuple(sorted(values)) for author, values in names.items()},
    )


def _cap_mechanical_authors(
    contributions: dict[str, dict[str, Contribution]],
    commits: list[_RawCommit],
) -> dict[str, dict[str, Contribution]]:
    mechanical = mechanical_authors(tuple((commit.author, commit.subject) for commit in commits))
    if not mechanical:
        return contributions
    return {
        path: {
            author: replace(contrib, credit=min(contrib.credit, 1.0))
            if author in mechanical
            else contrib
            for author, contrib in authors.items()
        }
        for path, authors in contributions.items()
    }


def _detect_strategy(records: list[_ParsedCommit]) -> ReportedMergeStrategy:
    observed = len(records)
    if observed == 0:
        return "rebase"
    merges = sum(1 for record in records if record.parent_count >= 2)
    if _share_at_least(merges, observed, _MERGE_COMMIT_MIN_PERCENT):
        return "merge"
    rest = observed - merges
    squash_subjects = sum(
        1 for record in records if record.parent_count < 2 and record.squash_subject
    )
    if _share_at_least(squash_subjects, rest, _SQUASH_SUBJECT_MIN_PERCENT):
        return "squash"
    return "rebase"


def _share_at_least(part: int, whole: int, percent: int) -> bool:
    return whole > 0 and part * 100 >= whole * percent


def _applied_strategy(
    configured: ConfiguredMergeStrategy,
    detected: ReportedMergeStrategy,
) -> ReportedMergeStrategy:
    if configured == "auto":
        return detected
    return configured


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _status_score(status: str) -> int:
    digits = "".join(char for char in status if char.isdigit())
    if not digits:
        return 100
    return max(0, min(100, int(digits)))


def _parse_status_paths(blob: str) -> tuple[tuple[str, ...], tuple[PathMove, ...]]:
    files: list[str] = []
    moves: list[PathMove] = []
    for raw in blob.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "\t" not in line:
            files.append(line)
            continue
        status, remainder = line.split("\t", 1)
        kind = status[:1]
        if kind in {"R", "C"} and "\t" in remainder:
            source, dest = remainder.split("\t", 1)
            files.append(dest)
            moves.append(PathMove(kind=kind, score=_status_score(status), source=source, dest=dest))
            continue
        if kind in {"A", "M", "D", "T", "U"}:
            files.append(remainder.split("\t")[-1])
            continue
        files.append(line)
    return tuple(files), tuple(moves)


def _logical_id(path: str, taken: set[str], nonce: str) -> str:
    digest = hashlib.sha256(path.encode()).hexdigest()[:16]
    if digest not in taken:
        return digest
    return hashlib.sha256(f"{path}\n{nonce}".encode()).hexdigest()[:16]


def _follow_path(path: str, forward: Mapping[str, str]) -> str:
    seen: set[str] = set()
    while path in forward and path not in seen:
        seen.add(path)
        path = forward[path]
    return path


def _parent_dir(path: str) -> str:
    slash = path.rfind("/")
    if slash < 0:
        return ""
    return path[:slash]


def _common_directory(paths: list[str]) -> str:
    parents = [_parent_dir(path) for path in paths]
    if not parents or any(parent == "" for parent in parents):
        return ""
    common = parents[0]
    for parent in parents[1:]:
        while parent != common and not parent.startswith(f"{common}/"):
            slash = common.rfind("/")
            if slash < 0:
                return ""
            common = common[:slash]
        if not common:
            return ""
    return common


def _collapse_directory_moves(
    moves: tuple[PathMove, ...],
    living: Mapping[str, str],
) -> tuple[PathMove, ...]:
    renames = [move for move in moves if move.kind == "R"]
    if len(renames) < 2:
        return moves
    old_prefix = _common_directory([move.source for move in renames])
    new_prefix = _common_directory([move.dest for move in renames])
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return moves
    old_root = f"{old_prefix}/"
    new_root = f"{new_prefix}/"
    sources: set[str] = set()
    for move in renames:
        if not move.source.startswith(old_root) or not move.dest.startswith(new_root):
            return moves
        if move.source[len(old_root) :] != move.dest[len(new_root) :]:
            return moves
        sources.add(move.source)
    for path in living:
        if path.startswith(old_root) and path not in sources:
            return moves
    others = tuple(move for move in moves if move.kind != "R")
    score = min(move.score for move in renames)
    return (*others, PathMove(kind="D", score=score, source=old_prefix, dest=new_prefix))


def _apply_renames(
    living: dict[str, str],
    origin: dict[str, str],
    priors: dict[str, list[str]],
    confidence: dict[str, int],
    moves: tuple[PathMove, ...],
    nonce: str,
) -> None:
    sources: dict[str, str] = {}
    for move in moves:
        if move.kind == "R" and move.source in living:
            sources[move.source] = living.pop(move.source)
    for move in moves:
        if move.kind != "R":
            continue
        logical = sources.get(move.source)
        if logical is None:
            taken = set(origin)
            logical = _logical_id(move.source, taken, nonce)
            origin[logical] = move.source
            confidence[logical] = 100
        priors.setdefault(logical, []).append(move.source)
        confidence[logical] = min(confidence.get(logical, 100), move.score)
        living[move.dest] = logical


def _apply_directory_move(
    living: dict[str, str],
    priors: dict[str, list[str]],
    confidence: dict[str, int],
    move: PathMove,
) -> None:
    prefix = f"{move.source}/"
    dest_prefix = f"{move.dest}/"
    matched = [path for path in living if path.startswith(prefix)]
    for path in matched:
        logical = living.pop(path)
        dest = dest_prefix + path[len(prefix) :]
        priors.setdefault(logical, []).append(path)
        confidence[logical] = min(confidence.get(logical, 100), move.score)
        living[dest] = logical


def _apply_copy(
    living: dict[str, str],
    origin: dict[str, str],
    priors: dict[str, list[str]],
    confidence: dict[str, int],
    move: PathMove,
    nonce: str,
) -> str | None:
    source_id = living.get(move.source)
    taken = set(origin)
    new_id = _logical_id(move.dest, taken, nonce)
    living[move.dest] = new_id
    origin[new_id] = move.dest
    priors.setdefault(new_id, []).append(move.source)
    confidence[new_id] = move.score
    return source_id


def _with_lineage(history: _CommitHistory) -> _CommitHistory:
    ordered = sorted(history.commits, key=lambda commit: (commit.timestamp, commit.sha))
    living: dict[str, str] = {}
    origin: dict[str, str] = {}
    priors: dict[str, list[str]] = {}
    confidence: dict[str, int] = {}
    files_by_sha: dict[str, tuple[str, ...]] = {}
    applied: dict[str, tuple[PathMove, ...]] = {}
    for commit in ordered:
        raw_moves = history.moves.get(commit.sha, ())
        commit_moves = _collapse_directory_moves(raw_moves, living)
        applied[commit.sha] = commit_moves
        _apply_renames(living, origin, priors, confidence, commit_moves, commit.sha)
        for move in commit_moves:
            if move.kind == "D":
                _apply_directory_move(living, priors, confidence, move)
        copied: list[tuple[str, str]] = []
        for move in commit_moves:
            if move.kind != "C":
                continue
            source_id = _apply_copy(living, origin, priors, confidence, move, commit.sha)
            new_id = living.get(move.dest)
            if source_id is not None and new_id is not None:
                copied.append((new_id, source_id))
        identities: list[str] = []
        for path in commit.files:
            if path not in living:
                logical = _logical_id(path, set(origin), commit.sha)
                living[path] = logical
                origin[logical] = path
                confidence.setdefault(logical, 100)
            identities.append(living[path])
        files_by_sha[commit.sha] = tuple(dict.fromkeys(identities))
        for new_id, source_id in copied:
            for sha, file_ids in files_by_sha.items():
                if source_id in file_ids and new_id not in file_ids:
                    files_by_sha[sha] = (*file_ids, new_id)
    id_to_path = {logical: path for path, logical in living.items()}
    rewritten: list[_RawCommit] = []
    for commit in history.commits:
        rewritten.append(replace(commit, files=files_by_sha.get(commit.sha, ())))
    path_confidence = {path: confidence.get(logical, 100) / 100 for path, logical in living.items()}
    path_priors = {
        path: _bounded_priors(priors.get(logical, []), path) for path, logical in living.items()
    }
    path_ids = dict(living)
    return replace(
        history,
        commits=rewritten,
        moves={sha: moves for sha, moves in applied.items() if moves},
        id_to_path=id_to_path,
        confidence=path_confidence,
        priors=path_priors,
        logical_ids=path_ids,
    )


def _bounded_priors(previous: list[str], current: str) -> tuple[str, ...]:
    seen: set[str] = set()
    names: list[str] = []
    for item in previous:
        if item == current or item in seen:
            break
        seen.add(item)
        names.append(item)
    return tuple(names)


def _finish_history(history: _CommitHistory, *, follow_renames: bool) -> _CommitHistory:
    if not follow_renames:
        return history
    return _with_lineage(history)


def _current_commits(history: _CommitHistory) -> list[_RawCommit]:
    if not history.id_to_path:
        return history.commits
    mapped: list[_RawCommit] = []
    for commit in history.commits:
        paths = tuple(
            dict.fromkeys(
                history.id_to_path[item] for item in commit.files if item in history.id_to_path
            )
        )
        mapped.append(replace(commit, files=paths))
    return mapped


def _remap_contributions(
    contributions: dict[str, dict[str, Contribution]],
    id_to_path: Mapping[str, str],
) -> dict[str, dict[str, Contribution]]:
    if not id_to_path:
        return contributions
    remapped: dict[str, dict[str, Contribution]] = {}
    for logical, authors in contributions.items():
        current = id_to_path.get(logical)
        if current is None:
            continue
        remapped[current] = authors
    return remapped


def path_lineage(
    repo_root: Path,
    target: str,
    config: Config,
    as_of: datetime,
) -> tuple[tuple[str, ...], str, float]:
    """Return prior names, logical id, and rename confidence for `target`."""
    history = _get_commit_history(
        repo_root,
        lookback_start(config.analysis.lookback_days, as_of),
        as_of,
        use_mailmap=config.git.use_mailmap,
        pathspec=(target,),
        count_co_authors=config.git.count_co_authors,
        merge_strategy=config.git.merge_strategy,
        follow_renames=True,
        rename_similarity=config.analysis.rename_similarity,
    )
    return (
        history.priors.get(target, ()),
        history.logical_ids.get(target, ""),
        history.confidence.get(target, 1.0),
    )


def _scale_lineage_confidence(
    ownership: OwnershipMap,
    confidence: Mapping[str, float],
) -> OwnershipMap:
    factors = {path: value for path, value in confidence.items() if value < 1.0}
    if not factors:
        return ownership

    def scale(path: str, owner: OwnerEntry) -> OwnerEntry:
        factor = factors.get(path)
        if factor is None:
            return owner
        return replace(owner, evidence_quality=_clamp(owner.evidence_quality * factor))

    paths = {
        path: replace(
            path_ownership,
            owners=tuple(scale(path, owner) for owner in path_ownership.owners),
            candidates=tuple(scale(path, owner) for owner in path_ownership.candidates),
            scored_owners=tuple(scale(path, owner) for owner in path_ownership.scored_owners),
        )
        for path, path_ownership in ownership.paths.items()
    }
    return replace(ownership, paths=paths)


def _aggregate_contributions(
    commits: list[_RawCommit],
    *,
    co_author_weight: float = 1.0,
) -> dict[str, dict[str, Contribution]]:
    """Aggregate per-(path, author) counts, credit, cadence, and later commits."""
    events: dict[str, list[tuple[str, datetime, float, str]]] = {}
    for commit in commits:
        people = [
            (commit.author, 1.0),
            *((email, co_author_weight) for email in commit.coauthors),
        ]
        for file_path in commit.files:
            bucket = events.setdefault(file_path, [])
            for author, weight in people:
                bucket.append((author, commit.timestamp, weight, commit.sha))
    result: dict[str, dict[str, Contribution]] = {}
    for path in sorted(events):
        path_events = events[path]
        counts: dict[str, int] = {}
        credits_by_author: dict[str, float] = {}
        latest: dict[str, datetime] = {}
        latest_sha: dict[str, str] = {}
        for author, timestamp, weight, sha in path_events:
            counts[author] = counts.get(author, 0) + 1
            credits_by_author[author] = credits_by_author.get(author, 0.0) + weight
            prior = latest.get(author)
            if prior is None or timestamp > prior:
                latest[author] = timestamp
                latest_sha[author] = sha
        cadence = _cadence_days([timestamp for _author, timestamp, _weight, _sha in path_events])
        result[path] = {
            author: Contribution(
                commits=commits_n,
                last_commit=latest[author],
                later_commits=sum(
                    1
                    for other, timestamp, _weight, _sha in path_events
                    if other != author and timestamp > latest[author]
                ),
                cadence_days=cadence,
                credit=credits_by_author[author],
                last_sha=latest_sha[author],
            )
            for author, commits_n in sorted(counts.items())
        }
    return result


_COCHANGE_COMMIT_CAP = 20


def _path_activity(
    commits: list[_RawCommit],
    kept: frozenset[str],
) -> dict[str, tuple[int, float | None, float | None]]:
    """Return `(change_events, cadence_days, cochange_degree)` for each path in `kept`."""
    counts = dict.fromkeys(kept, 0)
    timestamps: dict[str, list[datetime]] = {path: [] for path in kept}
    partners: dict[str, set[str]] = {path: set() for path in kept}
    for commit in commits:
        seen: list[str] = []
        seen_set: set[str] = set()
        for file_path in commit.files:
            if file_path in kept and file_path not in seen_set:
                seen.append(file_path)
                seen_set.add(file_path)
        for file_path in seen:
            counts[file_path] += 1
            timestamps[file_path].append(commit.timestamp)
        if len(set(commit.files)) > _COCHANGE_COMMIT_CAP or len(seen) < 2:
            continue
        for file_path in seen:
            partners[file_path].update(other for other in seen if other != file_path)
    degrees = {path: len(partners[path]) for path in kept}
    max_degree = max(degrees.values(), default=0)
    activity: dict[str, tuple[int, float | None, float | None]] = {}
    for path in kept:
        degree = degrees[path]
        coupling = degree / max_degree if max_degree > 0 and degree > 0 else None
        activity[path] = (counts[path], _cadence_days(timestamps[path]), coupling)
    return activity


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


def _filter_bot_authors(
    contributions: dict[str, dict[str, Contribution]],
    extra: Sequence[str] = (),
) -> dict[str, dict[str, Contribution]]:
    result: dict[str, dict[str, Contribution]] = {}
    for path, authors in contributions.items():
        humans = {a: c for a, c in authors.items() if not is_bot_identity(a, extra)}
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
    return any(fnmatch_posix(path, pattern) for pattern in patterns)


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
    required = ".".join(str(part) for part in MIN_GIT_VERSION)
    try:
        result = run_git(repo_root, ["git", "version"], check=True)
    except OSError as exc:
        msg = (
            f"checkOwners requires Git {required} or newer; found none. "
            "Install a newer Git and rerun."
        )
        raise GitRequirementError(msg) from exc
    try:
        found = parse_git_version(result.stdout)
    except ValueError as exc:
        raise GitRequirementError(str(exc)) from exc
    if found < MIN_GIT_VERSION:
        pretty = ".".join(str(part) for part in found)
        msg = (
            f"checkOwners requires Git {required} or newer; found {pretty}. "
            "Install a newer Git and rerun."
        )
        raise GitRequirementError(msg)


def _git_config_value(repo_root: Path, key: str) -> str:
    try:
        result = run_git(repo_root, ["git", "config", "--get", key], check=True)
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
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_mailmap_file(repo_root: Path) -> Path | None:
    candidate = repo_root / ".mailmap"
    if candidate.is_file():
        return candidate
    return None


def _tracked_file_count(repo_root: Path) -> int:
    result = run_git(repo_root, ["git", "ls-files"], check=True, paths=True)
    return sum(1 for line in result.stdout.splitlines() if line)


def _parse_modified_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    current_sha = ""
    modified = 0

    def flush() -> None:
        nonlocal current_sha, modified
        if current_sha:
            counts[current_sha] = modified
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
    return counts


def _modified_counts(repo_root: Path, revision: str | None = None) -> dict[str, int]:
    argv = ["git", "log", "--name-status", "--pretty=format:%H"]
    if revision:
        argv.append(revision)
    result = run_git(repo_root, argv, check=True, paths=True)
    return _parse_modified_counts(result.stdout)


def _mass_shas(counts: Mapping[str, int], tracked: int, fraction: float) -> tuple[str, ...]:
    if fraction <= 0.0 or tracked <= 0:
        return ()
    return tuple(sha for sha, modified in counts.items() if modified / tracked >= fraction)


def _mass_refactor_revs(repo_root: Path, fraction: float) -> tuple[str, ...]:
    if fraction <= 0.0:
        return ()
    tracked = _tracked_file_count(repo_root)
    if tracked == 0:
        return ()
    return _mass_shas(_modified_counts(repo_root), tracked, fraction)


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
    revision: str | None = None,
) -> list[str]:
    argv = ["git", "blame", "--line-porcelain", "-w", mailmap_flag(git.use_mailmap)]
    if git.detect_moves:
        argv.extend(["-M", "-C"])
    if ignore_revs is not None:
        argv.append(f"--ignore-revs-file={ignore_revs}")
    if extra_revs is not None:
        argv.append(f"--ignore-revs-file={extra_revs}")
    if revision is not None:
        argv.append(revision)
    return argv


def set_blame_cache_enabled(enabled: bool) -> None:
    _USE_BLAME_CACHE.set(enabled)


def set_blame_verbose(enabled: bool) -> None:
    _BLAME_VERBOSE.set(enabled)


def _file_bytes(path: Path | None) -> bytes:
    if path is None:
        return b""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _blame_cache_digest(
    blob: str,
    argv: list[str],
    *,
    path: str,
    ignore_bytes: bytes,
    mass: tuple[str, ...],
    mailmap_bytes: bytes,
) -> str:
    flags = tuple(
        part for part in argv if part in {"-w", "-M", "-C", "--use-mailmap", "--no-use-mailmap"}
    )
    payload = json.dumps(
        {
            "blob": blob,
            "path": path,
            "flags": flags,
            "ignore": ignore_bytes.decode("utf-8", "surrogateescape"),
            "mailmap": mailmap_bytes.decode("utf-8", "surrogateescape"),
            "mass": mass,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _fractions_from_counts(counts: Mapping[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {}
    return dict(sorted((author, count / total) for author, count in counts.items()))


def gather_blame_coverage(
    paths: Iterable[str],
    repo_root: Path,
    *,
    git: GitConfig | None = None,
    on_progress: ProgressHook | None = None,
    max_workers: int | None = None,
    mass_revs: tuple[str, ...] | None = None,
    detail_sink: dict[str, _BlameDetail] | None = None,
    revision: str | None = None,
    blobs: Mapping[str, str] | None = None,
    report_cache: bool = True,
) -> _BlamePass:
    """Return author coverage for `paths`, reusing `mass_revs` when it is passed."""
    git_config = git if git is not None else GitConfig()
    path_list = sorted(paths)
    total = len(path_list)
    if total == 0:
        return _BlamePass()
    _ensure_git_version(repo_root)
    ignore_revs = _resolve_ignore_revs_file(repo_root, git_config.blame_ignore_revs_file)
    extra_path: Path | None = None
    try:
        mass = (
            mass_revs
            if mass_revs is not None
            else _mass_refactor_revs(repo_root, git_config.mass_refactor_file_fraction)
        )
        if mass:
            extra_path = _write_ignore_revs(mass)
        argv = _blame_argv(git_config, ignore_revs, extra_path, revision)
        if not _blame_accepts_mailmap_flags(repo_root):
            argv = _drop_mailmap_flags(argv)
        mailmap_bytes = _file_bytes(repo_root / ".mailmap")
        ignore_bytes = _file_bytes(ignore_revs)
        mass_key = tuple(sorted(mass))
        if blobs is not None:
            resolved_blobs = dict(blobs)
        elif revision is not None:
            resolved_blobs = _blobs_at_revision(repo_root, revision, path_list)
        else:
            resolved_blobs = _blob_shas(repo_root, path_list)
        use_cache = _USE_BLAME_CACHE.get()
        coverage: dict[str, dict[str, float]] = {}
        line_counts: dict[str, dict[str, int]] = {}
        pending_paths: list[tuple[str, str | None]] = []
        hits = 0
        sink = detail_sink if detail_sink is not None else {}
        for path in path_list:
            blob = resolved_blobs.get(path, "")
            digest = (
                _blame_cache_digest(
                    blob,
                    argv,
                    path=path,
                    ignore_bytes=ignore_bytes,
                    mass=mass_key,
                    mailmap_bytes=mailmap_bytes,
                )
                if use_cache and blob
                else ""
            )
            cached = read_blame_cache(digest) if digest else None
            if cached is None:
                pending_paths.append((path, digest or None))
                continue
            hits += 1
            line_counts[path] = dict(cached)
            fractions = _fractions_from_counts(cached)
            if fractions:
                coverage[path] = fractions
        if report_cache and _BLAME_VERBOSE.get():
            print(
                f"blame cache: hits={hits} misses={len(pending_paths)}",
                file=sys.stderr,
            )
        workers = max_workers if max_workers is not None else min(32, os.cpu_count() or 4)
        workers = max(workers, 1)
        done = hits
        truncated = False
        deadline = _BLAME_DEADLINE.get()
        if on_progress is not None and hits == total:
            on_progress(total, total)
        with (
            _suppress_workdir_mailmap(repo_root, enabled=git_config.use_mailmap),
            ThreadPoolExecutor(max_workers=workers) as pool,
        ):
            pending: list[tuple[str, Future[dict[str, float]]]] = []
            for path, cache_id in pending_paths:
                if deadline is not None and time.monotonic() >= deadline:
                    truncated = True
                    break
                pending.append(
                    (
                        path,
                        pool.submit(
                            _blame_for_path,
                            repo_root,
                            path,
                            argv,
                            sink,
                            cache_id if use_cache else None,
                        ),
                    )
                )
            for path, future in pending:
                per_author = future.result()
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
                if per_author:
                    coverage[path] = per_author
                detail = sink.get(path)
                if detail is not None:
                    line_counts[path] = dict(detail.lines)
        display = _display_ignore_revs_path(repo_root, ignore_revs) if ignore_revs else ""
        return _BlamePass(
            coverage=dict(sorted(coverage.items())),
            ignore_revs_file=display,
            ignore_revs_applied=ignore_revs is not None,
            truncated=truncated,
            lines={
                path: dict(sorted(counts.items())) for path, counts in sorted(line_counts.items())
            },
            mass=tuple(mass),
        )
    finally:
        if extra_path is not None:
            extra_path.unlink(missing_ok=True)


def _drop_mailmap_flags(argv: list[str]) -> list[str]:
    return [part for part in argv if part not in _MAILMAP_FLAGS]


def _blame_accepts_mailmap_flags(repo_root: Path) -> bool:
    result = run_git(repo_root, ["git", "blame", "--use-mailmap"], check=False)
    return "unknown option" not in f"{result.stdout}{result.stderr}"


def _blame_for_path(
    repo_root: Path,
    path: str,
    argv: list[str],
    sink: dict[str, _BlameDetail] | None = None,
    digest: str | None = None,
) -> dict[str, float]:
    """Run git blame with the prebuilt argv on one path; return coverage fractions."""
    try:
        result = run_git(repo_root, [*argv, "--", path], check=True, paths=True)
    except subprocess.CalledProcessError:
        detail = _BlameDetail(fractions={}, commits={}, reusable=False)
    else:
        detail = _parse_blame_detail(result.stdout)
        if digest is not None:
            write_blame_cache(digest, detail.lines)
    if sink is not None:
        with _BLAME_SINK_LOCK:
            sink[path] = detail
    return detail.fractions


def _parse_blame_output(stdout: str) -> dict[str, float]:
    return _parse_blame_detail(stdout).fractions


def _parse_blame_detail(stdout: str) -> _BlameDetail:
    counts: dict[str, int] = {}
    commits: dict[str, int] = {}
    total = 0
    reusable = True
    current = ""
    for line in stdout.splitlines():
        if "\t" not in line:
            token = line.split(" ", 1)[0]
            if _FULL_SHA_RE.fullmatch(token):
                current = token
                if token == "0" * 40:
                    reusable = False
                continue
        if not line.startswith("author-mail "):
            continue
        email = line[len("author-mail ") :].strip().strip("<>")
        counts[email] = counts.get(email, 0) + 1
        total += 1
        if not current or not reusable:
            reusable = False
            continue
        commits[current] = commits.get(current, 0) + 1
    if total == 0:
        return _BlameDetail(fractions={}, commits={}, reusable=False)
    fractions = dict(sorted((author, count / total) for author, count in counts.items()))
    return _BlameDetail(
        fractions=fractions,
        commits=commits,
        reusable=reusable,
        lines=dict(sorted(counts.items())),
    )

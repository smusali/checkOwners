"""Git history analysis for confidence-scored ownership inference."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from checkowners import blame, git, history
from checkowners.config import (
    Config,
    GitConfig,
    ScoringConfig,
    load_config,
)
from checkowners.expertise import path_matches_glob
from checkowners.identity import bot_identities
from checkowners.models import (
    AnalysisCompleteness,
    DecayWarning,
    FreshnessStatus,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    QualificationStrategy,
    ReportedMergeStrategy,
    history_evidence_gaps,
    merge_gaps,
    with_gaps,
)
from checkowners.providers import (
    LayeredOwnership,
    collection_gaps,
    path_evidence,
    reconcile_layers,
    reset_review_window,
    review_was_omitted,
    review_window,
)
from checkowners.scoring import (
    Contribution,
    clamp,
    combine_available_signals,
    elapsed_days,
    frequency_credit,
    frequency_prior_for,
    score_owners,
    signal_reliabilities,
    signal_weights,
)
from checkowners.state import (
    BlameLines,
    HistoryBaseline,
    PathMove,
    read_history_baseline,
)

#: A review provider maps a set of contributor emails to per-path, per-email
#: review-coverage fractions (path -> {email: fraction in [0, 1]}). It is
#: injected so analyze.py itself stays free of network calls; the CLI supplies
#: a GitHub-backed implementation only when the API is enabled.
ReviewProvider = Callable[[set[str]], dict[str, dict[str, float]]]


@dataclass(frozen=True)
class AnalysisOutcome:
    ownership: OwnershipMap
    baseline: HistoryBaseline
    mode: Literal["full", "incremental"]
    fallback: str
    prior_ref: str
    authors: history.AuthorIndex = field(default_factory=history.AuthorIndex)


@dataclass(frozen=True)
class AnalysisResult:
    """One analysis run: the map, the author index, and the replay outcome.

    `outcome` is None unless the run was asked to collect a baseline.
    """

    ownership: OwnershipMap
    authors: history.AuthorIndex
    outcome: AnalysisOutcome | None = None


class RepositoryAnalyzer:
    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.root = root
        self.config = load_config(root) if config is None else config

    def analyze(self) -> OwnershipMap:
        return analyze_ownership(self.root, self.config)

    def reconcile(self) -> tuple[LayeredOwnership, ...]:
        return reconcile_layers(self.root, self.analyze(), self.config)


def analyze_ownership(
    repo_root: Path,
    config: Config,
    *,
    review_provider: ReviewProvider | None = None,
    on_progress: blame.ProgressHook | None = None,
    as_of: datetime | None = None,
    analysis_ref: str | None = None,
    max_workers: int | None = None,
    pathspec: tuple[str, ...] | None = None,
    retain_all: bool = False,
    incremental: bool = False,
    revision: str | None = None,
    mass_revs: tuple[str, ...] | None = None,
) -> OwnershipMap:
    """Analyze git history and return a confidence-scored ownership map.

    When ``review_provider`` is supplied, its per-path, per-email review
    coverage feeds the review signal; otherwise review is unavailable and
    remaining weights are renormalized. ``on_progress`` receives
    (done, total) updates while the per-file blame pass runs.

    ``as_of`` is the instant recency and decay are scored against. When
    omitted, ``git.resolve_as_of(None, repo_root)`` supplies it (SOURCE_DATE_EPOCH
    or HEAD, never the wall clock). ``analysis_ref`` is the commit SHA; when
    omitted, HEAD is used. ``pathspec`` limits git log and blame to those
    paths. ``retain_all`` stores every scored author on ``candidates``;
    inferred ``owners`` still apply the confidence threshold and top-N cap.
    ``incremental`` replays a stored baseline when one is usable.
    ``revision`` logs and blames that commit instead of HEAD.
    ``mass_revs`` is passed through to blame; an empty tuple skips the scan.
    """
    return analyze_repository(
        repo_root,
        config,
        review_provider=review_provider,
        on_progress=on_progress,
        as_of=as_of,
        analysis_ref=analysis_ref,
        max_workers=max_workers,
        pathspec=pathspec,
        retain_all=retain_all,
        incremental=incremental,
        revision=revision,
        mass_revs=mass_revs,
    ).ownership


def analyze_repository(
    repo_root: Path,
    config: Config,
    *,
    review_provider: ReviewProvider | None = None,
    on_progress: blame.ProgressHook | None = None,
    as_of: datetime | None = None,
    analysis_ref: str | None = None,
    max_workers: int | None = None,
    pathspec: tuple[str, ...] | None = None,
    retain_all: bool = False,
    incremental: bool = False,
    capture_baseline: bool = False,
    revision: str | None = None,
    mass_revs: tuple[str, ...] | None = None,
) -> AnalysisResult:
    """Analyze git history and return the map, the authors, and the outcome.

    The keyword arguments are those of ``analyze_ownership``.
    ``capture_baseline`` also returns the baseline that reproduces the run.
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
        return AnalysisResult(
            ownership=outcome.ownership,
            authors=outcome.authors,
            outcome=outcome if capture_baseline else None,
        )
    when = as_of if as_of is not None else git.resolve_as_of(None, repo_root)
    ref = analysis_ref if analysis_ref is not None else git.head_commit_sha(repo_root)
    reset_review_window()
    deadline = time.monotonic() + config.analysis.max_runtime_seconds
    deadline_token = blame.BLAME_DEADLINE.set(deadline)
    try:
        ownership, authors = _analyze_ownership(
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
        blame.BLAME_DEADLINE.reset(deadline_token)
    return AnalysisResult(ownership=ownership, authors=authors)


def _analyze_ownership(
    repo_root: Path,
    config: Config,
    *,
    when: datetime,
    ref: str,
    review_provider: ReviewProvider | None,
    on_progress: blame.ProgressHook | None,
    max_workers: int | None,
    pathspec: tuple[str, ...] | None,
    retain_all: bool,
    deadline: float,
    prepared_history: history.CommitHistory | None = None,
    blame_runner: Callable[[Iterable[str]], blame.BlamePass] | None = None,
    renamed: bool | None = None,
    revision: str | None = None,
    mass_revs: tuple[str, ...] | None = None,
) -> tuple[OwnershipMap, history.AuthorIndex]:
    mailmap_path = git.resolve_mailmap_file(repo_root)
    log = prepared_history
    if log is None:
        log = history.get_commit_history(
            repo_root,
            history.lookback_start(config.analysis.lookback_days, when),
            when,
            use_mailmap=config.git.use_mailmap,
            pathspec=pathspec,
            count_co_authors=config.git.count_co_authors,
            merge_strategy=config.git.merge_strategy,
            tip=revision,
            follow_renames=config.analysis.follow_renames,
            rename_similarity=config.analysis.rename_similarity,
        )
    commits = log.commits
    authors = history.author_index(commits)
    contributions = history.remap_contributions(
        history.aggregate_contributions(
            commits,
            co_author_weight=config.git.co_author_weight,
        ),
        log.id_to_path,
    )
    commits = history.current_commits(log)
    if pathspec:
        contributions = _filter_to_pathspec(contributions, pathspec)
    linguist = (
        history.linguist_excluded_paths(repo_root, contributions)
        if config.analysis.respect_gitattributes
        else frozenset()
    )
    gitattributes_n = sum(1 for path in contributions if path in linguist)
    contributions = {
        path: authors for path, authors in contributions.items() if path not in linguist
    }
    before_static = len(contributions)
    contributions = history.filter_excluded(contributions, config.paths.exclude)
    static_n = before_static - len(contributions)
    contributions = history.filter_nonexistent(contributions, repo_root)
    contributions = history.cap_mechanical_authors(contributions, commits)
    if config.analysis.exclude_bots:
        contributions = history.filter_bot_authors(contributions, bot_identities(config))
    if config.qualification.strategy == "threshold":
        contributions = history.filter_unqualified(contributions, config.analysis.min_commits)
    if blame_runner is not None:
        blame_pass = blame_runner(contributions.keys())
    elif time.monotonic() >= deadline:
        blame_pass = blame.BlamePass(truncated=True)
    elif mass_revs is None and revision is None:
        blame_pass = blame.gather_blame_coverage(
            contributions.keys(),
            repo_root,
            git_config=config.git,
            on_progress=on_progress,
            max_workers=max_workers,
        )
    else:
        blame_pass = blame.gather_blame_coverage(
            contributions.keys(),
            repo_root,
            git_config=config.git,
            on_progress=on_progress,
            max_workers=max_workers,
            mass_revs=mass_revs,
            revision=revision,
        )
    review_coverage = _gather_review_coverage(contributions, review_provider)
    review_omitted = review_was_omitted()
    activity = history.path_activity(commits, frozenset(contributions))
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
        priors=log.priors,
        repo_root=repo_root,
        mass_revs=blame_pass.mass,
    )
    if renamed is None and config.analysis.follow_renames:
        detected = history.rename_commit_shas(
            repo_root,
            history.lookback_start(config.analysis.lookback_days, when),
            when,
        )
        renamed = any(sha not in log.moves for sha in detected)
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
        merge_strategy=log.merge_strategy,
        co_author_count=log.co_author_count,
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
    return history.scale_lineage_confidence(ownership, log.confidence), authors


def _run_completeness(
    repo_root: Path,
    config: Config,
    *,
    when: datetime,
    commits: list[history.RawCommit],
    mailmap_path: Path | None,
    blame_pass: blame.BlamePass,
    gitattributes_n: int,
    static_n: int,
    review_missing: bool,
    merge_strategy: ReportedMergeStrategy,
    co_author_count: int,
    renamed: bool | None = None,
) -> AnalysisCompleteness:
    since = history.lookback_start(config.analysis.lookback_days, when)
    gaps = history_evidence_gaps(
        shallow=git.is_shallow_repository(repo_root),
        insufficient=history.insufficient_history(repo_root, commits),
        renamed=(
            history.window_has_renames(repo_root, since, when) if renamed is None else renamed
        ),
        mailmap_missing=config.git.use_mailmap and mailmap_path is None,
        ignore_revs_missing=git.resolve_ignore_revs_file(
            repo_root, config.git.blame_ignore_revs_file
        )
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
        mailmap_file=(
            git.display_ignore_revs_path(repo_root, mailmap_path) if mailmap_path else ""
        ),
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
        return replace(owner, evidence_quality=clamp(quality * factor))

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
        qualified = qualify_authors(
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
        evidence = path_evidence(
            path,
            qualified,
            path_blame,
            path_review,
            blame_available=path in blame_coverage,
            review_available=review_available,
            survival=survival_by_path.get(path),
        )
        result[path] = PathOwnership(
            owners=top,
            qualified_owner_count=qualified_owner_count,
            decay_warnings=decay,
            candidates=entries if retain_all else (),
            scored_owners=filtered,
            change_events=events,
            cadence_days=cadence,
            cochange_degree=coupling,
            evidence=evidence,
        )
    return dict(sorted(result.items()))


def qualify_authors(
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


def review_window_start(config: Config, when: datetime) -> datetime | None:
    """Return the review window start for `config` at `when`, or None when it is open-ended."""
    days = config.analysis.review_lookback_days
    if days is None:
        return history.lookback_start(config.analysis.lookback_days, when)
    return when - timedelta(days=days)


def _survival_by_path(
    contributions: Mapping[str, Mapping[str, Contribution]],
    blamed: set[str],
    blame_lines: Mapping[str, Mapping[str, int]],
    priors: Mapping[str, tuple[str, ...]],
    repo_root: Path | None,
    git_config: GitConfig,
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
        blobs = git.blobs_at_revision(repo_root, revision, candidates)
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
        passed = blame.gather_blame_coverage(
            blobs,
            repo_root,
            git_config=git_config,
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
            None if denominator <= 0 else clamp(current / denominator)
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
                days_since_last_commit=elapsed_days(contrib.last_commit, now),
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
    if elapsed_days(contrib.last_commit, now) <= threshold_days:
        return None
    return "inactive"


def _count_qualified_owners(top: tuple[OwnerEntry, ...], threshold: float) -> int:
    return sum(1 for entry in top if entry.confidence >= threshold)


def _filter_to_pathspec(
    contributions: dict[str, dict[str, Contribution]],
    pathspec: tuple[str, ...],
) -> dict[str, dict[str, Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if any(path_matches_glob(path, spec) for spec in pathspec)
    }


class _ReplayError(Exception):
    pass


def collect_analysis(
    repo_root: Path,
    config: Config,
    *,
    review_provider: ReviewProvider | None = None,
    on_progress: blame.ProgressHook | None = None,
    as_of: datetime | None = None,
    analysis_ref: str | None = None,
    max_workers: int | None = None,
    incremental: bool = True,
) -> AnalysisOutcome:
    """Return scored ownership for `repo_root` and the baseline that reproduces it."""
    when = as_of if as_of is not None else git.resolve_as_of(None, repo_root)
    ref = analysis_ref if analysis_ref is not None else git.head_commit_sha(repo_root)
    reset_review_window()
    deadline = time.monotonic() + config.analysis.max_runtime_seconds
    token = blame.BLAME_DEADLINE.set(deadline)
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
        blame.BLAME_DEADLINE.reset(token)


def _collect_analysis(
    repo_root: Path,
    config: Config,
    *,
    when: datetime,
    ref: str,
    review_provider: ReviewProvider | None,
    on_progress: blame.ProgressHook | None,
    max_workers: int | None,
    incremental: bool,
    deadline: float,
) -> AnalysisOutcome:
    since = history.lookback_start(config.analysis.lookback_days, when)
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
        history.CommitHistory,
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
    log, files, modified, renames = window
    saved = _BlameSaved()

    def blame_runner(paths: Iterable[str]) -> blame.BlamePass:
        blame_pass, lines, blame_inputs, truncated = blame.coverage_with_reuse(
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

    ownership, authors = _analyze_ownership(
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
        prepared_history=log,
        blame_runner=blame_runner,
        renamed=(
            any(sha not in log.moves for sha in renames)
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
        rename_detection=git.rename_detection(repo_root),
        truncated=saved.truncated,
        follow_renames=config.analysis.follow_renames,
        rename_similarity=config.analysis.rename_similarity,
        moves=log.moves,
    )
    return AnalysisOutcome(
        ownership=ownership,
        baseline=baseline,
        mode=mode,
        fallback=fallback,
        prior_ref=prior_ref,
        authors=authors,
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
) -> tuple[history.CommitHistory, dict[str, tuple[str, ...]], dict[str, int], frozenset[str]]:
    records = history.parsed_commits(
        repo_root,
        since,
        when,
        use_mailmap=config.git.use_mailmap,
        name_only=not config.analysis.follow_renames,
        track_renames=config.analysis.follow_renames,
        rename_similarity=config.analysis.rename_similarity,
    )
    log = history.finish_history(
        history.history_from_records(
            repo_root,
            records,
            use_mailmap=config.git.use_mailmap,
            count_co_authors=config.git.count_co_authors,
            merge_strategy=config.git.merge_strategy,
        ),
        follow_renames=config.analysis.follow_renames,
    )
    files = {record.sha: record.files for record in records}
    return (
        log,
        files,
        git.modified_counts(repo_root),
        history.rename_commit_shas(repo_root, since, when),
    )


def _replay_window(
    repo_root: Path,
    config: Config,
    baseline: HistoryBaseline,
    since: datetime | None,
    when: datetime,
    head: str,
) -> tuple[history.CommitHistory, dict[str, tuple[str, ...]], dict[str, int], frozenset[str]]:
    records = history.parsed_commits(
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
        ancestors = git.ancestor_shas(repo_root, baseline.analysis_ref)
        if any(sha in ancestors for sha in missing):
            raise _ReplayError("incremental baseline does not cover this lookback window")
        fetched, fetched_moves = history.fetch_commit_files(
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
    attached: list[history.ParsedCommit] = []
    for record in records:
        paths = baseline.files.get(record.sha, fetched.get(record.sha, ()))
        files[record.sha] = paths
        attached.append(replace(record, files=paths))
    combined_moves = dict(baseline.moves)
    combined_moves.update(fetched_moves)
    log = replace(
        history.history_from_records(
            repo_root,
            attached,
            use_mailmap=config.git.use_mailmap,
            count_co_authors=config.git.count_co_authors,
            merge_strategy=config.git.merge_strategy,
        ),
        moves=combined_moves,
    )
    log = history.finish_history(log, follow_renames=config.analysis.follow_renames)
    revision = f"{baseline.analysis_ref}..{head}"
    modified = dict(baseline.modified)
    modified.update(git.modified_counts(repo_root, revision))
    delta_renames = history.rename_commit_shas(repo_root, since, when, revision)
    window_shas = set(files)
    renames = frozenset(sha for sha in baseline.renames | delta_renames if sha in window_shas)
    return log, files, modified, renames


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
    if not git.commit_exists(repo_root, baseline.analysis_ref):
        return "cached ref is missing"
    if not git.is_ancestor(repo_root, baseline.analysis_ref, head):
        return "cached ref is not an ancestor of HEAD"
    if baseline.rename_detection != git.rename_detection(repo_root):
        return "rename detection changed"
    active = config if config is not None else Config()
    if (
        baseline.follow_renames != active.analysis.follow_renames
        or baseline.rename_similarity != active.analysis.rename_similarity
    ):
        return "lineage settings changed"
    return ""

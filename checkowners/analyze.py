"""Git history analysis for confidence-scored ownership inference."""

from __future__ import annotations

import fnmatch
import math
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from checkowners.models import (
    ConfidenceScore,
    Config,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    QualificationStrategy,
    ScoringConfig,
    SignalScore,
)

_COMMIT_SENTINEL = "COMMIT_START"
FREQUENCY_SHRINKAGE_PRIOR = 3.0
SOURCE_DATE_EPOCH_ENV = "SOURCE_DATE_EPOCH"

#: A review provider maps a set of contributor emails to per-path, per-email
#: review-coverage fractions (path -> {email: fraction in [0, 1]}). It is
#: injected so analyze.py itself stays free of network calls; the CLI supplies
#: a GitHub-backed implementation only when the API is enabled.
ReviewProvider = Callable[[set[str]], dict[str, dict[str, float]]]

#: Optional progress hook: called as on_progress(done, total) while blame runs.
#: Injected by the CLI to drive a progress bar; analyze.py stays console-free.
ProgressHook = Callable[[int, int], None]


@dataclass(frozen=True)
class _Contribution:
    """Raw per-(path, author) signal aggregated from git log."""

    commits: int
    last_commit: datetime


@dataclass(frozen=True)
class _RawCommit:
    """A single commit's parsed metadata."""

    author: str
    timestamp: datetime
    files: tuple[str, ...]


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
) -> OwnershipMap:
    """Analyze git history and return a confidence-scored ownership map.

    When ``review_provider`` is supplied, its per-path, per-email review
    coverage feeds the review signal; otherwise review is unavailable and
    remaining weights are renormalized. ``on_progress`` receives
    (done, total) updates while the per-file blame pass runs.

    ``as_of`` is the instant recency and decay are scored against. When
    omitted, ``resolve_as_of(None, repo_root)`` supplies it (SOURCE_DATE_EPOCH
    or HEAD, never the wall clock). ``analysis_ref`` is the commit SHA; when
    omitted, HEAD is used.
    """
    when = as_of if as_of is not None else resolve_as_of(None, repo_root)
    ref = analysis_ref if analysis_ref is not None else head_commit_sha(repo_root)
    commits = _get_commit_history(repo_root, config.analysis.lookback_days, when)
    contributions = _aggregate_contributions(commits)
    contributions = _filter_excluded(contributions, config.paths.exclude)
    contributions = _filter_nonexistent(contributions, repo_root)
    if config.analysis.exclude_bots:
        contributions = _filter_bot_authors(contributions)
    if config.qualification.strategy == "threshold":
        contributions = _filter_unqualified(contributions, config.analysis.min_commits)
    blame_coverage = _gather_blame_coverage(
        contributions.keys(),
        repo_root,
        on_progress=on_progress,
        max_workers=max_workers,
    )
    review_coverage = _gather_review_coverage(contributions, review_provider)
    paths = _build_path_ownerships(
        contributions,
        blame_coverage,
        review_coverage,
        config,
        when,
        review_available=review_provider is not None,
    )
    return OwnershipMap(
        paths=dict(sorted(paths.items())),
        last_analyzed=when,
        analysis_ref=ref,
    )


def _gather_review_coverage(
    contributions: dict[str, dict[str, _Contribution]],
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
    contributions: dict[str, dict[str, _Contribution]],
    blame_coverage: dict[str, dict[str, float]],
    review_coverage: dict[str, dict[str, float]],
    config: Config,
    now: datetime,
    *,
    review_available: bool,
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
        entries = _score_owners(
            qualified,
            path_blame,
            path_review,
            max_commits=max_commits,
            scoring=config.scoring,
            now=now,
            blame_available=path in blame_coverage,
            review_available=review_available,
            frequency_prior=frequency_prior,
        )
        filtered = tuple(e for e in entries if e.confidence >= config.analysis.confidence_threshold)
        if not filtered:
            continue
        top = filtered[: config.analysis.top_n_owners]
        decay = _detect_decay(path, qualified, top, config.decay.threshold_days, now)
        qualified_owner_count = _count_qualified_owners(top, config.analysis.confidence_threshold)
        result[path] = PathOwnership(
            owners=top,
            qualified_owner_count=qualified_owner_count,
            decay_warnings=decay,
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
    authors: Mapping[str, _Contribution],
    *,
    min_commits: int,
    strategy: QualificationStrategy,
    path_blame: Mapping[str, float],
    blame_override: float,
) -> dict[str, _Contribution]:
    if strategy == "threshold":
        return {
            author: contrib for author, contrib in authors.items() if contrib.commits >= min_commits
        }
    return {
        author: contrib
        for author, contrib in authors.items()
        if contrib.commits >= min_commits or path_blame.get(author, 0.0) >= blame_override
    }


def _score_owners(
    qualified: dict[str, _Contribution],
    path_blame: dict[str, float],
    path_review: dict[str, float],
    *,
    max_commits: int,
    scoring: ScoringConfig,
    now: datetime,
    blame_available: bool,
    review_available: bool,
    frequency_prior: float = 0.0,
) -> tuple[OwnerEntry, ...]:
    scored: list[OwnerEntry] = []
    weights = signal_weights(scoring)
    reliabilities = signal_reliabilities(scoring)
    for author, contrib in qualified.items():
        recency = _recency_score(contrib.last_commit, now, scoring.recency_half_life_days)
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
        scored.append(
            OwnerEntry(
                handle=author,
                ownership_score=total,
                last_commit=contrib.last_commit,
                commits=contrib.commits,
                evidence_quality=quality,
                score_breakdown=breakdown,
            )
        )
    scored.sort(key=lambda e: (-e.confidence, e.handle))
    return tuple(scored)


def _recency_score(last_commit: datetime, now: datetime, half_life_days: int) -> float:
    if half_life_days <= 0:
        return 1.0
    delta_days = max(0.0, (now - last_commit).total_seconds() / 86400.0)
    return _clamp(math.pow(0.5, delta_days / half_life_days))


def _frequency_score(commits: int, max_commits: int, prior: float = 0.0) -> float:
    if max_commits <= 0:
        return 0.0
    return _clamp(commits / (max_commits + prior))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _detect_decay(
    path: str,
    qualified: dict[str, _Contribution],
    top: tuple[OwnerEntry, ...],
    threshold_days: int,
    now: datetime,
) -> tuple[DecayWarning, ...]:
    warnings: list[DecayWarning] = []
    for entry in top:
        contrib = qualified.get(entry.handle)
        if contrib is None:
            continue
        days = int((now - contrib.last_commit).total_seconds() // 86400)
        if days > threshold_days:
            warnings.append(
                DecayWarning(
                    handle=entry.handle,
                    path=path,
                    last_commit=contrib.last_commit,
                    days_since_last_commit=days,
                    historical_confidence=entry.confidence,
                )
            )
    return tuple(warnings)


def _count_qualified_owners(top: tuple[OwnerEntry, ...], threshold: float) -> int:
    return sum(1 for entry in top if entry.confidence >= threshold)


def _get_commit_history(repo_root: Path, since_days: int, as_of: datetime) -> list[_RawCommit]:
    """Run git log and parse (author, timestamp, files) triples."""
    since = as_of - timedelta(days=since_days)
    result = subprocess.run(  # noqa: S603  # literal git argv, no shell
        [  # noqa: S607  # git from PATH; not user-supplied
            "git",
            "log",
            f"--format={_COMMIT_SENTINEL}%n%ae%n%cI",
            "--name-only",
            f"--since={since.isoformat()}",
            f"--until={as_of.isoformat()}",
        ],
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
) -> dict[str, dict[str, _Contribution]]:
    """Aggregate per-(path, author) commit counts and most-recent commit time."""
    counts: dict[str, dict[str, int]] = {}
    latest: dict[str, dict[str, datetime]] = {}
    for commit in commits:
        for file_path in commit.files:
            counts.setdefault(file_path, {})
            latest.setdefault(file_path, {})
            counts[file_path][commit.author] = counts[file_path].get(commit.author, 0) + 1
            prior = latest[file_path].get(commit.author)
            if prior is None or commit.timestamp > prior:
                latest[file_path][commit.author] = commit.timestamp
    result: dict[str, dict[str, _Contribution]] = {}
    for path in sorted(counts):
        authors = counts[path]
        result[path] = {
            author: _Contribution(commits=commits_n, last_commit=latest[path][author])
            for author, commits_n in sorted(authors.items())
        }
    return result


def _filter_excluded(
    contributions: dict[str, dict[str, _Contribution]],
    exclude_patterns: tuple[str, ...],
) -> dict[str, dict[str, _Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if not _is_excluded(path, exclude_patterns)
    }


def _filter_nonexistent(
    contributions: dict[str, dict[str, _Contribution]],
    repo_root: Path,
) -> dict[str, dict[str, _Contribution]]:
    return {path: authors for path, authors in contributions.items() if (repo_root / path).exists()}


def _is_bot_email(email: str) -> bool:
    """True for automation authors (GitHub Apps sign as `name[bot]@...`)."""
    lowered = email.lower()
    return "[bot]" in lowered or lowered == "actions@github.com" or lowered.startswith("bot@")


def _filter_bot_authors(
    contributions: dict[str, dict[str, _Contribution]],
) -> dict[str, dict[str, _Contribution]]:
    result: dict[str, dict[str, _Contribution]] = {}
    for path, authors in contributions.items():
        humans = {a: c for a, c in authors.items() if not _is_bot_email(a)}
        if humans:
            result[path] = humans
    return result


def _filter_unqualified(
    contributions: dict[str, dict[str, _Contribution]],
    min_commits: int,
) -> dict[str, dict[str, _Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if any(contrib.commits >= min_commits for contrib in authors.values())
    }


def _is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _gather_blame_coverage(
    paths: Iterable[str],
    repo_root: Path,
    *,
    on_progress: ProgressHook | None = None,
    max_workers: int | None = None,
) -> dict[str, dict[str, float]]:
    """Run git blame per path (in parallel) and return author -> coverage.

    git blame is a subprocess, so threads parallelize cleanly; the worker
    count tracks the CPU count since blame is compute-bound inside git.
    """
    path_list = sorted(paths)
    total = len(path_list)
    if total == 0:
        return {}
    coverage: dict[str, dict[str, float]] = {}
    workers = max_workers if max_workers is not None else min(32, os.cpu_count() or 4)
    workers = max(workers, 1)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, per_author in zip(
            path_list, pool.map(lambda p: _blame_for_path(repo_root, p), path_list), strict=True
        ):
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            if per_author:
                coverage[path] = per_author
    return dict(sorted(coverage.items()))


def _blame_for_path(repo_root: Path, path: str) -> dict[str, float]:
    """Run `git blame --line-porcelain` on a single path; return coverage fractions."""
    try:
        result = subprocess.run(  # noqa: S603  # literal git argv, no shell
            ["git", "blame", "--line-porcelain", "--", path],  # noqa: S607  # git from PATH; path follows --
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

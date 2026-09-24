"""Path-scoped ownership explanations and ranked owner lists."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from checkowners import __version__
from checkowners.analyze import (
    _Contribution,
    _gather_blame_coverage,
    _mailmap_flag,
    _score_owners,
    frequency_prior_for,
    signal_weights,
)
from checkowners.expertise import path_matches_glob
from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    Config,
    OwnerEntry,
    OwnershipMap,
    ScoringConfig,
    SignalScore,
    models_payload,
    owner_json,
    path_analysis_json,
    risk_from_scores,
)

_SHA_CAP = 5
_SHA_LEN = 12
Assessment = Literal["aligned", "diverged", "unverifiable"]
TargetKind = Literal["file", "directory"]
WhyNotKind = Literal["below_threshold", "outside_window", "never_contributed", "outside_top_n"]


@dataclass(frozen=True)
class SignalEvidence:
    name: str
    score: float
    weight: float
    available: bool
    commits: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class ExplainedOwner:
    entry: OwnerEntry
    signals: tuple[SignalEvidence, ...]
    source_path: str


@dataclass(frozen=True)
class WhyNotResult:
    handle: str
    kind: WhyNotKind
    reasons: tuple[str, ...]
    ownership_score: float
    threshold: float
    signals: tuple[SignalEvidence, ...]
    knobs: tuple[str, ...]


@dataclass(frozen=True)
class PathExplanation:
    target: str
    kind: TargetKind
    files: tuple[str, ...]
    inferred: tuple[ExplainedOwner, ...]
    candidates: tuple[ExplainedOwner, ...]
    evidence_quality: float
    declared: tuple[str, ...]
    team_resolution: tuple[str, ...]
    assessment: Assessment
    lineage: tuple[str, ...]
    knobs: tuple[str, ...]
    weights: dict[str, float]
    why_not: WhyNotResult | None


def identities_match(handle: str, query: str) -> bool:
    """True when ``handle`` and ``query`` name the same person."""
    left = _normalize_identity(handle)
    right = _normalize_identity(query)
    if not left or not right:
        return False
    if left == right:
        return True
    left_local = left.split("@", 1)[0]
    right_local = right.split("@", 1)[0]
    return left_local == right or right_local == left


def matching_files(ownership: OwnershipMap, target: str) -> tuple[str, ...]:
    """Return ownership paths that match ``target`` as a file, glob, or directory."""
    return tuple(sorted(path for path in ownership.paths if path_matches_glob(path, target)))


def ranked_owners(
    ownership: OwnershipMap,
    target: str,
    config: Config,
) -> tuple[OwnerEntry, ...]:
    """Return inferred owners for ``target``, aggregated for a directory."""
    inferred, _candidates = _aggregate_target(ownership, target, config)
    return tuple(item.entry for item in inferred)


def build_explanation(
    ownership: OwnershipMap,
    target: str,
    config: Config,
    repo_root: Path,
    *,
    as_of: datetime,
    declared: tuple[str, ...] = (),
    team_members: Mapping[str, frozenset[str]] | None = None,
    owner: str | None = None,
    why_not: str | None = None,
) -> PathExplanation:
    """Build a path-scoped explanation from a retain-all ownership map.

    ``owner`` keeps only that inferred contributor. ``why_not`` explains why
    a contributor was excluded. Git evidence (SHAs, lineage, unbounded last
    commit, blame for outsiders) is collected only for ``target``.
    """
    files = matching_files(ownership, target)
    inferred, candidates = _aggregate_target(ownership, target, config)
    shas = commit_shas(repo_root, target, config, as_of)
    inferred = tuple(_with_shas(item, shas, config.scoring) for item in inferred)
    candidates = tuple(_with_shas(item, shas, config.scoring) for item in candidates)
    if owner:
        inferred = tuple(item for item in inferred if identities_match(item.entry.handle, owner))
    kind = _target_kind(repo_root, target, files)
    lineage = rename_lineage(repo_root, target) if kind == "file" else ()
    team_resolution = _team_resolution(inferred, declared, team_members)
    assessment = _assess(inferred, declared, team_members)
    quality = inferred[0].entry.evidence_quality if inferred else _fallback_quality(candidates)
    knobs = path_knobs(config, inferred, candidates)
    why = None
    if why_not:
        why = explain_why_not(
            why_not,
            ownership,
            target,
            config,
            repo_root,
            as_of=as_of,
            inferred=inferred,
            candidates=candidates,
        )
        knobs = why.knobs
    return PathExplanation(
        target=target,
        kind=kind,
        files=files,
        inferred=inferred,
        candidates=candidates,
        evidence_quality=quality,
        declared=declared,
        team_resolution=team_resolution,
        assessment=assessment,
        lineage=lineage,
        knobs=knobs,
        weights=signal_weights(config.scoring),
        why_not=why,
    )


def explain_why_not(
    query: str,
    ownership: OwnershipMap,
    target: str,
    config: Config,
    repo_root: Path,
    *,
    as_of: datetime,
    inferred: tuple[ExplainedOwner, ...],
    candidates: tuple[ExplainedOwner, ...],
) -> WhyNotResult:
    """Explain why ``query`` is not an inferred owner of ``target``."""
    threshold = config.analysis.confidence_threshold
    inferred_handles = {item.entry.handle for item in inferred}
    matched = next(
        (item for item in candidates if identities_match(item.entry.handle, query)),
        None,
    )
    if matched is not None and matched.entry.handle in inferred_handles:
        return WhyNotResult(
            handle=matched.entry.handle,
            kind="below_threshold",
            reasons=(f"{matched.entry.handle} is an inferred owner of {target}",),
            ownership_score=matched.entry.ownership_score,
            threshold=threshold,
            signals=matched.signals,
            knobs=(),
        )
    if matched is not None:
        return _why_not_from_candidate(matched, inferred, config, as_of)
    return _why_not_outsider(query, ownership, target, config, repo_root, as_of=as_of)


def commit_shas(
    repo_root: Path,
    target: str,
    config: Config,
    as_of: datetime,
) -> dict[str, tuple[str, ...]]:
    """Return newest path-scoped commit SHAs per author inside the lookback window."""
    window_start = as_of - timedelta(days=config.analysis.lookback_days)
    email_fmt = "%aE" if config.git.use_mailmap else "%ae"
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "log",
            _mailmap_flag(config.git.use_mailmap),
            f"--format=%H%n{email_fmt}%n%cI",
            f"--since={window_start.isoformat()}",
            f"--until={as_of.isoformat()}",
            "--",
            target,
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    return _parse_sha_log(result.stdout)


def rename_lineage(repo_root: Path, target: str) -> tuple[str, ...]:
    """Return prior names of ``target`` from ``git log --follow``, if any."""
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "log",
            "--follow",
            "--name-status",
            "--format=",
            "--",
            target,
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    names: list[str] = []
    seen = {target.lstrip("/")}
    for line in result.stdout.splitlines():
        if not line.startswith("R"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        old = parts[1].strip()
        if old and old not in seen:
            seen.add(old)
            names.append(old)
    return tuple(names)


def last_contribution(
    repo_root: Path,
    target: str,
    query: str,
    *,
    use_mailmap: bool,
) -> tuple[str, datetime] | None:
    """Newest matching commit on ``target`` with no lookback bound."""
    email_fmt = "%aE" if use_mailmap else "%ae"
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "log",
            _mailmap_flag(use_mailmap),
            f"--format=%H%n{email_fmt}%n%cI",
            "--",
            target,
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    for author, when, _sha in _iter_sha_log(result.stdout):
        if identities_match(author, query):
            return author, when
    return None


def blame_shares(
    repo_root: Path,
    files: tuple[str, ...],
    config: Config,
) -> dict[str, dict[str, float]]:
    """Blame only ``files`` and return per-path author shares."""
    if not files:
        return {}
    return _gather_blame_coverage(files, repo_root, git=config.git).coverage


def path_knobs(
    config: Config,
    inferred: tuple[ExplainedOwner, ...],
    candidates: tuple[ExplainedOwner, ...],
) -> tuple[str, ...]:
    """Configuration keys that would change who is inferred or how they score."""
    knobs: list[str] = []
    threshold = config.analysis.confidence_threshold
    inferred_handles = {item.entry.handle for item in inferred}
    below = [
        item
        for item in candidates
        if item.entry.handle not in inferred_handles and item.entry.ownership_score < threshold
    ]
    if below:
        first = below[0]
        knobs.append(
            f"analysis.confidence_threshold is {threshold:.2f}; "
            f"{first.entry.handle} scores {first.entry.ownership_score:.2f}"
        )
    if len(candidates) > config.analysis.top_n_owners:
        knobs.append(f"analysis.top_n_owners is {config.analysis.top_n_owners}")
    review_unavailable = any(
        item.entry.score_breakdown is not None and not item.entry.score_breakdown.review.available
        for item in (*inferred, *candidates)
    )
    if review_unavailable and not config.github.api_enabled:
        knobs.append("github.api_enabled is false; the review signal is omitted")
    knobs.append(
        "scoring weights are "
        f"recency {config.scoring.recency_weight:.2f}, "
        f"frequency {config.scoring.frequency_weight:.2f}, "
        f"blame {config.scoring.blame_weight:.2f}, "
        f"review {config.scoring.review_weight:.2f}"
    )
    knobs.append(f"scoring.recency_half_life_days is {config.scoring.recency_half_life_days}")
    return tuple(knobs)


def owners_payload(
    owners: tuple[OwnerEntry, ...],
    target: str,
    ownership: OwnershipMap,
) -> dict[str, object]:
    """Return the ownership document for `target` and `owners`."""
    return {
        "schema_version": COMMAND_SCHEMA_VERSION,
        "models": models_payload(),
        "checkowners_version": __version__,
        "path": target,
        "head_sha": ownership.analysis_ref,
        "analysis": path_analysis_json(owners),
        "owners": [owner_json(entry) for entry in owners],
        "risk": risk_from_scores(tuple(entry.ownership_score for entry in owners)),
    }


def explanation_payload(explanation: PathExplanation, ownership: OwnershipMap) -> dict[str, object]:
    """Schema-versioned explain payload."""
    payload: dict[str, object] = {
        "schema_version": COMMAND_SCHEMA_VERSION,
        "models": models_payload(),
        "checkowners_version": __version__,
        "path": explanation.target,
        "kind": explanation.kind,
        "files": list(explanation.files),
        "owners": [_explained_payload(item) for item in explanation.inferred],
        "candidates": [_explained_payload(item) for item in explanation.candidates],
        "evidence_quality": round(explanation.evidence_quality, 4),
        "declared": list(explanation.declared),
        "team_resolution": list(explanation.team_resolution),
        "assessment": explanation.assessment,
        "lineage": list(explanation.lineage),
        "knobs": list(explanation.knobs),
        "weights": {name: round(weight, 4) for name, weight in explanation.weights.items()},
    }
    if explanation.why_not is not None:
        payload["why_not"] = _why_not_payload(explanation.why_not)
    return payload


def signal_tuples(entry: OwnerEntry) -> dict[str, tuple[float, bool]]:
    """Map an owner's breakdown into ``combine_available_signals`` input."""
    breakdown = entry.score_breakdown
    if breakdown is None:
        return {
            "recency": (0.0, True),
            "frequency": (0.0, True),
            "blame": (0.0, False),
            "review": (0.0, False),
        }
    return {
        "recency": (breakdown.recency.score, breakdown.recency.available),
        "frequency": (breakdown.frequency.score, breakdown.frequency.available),
        "blame": (breakdown.blame.score, breakdown.blame.available),
        "review": (breakdown.review.score, breakdown.review.available),
    }


def _aggregate_target(
    ownership: OwnershipMap,
    target: str,
    config: Config,
) -> tuple[tuple[ExplainedOwner, ...], tuple[ExplainedOwner, ...]]:
    files = matching_files(ownership, target)
    inferred_rows: list[tuple[str, OwnerEntry]] = []
    candidate_rows: list[tuple[str, OwnerEntry]] = []
    for path in files:
        po = ownership.paths[path]
        inferred_rows.extend((path, entry) for entry in po.owners)
        source = po.candidates if po.candidates else po.owners
        candidate_rows.extend((path, entry) for entry in source)
    inferred = _merge_rows(inferred_rows, config.scoring)
    candidates = _merge_rows(candidate_rows, config.scoring)
    inferred = inferred[: config.analysis.top_n_owners]
    return inferred, candidates


def _merge_rows(
    rows: list[tuple[str, OwnerEntry]],
    scoring: ScoringConfig,
) -> tuple[ExplainedOwner, ...]:
    grouped: dict[str, list[tuple[str, OwnerEntry]]] = {}
    for path, entry in rows:
        grouped.setdefault(entry.handle, []).append((path, entry))
    merged: list[ExplainedOwner] = []
    for handle, group in grouped.items():
        best_path, best = max(group, key=lambda item: (item[1].ownership_score, -item[1].commits))
        last_commits = [
            entry.last_commit for _path, entry in group if entry.last_commit is not None
        ]
        entry = OwnerEntry(
            handle=handle,
            ownership_score=best.ownership_score,
            last_commit=max(last_commits) if last_commits else None,
            commits=sum(entry.commits for _path, entry in group),
            evidence_quality=best.evidence_quality,
            score_breakdown=best.score_breakdown,
        )
        merged.append(
            ExplainedOwner(
                entry=entry,
                signals=_signals_for(entry, scoring, ()),
                source_path=best_path,
            )
        )
    merged.sort(key=lambda item: (-item.entry.ownership_score, item.entry.handle))
    return tuple(merged)


def _with_shas(
    item: ExplainedOwner,
    shas: Mapping[str, tuple[str, ...]],
    scoring: ScoringConfig,
) -> ExplainedOwner:
    keyed = _lookup_shas(item.entry.handle, shas)
    return ExplainedOwner(
        entry=item.entry,
        signals=_signals_for(item.entry, scoring, keyed),
        source_path=item.source_path,
    )


def _signals_for(
    entry: OwnerEntry,
    scoring: ScoringConfig,
    shas: tuple[str, ...],
) -> tuple[SignalEvidence, ...]:
    weights = signal_weights(scoring)
    breakdown = entry.score_breakdown
    recency = breakdown.recency if breakdown else SignalScore(available=True, score=0.0)
    frequency = breakdown.frequency if breakdown else SignalScore(available=True, score=0.0)
    blame = breakdown.blame if breakdown else SignalScore(available=False)
    review = breakdown.review if breakdown else SignalScore(available=False)
    recency_shas = shas[:1]
    return (
        SignalEvidence(
            name="recency",
            score=recency.score,
            weight=weights["recency"],
            available=recency.available,
            commits=recency_shas,
            detail=_sha_detail(recency_shas) if recency_shas else "no commit in window",
        ),
        SignalEvidence(
            name="frequency",
            score=frequency.score,
            weight=weights["frequency"],
            available=frequency.available,
            commits=shas,
            detail=f"{entry.commits} commits",
        ),
        SignalEvidence(
            name="blame",
            score=blame.score,
            weight=weights["blame"],
            available=blame.available,
            commits=(),
            detail=_blame_detail(blame),
        ),
        SignalEvidence(
            name="review",
            score=review.score,
            weight=weights["review"],
            available=review.available,
            commits=(),
            detail=_review_detail(review),
        ),
    )


def _why_not_from_candidate(
    matched: ExplainedOwner,
    inferred: tuple[ExplainedOwner, ...],
    config: Config,
    as_of: datetime,
) -> WhyNotResult:
    threshold = config.analysis.confidence_threshold
    score = matched.entry.ownership_score
    kind: WhyNotKind
    if score < threshold:
        kind = "below_threshold"
    elif len(inferred) >= config.analysis.top_n_owners:
        kind = "outside_top_n"
    else:
        kind = "below_threshold"
    reasons = _exclusion_reasons(matched, config, as_of, kind)
    knobs = _why_not_knobs(matched, config, kind)
    return WhyNotResult(
        handle=matched.entry.handle,
        kind=kind,
        reasons=reasons,
        ownership_score=score,
        threshold=threshold,
        signals=matched.signals,
        knobs=knobs,
    )


def _why_not_outsider(
    query: str,
    ownership: OwnershipMap,
    target: str,
    config: Config,
    repo_root: Path,
    *,
    as_of: datetime,
) -> WhyNotResult:
    found = last_contribution(repo_root, target, query, use_mailmap=config.git.use_mailmap)
    files = matching_files(ownership, target)
    blame_paths = files if files else (target,)
    coverage = blame_shares(repo_root, blame_paths, config)
    author, share, blame_available = _outsider_blame(query, coverage)
    handle = author if author is not None else (found[0] if found else query)
    if found is None and (not blame_available or share <= 0.0):
        return _never_contributed(handle, config)
    last_when = found[1] if found else as_of
    review_available = _review_available(ownership)
    scored = _score_owners(
        {handle: _Contribution(commits=0, last_commit=last_when)},
        {handle: share} if blame_available else {},
        {},
        max_commits=1,
        scoring=config.scoring,
        now=as_of,
        blame_available=blame_available,
        review_available=review_available,
        frequency_prior=frequency_prior_for(config.qualification.strategy),
    )
    entry = scored[0]
    signals = _signals_for(entry, config.scoring, ())
    blame_line = (
        _blame_detail(entry.score_breakdown.blame) if entry.score_breakdown else "blame unavailable"
    )
    reasons = (
        f"last contribution: {_days_ago(last_when, as_of)}"
        if found
        else "no commits in the observation window",
        blame_line,
        "no reviews in observation window",
        _threshold_reason(entry.ownership_score, config.analysis.confidence_threshold),
    )
    knobs = (
        f"analysis.lookback_days is {config.analysis.lookback_days}",
        f"analysis.confidence_threshold is {config.analysis.confidence_threshold:.2f}",
    )
    return WhyNotResult(
        handle=handle,
        kind="outside_window",
        reasons=reasons,
        ownership_score=entry.ownership_score,
        threshold=config.analysis.confidence_threshold,
        signals=signals,
        knobs=knobs,
    )


def _never_contributed(handle: str, config: Config) -> WhyNotResult:
    threshold = config.analysis.confidence_threshold
    weights = signal_weights(config.scoring)
    signals = (
        SignalEvidence("recency", 0.0, weights["recency"], True, (), "never contributed"),
        SignalEvidence("frequency", 0.0, weights["frequency"], True, (), "0 commits"),
        SignalEvidence("blame", 0.0, weights["blame"], True, (), "0% current blame"),
        SignalEvidence("review", 0.0, weights["review"], False, (), "reviews unavailable"),
    )
    return WhyNotResult(
        handle=handle,
        kind="never_contributed",
        reasons=(
            "never contributed to this path",
            "0% current blame",
            "no reviews in observation window",
            f"confidence: 0.00 < threshold {threshold:.2f}",
        ),
        ownership_score=0.0,
        threshold=threshold,
        signals=signals,
        knobs=(f"analysis.confidence_threshold is {threshold:.2f}",),
    )


def _exclusion_reasons(
    matched: ExplainedOwner,
    config: Config,
    as_of: datetime,
    kind: WhyNotKind,
) -> tuple[str, ...]:
    entry = matched.entry
    blame = next((s for s in matched.signals if s.name == "blame"), None)
    review = next((s for s in matched.signals if s.name == "review"), None)
    reasons = [
        f"last contribution: {_days_ago(entry.last_commit, as_of)}",
        blame.detail if blame else "blame unavailable",
        review.detail if review and review.available else "no reviews in observation window",
        _threshold_reason(entry.ownership_score, config.analysis.confidence_threshold),
    ]
    if kind == "outside_top_n":
        reasons.append(f"ranked below analysis.top_n_owners ({config.analysis.top_n_owners})")
    return tuple(reasons)


def _why_not_knobs(
    matched: ExplainedOwner,
    config: Config,
    kind: WhyNotKind,
) -> tuple[str, ...]:
    knobs = [
        f"analysis.confidence_threshold is {config.analysis.confidence_threshold:.2f}; "
        f"{matched.entry.handle} scores {matched.entry.ownership_score:.2f}",
    ]
    if kind == "outside_top_n":
        knobs.append(f"analysis.top_n_owners is {config.analysis.top_n_owners}")
    knobs.append(f"scoring.recency_half_life_days is {config.scoring.recency_half_life_days}")
    knobs.append(f"analysis.lookback_days is {config.analysis.lookback_days}")
    return tuple(knobs)


def _explained_payload(item: ExplainedOwner) -> dict[str, object]:
    entry = item.entry
    payload: dict[str, object] = {
        "handle": entry.handle,
        "ownership_score": round(entry.ownership_score, 4),
        "evidence_quality": round(entry.evidence_quality, 4),
        "commits": entry.commits,
        "last_commit": entry.last_commit.isoformat() if entry.last_commit else None,
        "source_path": item.source_path,
        "signals": {signal.name: _signal_payload(signal) for signal in item.signals},
    }
    return payload


def _signal_payload(signal: SignalEvidence) -> dict[str, object]:
    payload: dict[str, object] = {
        "available": signal.available,
        "weight": round(signal.weight, 4),
        "detail": signal.detail,
        "commits": list(signal.commits),
    }
    if signal.available:
        payload["score"] = round(signal.score, 4)
    return payload


def _why_not_payload(result: WhyNotResult) -> dict[str, object]:
    return {
        "handle": result.handle,
        "kind": result.kind,
        "reasons": list(result.reasons),
        "ownership_score": round(result.ownership_score, 4),
        "threshold": round(result.threshold, 4),
        "signals": {signal.name: _signal_payload(signal) for signal in result.signals},
        "knobs": list(result.knobs),
    }


def _team_resolution(
    inferred: tuple[ExplainedOwner, ...],
    declared: tuple[str, ...],
    team_members: Mapping[str, frozenset[str]] | None,
) -> tuple[str, ...]:
    if not inferred or not declared or team_members is None:
        return ()
    lines: list[str] = []
    for item in inferred:
        login = _normalize_identity(item.entry.handle)
        for team, members in team_members.items():
            normalized = {_normalize_identity(member) for member in members}
            if login in normalized:
                team_label = team if team.startswith("@") else f"@{team}"
                lines.append(f"{item.entry.handle} ∈ {team_label}")
                break
    return tuple(lines)


def _assess(
    inferred: tuple[ExplainedOwner, ...],
    declared: tuple[str, ...],
    team_members: Mapping[str, frozenset[str]] | None,
) -> Assessment:
    if not declared:
        return "unverifiable"
    declared_norm = {_normalize_identity(name) for name in declared}
    inferred_norm = {_normalize_identity(item.entry.handle) for item in inferred}
    if inferred_norm & declared_norm:
        return "aligned"
    team_declared = [name for name in declared if "/" in name]
    if team_declared and team_members is None:
        return "unverifiable"
    if team_declared and team_members is not None:
        members = _members_for_declared_teams(team_declared, declared_norm, team_members)
        if inferred_norm and inferred_norm <= members:
            return "aligned"
        if inferred_norm & members:
            return "aligned"
        return "diverged"
    return "diverged"


def _members_for_declared_teams(
    team_declared: list[str],
    declared_norm: set[str],
    team_members: Mapping[str, frozenset[str]],
) -> set[str]:
    members: set[str] = set()
    wanted = {_team_tail(name) for name in team_declared}
    wanted.update(declared_norm)
    for team, logins in team_members.items():
        slug = _normalize_identity(team)
        if slug in wanted or _team_tail(slug) in wanted:
            members.update(_normalize_identity(login) for login in logins)
    return members


def _team_tail(value: str) -> str:
    return _normalize_identity(value).rsplit("/", 1)[-1]


def _target_kind(repo_root: Path, target: str, files: tuple[str, ...]) -> TargetKind:
    if target.endswith("/") or len(files) > 1:
        return "directory"
    if (repo_root / target).is_dir():
        return "directory"
    return "file"


def _fallback_quality(candidates: tuple[ExplainedOwner, ...]) -> float:
    if not candidates:
        return 0.0
    return max(item.entry.evidence_quality for item in candidates)


def _review_available(ownership: OwnershipMap) -> bool:
    for po in ownership.paths.values():
        for entry in (*po.owners, *po.candidates):
            if entry.score_breakdown is not None and entry.score_breakdown.review.available:
                return True
    return False


def _outsider_blame(
    query: str,
    coverage: Mapping[str, Mapping[str, float]],
) -> tuple[str | None, float, bool]:
    if not coverage:
        return None, 0.0, False
    best_author: str | None = None
    best_share = 0.0
    for per_author in coverage.values():
        for author, share in per_author.items():
            if identities_match(author, query) and share >= best_share:
                best_author = author
                best_share = share
    return best_author, best_share, True


def _lookup_shas(handle: str, shas: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    if handle in shas:
        return shas[handle]
    for author, values in shas.items():
        if identities_match(author, handle):
            return values
    return ()


def _parse_sha_log(stdout: str) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for author, _when, sha in _iter_sha_log(stdout):
        bucket = grouped.setdefault(author, [])
        if sha in bucket or len(bucket) >= _SHA_CAP:
            continue
        bucket.append(sha)
    return {author: tuple(values) for author, values in grouped.items()}


def _iter_sha_log(stdout: str) -> tuple[tuple[str, datetime, str], ...]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    rows: list[tuple[str, datetime, str]] = []
    index = 0
    while index + 2 < len(lines):
        sha = _short_sha(lines[index])
        author = lines[index + 1]
        try:
            when = datetime.fromisoformat(lines[index + 2])
        except ValueError:
            index += 1
            continue
        rows.append((author, when, sha))
        index += 3
    return tuple(rows)


def _short_sha(value: str) -> str:
    return value[:_SHA_LEN] if len(value) > _SHA_LEN else value


def _blame_detail(signal: SignalScore) -> str:
    if not signal.available:
        return "blame unavailable"
    return f"{signal.score:.0%} current blame"


def _review_detail(signal: SignalScore) -> str:
    if not signal.available:
        return "reviews unavailable"
    if signal.score <= 0.0:
        return "no reviews in observation window"
    return f"review share {signal.score:.2f}"


def _sha_detail(shas: tuple[str, ...]) -> str:
    return shas[0] if shas else "no commit in window"


def _days_ago(last: datetime | None, as_of: datetime) -> str:
    if last is None:
        return "unknown"
    days = max(0, int((as_of - last).total_seconds() // 86400))
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def _threshold_reason(score: float, threshold: float) -> str:
    if score < threshold:
        return f"confidence: {score:.2f} < threshold {threshold:.2f}"
    return f"confidence: {score:.2f}"


def _normalize_identity(value: str) -> str:
    return value.lstrip("@").strip().lower()

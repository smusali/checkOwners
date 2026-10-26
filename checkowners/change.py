from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from checkowners.analyze import (
    ReviewProvider,
    analyze_ownership,
    head_commit_sha,
    resolve_as_of,
    run_git,
)
from checkowners.balance import BalanceReport, ReviewLoad, analyze_balance
from checkowners.config import find_codeowners_path
from checkowners.expertise import path_matches_glob
from checkowners.explain import signal_tuples
from checkowners.models import Config, OwnerEntry, OwnershipMap, PathOwnership
from checkowners.patterns import CodeownersRule, match_path, parse_rules
from checkowners.privacy import is_excluded

_STALE = frozenset({"inactive", "superseded", "departed"})
_REAL_LOAD = frozenset({"github_completed_reviews", "external"})


@dataclass(frozen=True)
class ReviewerRow:
    handle: str
    score: float
    reason: str
    evidence_quality: float
    signals: dict[str, float]
    demoted: bool
    demote_reason: str


@dataclass(frozen=True)
class PathImpact:
    path: str
    before_primary: str
    after_primary: str
    before_count: int
    after_count: int
    kind: str


@dataclass(frozen=True)
class ChangeReport:
    files: tuple[str, ...]
    clusters: int
    recommended: tuple[ReviewerRow, ...]
    not_recommended: tuple[ReviewerRow, ...]
    covered: int
    total: int
    risks: tuple[str, ...]
    drift: tuple[str, ...]
    impacts: tuple[PathImpact, ...]
    single_owner_after: int
    team: str | None = None


def split_spec(spec: str) -> tuple[str, str]:
    if "..." in spec:
        base, head = spec.split("...", 1)
        return base or "HEAD", head or "HEAD"
    return spec, "HEAD"


def merge_base(repo_root: Path, spec: str) -> str:
    base, head = split_spec(spec)
    result = run_git(repo_root, ["git", "merge-base", base, head], check=True)
    sha = result.stdout.strip()
    if not sha:
        msg = f"No merge base for {spec}"
        raise ValueError(msg)
    return sha


def changed_paths(
    repo_root: Path,
    spec: str,
    *,
    exclude: tuple[str, ...] = (),
) -> tuple[str, ...]:
    base, head = split_spec(spec)
    result = run_git(
        repo_root,
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...{head}"],
        check=True,
        paths=True,
    )
    paths = tuple(line for line in result.stdout.splitlines() if line)
    if not exclude:
        return paths
    return tuple(path for path in paths if not _matches_exclude(path, exclude))


def load_paths(
    repo_root: Path,
    config: Config,
    paths: tuple[str, ...],
    revision: str | None,
    *,
    as_of: datetime | None = None,
    review_provider: ReviewProvider | None = None,
) -> OwnershipMap:
    when = as_of if as_of is not None else resolve_as_of(None, repo_root)
    if not paths:
        ref = revision if revision is not None else ""
        return OwnershipMap(paths={}, last_analyzed=when, analysis_ref=ref)
    ref = revision if revision is not None else head_commit_sha(repo_root)
    return analyze_ownership(
        repo_root,
        config,
        review_provider=None if revision is not None else review_provider,
        as_of=when,
        analysis_ref=ref,
        pathspec=paths,
        incremental=False,
        revision=revision,
        mass_revs=() if revision is not None else None,
    )


def build_change(
    repo_root: Path,
    config: Config,
    paths: tuple[str, ...],
    *,
    base_revision: str,
    load: Callable[[tuple[str, ...], str | None], OwnershipMap] | None = None,
    review_counts: dict[str, int] | None = None,
    team_resolve: Callable[[tuple[str, ...]], str | None] | None = None,
) -> ChangeReport:
    kept = tuple(path for path in paths if not _matches_exclude(path, config.paths.exclude))
    if not kept:
        return _empty()
    loader = load if load is not None else _loader(repo_root, config)
    head = loader(kept, None)
    base = loader(kept, base_revision)
    rules = _rules(repo_root)
    return _report(
        kept,
        head,
        base,
        config,
        rules,
        review_counts=review_counts,
        team_resolve=team_resolve,
    )


def change_payload(report: ChangeReport) -> dict[str, object]:
    payload: dict[str, object] = {
        "files": list(report.files),
        "clusters": report.clusters,
        "recommended": [_row_payload(row) for row in report.recommended],
        "not_recommended": [_row_payload(row) for row in report.not_recommended],
        "covered": report.covered,
        "total": report.total,
        "risks": list(report.risks),
        "drift": list(report.drift),
        "impacts": [
            {
                "path": impact.path,
                "before_primary": impact.before_primary,
                "after_primary": impact.after_primary,
                "before_count": impact.before_count,
                "after_count": impact.after_count,
                "kind": impact.kind,
            }
            for impact in report.impacts
        ],
        "single_owner_after": report.single_owner_after,
    }
    if report.team:
        payload["team"] = report.team
    return payload


def render_suggest(report: ChangeReport) -> str:
    lines = [
        f"{report.total} files changed across {report.clusters} ownership clusters.",
        "",
        "Recommended reviewers:",
    ]
    if report.team:
        lines.append(f"  {report.team}   declared team")
    if report.recommended:
        lines.extend(_row_line(row) for row in report.recommended)
    else:
        lines.append("  none")
    if report.not_recommended:
        lines.append("")
        lines.append("Not recommended:")
        lines.extend(_row_line(row) for row in report.not_recommended)
    lines.append("")
    lines.append(
        f"Coverage: {report.covered}/{report.total} changed files "
        "have at least one qualified reviewer."
    )
    lines.extend(_impact_lines(report))
    return "\n".join(lines) + "\n"


def render_diff(report: ChangeReport) -> str:
    changed = ", ".join(report.files) if report.files else "none"
    reviewers = ", ".join(f"{row.handle} {row.score:.2f}" for row in report.recommended)
    if report.team:
        reviewers = f"{report.team}, {reviewers}" if reviewers else report.team
    risks = "; ".join(report.risks) if report.risks else "none"
    drift = "; ".join(report.drift) if report.drift else "none"
    lines = [
        f"Changed: {changed}",
        f"Recommended reviewers: {reviewers or 'none'}",
        f"Ownership risks introduced: {risks}",
        f"CODEOWNERS drift: {drift}",
    ]
    lines.extend(_impact_lines(report))
    return "\n".join(lines) + "\n"


def _loader(
    repo_root: Path,
    config: Config,
) -> Callable[[tuple[str, ...], str | None], OwnershipMap]:
    def load(paths: tuple[str, ...], revision: str | None) -> OwnershipMap:
        return load_paths(repo_root, config, paths, revision)

    return load


def _empty() -> ChangeReport:
    return ChangeReport(
        files=(),
        clusters=0,
        recommended=(),
        not_recommended=(),
        covered=0,
        total=0,
        risks=(),
        drift=(),
        impacts=(),
        single_owner_after=0,
    )


def _report(
    paths: tuple[str, ...],
    head: OwnershipMap,
    base: OwnershipMap,
    config: Config,
    rules: tuple[CodeownersRule, ...],
    *,
    review_counts: dict[str, int] | None,
    team_resolve: Callable[[tuple[str, ...]], str | None] | None,
) -> ChangeReport:
    threshold = config.analysis.confidence_threshold
    impacts = tuple(
        _impact(path, base.paths.get(path), head.paths.get(path), config) for path in paths
    )
    risks: list[str] = []
    drift: list[str] = []
    clusters: set[tuple[str, ...]] = set()
    covered = 0
    single = 0
    people: dict[str, _Person] = {}
    for path in paths:
        current = head.paths.get(path)
        qualified = _qualified(current, threshold)
        clusters.add(tuple(sorted(handle.casefold() for handle in qualified)))
        if qualified:
            covered += 1
        after_count = 0 if current is None else current.qualified_owner_count
        if after_count == 1:
            single += 1
            risks.append(f"{path} has only one inferred expert")
        stale = _stale_owner(current, threshold)
        if stale:
            risks.append(f"{path} ownership for {stale} is stale")
        drift_line = _drift_line(path, rules, qualified)
        if drift_line:
            drift.append(drift_line)
        _accumulate(people, current, threshold, config.contributors_exclude)
    balance = analyze_balance(
        head,
        config,
        review_counts=review_counts,
        rules=rules,
    )
    recommended, demoted = _rows(people, balance, len(paths), head.last_analyzed)
    team = None
    if team_resolve is not None and recommended:
        team = team_resolve(tuple(row.handle for row in recommended))
    return ChangeReport(
        files=paths,
        clusters=len(clusters),
        recommended=tuple(recommended),
        not_recommended=tuple(demoted),
        covered=covered,
        total=len(paths),
        risks=tuple(risks),
        drift=tuple(drift),
        impacts=impacts,
        single_owner_after=single,
        team=team,
    )


@dataclass
class _Person:
    handle: str
    score: float
    quality: float
    files: int
    last: datetime | None
    entry: OwnerEntry


def _accumulate(
    people: dict[str, _Person],
    current: PathOwnership | None,
    threshold: float,
    excluded: tuple[str, ...],
) -> None:
    if current is None:
        return
    for owner in current.owners:
        if owner.ownership_score < threshold or is_excluded(owner.handle, excluded):
            continue
        found = people.get(owner.handle.casefold())
        if found is None:
            people[owner.handle.casefold()] = _Person(
                handle=owner.handle,
                score=owner.ownership_score,
                quality=owner.evidence_quality,
                files=1,
                last=owner.last_commit,
                entry=owner,
            )
            continue
        found.files += 1
        if owner.ownership_score > found.score:
            found.score = owner.ownership_score
            found.quality = owner.evidence_quality
            found.entry = owner
            found.handle = owner.handle
        if owner.last_commit is not None and (found.last is None or owner.last_commit > found.last):
            found.last = owner.last_commit


def _rows(
    people: dict[str, _Person],
    balance: BalanceReport,
    total: int,
    as_of: datetime,
) -> tuple[list[ReviewerRow], list[ReviewerRow]]:
    ranked = sorted(people.values(), key=lambda person: (-person.score, person.handle.casefold()))
    loads = {load.handle.casefold(): load for load in balance.loads}
    recommended: list[ReviewerRow] = []
    demoted: list[ReviewerRow] = []
    real_load = balance.load_signal in _REAL_LOAD
    for person in ranked:
        load = loads.get(person.handle.casefold())
        reason = f"owns {person.files}/{total} changed files, {_recency(person.last, as_of)}"
        signals = _signal_scores(person.entry)
        demote_reason = ""
        demoted_row = False
        if real_load and load is not None and load.load_band == "high":
            demote_reason = _overload_reason(load)
            demoted_row = True
        row = ReviewerRow(
            handle=person.handle,
            score=round(person.score, 4),
            reason=reason,
            evidence_quality=round(person.quality, 4),
            signals=signals,
            demoted=demoted_row,
            demote_reason=demote_reason,
        )
        if demoted_row:
            demoted.append(row)
        else:
            recommended.append(row)
    return recommended, demoted


def _impact(
    path: str,
    before: PathOwnership | None,
    after: PathOwnership | None,
    config: Config,
) -> PathImpact:
    before_count = 0 if before is None else before.qualified_owner_count
    after_count = 0 if after is None else after.qualified_owner_count
    before_primary = _primary(before)
    after_primary = _primary(after)
    shift = bool(
        before_primary and after_primary and before_primary.casefold() != after_primary.casefold()
    )
    regression = (
        before is not None
        and after_count < before_count
        and after_count <= config.bus_factor.warn_threshold
    )
    if regression:
        kind = "regression"
    elif shift:
        kind = "shift"
    elif before is None and after_primary:
        kind = "new"
    elif after_count == 1:
        kind = "single"
    else:
        kind = "ok"
    return PathImpact(
        path=path,
        before_primary=before_primary,
        after_primary=after_primary,
        before_count=before_count,
        after_count=after_count,
        kind=kind,
    )


def _primary(path_ownership: PathOwnership | None) -> str:
    if path_ownership is None or not path_ownership.owners:
        return ""
    best = max(
        path_ownership.owners,
        key=lambda owner: (owner.ownership_score, owner.handle.casefold()),
    )
    return best.handle


def _qualified(path_ownership: PathOwnership | None, threshold: float) -> tuple[str, ...]:
    if path_ownership is None:
        return ()
    return tuple(
        owner.handle for owner in path_ownership.owners if owner.ownership_score >= threshold
    )


def _stale_owner(path_ownership: PathOwnership | None, threshold: float) -> str:
    if path_ownership is None:
        return ""
    for owner in path_ownership.owners:
        if owner.ownership_score < threshold or owner.freshness is None:
            continue
        if owner.freshness.status in _STALE:
            return owner.handle
    return ""


def _drift_line(
    path: str,
    rules: tuple[CodeownersRule, ...],
    observed: Sequence[str],
) -> str:
    rule = match_path(rules, path)
    if rule is None or not rule.owners:
        return f"{path} has no CODEOWNERS owner"
    declared = {owner.casefold() for owner in rule.owners}
    seen = {owner.casefold() for owner in observed}
    if not seen or declared == seen:
        return ""
    declared_text = ", ".join(rule.owners)
    observed_text = ", ".join(observed)
    return f"{path} declared {declared_text}, observed {observed_text}"


def _rules(repo_root: Path) -> tuple[CodeownersRule, ...]:
    path = find_codeowners_path(repo_root)
    if not path.is_file():
        return ()
    return parse_rules(path.read_text(encoding="utf-8"))


def _signal_scores(entry: OwnerEntry) -> dict[str, float]:
    return {
        name: round(score, 4)
        for name, (score, available) in signal_tuples(entry).items()
        if available
    }


def _recency(last: datetime | None, as_of: datetime) -> str:
    if last is None:
        return "no recent commit"
    days = max((as_of - last).days, 0)
    if days <= 7:
        return "active this week"
    return f"last touched {days} days ago"


def _overload_reason(load: ReviewLoad) -> str:
    count = load.prs_waiting if load.prs_waiting is not None else load.reviews
    return f"{count} open reviews already (overloaded)"


def _row_payload(row: ReviewerRow) -> dict[str, object]:
    return {
        "handle": row.handle,
        "score": row.score,
        "reason": row.reason,
        "evidence_quality": row.evidence_quality,
        "signals": dict(row.signals),
        "demoted": row.demoted,
        "demote_reason": row.demote_reason,
    }


def _row_line(row: ReviewerRow) -> str:
    signals = ", ".join(f"{name} {score:.2f}" for name, score in row.signals.items())
    detail = row.demote_reason if row.demoted and row.demote_reason else row.reason
    evidence = f"evidence {row.evidence_quality:.2f}"
    signal_text = f" [{signals}]" if signals else ""
    return f"  {row.handle}   {row.score:.2f}   {evidence}   {detail}{signal_text}"


def _impact_lines(report: ChangeReport) -> list[str]:
    lines: list[str] = []
    for impact in report.impacts:
        if (
            impact.before_primary
            and impact.after_primary
            and impact.before_primary.casefold() != impact.after_primary.casefold()
        ):
            lines.append(
                f"{impact.path}   {impact.before_primary} -> {impact.after_primary}   [new primary]"
            )
        if impact.kind == "regression":
            lines.append(
                f"{impact.path}   bus factor {impact.before_count} -> "
                f"{impact.after_count}   [regression]"
            )
    if report.single_owner_after:
        lines.append(
            f"After merge, {report.single_owner_after} files in this PR would have a single owner."
        )
    if not lines:
        return []
    return ["", *lines]


def _matches_exclude(path: str, exclude: tuple[str, ...]) -> bool:
    return any(path_matches_glob(path, pattern) for pattern in exclude)

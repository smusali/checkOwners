"""Getting an ownership map for a command to work with.

Each entry point here decides whether to reuse this repository's cached
analysis, score the full lookback window again, or score only the paths a
diff touched, and then resolves commit emails to GitHub handles.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

from checkowners.analyze import (
    AnalysisOutcome,
    ReviewProvider,
    analyze_repository,
    apply_completeness,
    review_window_start,
)
from checkowners.change import (
    changed_paths,
    load_paths,
)
from checkowners.cli.common import (
    _guard_ownership,
    _try_head_sha,
    console,
    err_console,
    exit_with,
    run_options,
)
from checkowners.config import (
    Config,
    ScoringConfig,
    find_codeowners_path,
)
from checkowners.drift import (
    detect_drift,
)
from checkowners.errors import ConfigError, ExitCode
from checkowners.git import (
    head_commit_sha,
    resolve_as_of,
)
from checkowners.github import (
    PullRequestFilesError,
    ReconciliationError,
    TeamDirectory,
    build_review_coverage,
    collection_gaps,
    create_team_resolver,
    get_github_token,
    github_repository,
    load_team_directory,
    offline_enabled,
    organization_members,
    pull_request_base,
    pull_request_paths,
    resolve_handle_details,
)
from checkowners.history import (
    AuthorIndex,
)
from checkowners.identity import (
    HandleHit,
    OwnerMerge,
    apply_contributor_status,
    load_identity_graph,
    merge_owners,
    override_handles,
    record_certain_merges,
    save_identity_graph,
)
from checkowners.models import (
    ABSENT_TOKEN_REASON,
    AnalysisGap,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    ambiguous_identity_reason,
    with_gaps,
)
from checkowners.patterns import (
    matching_rules,
    parse_rules,
)
from checkowners.privacy import (
    exclude_contributors,
    is_excluded,
)
from checkowners.state import (
    read_state,
    reusable_ownership,
    write_state,
)

if TYPE_CHECKING:
    pass


def _unresolved_email_count(ownership: OwnershipMap) -> int:
    emails: set[str] = set()
    for path_ownership in ownership.paths.values():
        for owner in (*path_ownership.owners, *path_ownership.candidates):
            handle = owner.handle
            if "@" in handle and not handle.startswith("@"):
                emails.add(handle)
    return len(emails)


def _apply_extra_gaps(
    ownership: OwnershipMap,
    extra: tuple[AnalysisGap, ...],
    scoring: ScoringConfig,
) -> OwnershipMap:
    if ownership.analysis_completeness.score is None or not extra:
        return ownership
    return apply_completeness(ownership, with_gaps(ownership.analysis_completeness, extra), scoring)


def _api_gaps(ownership: OwnershipMap, config: Config) -> tuple[AnalysisGap, ...]:
    gaps = list(collection_gaps())
    unresolved = _unresolved_email_count(ownership) if config.github.resolve_handles else 0
    needs_token = config.github.api_enabled or unresolved > 0
    if needs_token and not get_github_token():
        gaps.append(AnalysisGap("absent_token", ABSENT_TOKEN_REASON))
    if unresolved > 0:
        gaps.append(AnalysisGap("ambiguous_identity", ambiguous_identity_reason(unresolved)))
    return tuple(gaps)


def detect_command_drift(
    repo_root: Path,
    ownership: OwnershipMap,
    config: Config,
    codeowners_path: Path,
) -> DriftResult:
    try:
        return detect_drift(
            repo_root,
            ownership,
            config,
            codeowners_path=codeowners_path,
            teams=_load_teams(config),
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        console.print(f"[red]Git command failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)


def _load_teams(config: Config) -> TeamDirectory | None:
    if not config.github.resolve_teams or offline_enabled():
        return None
    token = get_github_token()
    org = config.github.org.strip()
    if not token or not org:
        return None
    return load_team_directory(token, org)


def _try_analyze(config: Config, repo_root: Path) -> OwnershipMap | None:
    try:
        return _run_analyze(config, repo_root)
    except (typer.Exit, ValueError, subprocess.CalledProcessError, OSError):
        return None


def _resolve_clock(repo_root: Path) -> tuple[datetime, str]:
    try:
        as_of = resolve_as_of(run_options().as_of, repo_root)
        ref = head_commit_sha(repo_root)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        console.print(f"[red]Git command failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)
    return as_of, ref


def _status_configured(config: Config) -> bool:
    if config.contributor_roster or config.bot_addresses or config.github.org:
        return True
    return any(override.service_account for _handle, override in config.identity_overrides)


def _resolve_github_owners(
    ownership: OwnershipMap,
    config: Config,
    authors: AuthorIndex,
    repo_root: Path | None = None,
) -> OwnershipMap:
    """Return `ownership` with handles resolved, certain duplicates merged, and status set."""
    index = authors
    if not config.github.resolve_handles and not _status_configured(config):
        return ownership
    root = repo_root if repo_root is not None else Path.cwd()
    emails = {
        owner.handle
        for po in ownership.paths.values()
        for owner in (*po.owners, *po.scored_owners, *po.candidates)
    }
    for po in ownership.paths.values():
        emails.update(warning.handle for warning in po.decay_warnings)
    emails = {email for email in emails if not is_excluded(email, config.contributors_exclude)}
    hits: dict[str, HandleHit] = {}
    if config.github.resolve_handles and emails:
        hits = resolve_handle_details(
            emails,
            get_github_token(),
            repo_root=root,
            author_shas=index.shas,
            overrides=override_handles(config.identity_overrides),
            ttl=config.negative_cache_ttl_days,
        )
    if not emails and not _status_configured(config):
        return ownership
    graph = load_identity_graph(root)
    separations = (*config.identity_do_not_merge, *graph.separations)
    org_members = _org_members(config)
    as_of = ownership.last_analyzed
    new_paths: dict[str, PathOwnership] = {}
    groups: list[tuple[str, ...]] = []
    for path, path_ownership in ownership.paths.items():
        resolved, recorded = _resolved_path(
            path_ownership,
            config,
            hits,
            separations,
            org_members,
            as_of,
        )
        new_paths[path] = resolved
        groups.extend(recorded)
    if config.github.resolve_handles and groups:
        into = {
            email: hit.handle
            for email, hit in hits.items()
            if hit.handle and hit.source != "search"
        }
        updated = record_certain_merges(graph, groups, into)
        if updated is not graph:
            save_identity_graph(root, updated)
    return replace(ownership, paths=new_paths)


def _merged_owners(
    owners: tuple[OwnerEntry, ...],
    hits: Mapping[str, HandleHit],
    separations: tuple[tuple[str, str], ...],
    config: Config,
) -> OwnerMerge:
    if not config.github.resolve_handles:
        return OwnerMerge(owners, ())
    return merge_owners(owners, hits, separations)


def _resolved_path(
    path_ownership: PathOwnership,
    config: Config,
    hits: Mapping[str, HandleHit],
    separations: tuple[tuple[str, str], ...],
    org_members: frozenset[str] | None,
    as_of: datetime,
) -> tuple[PathOwnership, tuple[tuple[str, ...], ...]]:
    owner_merge = _merged_owners(path_ownership.owners, hits, separations, config)
    owners = apply_contributor_status(
        owner_merge.owners,
        config=config,
        hits=hits,
        org_members=org_members,
        as_of=as_of,
    )
    candidates = apply_contributor_status(
        _merged_owners(path_ownership.candidates, hits, separations, config).owners,
        config=config,
        hits=hits,
        org_members=org_members,
        as_of=as_of,
    )
    scored = apply_contributor_status(
        _merged_owners(path_ownership.scored_owners, hits, separations, config).owners,
        config=config,
        hits=hits,
        org_members=org_members,
        as_of=as_of,
    )
    if config.analysis.exclude_bots:
        owners = tuple(owner for owner in owners if owner.contributor_status != "bot")
        candidates = tuple(owner for owner in candidates if owner.contributor_status != "bot")
        scored = tuple(owner for owner in scored if owner.contributor_status != "bot")
    warnings = tuple(
        replace(warning, handle=_certain_handle(warning.handle, hits))
        if config.github.resolve_handles
        else warning
        for warning in path_ownership.decay_warnings
    )
    threshold = config.analysis.confidence_threshold
    qualified = sum(1 for owner in owners if owner.confidence >= threshold)
    resolved = PathOwnership(
        owners=owners,
        qualified_owner_count=qualified,
        decay_warnings=warnings,
        candidates=candidates,
        scored_owners=scored,
        change_events=path_ownership.change_events,
        cadence_days=path_ownership.cadence_days,
        cochange_degree=path_ownership.cochange_degree,
    )
    return resolved, owner_merge.groups


def _certain_handle(identity: str, hits: Mapping[str, HandleHit]) -> str:
    hit = hits.get(identity)
    if hit is None or not hit.handle or hit.source == "search":
        return identity
    return hit.handle


def _org_members(config: Config) -> frozenset[str] | None:
    if not config.github.org:
        return None
    return organization_members(get_github_token(), config.github.org)


def _merge_identities(
    owners: tuple[OwnerEntry, ...],
    email_to_handle: Mapping[str, str],
) -> tuple[OwnerEntry, ...]:
    """Merge owner entries whose emails resolve to the same @handle."""
    hits = {
        email: HandleHit(handle=handle, source="commit")
        for email, handle in email_to_handle.items()
        if handle
    }
    return merge_owners(owners, hits).owners


def _warn_missing_api_token(config: Config) -> None:
    if config.github.api_enabled and not get_github_token():
        err_console.print(
            "[yellow]github.api_enabled is true but GITHUB_TOKEN is not set. "
            "Review coverage, topology reconciliation, and review-load "
            "balance are disabled.[/yellow]"
        )


def _review_provider(
    config: Config,
    as_of: datetime,
    repo_root: Path | None = None,
) -> ReviewProvider | None:
    """Return the review provider for `config` at `as_of`, or None when collection is off."""
    if not config.github.api_enabled:
        return None
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo_full_name:
        return None
    token = get_github_token()
    if not token:
        return None
    since = review_window_start(config, as_of)

    def provider(emails: set[str]) -> dict[str, dict[str, float]]:
        allowed = {email for email in emails if not is_excluded(email, config.contributors_exclude)}
        return build_review_coverage(
            token,
            repo_full_name,
            allowed,
            as_of=as_of,
            since=since,
            repo_root=repo_root,
        )

    return provider


def _run_analyze(config: Config, repo_root: Path) -> OwnershipMap:
    _warn_missing_api_token(config)
    as_of, analysis_ref = _resolve_clock(repo_root)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=err_console,
        transient=True,
        disable=not err_console.is_terminal,
    )
    outcome: AnalysisOutcome | None = None
    authors = AuthorIndex()
    try:
        with progress:
            task_id = progress.add_task("Analyzing git history (blame pass)", total=None)

            def on_progress(done: int, total: int) -> None:
                progress.update(task_id, completed=done, total=total)

            result = analyze_repository(
                repo_root,
                config,
                review_provider=_review_provider(config, as_of, repo_root),
                on_progress=on_progress,
                as_of=as_of,
                analysis_ref=analysis_ref,
                max_workers=config.analysis.max_git_workers,
                incremental=not run_options().no_incremental and not run_options().no_cache,
                capture_baseline=not run_options().no_cache,
            )
            ownership = result.ownership
            authors = result.authors
            outcome = result.outcome
            if outcome is not None and outcome.mode == "incremental":
                err_console.print(
                    "[dim]Incremental analysis from "
                    f"{outcome.prior_ref} to {ownership.analysis_ref}.[/dim]"
                )
            elif outcome is not None and outcome.fallback:
                err_console.print(f"[dim]Full analysis: {outcome.fallback}.[/dim]")
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        _guard_ownership(exc)
    ownership = _resolve_github_owners(ownership, config, authors, repo_root)
    ownership = exclude_contributors(
        ownership,
        config.contributors_exclude,
        confidence_threshold=config.analysis.confidence_threshold,
    )
    ownership = _apply_extra_gaps(ownership, _api_gaps(ownership, config), config.scoring)
    if not run_options().no_cache:
        write_state(
            repo_root,
            ownership,
            qualified_owner_count_cap=config.analysis.top_n_owners,
            config=config,
            history=None if outcome is None else outcome.baseline,
        )
    return ownership


def _load_or_analyze(config: Config, repo_root: Path) -> OwnershipMap:
    """Use this repo's cached state when it is fresh; otherwise re-analyze."""
    if run_options().no_cache:
        return _run_analyze(config, repo_root)
    head = _try_head_sha(repo_root)
    cached = reusable_ownership(
        repo_root,
        config,
        head=head,
        allow_stale=run_options().allow_stale,
        max_age=run_options().max_age,
    )
    if cached is not None:
        _warn_missing_api_token(config)
        cached_at = cached.last_analyzed.isoformat(timespec="seconds")
        err_console.print(
            f"[dim]Using cached analysis from {cached_at}; "
            "run `checkowners analyze` to refresh.[/dim]"
        )
        return cached
    stored = read_state(repo_root)
    if stored is not None:
        ref = stored.get("analysis_ref", "")
        shown = ref if isinstance(ref, str) and ref else "unknown"
        err_console.print(
            f"[dim]Cached analysis is stale (ref {shown} is not HEAD); re-analyzing.[/dim]"
        )
    return _run_analyze(config, repo_root)


def _resolved_ownership(
    ownership: OwnershipMap,
    config: Config,
    authors: AuthorIndex,
    repo_root: Path,
) -> OwnershipMap:
    resolved = _resolve_github_owners(ownership, config, authors, repo_root)
    resolved = exclude_contributors(
        resolved,
        config.contributors_exclude,
        confidence_threshold=config.analysis.confidence_threshold,
    )
    return _apply_extra_gaps(resolved, _api_gaps(resolved, config), config.scoring)


def _ownership_for_spec(config: Config, repo_root: Path, spec: str) -> OwnershipMap:
    paths = changed_paths(repo_root, spec, exclude=config.paths.exclude)
    as_of, analysis_ref = _resolve_clock(repo_root)
    if not paths:
        return OwnershipMap(paths={}, last_analyzed=as_of, analysis_ref=analysis_ref)
    if not run_options().no_cache:
        cached = reusable_ownership(
            repo_root,
            config,
            head=_try_head_sha(repo_root),
            allow_stale=run_options().allow_stale,
            max_age=run_options().max_age,
        )
        if cached is not None:
            kept = {path: cached.paths[path] for path in paths if path in cached.paths}
            return replace(cached, paths=kept)
    try:
        scoped = load_paths(
            repo_root,
            config,
            paths,
            None,
            as_of=as_of,
            review_provider=_review_provider(config, as_of, repo_root),
        )
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        _guard_ownership(exc)
    return _resolved_ownership(scoped.ownership, config, scoped.authors, repo_root)


def _fresh_or_scoped(config: Config, repo_root: Path) -> OwnershipMap:
    spec = run_options().diff_only
    if spec is None:
        return _run_analyze(config, repo_root)
    return _ownership_for_spec(config, repo_root, spec)


def _load_for_command(config: Config, repo_root: Path) -> OwnershipMap:
    spec = run_options().diff_only
    if spec is None:
        return _load_or_analyze(config, repo_root)
    return _ownership_for_spec(config, repo_root, spec)


def _load_cached(config: Config, repo_root: Path) -> OwnershipMap:
    """Return this repo's cached ownership, or exit 2 when it cannot be reused."""
    if run_options().no_cache:
        console.print("[red]Cached analysis is required; run `checkowners analyze`.[/red]")
        exit_with(ExitCode.CONFIG)
    cached = reusable_ownership(
        repo_root,
        config,
        head=_try_head_sha(repo_root),
        allow_stale=run_options().allow_stale,
        max_age=run_options().max_age,
    )
    if cached is None:
        console.print("[red]Cached analysis is stale or missing; run `checkowners analyze`.[/red]")
        exit_with(ExitCode.CONFIG)
    return cached


def _pull_request_change(repo_root: Path, number: int) -> tuple[tuple[str, ...], str]:
    if offline_enabled():
        raise PullRequestFilesError("Offline mode cannot read pull request files.")
    token = get_github_token()
    if not token:
        raise PullRequestFilesError("A token with pull-requests: read is required.")
    try:
        repository = github_repository(repo_root)
    except ReconciliationError as exc:
        raise PullRequestFilesError(
            "Could not read pull request files. Grant pull-requests: read."
        ) from exc
    return (
        pull_request_paths(repository, number, token),
        pull_request_base(repository, number, token),
    )


def _change_loader(
    repo_root: Path,
    config: Config,
) -> Callable[[tuple[str, ...], str | None], OwnershipMap]:
    as_of, _analysis_ref = _resolve_clock(repo_root)
    provider = _review_provider(config, as_of, repo_root)

    def load(paths: tuple[str, ...], revision: str | None) -> OwnershipMap:
        scoped = load_paths(
            repo_root,
            config,
            paths,
            revision,
            as_of=as_of,
            review_provider=provider,
        )
        return _resolved_ownership(scoped.ownership, config, scoped.authors, repo_root)

    return load


def _change_team_resolver(
    config: Config,
    repo_root: Path,
) -> Callable[[tuple[str, ...]], str | None] | None:
    if not config.github.resolve_teams or offline_enabled():
        return None
    token = get_github_token()
    if not token:
        return None
    org = config.github.org
    if not org:
        try:
            slug = github_repository(repo_root)
        except ReconciliationError:
            return None
        org = slug.split("/", 1)[0]
    if not org:
        return None
    return create_team_resolver(token, org)


def _analyze_target(config: Config, repo_root: Path, target: str) -> OwnershipMap:
    spec = run_options().diff_only
    if spec is not None:
        changed = set(changed_paths(repo_root, spec, exclude=config.paths.exclude))
        if target not in changed:
            as_of, analysis_ref = _resolve_clock(repo_root)
            return OwnershipMap(paths={}, last_analyzed=as_of, analysis_ref=analysis_ref)
    _warn_missing_api_token(config)
    as_of, analysis_ref = _resolve_clock(repo_root)
    try:
        scoped = analyze_repository(
            repo_root,
            config,
            review_provider=_review_provider(config, as_of, repo_root),
            as_of=as_of,
            analysis_ref=analysis_ref,
            pathspec=(target,),
            retain_all=True,
            max_workers=config.analysis.max_git_workers,
        )
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        _guard_ownership(exc)
    ownership = _resolve_github_owners(scoped.ownership, config, scoped.authors, repo_root)
    ownership = exclude_contributors(
        ownership,
        config.contributors_exclude,
        confidence_threshold=config.analysis.confidence_threshold,
    )
    return _apply_extra_gaps(ownership, _api_gaps(ownership, config), config.scoring)


def _declared_owners(repo_root: Path, target: str) -> tuple[str, ...]:
    codeowners_path = find_codeowners_path(repo_root)
    if not codeowners_path.exists():
        return ()
    rules = parse_rules(codeowners_path.read_text(encoding="utf-8"))
    matches = matching_rules(rules, target)
    if not matches:
        return ()
    return matches[-1].owners

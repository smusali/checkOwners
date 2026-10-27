"""checkOwners CLI entry point."""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn, TypeGuard

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from checkowners import __version__
from checkowners.action_report import (
    DIAGNOSTIC,
    build,
    publish_outputs,
    write_step_summary,
)
from checkowners.analyze import (
    GitRequirementError,
    ReviewProvider,
    analysis_epoch,
    analyze_ownership,
    apply_completeness,
    consume_analysis_outcome,
    consume_author_index,
    head_commit_sha,
    resolve_as_of,
    review_window_start,
    run_git,
)
from checkowners.balance import RECOMMENDATION_REASON, BalanceReport, ReviewLoad, analyze_balance
from checkowners.baseline import (
    DEFAULT_BASELINE_PATH,
    RatchetOutcome,
    apply_ratchet,
    finding_payload,
    load_baseline,
    visible_findings,
    write_baseline,
)
from checkowners.busfactor import (
    BusFactorReport,
    SimulationReport,
    UnknownIdentityError,
    classify,
    compute_qualified_owners,
    distribution_json,
    format_qualified_owner_count,
    qualified_owner_count_fields,
    simulate_removal,
    simulation_payload,
)
from checkowners.change import (
    build_change,
    change_payload,
    changed_paths,
    load_paths,
    merge_base,
    render_diff,
    render_suggest,
)
from checkowners.config import (
    CONFIG_ENV_VAR,
    CONFIG_FILENAME,
    find_codeowners_path,
    load_config,
)
from checkowners.decay import DecayReport, detect_decay
from checkowners.drift import (
    apply_severity_hysteresis,
    compute_severity,
    detect_drift,
    drift_entry_payload,
    evidence_gaps,
    format_fix_plan,
    reconciliation_body,
    write_github_output,
)
from checkowners.expertise import rank_expertise
from checkowners.explain import (
    ExplainedOwner,
    PathExplanation,
    WhyNotResult,
    build_explanation,
    explanation_payload,
    owners_payload,
    ranked_owners,
)
from checkowners.generate import (
    SIZE_WARN_BYTES,
    BroadPatternRecord,
    CodeownersGenerateError,
    CodeownersOverwriteError,
    CodeownersVerificationError,
    GenerateResult,
    broad_pattern_warning,
    codeowners_write_metrics,
    ensure_overwrite_safe,
    generate_codeowners,
)
from checkowners.github import (
    PullRequestFilesError,
    ReconciliationError,
    ReconciliationResult,
    begin_api_budget,
    build_review_coverage,
    clear_api_evidence,
    collection_gaps,
    create_team_resolver,
    external_evidence_payload,
    get_github_token,
    github_repository,
    offline_enabled,
    open_or_update_reconciliation,
    organization_members,
    publish_check_run,
    pull_request_base,
    pull_request_paths,
    resolve_handle_details,
    set_offline,
)
from checkowners.graph import (
    GraphExtraMissingError,
    build_graph,
    for_display,
    from_serializable,
    to_dot,
    to_serializable,
    to_text,
)
from checkowners.identity import (
    HandleHit,
    IdentityGraph,
    OwnerMerge,
    apply_contributor_status,
    apply_merge,
    audit_payload,
    audit_records,
    load_identity_graph,
    merge_owners,
    override_handles,
    probable_duplicates,
    record_certain_merges,
    revert_merge,
    save_identity_graph,
)
from checkowners.models import (
    ABSENT_TOKEN_REASON,
    AnalysisCompleteness,
    AnalysisGap,
    AnalyzeAnalysisJson,
    Config,
    ContributorStatus,
    DecayWarning,
    DecayWarningJson,
    DriftResult,
    ExpertiseRank,
    Finding,
    OwnerEntry,
    OwnerJson,
    OwnershipMap,
    PathOwnership,
    PathOwnershipJson,
    ScoringConfig,
    Severity,
    Suppression,
    ambiguous_identity_reason,
    analysis_flags_json,
    completeness_label,
    envelope_completeness,
    is_contributor_status,
    models_payload,
    owner_json,
    ownership_signal_completeness,
    path_analysis_json,
    repository_label,
    risk_from_scores,
    run_completeness,
    stamp_json,
    with_gaps,
)
from checkowners.onboard import OnboardingPath, generate_onboarding_path
from checkowners.patterns import (
    CodeownersMarkerError,
    CodeownersRule,
    matching_rules,
    parse_rules,
)
from checkowners.policy import (
    blocking_findings,
    effective_policy,
    invalid_owner_findings,
    security_findings,
)
from checkowners.privacy import (
    KNOWLEDGE_RISK_NOTICE,
    exclude_contributors,
    is_excluded,
    label,
    named_codeowners_allowed,
    sanitize,
    scrub_text,
    without_emails,
)
from checkowners.report_formats import (
    ReportFinding,
    check_annotations,
    combine_findings,
    findings_for_bus,
    findings_for_drift,
    findings_for_gaps,
    findings_for_shadows,
    findings_for_validation,
    junit_document,
    sarif_document,
    workflow_annotations,
)
from checkowners.risk import (
    Pathology,
    RiskEntry,
    RiskReport,
    assess_risk,
    risk_payload,
    select_pathologies,
)
from checkowners.state import (
    CacheInfo,
    cache_clear,
    cache_directory,
    cache_info,
    cache_purge,
    load_hysteresis,
    load_ownership,
    read_graph_cache,
    read_state,
    repository_identity,
    reusable_ownership,
    write_graph_cache,
    write_state,
)
from checkowners.topology import (
    TopologyReport,
    declared_teams_from_github,
    infer_topology,
)
from checkowners.trends import (
    HISTORICAL_LIMITATION,
    SHARED_AXIS_REFUSAL,
    TrendReport,
    analyze_trends,
    compared_axis_line,
    historical_model_line,
    trend_report_payload,
)
from checkowners.validate import validate_codeowners

if TYPE_CHECKING:
    import networkx as nx

app = typer.Typer(
    name="checkowners",
    help="Infer and maintain CODEOWNERS from git history.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)
baseline_app = typer.Typer(help="Manage the accepted-findings baseline.")
app.add_typer(baseline_app, name="baseline")
cache_app = typer.Typer(help="Inspect and delete the on-disk cache.")
app.add_typer(cache_app, name="cache")
identity_app = typer.Typer(help="Inspect and edit contributor identity.")
app.add_typer(identity_app, name="identity")

console = Console()
err_console = Console(stderr=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Output as JSON.")]
FormatOption = Annotated[
    str | None,
    typer.Option(
        "--format",
        help="Output format: text, json, sarif, or junit. --json selects json.",
        case_sensitive=False,
    ),
]
_FormatName = Literal["text", "json", "sarif", "junit"]
_MachineFormat = Literal["sarif", "junit"]
BaselineOption = Annotated[
    str | None,
    typer.Option(
        "--baseline",
        help="Accepted-findings file. Fail only on new findings.",
    ),
]

_CLI_AS_OF: ContextVar[str | None] = ContextVar("cli_as_of", default=None)
_EXIT_ZERO: ContextVar[bool] = ContextVar("cli_exit_zero", default=False)
_FAIL_ON_INCOMPLETE: ContextVar[bool] = ContextVar("cli_fail_on_incomplete", default=False)
_ALLOW_STALE: ContextVar[bool] = ContextVar("cli_allow_stale", default=False)
_MAX_AGE: ContextVar[int | None] = ContextVar("cli_max_age", default=None)
_NO_CACHE: ContextVar[bool] = ContextVar("cli_no_cache", default=False)
_NO_INCREMENTAL: ContextVar[bool] = ContextVar("cli_no_incremental", default=False)
_REDACT_EMAILS: ContextVar[bool] = ContextVar("cli_redact_emails", default=False)
_ACTIVE_CONFIG: ContextVar[Config | None] = ContextVar("cli_config", default=None)
_DIFF_ONLY: ContextVar[str | None] = ContextVar("cli_diff_only", default=None)
_DIFF_COMMANDS = frozenset(
    {
        "analyze",
        "drift",
        "risk",
        "bus-factor",
        "decay",
        "suggest",
        "balance",
        "explain",
        "owners",
        "who",
        "expertise",
        "qualified-owners",
    }
)

_OFFLINE_LINES = (
    "Network access: disabled",
    "Review evidence: unavailable",
    "Team verification: unavailable",
)


class ExitCode(IntEnum):
    SUCCESS = 0
    INTERNAL = 1
    CONFIG = 2
    FINDINGS = 3
    INTEGRATION = 4


def exit_with(code: ExitCode) -> NoReturn:
    if code is ExitCode.FINDINGS and _EXIT_ZERO.get():
        raise typer.Exit(code=ExitCode.SUCCESS)
    raise typer.Exit(code=code)


def _load_config() -> Config:
    try:
        config = load_config()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)
    if _REDACT_EMAILS.get():
        config = replace(config, privacy=replace(config.privacy, redact_emails=True))
    if config.policy.incomplete_analysis.fail:
        _FAIL_ON_INCOMPLETE.set(True)
    begin_api_budget(config.analysis.max_api_requests)
    _ACTIVE_CONFIG.set(config)
    return config


def _repo_id() -> str:
    return repository_identity(Path.cwd())


def _active_config() -> Config | None:
    return _ACTIVE_CONFIG.get()


def _aggregate() -> bool:
    config = _active_config()
    return config is not None and config.output.aggregate_only


def _person(identity: str) -> str:
    config = _active_config()
    if config is None:
        return identity
    if config.output.aggregate_only:
        return ""
    return label(identity, config, _repo_id())


def _public_payload(data: Mapping[str, object]) -> dict[str, object]:
    config = _active_config()
    if config is None:
        return dict(data)
    return sanitize(data, config, _repo_id())


def _scrubbed(text: str) -> str:
    config = _active_config()
    if config is None:
        return text
    return scrub_text(text, config, _repo_id())


def _scrubbed_annotations(findings: tuple[ReportFinding, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in check_annotations(findings):
        message = row.get("message")
        path = row.get("path")
        rows.append(
            {
                **row,
                "message": _scrubbed(message) if isinstance(message, str) else message,
                "path": _scrubbed(path) if isinstance(path, str) else path,
            }
        )
    return rows


def _refuse_named_codeowners(config: Config) -> None:
    if named_codeowners_allowed(config):
        return
    console.print(
        "[red]CODEOWNERS generation cannot name people while anonymize, "
        "aggregate_only, or identity.mode hashed is set.[/red]"
    )
    exit_with(ExitCode.CONFIG)


def _codeowners_ownership(ownership: OwnershipMap, config: Config) -> OwnershipMap:
    _refuse_named_codeowners(config)
    if not config.privacy.redact_emails:
        return ownership
    return without_emails(
        ownership,
        confidence_threshold=config.analysis.confidence_threshold,
    )


def _guard_ownership(exc: subprocess.CalledProcessError | OSError | ValueError) -> NoReturn:
    if isinstance(exc, (subprocess.CalledProcessError, OSError)):
        console.print(f"[red]Git command failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)
    if isinstance(exc, GitRequirementError):
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.INTEGRATION)
    console.print(f"[red]{exc}[/red]")
    exit_with(ExitCode.INTERNAL)


def _finish_analysis(ownership: OwnershipMap) -> None:
    if not _FAIL_ON_INCOMPLETE.get():
        return
    if run_completeness(ownership) < 1:
        exit_with(ExitCode.FINDINGS)


def _render_completeness(ownership: OwnershipMap) -> None:
    console.print(completeness_label(run_completeness(ownership)))
    for gap in ownership.analysis_completeness.gaps:
        console.print(gap.reason)


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


def _detect_drift(
    repo_root: Path,
    ownership: OwnershipMap,
    config: Config,
    codeowners_path: Path,
) -> DriftResult:
    try:
        return detect_drift(repo_root, ownership, config, codeowners_path=codeowners_path)
    except CodeownersMarkerError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)
    except (subprocess.CalledProcessError, OSError) as exc:
        console.print(f"[red]Git command failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"checkowners {__version__}")
        raise typer.Exit()


@app.callback()
def _app_callback(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Score recency as of this UTC instant (ISO 8601).",
        ),
    ] = None,
    deterministic: Annotated[
        bool,
        typer.Option(
            "--deterministic",
            help=(
                "Documented guarantee: analysis is a pure function of "
                "the repository, commit, and config."
            ),
        ),
    ] = False,
    exit_zero: Annotated[
        bool,
        typer.Option(
            "--exit-zero",
            help="Exit 0 when the only problem is findings.",
        ),
    ] = False,
    fail_on_incomplete: Annotated[
        bool,
        typer.Option(
            "--fail-on-incomplete",
            help="Exit 3 when scored-signal completeness is below 1.",
        ),
    ] = False,
    allow_stale: Annotated[
        bool,
        typer.Option(
            "--allow-stale",
            help="Reuse cached analysis even when its commit is not HEAD.",
        ),
    ] = False,
    max_age: Annotated[
        int | None,
        typer.Option(
            "--max-age",
            min=0,
            help="Refuse cached analysis older than this many seconds. 0 expires immediately.",
        ),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Do not read or write the analysis cache.",
        ),
    ] = False,
    no_incremental: Annotated[
        bool,
        typer.Option(
            "--no-incremental",
            help="Recompute the full lookback window.",
        ),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Make no network calls. Review evidence and team verification are unavailable.",
        ),
    ] = False,
    redact_emails: Annotated[
        bool,
        typer.Option(
            "--redact-emails",
            help="Replace email addresses in output with stable tokens.",
        ),
    ] = False,
    diff_only: Annotated[
        str | None,
        typer.Option(
            "--diff-only",
            help="Limit read-only analysis to paths changed in SPEC (base...head or a revision).",
        ),
    ] = None,
) -> None:
    """Infer and maintain CODEOWNERS from git history."""
    clear_api_evidence()
    ctx.ensure_object(dict)
    ctx.obj["as_of"] = as_of
    ctx.obj["deterministic"] = deterministic
    _CLI_AS_OF.set(as_of)
    _EXIT_ZERO.set(exit_zero)
    _FAIL_ON_INCOMPLETE.set(fail_on_incomplete)
    _ALLOW_STALE.set(allow_stale)
    _MAX_AGE.set(max_age)
    _NO_CACHE.set(no_cache)
    _NO_INCREMENTAL.set(no_incremental)
    _REDACT_EMAILS.set(redact_emails)
    _DIFF_ONLY.set(diff_only)
    if diff_only is not None and ctx.invoked_subcommand not in _DIFF_COMMANDS:
        raise typer.BadParameter("--diff-only applies to read-only analysis commands")
    set_offline(offline)
    if offline:
        for line in _OFFLINE_LINES:
            err_console.print(line, highlight=False, markup=False)


def _analysis_stamp(ownership: OwnershipMap) -> dict[str, str]:
    return {
        "analysis_ref": ownership.analysis_ref,
        "analysis_epoch": analysis_epoch(ownership.last_analyzed),
    }


def _try_head_sha(repo_root: Path) -> str:
    try:
        return head_commit_sha(repo_root)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return ""


def _try_generated_at(repo_root: Path) -> str:
    try:
        return analysis_epoch(resolve_as_of(_CLI_AS_OF.get(), repo_root))
    except (OSError, ValueError, subprocess.CalledProcessError):
        return ""


def _stamp_output(
    data: Mapping[str, object],
    ownership: OwnershipMap | None = None,
    *,
    head_sha: str | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    repo_root = Path.cwd()
    resolved_head = head_sha
    if resolved_head is None:
        resolved_head = (
            ownership.analysis_ref if ownership is not None else _try_head_sha(repo_root)
        )
    resolved_at = generated_at
    if resolved_at is None:
        resolved_at = (
            analysis_epoch(ownership.last_analyzed)
            if ownership is not None
            else _try_generated_at(repo_root)
        )
    completeness, gaps = envelope_completeness(ownership)
    stamped = stamp_json(
        dict(data),
        repository=repository_label(repo_root),
        head_sha=resolved_head,
        generated_at=resolved_at,
        analysis_completeness=completeness,
        analysis_gaps=gaps,
        evidence=external_evidence_payload(resolved_head),
    )
    stamped["reviews_sampled"] = 0 if ownership is None else ownership.reviews_sampled
    stamped["reviews_window_days"] = 0 if ownership is None else ownership.reviews_window_days
    return stamped


def _emit_json(
    data: Mapping[str, object],
    ownership: OwnershipMap | None = None,
    *,
    head_sha: str | None = None,
    generated_at: str | None = None,
) -> None:
    stamped = _stamp_output(data, ownership, head_sha=head_sha, generated_at=generated_at)
    typer.echo(json.dumps(_public_payload(stamped), indent=2, sort_keys=True))


def _output_format(json_output: bool, output_format: str | None) -> _FormatName:
    chosen = output_format.casefold() if output_format else None
    if chosen is not None and chosen not in {"text", "json", "sarif", "junit"}:
        raise typer.BadParameter("--format must be text, json, sarif, or junit")
    if json_output and chosen is not None and chosen != "json":
        raise typer.BadParameter("--json and --format disagree")
    if json_output or chosen == "json":
        return "json"
    if chosen == "sarif":
        return "sarif"
    if chosen == "junit":
        return "junit"
    return "text"


def _is_machine(fmt: _FormatName) -> TypeGuard[_MachineFormat]:
    return fmt in {"sarif", "junit"}


def _emit_machine(
    fmt: _MachineFormat,
    findings: tuple[ReportFinding, ...],
    *,
    command: str,
    ownership: OwnershipMap | None,
) -> None:
    if fmt == "sarif":
        document = sarif_document(
            findings,
            command=command,
            provenance=_stamp_output({}, ownership),
        )
        typer.echo(json.dumps(_public_payload(document), indent=2, sort_keys=True))
        return
    typer.echo(_scrubbed(junit_document(findings, command=command)))


def _repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _gap_uri(repo_root: Path, codeowners_path: Path) -> str:
    override = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
    else:
        candidate = repo_root / CONFIG_FILENAME
    if candidate.is_file():
        return _repo_relative(repo_root, candidate)
    if codeowners_path.is_file():
        return _repo_relative(repo_root, codeowners_path)
    return _repo_relative(repo_root, candidate)


def _tracked_paths(repo_root: Path) -> tuple[str, ...]:
    try:
        result = run_git(repo_root, ["git", "ls-files"], check=True, paths=True)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def _gap_findings(
    ownership: OwnershipMap | None,
    repo_root: Path,
    codeowners_path: Path,
    config: Config,
) -> tuple[ReportFinding, ...]:
    if ownership is None:
        return ()
    return findings_for_gaps(
        ownership.analysis_completeness.gaps,
        _gap_uri(repo_root, codeowners_path),
        config.policy.incomplete_analysis.severity,
    )


def _report_notice() -> None:
    err_console.print(KNOWLEDGE_RISK_NOTICE, highlight=False, markup=False, soft_wrap=True)


def _report_models(*names: Literal["ownership", "risk", "topology"]) -> None:
    payload = models_payload()
    parts = [f"{name} {payload[name]}" for name in names]
    err_console.print(f"[dim]models: {', '.join(parts)}[/dim]")
    _report_notice()


def _resolve_clock(repo_root: Path) -> tuple[datetime, str]:
    try:
        as_of = resolve_as_of(_CLI_AS_OF.get(), repo_root)
        ref = head_commit_sha(repo_root)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)
    except (subprocess.CalledProcessError, OSError) as exc:
        console.print(f"[red]Git command failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)
    return as_of, ref


def _severity_with_hysteresis(
    repo_root: Path,
    ownership: OwnershipMap,
    result: DriftResult,
    config: Config,
) -> Severity:
    raw = compute_severity(result, config)
    reported, pending, streak = apply_severity_hysteresis(
        raw, result.max_confidence_delta, config, load_hysteresis(repo_root)
    )
    if not _NO_CACHE.get():
        write_state(
            repo_root,
            ownership,
            drift_detected=result.drift_detected,
            qualified_owner_count_cap=config.analysis.top_n_owners,
            drift_reported_severity=reported,
            drift_pending_severity=pending,
            drift_pending_streak=streak,
            config=config,
        )
    return reported


def _status_configured(config: Config) -> bool:
    if config.contributor_roster or config.bot_addresses or config.github.org:
        return True
    return any(override.service_account for _handle, override in config.identity_overrides)


def _resolve_github_owners(
    ownership: OwnershipMap,
    config: Config,
    repo_root: Path | None = None,
) -> OwnershipMap:
    """Return `ownership` with handles resolved, certain duplicates merged, and status set."""
    index = consume_author_index()
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


def _confidence_style(confidence: float) -> str:
    if confidence >= 0.7:
        return "green"
    if confidence >= 0.4:
        return "yellow"
    return "red"


def _format_last_commit(value: datetime | None) -> str:
    return value.date().isoformat() if value else "-"


def _decay_warning_json(
    warning: DecayWarning,
    owners: tuple[OwnerEntry, ...] = (),
) -> DecayWarningJson:
    payload: DecayWarningJson = {
        "handle": warning.handle,
        "days_since_last_commit": warning.days_since_last_commit,
        "last_commit": warning.last_commit.isoformat(),
        "historical_confidence": round(warning.historical_confidence, 4),
        "status": warning.status,
    }
    for owner in owners:
        if owner.handle == warning.handle and owner.contributor_status == "former":
            payload["contributor_status"] = "former"
            break
    return payload


def _owner_payload(owner: OwnerEntry) -> OwnerJson:
    return owner_json(owner)


def _path_payload(
    po: PathOwnership,
    cap: int,
    thresholds: tuple[float, float, float],
) -> PathOwnershipJson:
    counts = qualified_owner_count_fields(po.qualified_owner_count, cap)
    scored = po.scored_owners or po.owners
    return {
        "owners": [_owner_payload(owner) for owner in po.owners],
        "analysis": path_analysis_json(po.owners),
        "risk": risk_from_scores(
            tuple(owner.ownership_score for owner in scored),
            thresholds,
        ),
        "qualified_owner_count": counts["qualified_owner_count"],
        "qualified_owner_count_cap": counts["qualified_owner_count_cap"],
        "decay_warnings": [
            _decay_warning_json(warning, po.owners) for warning in po.decay_warnings
        ],
    }


def _analyze_analysis(ownership: OwnershipMap) -> AnalyzeAnalysisJson:
    completeness, available = ownership_signal_completeness(ownership)
    return {
        "completeness": completeness,
        "signals_available": available,
        **analysis_flags_json(ownership.analysis_completeness),
    }


def _render_ownership_table(ownership: OwnershipMap, cap: int) -> None:
    if not ownership.paths:
        console.print("[yellow]No ownership data inferred.[/yellow]")
        return
    table = Table(title="Inferred Ownership")
    table.add_column("Path", style="cyan")
    table.add_column("Owners (score/quality)", style="white")
    table.add_column("Qualified owners", justify="right")
    table.add_column("Decay", justify="right")
    for path in sorted(ownership.paths):
        po = ownership.paths[path]
        if _aggregate():
            owners_str = "-"
        else:
            owners_str = ", ".join(
                (
                    f"[{_confidence_style(o.confidence)}]"
                    f"{escape(_person(o.handle))} ({o.score_label})[/]"
                )
                for o in po.owners
            )
        count = format_qualified_owner_count(po.qualified_owner_count, cap)
        if po.qualified_owner_count <= 1:
            count = f"[red]{count}[/red]"
        decay = str(len(po.decay_warnings)) if po.decay_warnings else "-"
        table.add_row(escape(path), owners_str, count, decay)
    console.print(table)


def _render_ignore_revs_line(completeness: AnalysisCompleteness) -> None:
    if completeness.ignore_revs_applied:
        label = completeness.ignore_revs_file or "configured"
        console.print(f"Blame ignore-revs: applied ({label})")
        return
    console.print("Blame ignore-revs: not found")


def _render_mailmap_line(completeness: AnalysisCompleteness, *, enabled: bool) -> None:
    if not enabled:
        console.print("Mailmap: disabled")
        return
    if completeness.mailmap_applied:
        label = completeness.mailmap_file or "configured"
        console.print(f"Mailmap: applied ({label})")
        return
    console.print("Mailmap: not found")


def _render_git_history_line(
    completeness: AnalysisCompleteness,
    *,
    count_co_authors: bool,
) -> None:
    if count_co_authors:
        console.print(
            f"Git history: merge strategy {completeness.merge_strategy}, "
            f"{completeness.co_author_count} co-authors"
        )
        return
    console.print(f"Git history: merge strategy {completeness.merge_strategy}, co-authors off")


def _render_exclusions_line(completeness: AnalysisCompleteness) -> None:
    console.print(
        f"Exclusions: {completeness.excluded_gitattributes} gitattributes, "
        f"{completeness.excluded_static} static"
    )


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
    outcome = None
    try:
        with progress:
            task_id = progress.add_task("Analyzing git history (blame pass)", total=None)

            def on_progress(done: int, total: int) -> None:
                progress.update(task_id, completed=done, total=total)

            consume_analysis_outcome()
            ownership = analyze_ownership(
                repo_root,
                config,
                review_provider=_review_provider(config, as_of, repo_root),
                on_progress=on_progress,
                as_of=as_of,
                analysis_ref=analysis_ref,
                max_workers=config.analysis.max_git_workers,
                incremental=not _NO_INCREMENTAL.get() and not _NO_CACHE.get(),
                capture_baseline=not _NO_CACHE.get(),
            )
            outcome = consume_analysis_outcome()
            if outcome is not None and outcome.mode == "incremental":
                err_console.print(
                    "[dim]Incremental analysis from "
                    f"{outcome.prior_ref} to {ownership.analysis_ref}.[/dim]"
                )
            elif outcome is not None and outcome.fallback:
                err_console.print(f"[dim]Full analysis: {outcome.fallback}.[/dim]")
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        _guard_ownership(exc)
    ownership = _resolve_github_owners(ownership, config, repo_root)
    ownership = exclude_contributors(
        ownership,
        config.contributors_exclude,
        confidence_threshold=config.analysis.confidence_threshold,
    )
    ownership = _apply_extra_gaps(ownership, _api_gaps(ownership, config), config.scoring)
    if not _NO_CACHE.get():
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
    if _NO_CACHE.get():
        return _run_analyze(config, repo_root)
    head = _try_head_sha(repo_root)
    cached = reusable_ownership(
        repo_root,
        config,
        head=head,
        allow_stale=_ALLOW_STALE.get(),
        max_age=_MAX_AGE.get(),
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
    repo_root: Path,
) -> OwnershipMap:
    resolved = _resolve_github_owners(ownership, config, repo_root)
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
    if not _NO_CACHE.get():
        cached = reusable_ownership(
            repo_root,
            config,
            head=_try_head_sha(repo_root),
            allow_stale=_ALLOW_STALE.get(),
            max_age=_MAX_AGE.get(),
        )
        if cached is not None:
            kept = {path: cached.paths[path] for path in paths if path in cached.paths}
            return replace(cached, paths=kept)
    try:
        ownership = load_paths(
            repo_root,
            config,
            paths,
            None,
            as_of=as_of,
            review_provider=_review_provider(config, as_of, repo_root),
        )
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        _guard_ownership(exc)
    return _resolved_ownership(ownership, config, repo_root)


def _fresh_or_scoped(config: Config, repo_root: Path) -> OwnershipMap:
    spec = _DIFF_ONLY.get()
    if spec is None:
        return _run_analyze(config, repo_root)
    return _ownership_for_spec(config, repo_root, spec)


def _load_for_command(config: Config, repo_root: Path) -> OwnershipMap:
    spec = _DIFF_ONLY.get()
    if spec is None:
        return _load_or_analyze(config, repo_root)
    return _ownership_for_spec(config, repo_root, spec)


def _expertise_rank_payload(rank: ExpertiseRank) -> dict[str, Any]:
    return {
        "handle": rank.handle,
        "confidence": round(rank.confidence, 4),
        "commits": rank.commits,
        "last_commit": rank.last_commit.isoformat() if rank.last_commit else None,
    }


@app.command()
def analyze(json_output: JsonOption = False) -> None:
    """Analyze git history and rank owners by ownership score."""
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _fresh_or_scoped(config, repo_root)
    cap = config.analysis.top_n_owners
    if json_output:
        data = {
            "analysis": _analyze_analysis(ownership),
            "inferred": {
                path: _path_payload(po, cap, config.risk.truck_factor_thresholds)
                for path, po in sorted(ownership.paths.items())
            },
            "last_analyzed": ownership.last_analyzed.isoformat(),
        }
        _emit_json(data, ownership)
    else:
        _render_ownership_table(ownership, cap)
        _render_ignore_revs_line(ownership.analysis_completeness)
        _render_mailmap_line(ownership.analysis_completeness, enabled=config.git.use_mailmap)
        _render_exclusions_line(ownership.analysis_completeness)
        _render_git_history_line(
            ownership.analysis_completeness,
            count_co_authors=config.git.count_co_authors,
        )
        _render_completeness(ownership)
        _report_models("ownership")
    _finish_analysis(ownership)


ForceOption = Annotated[
    bool,
    typer.Option(
        "--force",
        help=(
            "Overwrite a CODEOWNERS file that was not generated by checkOwners, "
            "or write a file larger than output.max_bytes."
        ),
    ),
]

AllowBroadOption = Annotated[
    bool,
    typer.Option(
        "--allow-broad-patterns",
        help=(
            "Emit sanitized wildcard rules even when they match extra paths with different owners."
        ),
    ),
]


def _check_overwrite_or_exit(codeowners_path: Path, config: Config, force: bool) -> None:
    """Fail before the expensive analyze when the target would be refused."""
    try:
        ensure_overwrite_safe(codeowners_path, config.output.header, force=force)
    except CodeownersOverwriteError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)


def _warn_codeowners_size(content: str) -> None:
    size = len(content.encode("utf-8"))
    if size < SIZE_WARN_BYTES:
        return
    err_console.print(
        f"[yellow]Generated CODEOWNERS is {size} bytes "
        f"(warning threshold 2 MB). GitHub does not load files over 3 MB.[/yellow]"
    )


def _warn_broad_patterns(records: tuple[BroadPatternRecord, ...]) -> None:
    for record in records:
        if record.accepted:
            continue
        err_console.print(f"[yellow]{escape(broad_pattern_warning(record))}[/yellow]")


def _generate_or_exit(
    repo_root: Path,
    ownership: OwnershipMap,
    config: Config,
    codeowners_path: Path,
    *,
    force: bool,
    write: bool = True,
) -> GenerateResult:
    token = get_github_token()
    try:
        result = generate_codeowners(
            repo_root,
            ownership,
            config,
            codeowners_path=codeowners_path,
            token=token,
            org=config.github.org,
            force=force,
            write=write,
        )
    except CodeownersVerificationError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.FINDINGS)
    except CodeownersGenerateError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)
    _warn_codeowners_size(result.content)
    _warn_broad_patterns(result.broad_patterns)
    return result


@app.command()
def generate(
    json_output: JsonOption = False,
    force: ForceOption = False,
    allow_broad_patterns: AllowBroadOption = False,
) -> None:
    """Generate a CODEOWNERS file from inferred ownership."""
    config = _load_config()
    if allow_broad_patterns:
        config = replace(config, output=replace(config.output, allow_broad_patterns=True))
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    _check_overwrite_or_exit(codeowners_path, config, force)
    ownership = _run_analyze(config, repo_root)
    ownership = _codeowners_ownership(ownership, config)
    result = _generate_or_exit(repo_root, ownership, config, codeowners_path, force=force)
    rel_path = codeowners_path.relative_to(repo_root)
    if json_output:
        _emit_json(
            {
                "path": rel_path.as_posix(),
                "content": result.content,
                "broad_patterns": [record.as_json() for record in result.broad_patterns],
                **codeowners_write_metrics(result.content),
            },
            ownership,
        )
    else:
        console.print(f"[green]Generated {rel_path}[/green]")
    _finish_analysis(ownership)


@app.command(name="print")
def print_cmd(json_output: JsonOption = False) -> None:
    """Print inferred ownership to stdout."""
    config = _load_config()
    ownership = _run_analyze(config, Path.cwd())
    cap = config.analysis.top_n_owners
    if json_output:
        _emit_json(
            {
                "paths": {
                    path: _path_payload(po, cap, config.risk.truck_factor_thresholds)
                    for path, po in sorted(ownership.paths.items())
                },
            },
            ownership,
        )
    else:
        for path in sorted(ownership.paths):
            if _aggregate():
                typer.echo(path)
                continue
            owners = " ".join(
                f"{_person(o.handle)}({o.score_label})" for o in ownership.paths[path].owners
            )
            typer.echo(f"{path}\t{owners}")
        _render_completeness(ownership)
        _report_models("ownership")
    _finish_analysis(ownership)


@app.command()
def validate(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
) -> None:
    """Validate CODEOWNERS file syntax."""
    fmt = _output_format(json_output, output_format)
    config = _load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    errors = validate_codeowners(repo_root, codeowners_path=codeowners_path)
    findings = invalid_owner_findings(
        repo_root,
        codeowners_path,
        errors,
        include_missing=True,
    )
    outcome = _ratchet_or_exit(
        DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        config,
        as_of=_policy_as_of(repo_root),
        baseline=_resolve_baseline(None, config),
        structural=findings,
    )
    blocking = blocking_findings(outcome.new, config, None, ())
    payload = {
        "valid": len(errors) == 0,
        "errors": [{"line": e.line_number, "message": e.message} for e in errors],
        **_policy_payload(config, blocking),
    }
    if fmt == "json":
        _emit_json(payload)
    elif _is_machine(fmt):
        uri = _repo_relative(repo_root, codeowners_path)
        rules = _codeowners_rules(codeowners_path)
        machine = combine_findings(
            findings_for_validation(errors, uri, config.policy.invalid_owner.severity),
            findings_for_shadows(rules, _tracked_paths(repo_root), uri),
        )
        _emit_machine(fmt, machine, command="validate", ownership=None)
    elif not errors:
        console.print("[green]CODEOWNERS is valid.[/green]")
        _render_blocking(blocking)
    else:
        for err in errors:
            console.print(f"[red]Line {err.line_number}:[/red] {escape(err.message)}")
        _render_blocking(blocking)
    if blocking:
        exit_with(ExitCode.FINDINGS)


@app.command()
def explain_path(
    path: Annotated[str, typer.Argument(help="Repo-relative path to resolve against CODEOWNERS.")],
    json_output: JsonOption = False,
) -> None:
    """Show which CODEOWNERS rule owns a path, and the full match chain."""
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    if not codeowners_path.exists():
        console.print(f"[red]No CODEOWNERS file found at {codeowners_path}.[/red]")
        exit_with(ExitCode.CONFIG)
    rules = parse_rules(codeowners_path.read_text(encoding="utf-8"))
    matches = matching_rules(rules, path)
    winner = matches[-1] if matches else None
    if json_output:
        match_payloads: list[dict[str, object]] = [
            {
                "line": rule.line_number,
                "pattern": rule.pattern,
                "owners": list(rule.owners),
                "wins": winner is not None and rule.line_number == winner.line_number,
            }
            for rule in matches
        ]
        winner_payload: dict[str, object] | None = (
            {
                "line": winner.line_number,
                "pattern": winner.pattern,
                "owners": list(winner.owners),
            }
            if winner is not None
            else None
        )
        _emit_json({"path": path, "matches": match_payloads, "winner": winner_payload})
        return
    if winner is None:
        console.print(f"[yellow]No CODEOWNERS rule matches {escape(path)}.[/yellow]")
        return
    console.print(f"[bold]{escape(path)}[/bold]")
    console.print("Match chain (last match wins):")
    for rule in matches:
        is_winner = rule.line_number == winner.line_number
        owners = " ".join(rule.owners) if rule.owners else "(none)"
        mark = "  <- winner" if is_winner else ""
        style = "green" if is_winner else "dim"
        console.print(
            f"  [{style}]line {rule.line_number}  {escape(rule.pattern)} {escape(owners)}{mark}[/]"
        )


def _resolve_baseline(flag: str | None, config: Config) -> Path | None:
    if flag is not None and flag.strip():
        return Path(flag.strip())
    raw = config.drift.baseline_file.strip()
    return Path(raw) if raw else None


def _print_expired_suppressions(expired: tuple[Suppression, ...]) -> None:
    for item in expired:
        expires = item.expires.isoformat() if item.expires is not None else ""
        console.print(
            f"[red]Expired suppression:[/red] {escape(item.path)} {item.rule} "
            f"expired {expires}: {escape(item.reason)}"
        )


def _ratchet_or_exit(
    result: DriftResult,
    config: Config,
    *,
    as_of: datetime,
    baseline: Path | None,
    bus: BusFactorReport | None = None,
    structural: tuple[Finding, ...] | None = None,
) -> RatchetOutcome:
    try:
        accepted = load_baseline(baseline) if baseline is not None else ()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)
    outcome = apply_ratchet(
        result,
        baseline=accepted,
        suppressions=config.suppressions,
        as_of=as_of.date(),
        bus=bus,
        structural=structural,
    )
    if outcome.expired:
        _print_expired_suppressions(outcome.expired)
        exit_with(ExitCode.FINDINGS)
    return outcome


def _ratchet_json(outcome: RatchetOutcome) -> dict[str, object]:
    return {
        "baselined": outcome.counts.baselined,
        "stale_baseline": [finding_payload(item) for item in outcome.stale_baseline],
        "suppressed": outcome.counts.suppressed,
    }


def _policy_as_of(repo_root: Path) -> datetime:
    try:
        return resolve_as_of(_CLI_AS_OF.get(), repo_root)
    except (ValueError, OSError, subprocess.CalledProcessError):
        return datetime(1970, 1, 1, tzinfo=UTC)


def _drift_override(config: Config, fail_on_drift: bool | None) -> Config:
    if fail_on_drift is None:
        return config
    updated = replace(config.policy.drift, fail=fail_on_drift)
    config = replace(config, policy=replace(config.policy, drift=updated))
    _ACTIVE_CONFIG.set(config)
    return config


def _codeowners_rules(codeowners_path: Path) -> tuple[CodeownersRule, ...]:
    if not codeowners_path.exists():
        return ()
    return parse_rules(codeowners_path.read_text(encoding="utf-8"))


def _policy_payload(config: Config, blocking: tuple[Finding, ...]) -> dict[str, object]:
    return {
        "policy": effective_policy(config),
        "blocking": [finding_payload(item) for item in blocking],
    }


def _render_blocking(blocking: tuple[Finding, ...]) -> None:
    if not blocking:
        console.print("Blocking: none")
        return
    shown = ", ".join(f"{item.rule} {escape(item.path)}" for item in blocking)
    console.print(f"[red]Blocking:[/red] {shown}")


def _enforcement(
    repo_root: Path,
    codeowners_path: Path,
    ownership: OwnershipMap,
    config: Config,
    result: DriftResult,
    *,
    baseline: Path | None,
    bus: BusFactorReport | None,
) -> tuple[RatchetOutcome, tuple[Finding, ...]]:
    rules = _codeowners_rules(codeowners_path)
    errors = validate_codeowners(repo_root, codeowners_path=codeowners_path)
    structural = (
        *invalid_owner_findings(
            repo_root,
            codeowners_path,
            errors,
            include_missing=False,
        ),
        *security_findings(ownership, config, rules),
    )
    outcome = _ratchet_or_exit(
        result,
        config,
        as_of=ownership.last_analyzed,
        baseline=baseline,
        bus=bus,
        structural=structural,
    )
    return outcome, blocking_findings(outcome.new, config, ownership, rules)


def _render_ratchet_summary(outcome: RatchetOutcome) -> None:
    counts = outcome.counts
    console.print(
        f"Baselined: {counts.baselined}  "
        f"Suppressed: {counts.suppressed}  "
        f"Stale baseline: {counts.stale_baseline}"
    )
    for item in outcome.stale_baseline:
        owners = " ".join(item.owners)
        suffix = f" {escape(owners)}" if owners else ""
        console.print(f"[yellow]stale baseline:[/yellow] {item.rule} {escape(item.path)}{suffix}")


def _filter_bus_payload(
    payload: dict[str, object],
    hidden: frozenset[str],
) -> dict[str, object]:
    if not hidden:
        return payload
    filtered = dict(payload)
    entries = payload.get("entries")
    if isinstance(entries, list):
        filtered["entries"] = [
            item for item in entries if not (isinstance(item, dict) and item.get("path") in hidden)
        ]
    paths = payload.get("critical_paths")
    if isinstance(paths, list):
        filtered["critical_paths"] = [item for item in paths if item not in hidden]
    return filtered


def _render_drift_table(result: DriftResult) -> None:
    table = Table(title="CODEOWNERS Drift")
    table.add_column("Category", style="bold")
    table.add_column("Path", style="cyan")
    table.add_column("Δ", justify="right")
    table.add_column("Reason")
    for entry in result.stale:
        table.add_row(
            "[red]stale[/red]",
            escape(entry.path),
            f"{entry.confidence_delta:.2f}",
            escape(entry.reason),
        )
    for entry in result.missing:
        count_low = entry.qualified_owner_count is not None and entry.qualified_owner_count <= 1
        flag = " [red](qualified_owner_count=1)[/red]" if count_low else ""
        decay = " [magenta](decay)[/magenta]" if entry.decay else ""
        table.add_row(
            "[yellow]missing[/yellow]",
            escape(entry.path),
            f"{entry.confidence_delta:.2f}",
            escape(entry.reason) + flag + decay,
        )
    for entry in result.changed:
        table.add_row(
            "[cyan]changed[/cyan]",
            escape(entry.path),
            f"{entry.confidence_delta:.2f}",
            escape(entry.reason),
        )
    console.print(table)


@baseline_app.command("create")
def baseline_create(
    output: Annotated[
        Path,
        typer.Option("--output", help="Path to write the accepted-findings file."),
    ] = Path(DEFAULT_BASELINE_PATH),
    json_output: JsonOption = False,
) -> None:
    """Write current findings to an accepted-findings file."""
    config = _load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    ownership = _run_analyze(config, repo_root)
    result = _detect_drift(repo_root, ownership, config, codeowners_path)
    bus = compute_qualified_owners(ownership, config, target=None)
    outcome, _blocking = _enforcement(
        repo_root,
        codeowners_path,
        ownership,
        config,
        result,
        baseline=None,
        bus=bus,
    )
    write_baseline(output, outcome.new)
    ownership = _apply_extra_gaps(ownership, evidence_gaps(outcome.drift), config.scoring)
    if json_output:
        _emit_json(
            {
                "path": str(output),
                "findings": [finding_payload(item) for item in outcome.new],
            },
            ownership,
        )
    else:
        written = len(outcome.new)
        console.print(f"[green]Wrote {written} finding(s) to {escape(str(output))}[/green]")
        _render_completeness(ownership)
    _finish_analysis(ownership)


@app.command()
def drift(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
    baseline: BaselineOption = None,
    fix_plan: Annotated[
        bool,
        typer.Option(
            "--fix-plan",
            help="Print suggested CODEOWNERS edits and write nothing.",
        ),
    ] = False,
) -> None:
    """Detect drift between inferred and current CODEOWNERS."""
    fmt = _output_format(json_output, output_format)
    config = _load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    ownership = _fresh_or_scoped(config, repo_root)
    result = _detect_drift(repo_root, ownership, config, codeowners_path)
    bus = compute_qualified_owners(ownership, config, target=None)
    outcome, blocking = _enforcement(
        repo_root,
        codeowners_path,
        ownership,
        config,
        result,
        baseline=_resolve_baseline(baseline, config),
        bus=bus,
    )
    result = outcome.drift
    ownership = _apply_extra_gaps(ownership, evidence_gaps(result), config.scoring)
    severity = _severity_with_hysteresis(repo_root, ownership, result, config)
    cap = config.analysis.top_n_owners
    stamp = _analysis_stamp(ownership)
    completeness, gaps = envelope_completeness(ownership)
    write_github_output(
        result,
        cap,
        analysis_ref=stamp["analysis_ref"],
        analysis_epoch=stamp["analysis_epoch"],
        config=config,
        analysis_completeness=completeness,
        analysis_gaps=gaps,
    )
    plan = format_fix_plan(result, ownership) if fix_plan else ""
    if fmt == "json":
        data: dict[str, object] = {
            "stale": [drift_entry_payload(e, cap, config) for e in result.stale],
            "missing": [drift_entry_payload(e, cap, config) for e in result.missing],
            "changed": [drift_entry_payload(e, cap, config) for e in result.changed],
            "drift_detected": result.drift_detected,
            "severity": severity,
            "max_confidence_delta": round(result.max_confidence_delta, 4),
            "notes": list(result.notes),
            **_ratchet_json(outcome),
            **_policy_payload(config, blocking),
        }
        if fix_plan:
            data["fix_plan"] = plan
        _emit_json(data, ownership)
    elif _is_machine(fmt):
        uri = _repo_relative(repo_root, codeowners_path)
        findings = combine_findings(
            findings_for_drift(result, codeowners_uri=uri, config=config),
            _gap_findings(ownership, repo_root, codeowners_path, config),
        )
        _emit_machine(fmt, findings, command="drift", ownership=ownership)
    else:
        for note in result.notes:
            console.print(f"[yellow]note:[/yellow] {escape(note)}")
        _render_ratchet_summary(outcome)
        if not result.drift_detected:
            console.print("[green]No drift detected.[/green]")
        else:
            console.print(
                f"[bold]severity:[/bold] [{_severity_style(severity)}]{severity.upper()}[/] "
                f"(Δmax={result.max_confidence_delta:.2f})"
            )
            _render_drift_table(result)
        _render_blocking(blocking)
        _render_completeness(ownership)
        _report_models("ownership", "risk")
        if fix_plan:
            console.print(plan, markup=False, highlight=False, soft_wrap=True)
    if blocking:
        exit_with(ExitCode.FINDINGS)
    _finish_analysis(ownership)


def _severity_style(severity: str) -> str:
    return {"critical": "red", "high": "red", "medium": "yellow", "low": "green"}[severity]


@app.command()
def sync(
    json_output: JsonOption = False,
    force: ForceOption = False,
    allow_broad_patterns: AllowBroadOption = False,
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Commit CODEOWNERS on the current branch instead of opening a pull request.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the CODEOWNERS diff. Write nothing and open nothing.",
        ),
    ] = False,
    pull_request: Annotated[
        bool,
        typer.Option(
            "--pr",
            "--pull-request",
            help="Open or update a reconciliation pull request. This is the default.",
        ),
    ] = False,
) -> None:
    """Open or update a pull request that proposes CODEOWNERS changes."""
    config = _load_config()
    if allow_broad_patterns:
        config = replace(config, output=replace(config.output, allow_broad_patterns=True))
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    _check_overwrite_or_exit(codeowners_path, config, force)
    ownership = _run_analyze(config, repo_root)
    ownership = _codeowners_ownership(ownership, config)
    direct_commit = commit and not pull_request and not dry_run
    rel_path = codeowners_path.relative_to(repo_root)
    if dry_run:
        result = _generate_or_exit(
            repo_root, ownership, config, codeowners_path, force=force, write=False
        )
        _finish_sync_preview(rel_path, codeowners_path, result, ownership, json_output=json_output)
        return
    if direct_commit:
        result = _generate_or_exit(repo_root, ownership, config, codeowners_path, force=force)
        _commit_synced(repo_root, rel_path, result, ownership, json_output=json_output)
        return
    drift_result = _detect_drift(repo_root, ownership, config, codeowners_path)
    result = _generate_or_exit(
        repo_root, ownership, config, codeowners_path, force=force, write=False
    )
    current = _read_codeowners(codeowners_path)
    if current == result.content:
        _emit_sync(
            rel_path,
            result,
            ownership,
            json_output=json_output,
            committed=False,
            message=f"{rel_path} is already in sync; nothing to open.",
        )
        return
    try:
        opened = open_or_update_reconciliation(
            token=get_github_token(),
            repository=github_repository(repo_root),
            path=rel_path.as_posix(),
            content=result.content,
            body=reconciliation_body(drift_result, ownership, config),
        )
    except ReconciliationError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.INTEGRATION)
    verb = "Updated" if opened["updated"] else "Opened"
    _emit_sync(
        rel_path,
        result,
        ownership,
        json_output=json_output,
        committed=False,
        message=f"{verb} {opened['url']}",
        pull_request=opened,
    )


def _read_codeowners(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]Could not read {path}:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)


def _sync_payload(
    rel_path: Path,
    result: GenerateResult,
    *,
    committed: bool,
    pull_request: ReconciliationResult | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": rel_path.as_posix(),
        "committed": committed,
        "content": result.content,
        "broad_patterns": [record.as_json() for record in result.broad_patterns],
        **codeowners_write_metrics(result.content),
    }
    if pull_request is not None:
        payload["pull_request"] = {
            "number": pull_request["number"],
            "url": pull_request["url"],
            "updated": pull_request["updated"],
        }
    return payload


def _emit_sync(
    rel_path: Path,
    result: GenerateResult,
    ownership: OwnershipMap,
    *,
    json_output: bool,
    committed: bool,
    message: str,
    pull_request: ReconciliationResult | None = None,
) -> None:
    if json_output:
        _emit_json(
            _sync_payload(rel_path, result, committed=committed, pull_request=pull_request),
            ownership,
        )
    else:
        console.print(f"[green]{message}[/green]")
    _finish_analysis(ownership)


def _finish_sync_preview(
    rel_path: Path,
    codeowners_path: Path,
    result: GenerateResult,
    ownership: OwnershipMap,
    *,
    json_output: bool,
) -> None:
    current = _read_codeowners(codeowners_path)
    if json_output:
        _emit_sync(
            rel_path,
            result,
            ownership,
            json_output=True,
            committed=False,
            message="",
        )
        return
    if current == result.content:
        _emit_sync(
            rel_path,
            result,
            ownership,
            json_output=False,
            committed=False,
            message=f"{rel_path} is already in sync; nothing to open.",
        )
        return
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            result.content.splitlines(keepends=True),
            fromfile=str(rel_path),
            tofile=str(rel_path),
        )
    )
    console.print(diff, markup=False, highlight=False)
    _finish_analysis(ownership)


def _commit_synced(
    repo_root: Path,
    rel_path: Path,
    result: GenerateResult,
    ownership: OwnershipMap,
    *,
    json_output: bool,
) -> None:
    if not _has_uncommitted_changes(repo_root, rel_path):
        _emit_sync(
            rel_path,
            result,
            ownership,
            json_output=json_output,
            committed=False,
            message=f"{rel_path} is already in sync; nothing to commit.",
        )
        return
    try:
        run_git(repo_root, ["git", "add", rel_path.as_posix()], check=True)
        run_git(
            repo_root,
            ["git", "commit", "-m", "chore: sync CODEOWNERS via checkowners"],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        console.print(f"[red]Git commit failed:[/red] {detail}")
        exit_with(ExitCode.INTEGRATION)
    except OSError as exc:
        console.print(f"[red]Git commit failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)
    _emit_sync(
        rel_path,
        result,
        ownership,
        json_output=json_output,
        committed=True,
        message=f"Generated and committed {rel_path}",
    )


def _has_uncommitted_changes(repo_root: Path, rel_path: Path) -> bool:
    """True when the generated file differs from what is committed."""
    try:
        result = run_git(
            repo_root,
            ["git", "status", "--porcelain", "--", rel_path.as_posix()],
            check=True,
        )
    except subprocess.CalledProcessError:
        return True
    return bool(result.stdout.strip())


def _positive_entry_limit(value: int) -> int:
    if value < 1:
        raise typer.BadParameter("must be a positive integer")
    return value


def _write_action_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(_public_payload(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _publish_action_failure() -> None:
    write_step_summary(DIAGNOSTIC)
    publish_outputs()
    publish_check_run(conclusion="failure", summary=DIAGNOSTIC, annotations=[])


def _action_findings(
    repo_root: Path,
    codeowners_path: Path,
    ownership: OwnershipMap,
    config: Config,
    result: DriftResult,
    bus: BusFactorReport | None,
) -> tuple[ReportFinding, ...]:
    uri = _repo_relative(repo_root, codeowners_path)
    errors = validate_codeowners(repo_root, codeowners_path=codeowners_path)
    rules = _codeowners_rules(codeowners_path)
    groups: list[tuple[ReportFinding, ...]] = [
        findings_for_drift(result, codeowners_uri=uri, config=config),
        findings_for_validation(errors, uri, config.policy.invalid_owner.severity),
        findings_for_shadows(rules, _tracked_paths(repo_root), uri),
        _gap_findings(ownership, repo_root, codeowners_path, config),
    ]
    if bus is not None:
        groups.append(findings_for_bus(bus))
    return combine_findings(*groups)


def _write_action_machines(
    findings: tuple[ReportFinding, ...],
    ownership: OwnershipMap,
) -> None:
    document = sarif_document(
        findings,
        command="github-action",
        provenance=_stamp_output({}, ownership),
    )
    _write_action_json(Path("checkowners.sarif"), document)
    Path("checkowners.junit.xml").write_text(
        _scrubbed(junit_document(findings, command="github-action")),
        encoding="utf-8",
    )


def _emit_action_annotations(findings: tuple[ReportFinding, ...]) -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    text = _scrubbed(workflow_annotations(findings))
    if text:
        print(text, end="", file=sys.stderr)


@app.command(name="github-action")
def github_action(
    fail_on_drift: Annotated[
        bool | None,
        typer.Option(
            "--fail-on-drift/--no-fail-on-drift",
            envvar="CHECKOWNERS_FAIL_ON_DRIFT",
            help="Override policy.drift.fail. Omit to use the configured policy.",
        ),
    ] = None,
    include_bus_factor: Annotated[
        bool,
        typer.Option(
            "--include-bus-factor/--no-include-bus-factor",
            envvar="CHECKOWNERS_INCLUDE_BUS_FACTOR",
            help="Write bus_factor.json and the bus_factor_summary output.",
        ),
    ] = True,
    include_decay: Annotated[
        bool,
        typer.Option(
            "--include-decay/--no-include-decay",
            envvar="CHECKOWNERS_INCLUDE_DECAY",
            help="Write decay.json and the decay_summary output.",
        ),
    ] = True,
    include_balance: Annotated[
        bool,
        typer.Option(
            "--include-balance/--no-include-balance",
            envvar="CHECKOWNERS_INCLUDE_BALANCE",
            help="Write balance.json and the balance_summary output.",
        ),
    ] = False,
    max_output_entries: Annotated[
        int,
        typer.Option(
            "--max-output-entries",
            envvar="MAX_OUTPUT_ENTRIES",
            callback=_positive_entry_limit,
            help="Maximum entries per list in GITHUB_OUTPUT summaries.",
        ),
    ] = 50,
    json_output: JsonOption = False,
    baseline: BaselineOption = None,
) -> None:
    """Run the full CI flow (drift + qualified owners + decay) and write GITHUB_OUTPUT."""
    owners_report: BusFactorReport | None = None
    decay_payload: dict[str, object] | None = None
    balance_payload: dict[str, object] | None = None
    decay_count = 0
    try:
        config = _drift_override(_load_config(), fail_on_drift)
        repo_root = Path.cwd()
        codeowners_path = find_codeowners_path(repo_root)
        ownership = _load_or_analyze(config, repo_root)
        cap = config.analysis.top_n_owners
        result = _detect_drift(repo_root, ownership, config, codeowners_path)
        if include_bus_factor:
            owners_report = compute_qualified_owners(ownership, config, target=None)
        if include_decay:
            decay_reports = detect_decay(ownership, config)
            decay_payload = {
                "reports": [_decay_report_payload(r) for r in decay_reports],
            }
            decay_count = len(decay_reports)
        if include_balance:
            balance_report = analyze_balance(
                ownership,
                config,
                rules=_codeowners_rules(codeowners_path),
            )
            balance_payload = _balance_payload(balance_report)
    except typer.Exit:
        _publish_action_failure()
        raise
    except GitRequirementError as exc:
        _publish_action_failure()
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.INTEGRATION)
    except (subprocess.CalledProcessError, OSError) as exc:
        _publish_action_failure()
        console.print(f"[red]Git command failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)
    except Exception:
        _publish_action_failure()
        exit_with(ExitCode.INTERNAL)

    outcome, blocking = _enforcement(
        repo_root,
        codeowners_path,
        ownership,
        config,
        result,
        baseline=_resolve_baseline(baseline, config),
        bus=owners_report,
    )
    result = outcome.drift
    ownership = _apply_extra_gaps(ownership, evidence_gaps(result), config.scoring)
    severity = _severity_with_hysteresis(repo_root, ownership, result, config)
    ratchet = _ratchet_json(outcome)
    drift_payload: dict[str, object] = {
        "drift_detected": result.drift_detected,
        "severity": severity,
        "max_confidence_delta": round(result.max_confidence_delta, 4),
        "stale": [drift_entry_payload(e, cap, config) for e in result.stale],
        "missing": [drift_entry_payload(e, cap, config) for e in result.missing],
        "changed": [drift_entry_payload(e, cap, config) for e in result.changed],
        "notes": list(result.notes),
        **ratchet,
        **_policy_payload(config, blocking),
    }
    bus_payload: dict[str, object] | None = None
    critical_paths = 0
    if owners_report is not None:
        bus_payload = _filter_bus_payload(
            {**_qualified_owners_payload(owners_report, config), **ratchet},
            outcome.hidden_bus_paths,
        )
        raw_paths = bus_payload.get("critical_paths")
        critical_paths = len(raw_paths) if isinstance(raw_paths, list) else 0

    drift_payload = _stamp_output(drift_payload, ownership)
    if bus_payload is not None:
        bus_payload = _stamp_output(bus_payload, ownership)
    if decay_payload is not None:
        decay_payload = _stamp_output(decay_payload, ownership)
    if balance_payload is not None:
        balance_payload = _stamp_output(balance_payload, ownership)

    _write_action_json(Path("drift.json"), drift_payload)
    if bus_payload is not None:
        _write_action_json(Path("bus_factor.json"), bus_payload)
    if decay_payload is not None:
        _write_action_json(Path("decay.json"), decay_payload)
    if balance_payload is not None:
        _write_action_json(Path("balance.json"), balance_payload)

    findings = _action_findings(
        repo_root,
        codeowners_path,
        ownership,
        config,
        result,
        owners_report,
    )
    _write_action_machines(findings, ownership)
    summary = _scrubbed(build(limit=max_output_entries))
    write_step_summary(summary)
    publish_outputs(limit=max_output_entries)
    _emit_action_annotations(findings)
    publish_check_run(
        conclusion="failure" if blocking else "success",
        summary=summary,
        annotations=_scrubbed_annotations(findings),
    )

    if json_output:
        printed: dict[str, object] = {"drift_summary": drift_payload}
        if bus_payload is not None:
            printed["bus_factor_summary"] = bus_payload
        if decay_payload is not None:
            printed["decay_summary"] = decay_payload
        if balance_payload is not None:
            printed["balance_summary"] = balance_payload
        _emit_json(printed, ownership)
    else:
        _render_ratchet_summary(outcome)
        console.print(
            f"[bold]drift:[/bold] {result.drift_detected} "
            f"([{_severity_style(severity)}]{severity}[/]) "
            f"· critical paths: {critical_paths} "
            f"· continuity-risk warnings: {decay_count}"
        )
        _render_blocking(blocking)
        _render_completeness(ownership)

    if blocking:
        exit_with(ExitCode.FINDINGS)
    _finish_analysis(ownership)


def _decay_report_payload(report: DecayReport) -> dict[str, object]:
    payload: dict[str, object] = {
        "handle": report.warning.handle,
        "path": report.warning.path,
        "days_since_last_commit": report.warning.days_since_last_commit,
        "last_commit": report.warning.last_commit.isoformat(),
        "historical_confidence": round(report.warning.historical_confidence, 4),
        "status": report.warning.status,
        "recommended_transfer": report.recommended_transfer,
    }
    if report.contributor_status == "former":
        payload["contributor_status"] = "former"
    return payload


def _build_or_load_graph(repo_root: Path, ownership: OwnershipMap, config: Config) -> nx.Graph:
    """Return the knowledge graph, reusing a fresh on-disk cache when available."""
    if not _NO_CACHE.get():
        cached = read_graph_cache(
            repo_root,
            ownership.last_analyzed,
            config=config,
            analysis_ref=ownership.analysis_ref,
        )
        if cached is not None:
            return from_serializable(cached)
    graph_obj = build_graph(ownership)
    if not _NO_CACHE.get():
        write_graph_cache(
            repo_root,
            ownership.last_analyzed,
            to_serializable(graph_obj),
            config=config,
            analysis_ref=ownership.analysis_ref,
        )
    return graph_obj


@app.command()
def graph(
    export: Annotated[
        str | None,
        typer.Option(
            "--export",
            help="Export the graph in the given format (currently 'dot').",
            case_sensitive=False,
        ),
    ] = None,
) -> None:
    """Render the ownership graph in the terminal."""
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _load_or_analyze(config, repo_root)
    try:
        graph_obj = _build_or_load_graph(repo_root, ownership, config)
    except GraphExtraMissingError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)
    if export is not None and export.strip().lower() != "dot":
        console.print(f"[red]Unsupported export format: {export!r}; supported: dot[/red]")
        exit_with(ExitCode.CONFIG)
    shown = for_display(graph_obj, config, _repo_id())
    if export is None:
        typer.echo(to_text(shown))
    else:
        typer.echo(to_dot(shown))
    _report_models("topology")
    _finish_analysis(ownership)


def _risk_impl(json_output: bool, baseline: str | None, output_format: str | None) -> None:
    fmt = _output_format(json_output, output_format)
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _load_for_command(config, repo_root)
    codeowners_path = find_codeowners_path(repo_root)
    rules = (
        ()
        if not codeowners_path.exists()
        else parse_rules(codeowners_path.read_text(encoding="utf-8"))
    )
    drift = _detect_drift(repo_root, ownership, config, codeowners_path)
    report = assess_risk(ownership, config, rules, drift)
    accepted = _accepted_findings(_resolve_baseline(baseline, config))
    findings = tuple(item.finding() for item in report.pathologies)
    visible, expired = visible_findings(
        findings,
        baseline=accepted,
        suppressions=config.suppressions,
        as_of=ownership.last_analyzed.date(),
    )
    report = select_pathologies(report, frozenset(item.identity() for item in visible))
    _print_expired_suppressions(expired)
    if fmt == "json":
        _emit_json(risk_payload(report), ownership)
    elif _is_machine(fmt):
        bus = compute_qualified_owners(ownership, config, target=None)
        machine = combine_findings(
            findings_for_bus(bus),
            _gap_findings(ownership, repo_root, codeowners_path, config),
        )
        _emit_machine(fmt, machine, command="risk", ownership=ownership)
    else:
        _render_risk(report)
        _report_models("ownership", "risk")
    _finish_analysis(ownership)


def risk(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
    baseline: BaselineOption = None,
) -> None:
    """Report composite knowledge risk for the repository."""
    _risk_impl(json_output, baseline, output_format)


def bus_factor(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
    baseline: BaselineOption = None,
) -> None:
    """Alias of risk."""
    _risk_impl(json_output, baseline, output_format)


def decay(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
    baseline: BaselineOption = None,
) -> None:
    """Alias of risk."""
    _risk_impl(json_output, baseline, output_format)


app.command(name="risk")(risk)
app.command(name="bus-factor")(bus_factor)
app.command(name="decay")(decay)


def _accepted_findings(baseline: Path | None) -> tuple[Finding, ...]:
    if baseline is None:
        return ()
    try:
        return load_baseline(baseline)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)


def _render_risk(report: RiskReport) -> None:
    console.print("Repository knowledge risk")
    console.print("─" * 60)
    if not report.entries:
        console.print("[green]No analyzed paths.[/green]")
    for entry in report.entries:
        _render_risk_entry(entry)
    if report.pathologies:
        console.print("")
        for item in report.pathologies:
            _render_pathology(item)
    console.print("")
    console.print("Repository")
    console.print("─" * 60)
    summary = report.summary
    _leader("Observed ownership coverage", f"{summary.observed_ownership_coverage:.1%}")
    _leader("Knowledge-at-risk", f"{summary.knowledge_at_risk:.1%}")
    _leader("Former contributor knowledge", f"{summary.former_knowledge_share:.1%}")
    _leader("Critical components", str(summary.critical_components))
    _leader("Ownership vacuums", str(summary.ownership_vacuums))
    _leader("High CODEOWNERS drift", str(summary.high_codeowners_drift))
    if report.criticality_unavailable:
        console.print("[dim]repository risk is incomplete without criticality[/dim]")


def _render_risk_entry(entry: RiskEntry) -> None:
    tier = {
        "critical": "[red]CRITICAL[/red]",
        "high": "[yellow]HIGH[/yellow]",
        "medium": "[yellow]MEDIUM[/yellow]",
        "low": "[green]LOW[/green]",
    }[entry.tier]
    console.print(f"{tier}  {escape(entry.path)}")
    owners = f"TF50: {entry.truck_factor_50} · effective owners {entry.effective_owners:.2f}"
    console.print(f"          {owners}")
    detail = _risk_detail(entry)
    if detail:
        console.print(f"          {detail}")
    if entry.reason:
        console.print(f"          {escape(entry.reason)}")
    if entry.former_knowledge_share > 0:
        percent = round(entry.former_knowledge_share * 100)
        console.print(f"          {percent}% of knowledge attributed to former contributors")


def _risk_detail(entry: RiskEntry) -> str:
    parts: list[str] = []
    if entry.dominant_expert is not None and entry.dominant_share is not None:
        share = f"{entry.dominant_share:.0%}"
        parts.append(f"dominant expert: {_person(entry.dominant_expert)} ({share})")
    label = entry.change_frequency_label
    if label is not None:
        parts.append(f"change frequency: {label}")
    return " · ".join(parts)


_PATHOLOGY_LABELS = {
    "knowledge-vacuum": "Knowledge vacuum",
    "phantom-ownership": "Phantom ownership",
    "shadow-maintainer": "Shadow maintainer",
    "ownership-review-divergence": "Ownership/review divergence",
}


def _render_pathology(item: Pathology) -> None:
    console.print(f"{_PATHOLOGY_LABELS[item.kind]}  {escape(item.path)}")
    console.print(f"          {escape(_pathology_detail(item))}")


def _pathology_detail(item: Pathology) -> str:
    if item.kind == "shadow-maintainer" and item.owners:
        declared = ", ".join(_person(name) for name in item.declared_owners)
        return f"declared {declared}, dominant observed expert {_person(item.owners[0])}"
    if (
        item.kind == "ownership-review-divergence"
        and item.writer
        and item.reviewer
        and item.declared
    ):
        writer = _person(item.writer)
        reviewer = _person(item.reviewer)
        declared = _person(item.declared)
        return f"writer {writer}, reviewer {reviewer}, declared {declared}"
    return item.detail


def _qualified_owners_impl(
    path: str | None,
    all_paths: bool,
    json_output: bool,
) -> None:
    if path is None and not all_paths:
        console.print("[yellow]Specify a path or pass --all to report every path.[/yellow]")
        exit_with(ExitCode.CONFIG)
    config = _load_config()
    ownership = _load_for_command(config, Path.cwd())
    target = path if path else None
    report = compute_qualified_owners(ownership, config, target=target)
    if json_output:
        data = _qualified_owners_payload(report, config)
        _emit_json(data, ownership)
        _finish_analysis(ownership)
        return
    if not report.entries:
        console.print("[yellow]No paths matched.[/yellow]")
        _report_models("risk")
        _finish_analysis(ownership)
        return
    cap = report.qualified_owner_count_cap
    table = Table(title="Qualified owners")
    table.add_column("Path", style="cyan")
    table.add_column("Qualified owners", justify="right")
    table.add_column("Tier")
    table.add_column("Owners")
    table.add_column("Candidate backup reviewers")
    for entry in report.entries:
        tier = classify(entry.qualified_owner_count, config.bus_factor)
        if _aggregate():
            owners = "-"
            backups = "-"
        else:
            owners = ", ".join(_person(name) for name in entry.contributors_above_threshold) or "-"
            backups = ", ".join(_person(name) for name in entry.recommended_backups) or "-"
        tier_str = {
            "critical": "[red]CRITICAL[/red]",
            "warning": "[yellow]WARN[/yellow]",
            "ok": "[green]OK[/green]",
        }[tier]
        count = format_qualified_owner_count(entry.qualified_owner_count, cap)
        table.add_row(escape(entry.path), count, tier_str, escape(owners), escape(backups))
    console.print(table)
    _print_owner_distribution(report)
    _report_models("risk")
    _finish_analysis(ownership)


def qualified_owners(
    path: Annotated[
        str | None,
        typer.Argument(help="Path (or glob) to limit the report to."),
    ] = None,
    all_paths: Annotated[
        bool,
        typer.Option("--all", help="Report every path in the repo."),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Count owners above the confidence threshold, capped by top_n_owners."""
    _qualified_owners_impl(path, all_paths, json_output)


app.command(name="qualified-owners")(qualified_owners)


def _load_cached(config: Config, repo_root: Path) -> OwnershipMap:
    """Return this repo's cached ownership, or exit 2 when it cannot be reused."""
    if _NO_CACHE.get():
        console.print("[red]Cached analysis is required; run `checkowners analyze`.[/red]")
        exit_with(ExitCode.CONFIG)
    cached = reusable_ownership(
        repo_root,
        config,
        head=_try_head_sha(repo_root),
        allow_stale=_ALLOW_STALE.get(),
        max_age=_MAX_AGE.get(),
    )
    if cached is None:
        console.print("[red]Cached analysis is stale or missing; run `checkowners analyze`.[/red]")
        exit_with(ExitCode.CONFIG)
    return cached


def _roster(path: Path) -> tuple[str, ...]:
    """Return identities from `path`, skipping blanks and comments."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]Cannot read roster: {exc}[/red]")
        exit_with(ExitCode.CONFIG)
    identities: list[str] = []
    for line in text.splitlines():
        item = line.split("#", 1)[0].strip()
        if item:
            identities.append(item)
    return tuple(identities)


def _leader(label: str, value: str) -> None:
    width = 42
    pad = max(2, width - len(label))
    typer.echo(f"{label} {'.' * pad} {value}")


def _render_simulation(report: SimulationReport) -> None:
    removed = len(report.removed)
    typer.echo(f"Removing {removed} of {report.contributor_count} contributors:")
    typer.echo("")
    percent = report.files_losing_only_owner_ratio * 100
    only = f"{report.files_losing_only_owner:,}  ({percent:.1f}% of repo)"
    _leader("Files losing their ONLY owner", only)
    _leader("Files dropping to bus factor 1", f"{report.files_dropping_to_one:,}")
    directory_count = len(report.orphaned_directories)
    _leader("Directories fully orphaned", f"{directory_count:,}")
    for directory in report.orphaned_directories:
        typer.echo(f"   {directory.path:<22} {directory.files:>4} files   no remaining owner")
    change = f"{report.repo_truck_factor_before} -> {report.repo_truck_factor_after}"
    _leader("Repo truck factor", change)
    typer.echo("")
    typer.echo("Suggested transfers (candidate backup reviewers, by residual confidence):")
    if not report.transfers:
        typer.echo("   none")
        return
    for transfer in report.transfers:
        parts = [
            f"{_person(candidate.identity)} ({candidate.confidence:.2f})"
            for candidate in transfer.candidates
        ]
        if len(parts) < 2:
            parts.append("no strong second")
        typer.echo(f"   {transfer.path:<22} -> {'  '.join(parts)}")


@app.command()
def simulate(
    remove: Annotated[
        list[str] | None,
        typer.Option("--remove", help="Identity to exclude. Repeat for each person."),
    ] = None,
    remove_file: Annotated[
        Path | None,
        typer.Option("--remove-file", help="Roster of identities, one per line."),
    ] = None,
    remove_status: Annotated[
        list[str] | None,
        typer.Option(
            "--remove-status",
            help="Also remove cached owners with this contributor status. Repeatable.",
        ),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Report what changes if the named contributors leave. Uses cached analysis only."""
    identities = tuple(item.strip() for item in (remove or ()) if item.strip())
    if remove_file is not None:
        identities = (*identities, *_roster(remove_file))
    statuses = _remove_statuses(remove_status)
    if not identities and not statuses:
        console.print(
            "[red]Pass --remove, --remove-file, or --remove-status "
            "with at least one identity.[/red]"
        )
        exit_with(ExitCode.CONFIG)
    config = _load_config()
    ownership = _load_cached(config, Path.cwd())
    identities = (*identities, *_owners_with_status(ownership, statuses))
    if not identities:
        console.print(
            "[red]Pass --remove, --remove-file, or --remove-status "
            "with at least one identity.[/red]"
        )
        exit_with(ExitCode.CONFIG)
    try:
        report = simulate_removal(ownership, config, identities)
    except UnknownIdentityError as exc:
        console.print(f"[red]Identity {exc.identity} does not appear in the ownership map.[/red]")
        exit_with(ExitCode.CONFIG)
    if json_output:
        _emit_json(simulation_payload(report), ownership)
    else:
        _render_simulation(report)
        _report_models("risk")
    _finish_analysis(ownership)


def _remove_statuses(values: list[str] | None) -> tuple[ContributorStatus, ...]:
    statuses: list[ContributorStatus] = []
    for value in values or ():
        if not is_contributor_status(value):
            console.print(f"[red]Unknown contributor status: {value}[/red]")
            exit_with(ExitCode.CONFIG)
        statuses.append(value)
    return tuple(statuses)


def _owners_with_status(
    ownership: OwnershipMap,
    statuses: tuple[ContributorStatus, ...],
) -> tuple[str, ...]:
    if not statuses:
        return ()
    found: list[str] = []
    for path_ownership in ownership.paths.values():
        for owner in path_ownership.owners:
            if owner.contributor_status in statuses and owner.handle not in found:
                found.append(owner.handle)
    return tuple(found)


@identity_app.command("audit")
def identity_audit(json_output: JsonOption = False) -> None:
    """List probable duplicate identities in this repository."""
    config = _load_config()
    repo_root = Path.cwd()
    graph = load_identity_graph(repo_root)
    records = audit_records(
        _audit_identities(repo_root, config, graph),
        _author_names(repo_root),
        config.identity_overrides,
    )
    groups = probable_duplicates(records, (*config.identity_do_not_merge, *graph.separations))
    payload = audit_payload(groups, aggregate_only=config.output.aggregate_only)
    if json_output:
        _emit_json(payload)
        return
    if config.output.aggregate_only:
        console.print(f"Probable duplicates: {payload['duplicate_count']}")
        return
    if not groups:
        console.print("No probable duplicate identities.")
        return
    for group in groups:
        left = _person(group.identities[0])
        right = _person(group.identities[1])
        console.print(f"{left} and {right}: {group.reason}")


@identity_app.command("merge")
def identity_merge(left: str, right: str, into: str) -> None:
    """Combine two identities into one and record the merge."""
    repo_root = Path.cwd()
    graph = apply_merge(load_identity_graph(repo_root), left, right, into)
    save_identity_graph(repo_root, graph)
    console.print(f"Merged {_person(left)} and {_person(right)} into {_person(into)}.")


@identity_app.command("unmerge")
def identity_unmerge(left: str, right: str) -> None:
    """Restore the records from a recorded merge and keep the pair separate."""
    repo_root = Path.cwd()
    current = load_identity_graph(repo_root)
    updated = revert_merge(current, left, right)
    if updated is current:
        console.print("[red]No recorded merge of those identities.[/red]")
        exit_with(ExitCode.CONFIG)
    save_identity_graph(repo_root, updated)
    console.print(f"Restored {_person(left)} and {_person(right)}.")


def _audit_identities(repo_root: Path, config: Config, graph: IdentityGraph) -> tuple[str, ...]:
    found: set[str] = set(_author_names(repo_root))
    cached = load_ownership(repo_root)
    if cached is not None:
        for path_ownership in cached.paths.values():
            for owner in (
                *path_ownership.owners,
                *path_ownership.candidates,
                *path_ownership.scored_owners,
            ):
                found.add(owner.handle)
    for handle, _override in config.identity_overrides:
        found.add(handle if handle.startswith("@") else f"@{handle}")
    for record in graph.contributors:
        found.add(record.handle)
    return tuple(found)


def _author_names(repo_root: Path) -> dict[str, tuple[str, ...]]:
    try:
        result = run_git(repo_root, ["git", "log", "--format=%aE%x09%aN"], check=False)
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    grouped: dict[str, set[str]] = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        email, name = line.split("\t", 1)
        if email.strip() and name.strip():
            grouped.setdefault(email.strip(), set()).add(name.strip())
    return {email: tuple(sorted(names)) for email, names in grouped.items()}


def _print_owner_distribution(report: BusFactorReport) -> None:
    distribution = report.distribution
    console.print(
        "[dim]qualified-owner distribution: "
        f"minimum {distribution.minimum:.2f}, "
        f"p10 {distribution.p10:.2f}, "
        f"median {distribution.median:.2f}, "
        f"p90 {distribution.p90:.2f}, "
        f"critical-path risk {distribution.critical_path_risk:.2f}, "
        f"knowledge at risk {distribution.knowledge_at_risk:.0%}, "
        f"former contributor knowledge {distribution.former_knowledge_share:.0%}[/dim]"
    )
    if distribution.criticality_incomplete:
        console.print("[dim]repository risk is incomplete without criticality[/dim]")


def _qualified_owners_payload(report: BusFactorReport, config: Config) -> dict[str, Any]:
    cap = report.qualified_owner_count_cap
    return {
        "distribution": distribution_json(report.distribution),
        "qualified_owner_count_cap": cap,
        "entries": [
            {
                "path": entry.path,
                **qualified_owner_count_fields(entry.qualified_owner_count, cap),
                "tier": classify(entry.qualified_owner_count, config.bus_factor),
                "contributors_above_threshold": list(entry.contributors_above_threshold),
                "recommended_backups": list(entry.recommended_backups),
            }
            for entry in report.entries
        ],
        "critical_paths": list(report.critical_paths),
    }


def _topology_payload(report: TopologyReport) -> dict[str, Any]:
    return {
        "clusters": [
            {
                "name": cluster.name,
                "members": list(cluster.members),
                "primary_paths": list(cluster.primary_paths),
                "declared": cluster.declared,
            }
            for cluster in report.clusters
        ],
        "mismatches": list(report.mismatches),
    }


def _balance_payload(report: BalanceReport) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": report.source,
        "load_signal": report.load_signal,
        "fallback_reason": report.fallback_reason,
        "average": report.average,
        "loads": [_load_payload(load) for load in report.loads],
        "overloaded": [_load_payload(load) for load in report.overloaded],
        "suggestions": [
            {
                "overloaded": suggestion.overloaded,
                "candidate": suggestion.candidate,
                "confidence": round(suggestion.confidence, 4),
                "proposed_shift": suggestion.proposed_shift,
            }
            for suggestion in report.suggestions
        ],
    }
    if report.review_concentration:
        payload["review_concentration"] = [
            {
                "path": item.path,
                "share": item.share,
                "reviewer": item.reviewer,
                "knowledgeable_owners": item.knowledgeable_owners,
            }
            for item in report.review_concentration
        ]
    if report.divergence:
        payload["divergence"] = [
            {
                "path": item.path,
                "writer": item.writer,
                "reviewer": item.reviewer,
                "declared": item.declared,
            }
            for item in report.divergence
        ]
    return payload


def _load_payload(load: ReviewLoad) -> dict[str, object]:
    item: dict[str, object] = {"handle": load.handle, "reviews": load.reviews}
    optional: tuple[tuple[str, object | None], ...] = (
        ("reviews_requested", load.reviews_requested),
        ("reviews_completed", load.reviews_completed),
        ("prs_waiting", load.prs_waiting),
        ("median_response_time_hours", load.median_response_time_hours),
        ("reviewed_lines", load.reviewed_lines),
        ("pr_complexity", load.pr_complexity),
        ("ownership_criticality", load.ownership_criticality),
    )
    for key, value in optional:
        if value is not None:
            item[key] = value
    if load.load_band is not None:
        item["expertise"] = load.expertise
        item["load_band"] = load.load_band
    return item


def _suggest_impl(json_output: bool) -> None:
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _load_for_command(config, repo_root)
    report = analyze_balance(
        ownership,
        config,
        rules=_codeowners_rules(find_codeowners_path(repo_root)),
    )
    if json_output:
        _emit_json(_balance_payload(report), ownership)
    else:
        _render_balance(report)
    _finish_analysis(ownership)


def suggest(
    json_output: JsonOption = False,
    diff: Annotated[
        str | None,
        typer.Option(
            "--diff",
            help="Recommend reviewers for SPEC (base...head or a revision).",
        ),
    ] = None,
    pull_request: Annotated[
        int | None,
        typer.Option("--pr", min=1, help="Recommend reviewers for pull request N."),
    ] = None,
) -> None:
    """Recommend a reviewer from expertise and current load."""
    if diff is not None and pull_request is not None:
        raise typer.BadParameter("Pass either --diff or --pr.")
    if (diff is not None or pull_request is not None) and _DIFF_ONLY.get() is not None:
        raise typer.BadParameter("Pass either --diff, --pr, or --diff-only.")
    if diff is not None or pull_request is not None:
        _emit_change(diff, pull_request, json_output=json_output, long=True)
        return
    _suggest_impl(json_output)


def balance(json_output: JsonOption = False) -> None:
    """Alias of suggest."""
    _suggest_impl(json_output)


app.command(name="suggest")(suggest)
app.command(name="balance")(balance)


@app.command("diff")
def diff_cmd(
    spec: Annotated[
        str,
        typer.Argument(help="base...head, or a revision compared with HEAD."),
    ],
    json_output: JsonOption = False,
) -> None:
    """Print ownership impact for the paths changed in SPEC."""
    _emit_change(spec, None, json_output=json_output, long=False)


def _emit_change(
    spec: str | None,
    number: int | None,
    *,
    json_output: bool,
    long: bool,
) -> None:
    config = _load_config()
    repo_root = Path.cwd()
    try:
        if number is not None:
            paths, base = _pull_request_change(repo_root, number)
        else:
            if spec is None:
                raise typer.BadParameter("SPEC is required")
            paths = changed_paths(repo_root, spec, exclude=config.paths.exclude)
            base = merge_base(repo_root, spec)
        report = build_change(
            repo_root,
            config,
            paths,
            base_revision=base,
            load=_change_loader(repo_root, config),
            team_resolve=_change_team_resolver(config, repo_root),
        )
    except PullRequestFilesError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.INTEGRATION)
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        _guard_ownership(exc)
    if json_output:
        _emit_json(change_payload(report))
        return
    text = render_suggest(report) if long else render_diff(report)
    typer.echo(scrub_text(text, config, _repo_id()), nl=False)


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
        ownership = load_paths(
            repo_root,
            config,
            paths,
            revision,
            as_of=as_of,
            review_provider=provider,
        )
        return _resolved_ownership(ownership, config, repo_root)

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


def _render_balance(report: BalanceReport) -> None:
    console.print(f"[dim]load_signal: {report.load_signal}[/dim]")
    if report.load_signal == "git_authorship_proxy":
        if report.fallback_reason:
            console.print(f"[dim]GitHub API unavailable ({escape(report.fallback_reason)}).[/dim]")
        console.print("Recorded reviews are unavailable. Commit activity is not review load.")
        console.print("No reviewer is recommended.")
        _report_notice()
        return
    if not _aggregate():
        _render_recommendations(report)
    _render_balance_facts(report)
    _report_notice()


def _render_recommendations(report: BalanceReport) -> None:
    loads = {load.handle: load for load in report.loads}
    if not report.suggestions:
        console.print("No reviewer recommendation.")
        return
    for index, suggestion in enumerate(report.suggestions):
        if index:
            console.print()
        _print_load_line(suggestion.overloaded, loads.get(suggestion.overloaded))
        _print_load_line(suggestion.candidate, loads.get(suggestion.candidate))
        console.print(f"Recommended: {escape(_person(suggestion.candidate))}")
        console.print(RECOMMENDATION_REASON)


def _print_load_line(handle: str, load: ReviewLoad | None) -> None:
    expertise = 0.0 if load is None else load.expertise
    band = "typical" if load is None or load.load_band is None else load.load_band
    console.print(f"{escape(_person(handle))}  expertise {expertise:.2f}  current load {band}")


def _render_balance_facts(report: BalanceReport) -> None:
    named = not _aggregate()
    for item in report.review_concentration:
        share = f"{item.share:.4f}".rstrip("0").rstrip(".")
        if named:
            console.print(
                "review_concentration: "
                f"{escape(item.path)} {share} {escape(_person(item.reviewer))} "
                f"{item.knowledgeable_owners}"
            )
        else:
            console.print(
                f"review_concentration: {escape(item.path)} {share} {item.knowledgeable_owners}"
            )
    for split in report.divergence:
        if named:
            console.print(
                f"{escape(split.path)}  writer {escape(_person(split.writer))}  "
                f"reviewer {escape(_person(split.reviewer))}  "
                f"declared {escape(_person(split.declared))}"
            )
        else:
            console.print(f"{escape(split.path)}  ownership/review divergence")


@app.command()
def topology(json_output: JsonOption = False) -> None:
    """Infer exploratory repository topology from co-commits. Clusters are not teams."""
    config = _load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    declared = declared_teams_from_github(config)
    report = infer_topology(ownership, config, declared_teams=declared)
    if json_output:
        _emit_json(_topology_payload(report), ownership)
        _finish_analysis(ownership)
        return
    if not report.clusters:
        console.print("[yellow]No clusters inferred.[/yellow]")
        _report_models("topology")
        _finish_analysis(ownership)
        return
    table = Table(title="Exploratory repository topology")
    table.add_column("Cluster", style="cyan")
    table.add_column("Members")
    table.add_column("Primary paths")
    table.add_column("Source")
    for cluster in report.clusters:
        source = "[green]declared[/green]" if cluster.declared else "[yellow]inferred[/yellow]"
        table.add_row(
            escape(cluster.name),
            escape(", ".join(_person(member) for member in cluster.members))
            if not _aggregate()
            else "-",
            escape(", ".join(cluster.primary_paths)) or "-",
            source,
        )
    console.print(table)
    if report.mismatches:
        console.print()
        console.print("[bold]Mismatches:[/bold]")
        for line in report.mismatches:
            console.print(f"  - {scrub_text(line, config, _repo_id())}")
    _report_models("topology")
    _finish_analysis(ownership)


def _onboarding_payload(report: OnboardingPath) -> dict[str, Any]:
    return {
        "target": report.target,
        "steps": [
            {
                "order": step.order,
                "path": step.path,
                "reviewer": step.reviewer,
                "complexity": step.complexity,
                "description": step.description,
            }
            for step in report.steps
        ],
    }


@app.command()
def onboard(
    path: Annotated[str, typer.Argument(help="Path or directory to onboard into.")],
    json_output: JsonOption = False,
    markdown: Annotated[
        bool,
        typer.Option("--markdown", help="Emit a Markdown checklist."),
    ] = False,
) -> None:
    """Generate a structured onboarding path for a codebase area."""
    config = _load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    report = generate_onboarding_path(ownership, config, target=path)
    if json_output:
        _emit_json(_onboarding_payload(report), ownership)
        _finish_analysis(ownership)
        return
    if markdown:
        typer.echo(scrub_text(report.to_markdown(), config, _repo_id()))
        _finish_analysis(ownership)
        return
    if not report.steps:
        console.print(f"[yellow]No onboarding path could be built for {path!r}.[/yellow]")
        _report_notice()
        _finish_analysis(ownership)
        return
    table = Table(title=f"Onboarding path: {path}")
    table.add_column("#", justify="right")
    table.add_column("Path", style="cyan")
    table.add_column("Reviewer")
    table.add_column("Complexity")
    table.add_column("Why")
    for step in report.steps:
        table.add_row(
            str(step.order),
            escape(step.path),
            escape(_person(step.reviewer)),
            step.complexity,
            escape(step.description),
        )
    console.print(table)
    _report_notice()
    _finish_analysis(ownership)


def _analyze_target(config: Config, repo_root: Path, target: str) -> OwnershipMap:
    spec = _DIFF_ONLY.get()
    if spec is not None:
        changed = set(changed_paths(repo_root, spec, exclude=config.paths.exclude))
        if target not in changed:
            as_of, analysis_ref = _resolve_clock(repo_root)
            return OwnershipMap(paths={}, last_analyzed=as_of, analysis_ref=analysis_ref)
    _warn_missing_api_token(config)
    as_of, analysis_ref = _resolve_clock(repo_root)
    try:
        ownership = analyze_ownership(
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
    ownership = _resolve_github_owners(ownership, config, repo_root)
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


def _signal_label(name: str, score: float, available: bool) -> str:
    title = "Reviews" if name == "review" else name.capitalize()
    if not available:
        return f"{title} n/a"
    return f"{title} {score:.2f}"


def _render_explained_owner(item: ExplainedOwner, as_of: datetime) -> None:
    entry = item.entry
    style = _confidence_style(entry.ownership_score)
    shown = _person(entry.handle)
    console.print(f"[{style}]{escape(shown):<28}[/] {entry.ownership_score:.2f} ownership score")
    labels = "   ".join(_signal_label(s.name, s.score, s.available) for s in item.signals)
    console.print(f"  {labels}")
    blame = next((s for s in item.signals if s.name == "blame"), None)
    review = next((s for s in item.signals if s.name == "review"), None)
    recency = next((s for s in item.signals if s.name == "recency"), None)
    parts = [f"{entry.commits} commits"]
    if blame is not None:
        parts.append(blame.detail)
    if review is not None:
        parts.append(review.detail)
    console.print("  " + " · ".join(parts))
    last = _days_since(entry.last_commit, as_of)
    sha = recency.commits[0] if recency is not None and recency.commits else ""
    change = f"last meaningful change: {last}"
    if sha:
        change = f"{change} ({sha})"
    console.print(f"  {change}")


def _days_since(last: datetime | None, as_of: datetime) -> str:
    if last is None:
        return "unknown"
    days = max(0, int((as_of - last).total_seconds() // 86400))
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def _render_explanation(explanation: PathExplanation, as_of: datetime) -> None:
    if explanation.why_not is not None:
        _render_why_not(explanation.why_not)
        return
    console.print(f"[bold]{escape(explanation.target)}[/bold]")
    if explanation.kind == "directory":
        console.print(
            f"[dim]{len(explanation.files)} files; each owner's signals "
            "are from their highest-scoring file[/dim]"
        )
    console.print()
    console.print("Observed owners")
    console.print("─" * 50)
    if not explanation.inferred:
        console.print("[yellow]No inferred owners.[/yellow]")
    for item in explanation.inferred:
        _render_explained_owner(item, as_of)
        console.print()
    console.print(f"Evidence quality:             {explanation.evidence_quality:.2f}")
    if explanation.declared:
        declared = " ".join(_person(name) for name in explanation.declared)
    else:
        declared = "(none)"
    console.print(f"Declared CODEOWNERS:          {escape(declared)}")
    team = ", ".join(explanation.team_resolution) if explanation.team_resolution else "unavailable"
    console.print(f"Team resolution:              {escape(team)}")
    console.print(f"Assessment:                   {explanation.assessment}")
    if explanation.lineage:
        console.print(f"Prior names:                  {escape(', '.join(explanation.lineage))}")
    if explanation.knobs:
        console.print("Would change the result:")
        for knob in explanation.knobs:
            console.print(f"  {escape(knob)}")


def _render_why_not(result: WhyNotResult) -> None:
    console.print(f"{escape(_person(result.handle))} was not inferred because:")
    for reason in result.reasons:
        console.print(f"- {escape(reason)}")
    if result.knobs:
        console.print("Would change the result:")
        for knob in result.knobs:
            console.print(f"  {escape(knob)}")


def _render_owners_list(owners: tuple[OwnerEntry, ...]) -> None:
    if not owners:
        console.print("[yellow]No inferred owners.[/yellow]")
        return
    labels = tuple(_person(entry.handle) for entry in owners)
    width = max(len(name) for name in labels) if labels else 0
    for entry, name in zip(owners, labels, strict=True):
        style = _confidence_style(entry.ownership_score)
        console.print(f"[{style}]{escape(name):<{width}}[/]  {entry.ownership_score:.2f}")


@app.command()
def explain(
    path: Annotated[str, typer.Argument(help="File or directory to explain.")],
    json_output: JsonOption = False,
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Show only this inferred contributor."),
    ] = None,
    why_not: Annotated[
        str | None,
        typer.Option("--why-not", help="Explain why this contributor was excluded."),
    ] = None,
) -> None:
    """Decompose inferred ownership for a path."""
    if owner and why_not:
        console.print("[red]Use --owner or --why-not, not both.[/red]")
        exit_with(ExitCode.CONFIG)
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _analyze_target(config, repo_root, path)
    explanation = build_explanation(
        ownership,
        path,
        config,
        repo_root,
        as_of=ownership.last_analyzed,
        declared=_declared_owners(repo_root, path),
        team_members=declared_teams_from_github(config),
        owner=owner,
        why_not=why_not,
    )
    if owner and not explanation.inferred and explanation.why_not is None:
        console.print(
            f"[yellow]{escape(owner)} is not an inferred owner of {escape(path)}. "
            f"Use --why-not {escape(owner)} to see why.[/yellow]"
        )
        _report_models("ownership")
        _finish_analysis(ownership)
        return
    if json_output:
        _emit_json(explanation_payload(explanation), ownership)
        _finish_analysis(ownership)
        return
    _render_explanation(explanation, ownership.last_analyzed)
    _render_completeness(ownership)
    _report_models("ownership")
    _finish_analysis(ownership)


def _run_owners(path: str, json_output: bool) -> None:
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _analyze_target(config, repo_root, path)
    owners = ranked_owners(ownership, path, config)
    if json_output:
        _emit_json(
            owners_payload(owners, path, ownership, config.risk.truck_factor_thresholds),
            ownership,
        )
        _finish_analysis(ownership)
        return
    _render_owners_list(owners)
    _report_models("ownership")
    _finish_analysis(ownership)


@app.command("owners")
def owners_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to list owners for.")],
    json_output: JsonOption = False,
) -> None:
    """List inferred owners for a path."""
    _run_owners(path, json_output)


@app.command("who")
def who_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to list owners for.")],
    json_output: JsonOption = False,
) -> None:
    """List inferred owners for a path."""
    _run_owners(path, json_output)


@app.command()
def expertise(
    path: Annotated[str, typer.Argument(help="Path or glob to rank evidence for.")],
    json_output: JsonOption = False,
) -> None:
    """Show the evidence ranking for a path."""
    config = _load_config()
    ownership = _load_for_command(config, Path.cwd())
    ranking = rank_expertise(ownership, path)
    if json_output:
        data = {
            "path": path,
            "ranking": [_expertise_rank_payload(r) for r in ranking],
        }
        _emit_json(data, ownership)
        _finish_analysis(ownership)
        return
    if not ranking:
        console.print(f"[yellow]No experts found for {path!r}.[/yellow]")
        _report_models("ownership")
        _finish_analysis(ownership)
        return
    table = Table(title=f"Expertise: {path}")
    table.add_column("#", justify="right")
    table.add_column("Handle", style="cyan")
    table.add_column("Confidence", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("Last commit", justify="right")
    for idx, rank in enumerate(ranking, start=1):
        table.add_row(
            str(idx),
            f"[{_confidence_style(rank.confidence)}]{escape(_person(rank.handle))}[/]",
            f"{rank.confidence:.2f}",
            str(rank.commits),
            _format_last_commit(rank.last_commit),
        )
    console.print(table)
    _report_models("ownership")
    _finish_analysis(ownership)


def _render_trends(report: TrendReport, cap: int, *, compared: bool) -> None:
    console.print(HISTORICAL_LIMITATION, soft_wrap=True)
    if compared:
        console.print(compared_axis_line(), soft_wrap=True)
    if not report.points or all(point.commits == 0 for point in report.points):
        console.print("[yellow]No history available for the requested range.[/yellow]")
    else:
        table = Table(title=f"Historical activity ({report.periods}x{report.period_days}d)")
        table.add_column("Period end", style="cyan")
        table.add_column("Commits", justify="right")
        table.add_column("Contributors", justify="right")
        table.add_column("Tracked paths", justify="right")
        table.add_column("Hist. activity", justify="right")
        table.add_column("Avg qualified owners", justify="right")
        for point in report.points:
            score = point.historical_activity_confidence
            table.add_row(
                point.period_end.date().isoformat(),
                str(point.commits),
                str(point.active_contributors),
                str(point.tracked_paths),
                f"[{_confidence_style(score)}]{score:.2f}[/]",
                f"{point.avg_qualified_owner_count:.2f} (capped by top_n_owners={cap})",
            )
        console.print(table)
    err_console.print(f"[dim]{historical_model_line()}[/dim]")


@app.command()
def trends(
    periods: Annotated[
        int,
        typer.Option("--periods", min=1, max=36, help="Number of periods to report."),
    ] = 6,
    period_days: Annotated[
        int,
        typer.Option("--period-days", min=1, help="Length of each period in days."),
    ] = 30,
    compare_ownership: Annotated[
        bool,
        typer.Option(
            "--compare-ownership",
            help="Name ownership_score beside this report. Requires acknowledgement.",
        ),
    ] = False,
    acknowledge_different_metrics: Annotated[
        bool,
        typer.Option(
            "--acknowledge-different-metrics",
            help="Allow naming ownership_score next to historical_activity_confidence.",
        ),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Show historical activity confidence and qualified owner count over time."""
    if compare_ownership and not acknowledge_different_metrics:
        console.print(f"[red]{SHARED_AXIS_REFUSAL}[/red]")
        exit_with(ExitCode.CONFIG)
    config = _load_config()
    cap = config.analysis.top_n_owners
    repo_root = Path.cwd()
    as_of, analysis_ref = _resolve_clock(repo_root)
    try:
        report = analyze_trends(
            repo_root, config, periods=periods, period_days=period_days, as_of=as_of
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        console.print(f"[red]Git command failed:[/red] {exc}")
        exit_with(ExitCode.INTEGRATION)
    if json_output:
        generated = analysis_epoch(as_of)
        data = trend_report_payload(report, cap, compared=compare_ownership)
        _emit_json(data, head_sha=analysis_ref, generated_at=generated)
        return
    _render_trends(report, cap, compared=compare_ownership)


def _print_cache_info(info: CacheInfo) -> None:
    typer.echo(f"path: {info['path']}")
    typer.echo(f"schema_version: {info['schema_version']}")
    typer.echo(f"bytes: {info['bytes']}")
    typer.echo(f"state_files: {info['state_files']}")
    typer.echo(f"graph_files: {info['graph_files']}")
    typer.echo(f"handles: {'yes' if info['handles'] else 'no'}")
    typer.echo(f"limit_bytes: {info['limit_bytes']}")
    for entry in info["entries"]:
        typer.echo(
            f"{entry['repo_id']}\t{entry['analysis_ref']}\t"
            f"{entry['last_analyzed']}\t{entry['bytes']}"
        )


@cache_app.command("info")
def cache_info_command(json_output: JsonOption = False) -> None:
    """Show cache location, size, and stored analysis refs."""
    info = cache_info()
    if json_output:
        typer.echo(json.dumps(info, indent=2, sort_keys=True))
        return
    _print_cache_info(info)


@cache_app.command("clear")
def cache_clear_command() -> None:
    """Delete analysis state and graph files. Keep the handle cache."""
    removed = cache_clear()
    typer.echo(f"removed {removed} files")


@cache_app.command("path")
def cache_path_command() -> None:
    """Print the cache directory."""
    typer.echo(str(cache_directory()))


@cache_app.command("purge")
def cache_purge_command() -> None:
    """Delete the cache, including contributor emails in the handle file."""
    root = cache_directory()
    removed = cache_purge()
    typer.echo(f"purged {root} ({removed} files)")


def main() -> None:
    """Entry point for the checkowners CLI."""
    app()

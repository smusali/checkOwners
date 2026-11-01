"""The multi-step flows more than one command runs.

Each function here takes a loaded config and an ownership map and returns a
result the calling command renders: the audit, the policy enforcement, the
graph, and the risk, qualified-owner and review-load reports.
"""

from __future__ import annotations

import difflib
import os
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.markup import escape
from rich.table import Table

from checkowners.audit import (
    AuditFacts,
    AuditReport,
    audit_body,
    audit_report_findings,
    build_audit,
    environment_rows,
    fail_findings,
    format_audit,
    without_hidden,
)
from checkowners.baseline import (
    RatchetOutcome,
    apply_ratchet,
    load_baseline,
    visible_findings,
)
from checkowners.cli.analysis import (
    _analyze_target,
    _load_for_command,
    _load_teams,
    _try_analyze,
    detect_command_drift,
)
from checkowners.cli.common import (
    _accepted_findings,
    _aggregate,
    _branch_name,
    _codeowners_rules,
    _environment_facts,
    _finish_analysis,
    _gap_findings,
    _has_uncommitted_changes,
    _is_machine,
    _load_config,
    _output_format,
    _person,
    _policy_as_of,
    _read_codeowners,
    _report_notice,
    _repository_name,
    _scrubbed,
    _tracked_paths,
    console,
    exit_with,
    resolve_baseline,
    run_options,
)
from checkowners.cli.emit import (
    _balance_payload,
    _emit_json,
    _emit_machine,
    _emit_sync,
    _qualified_owners_payload,
)
from checkowners.cli.render import (
    _print_owner_distribution,
    _render_balance,
    _render_owners_list,
    _render_risk,
    print_expired_suppressions,
)
from checkowners.concentration import (
    BusFactorReport,
    classify,
    compute_qualified_owners,
    format_qualified_owner_count,
)
from checkowners.config import (
    Config,
    find_codeowners_path,
)
from checkowners.errors import ConfigError, ExitCode
from checkowners.explain import (
    owners_payload,
    ranked_owners,
)
from checkowners.generate import (
    GenerateResult,
)
from checkowners.git import (
    run_git,
)
from checkowners.github import (
    AccessState,
    ReviewState,
    code_owner_review_required,
    fetch_codeowners_errors,
    get_github_token,
    offline_enabled,
    owner_access,
    repository_visibility,
)
from checkowners.graph import (
    TransferResult,
    build_graph,
    endpoint_is_contributor,
    from_serializable,
    to_serializable,
    transfer_path,
)
from checkowners.models import (
    DriftResult,
    Finding,
    OwnershipMap,
)
from checkowners.patterns import (
    CodeownersRule,
    parse_rules,
)
from checkowners.policy import (
    blocking_findings,
    invalid_owner_findings,
    security_findings,
)
from checkowners.report_formats import (
    combine_findings,
    findings_for_bus,
)
from checkowners.risk import (
    assess_risk,
    risk_payload,
    select_pathologies,
)
from checkowners.state import (
    read_graph_cache,
    write_graph_cache,
)
from checkowners.suggest import analyze_balance
from checkowners.topology import (
    infer_topology,
)
from checkowners.validate import validate_codeowners

if TYPE_CHECKING:
    import networkx as nx


def _audit_report(
    config: Config,
    *,
    include_inference: bool,
    env: bool,
) -> tuple[AuditReport, OwnershipMap | None, tuple[CodeownersRule, ...]]:
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    text = codeowners_path.read_text(encoding="utf-8") if codeowners_path.is_file() else ""
    rules = _codeowners_rules(codeowners_path)
    tracked = _tracked_paths(repo_root)
    head = _branch_name(repo_root)
    token = "" if offline_enabled() else get_github_token()
    repository = _repository_name(repo_root)
    private = False
    default_branch = head
    if token and repository:
        visible = repository_visibility(token, repository)
        if visible is not None:
            private, default_branch = visible
    base = os.environ.get("GITHUB_BASE_REF", "").strip() or default_branch
    remote = None
    access: dict[str, AccessState] = {}
    review: ReviewState = "unknown"
    if token and repository:
        remote = fetch_codeowners_errors(token, repository, head)
        for owner in {owner for rule in rules for owner in rule.owners}:
            access[owner] = owner_access(token, repository, owner)
        review = code_owner_review_required(token, repository, base or head)
    ownership = _try_analyze(config, repo_root)
    analysis_error = "" if ownership is not None else "Analysis did not run."
    drift = None
    if include_inference and ownership is not None:
        try:
            drift = detect_command_drift(repo_root, ownership, config, codeowners_path)
        except typer.Exit:
            drift = None
            analysis_error = "Drift was not compared."
    file_bytes = codeowners_path.stat().st_size if codeowners_path.is_file() else 0
    facts = AuditFacts(
        repo_root=repo_root,
        config=config,
        codeowners_path=codeowners_path,
        text=text,
        rules=rules,
        tracked=tracked,
        head_branch=head,
        base_branch=base,
        file_bytes=file_bytes,
        teams=_load_teams(config),
        ownership=ownership,
        analysis_error=analysis_error,
        errors=tuple(validate_codeowners(repo_root, codeowners_path=codeowners_path)),
        remote_errors=remote,
        private=private,
        ref=head,
        default_branch=default_branch,
        access=access,
        review=review,
        token_present=bool(token),
        include_inference=include_inference,
        drift=drift,
    )
    report = build_audit(facts)
    if env:
        report = replace(report, env=environment_rows(_environment_facts(facts, token)))
    return report, ownership, rules


def _apply_audit_baseline(report: AuditReport, config: Config, baseline: Path) -> AuditReport:
    structural = tuple(finding for row in report.rows for finding in row.findings)
    outcome = _ratchet_or_exit(
        DriftResult(stale=(), missing=(), changed=(), drift_detected=False),
        config,
        as_of=_policy_as_of(Path.cwd()),
        baseline=baseline,
        structural=structural,
    )
    visible = {item.identity() for item in outcome.new}
    return without_hidden(report, visible)


def _finish_audit(
    *,
    include_inference: bool,
    json_output: bool,
    output_format: str | None,
    baseline: str | None,
    env: bool,
) -> None:
    fmt = _output_format(json_output, output_format)
    config = _load_config()
    report, ownership, rules = _audit_report(config, include_inference=include_inference, env=env)
    if include_inference:
        path = resolve_baseline(baseline, config)
        if path is not None:
            report = _apply_audit_baseline(report, config, path)
    blocking = blocking_findings(fail_findings(report), config, ownership, rules)
    if fmt == "json":
        _emit_json(audit_body(report), ownership)
    elif _is_machine(fmt):
        _emit_machine(fmt, audit_report_findings(report), command="audit", ownership=ownership)
    else:
        typer.echo(_scrubbed(format_audit(report)), nl=False)
    if blocking:
        exit_with(ExitCode.FINDINGS)


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
        raise ConfigError(str(exc)) from exc
    outcome = apply_ratchet(
        result,
        baseline=accepted,
        suppressions=config.suppressions,
        as_of=as_of.date(),
        bus=bus,
        structural=structural,
    )
    if outcome.expired:
        print_expired_suppressions(outcome.expired)
        exit_with(ExitCode.FINDINGS)
    return outcome


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


def _build_or_load_graph(repo_root: Path, ownership: OwnershipMap, config: Config) -> nx.Graph:
    """Return the ownership graph, reusing a fresh on-disk cache when available."""
    if not run_options().no_cache:
        cached = read_graph_cache(
            repo_root,
            ownership.last_analyzed,
            config=config,
            analysis_ref=ownership.analysis_ref,
        )
        if cached is not None:
            return from_serializable(cached)
    rules = _codeowners_rules(find_codeowners_path(repo_root))
    clusters = infer_topology(ownership, config).clusters
    graph_obj = build_graph(
        ownership,
        clusters=clusters,
        rules=rules,
        repo_root=repo_root,
        repository=_repository_name(repo_root),
    )
    if not run_options().no_cache:
        write_graph_cache(
            repo_root,
            ownership.last_analyzed,
            to_serializable(graph_obj),
            config=config,
            analysis_ref=ownership.analysis_ref,
        )
    return graph_obj


def _graph_transfer(
    graph_obj: nx.Graph,
    config: Config,
    source: str | None,
    target: str | None,
) -> TransferResult | None:
    if source is None and target is None:
        return None
    if source is None or target is None:
        console.print("[red]--from and --to must both be set[/red]")
        exit_with(ExitCode.CONFIG)
    if config.output.aggregate_only and (
        endpoint_is_contributor(graph_obj, source) or endpoint_is_contributor(graph_obj, target)
    ):
        console.print("[red]person endpoints are omitted when aggregate_only is set[/red]")
        exit_with(ExitCode.CONFIG)
    result = transfer_path(graph_obj, source, target)
    if result.missing:
        console.print(f"[red]unknown graph endpoint: {', '.join(result.missing)}[/red]")
        exit_with(ExitCode.CONFIG)
    return result


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
    drift = detect_command_drift(repo_root, ownership, config, codeowners_path)
    report = assess_risk(ownership, config, rules, drift)
    accepted = _accepted_findings(resolve_baseline(baseline, config))
    findings = tuple(item.finding() for item in report.pathologies)
    visible, expired = visible_findings(
        findings,
        baseline=accepted,
        suppressions=config.suppressions,
        as_of=ownership.last_analyzed.date(),
    )
    report = select_pathologies(report, frozenset(item.identity() for item in visible))
    print_expired_suppressions(expired)
    if fmt == "json":
        _emit_json(risk_payload(report), ownership)
    elif _is_machine(fmt):
        bus = compute_qualified_owners(ownership, config, target=None)
        machine = combine_findings(
            findings_for_bus(bus, config.policy.single_expert.severity),
            _gap_findings(ownership, repo_root, codeowners_path, config),
        )
        _emit_machine(fmt, machine, command="risk", ownership=ownership)
    else:
        _render_risk(report)
        _report_notice()
    _finish_analysis(ownership)


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
        _report_notice()
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
    _report_notice()
    _finish_analysis(ownership)


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
    _report_notice()
    _finish_analysis(ownership)

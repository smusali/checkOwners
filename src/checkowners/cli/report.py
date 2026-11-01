"""Commands that report on a run: audit, doctor, the Action, cache, identity."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from checkowners.action_report import (
    build,
    publish_outputs,
    write_step_summary,
)
from checkowners.cli.analysis import (
    _apply_extra_gaps,
    _load_or_analyze,
    detect_command_drift,
)
from checkowners.cli.common import (
    BaselineOption,
    FormatOption,
    JsonOption,
    _action_findings,
    _audit_identities,
    _author_names,
    _codeowners_rules,
    _current_git_repo,
    _drift_override,
    _finish_analysis,
    _load_config,
    _person,
    _positive_entry_limit,
    _publish_action_failure,
    _report_notice,
    _scrubbed,
    _scrubbed_annotations,
    _severity_style,
    _severity_with_hysteresis,
    _stamp_output,
    console,
    err_console,
    exit_with,
    resolve_baseline,
)
from checkowners.cli.emit import (
    _balance_payload,
    _decay_report_payload,
    _emit_action_annotations,
    _emit_json,
    _policy_payload,
    _qualified_owners_payload,
    _ratchet_json,
    _write_action_json,
    _write_action_machines,
    filter_bus_payload,
)
from checkowners.cli.flows import (
    _enforcement,
    _finish_audit,
)
from checkowners.cli.render import (
    _print_cache_info,
    _render_blocking,
    _render_completeness,
    _render_ratchet_summary,
)
from checkowners.concentration import (
    BusFactorReport,
    compute_qualified_owners,
)
from checkowners.config import (
    find_codeowners_path,
)
from checkowners.drift import (
    detect_drift,
    drift_entry_payload,
    evidence_gaps,
    team_comparison_payload,
)
from checkowners.errors import ExitCode
from checkowners.git import (
    GitRequirementError,
)
from checkowners.github import (
    publish_check_run,
)
from checkowners.graph import GraphExtraMissingError
from checkowners.html_report import write_report
from checkowners.identity import (
    apply_merge,
    audit_payload,
    audit_records,
    load_identity_graph,
    probable_duplicates,
    revert_merge,
    save_identity_graph,
)
from checkowners.patterns import parse_rules
from checkowners.risk import (
    assess_risk,
    detect_decay,
)
from checkowners.state import (
    cache_clear,
    cache_info,
    cache_purge,
)
from checkowners.storage import (
    cache_directory,
)
from checkowners.suggest import analyze_balance
from checkowners.topology import infer_topology
from checkowners.trends import analyze_trends

if TYPE_CHECKING:
    pass

commands = typer.Typer()
cache = typer.Typer(help="Inspect and delete the on-disk cache.")
identity = typer.Typer(help="Inspect and edit contributor identity.")


@commands.command()
def doctor(
    json_output: JsonOption = False,
    env: Annotated[bool, typer.Option("--env", help="Include environment checks.")] = False,
) -> None:
    """Check CODEOWNERS configuration. Inference is limited to the bus-factor row."""
    _finish_audit(
        include_inference=False,
        json_output=json_output,
        output_format=None,
        baseline=None,
        env=env,
    )


@commands.command()
def audit(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
    baseline: BaselineOption = None,
) -> None:
    """Audit CODEOWNERS configuration, drift, and ownership risk."""
    _finish_audit(
        include_inference=True,
        json_output=json_output,
        output_format=output_format,
        baseline=baseline,
        env=False,
    )


@commands.command(name="github-action")
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
        result = detect_command_drift(repo_root, ownership, config, codeowners_path)
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
        baseline=resolve_baseline(baseline, config),
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
        "teams": [team_comparison_payload(row) for row in result.teams],
        "notes": list(result.notes),
        **ratchet,
        **_policy_payload(config, blocking),
    }
    bus_payload: dict[str, object] | None = None
    critical_paths = 0
    if owners_report is not None:
        bus_payload = filter_bus_payload(
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
    findings = tuple(
        item
        for item in findings
        if item.baseline_rule != "single-expert" or item.path not in outcome.hidden_bus_paths
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


@identity.command("audit")
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


@identity.command("merge")
def identity_merge(left: str, right: str, into: str) -> None:
    """Combine two identities into one and record the merge."""
    repo_root = Path.cwd()
    graph = apply_merge(load_identity_graph(repo_root), left, right, into)
    save_identity_graph(repo_root, graph)
    console.print(f"Merged {_person(left)} and {_person(right)} into {_person(into)}.")


@identity.command("unmerge")
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


@cache.command("info")
def cache_info_command(json_output: JsonOption = False) -> None:
    """Show cache location, size, stored refs, and this repository's state file."""
    info = cache_info(_current_git_repo())
    if json_output:
        typer.echo(json.dumps(info, indent=2, sort_keys=True))
    else:
        _print_cache_info(info)
    if not info["current_state_exists"]:
        exit_with(ExitCode.INTERNAL)


@cache.command("clear")
def cache_clear_command() -> None:
    """Delete analysis state and graph files. Keep the handle cache."""
    removed = cache_clear()
    typer.echo(f"removed {removed} files")


@cache.command("path")
def cache_path_command() -> None:
    """Print the cache directory."""
    typer.echo(str(cache_directory()))


@cache.command("purge")
def cache_purge_command() -> None:
    """Delete the cache, including contributor emails in the handle file."""
    root = cache_directory()
    removed = cache_purge()
    typer.echo(f"purged {root} ({removed} files)")


@commands.command()
def report(
    html: Annotated[
        Path,
        typer.Option("--html", help="Write a self-contained HTML report to this path."),
    ],
) -> None:
    """Write a self-contained offline HTML ownership report."""
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _load_or_analyze(config, repo_root)
    codeowners_path = find_codeowners_path(repo_root)
    text = codeowners_path.read_text(encoding="utf-8") if codeowners_path.is_file() else ""
    rules = parse_rules(text) if text else ()
    drift = detect_drift(repo_root, ownership, config, codeowners_path=codeowners_path)
    try:
        topology = infer_topology(ownership, config)
    except GraphExtraMissingError:
        topology = None
    try:
        destination = write_report(
            repo_root,
            html,
            config,
            ownership,
            drift=drift,
            risk=assess_risk(ownership, config, rules, drift),
            trends=analyze_trends(repo_root, config, as_of=ownership.last_analyzed),
            topology=topology,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        err_console.print(str(exc))
        exit_with(ExitCode.INTERNAL)
    _report_notice()
    typer.echo(str(destination))

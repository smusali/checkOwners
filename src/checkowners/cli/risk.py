"""Commands that score ownership risk and drift."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from checkowners.cli.analysis import (
    _apply_extra_gaps,
    _fresh_or_scoped,
    _load_cached,
    _resolve_clock,
    detect_command_drift,
)
from checkowners.cli.common import (
    BaselineOption,
    FormatOption,
    JsonOption,
    _analysis_stamp,
    _finish_analysis,
    _gap_findings,
    _is_machine,
    _load_config,
    _output_format,
    _owners_with_status,
    _remove_statuses,
    _repo_relative,
    _report_notice,
    _roster,
    _severity_style,
    _severity_with_hysteresis,
    console,
    exit_with,
    resolve_baseline,
    run_options,
)
from checkowners.cli.emit import (
    _emit_change,
    _emit_json,
    _emit_machine,
    _policy_payload,
    _ratchet_json,
)
from checkowners.cli.flows import (
    _enforcement,
    _qualified_owners_impl,
    _risk_impl,
    _suggest_impl,
)
from checkowners.cli.render import (
    _render_blocking,
    _render_completeness,
    _render_drift_table,
    _render_ratchet_summary,
    _render_simulation,
    _render_trends,
)
from checkowners.concentration import (
    compute_qualified_owners,
    simulate_removal,
    simulation_payload,
)
from checkowners.config import (
    find_codeowners_path,
)
from checkowners.drift import (
    drift_entry_payload,
    evidence_gaps,
    format_fix_plan,
    format_team_line,
    team_comparison_payload,
    write_github_output,
)
from checkowners.errors import ExitCode
from checkowners.git import (
    analysis_epoch,
)
from checkowners.payloads import (
    envelope_completeness,
)
from checkowners.report_formats import (
    combine_findings,
    findings_for_drift,
)
from checkowners.trends import (
    analyze_trends,
    trend_report_payload,
)

if TYPE_CHECKING:
    pass

commands = typer.Typer()


@commands.command()
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
    result = detect_command_drift(repo_root, ownership, config, codeowners_path)
    bus = compute_qualified_owners(ownership, config, target=None)
    outcome, blocking = _enforcement(
        repo_root,
        codeowners_path,
        ownership,
        config,
        result,
        baseline=resolve_baseline(baseline, config),
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
            "teams": [team_comparison_payload(row) for row in result.teams],
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
        for row in result.teams:
            console.print(f"[yellow]team:[/yellow] {escape(format_team_line(row))}")
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
        _report_notice()
        if fix_plan:
            console.print(plan, markup=False, highlight=False, soft_wrap=True)
    if blocking:
        exit_with(ExitCode.FINDINGS)
    _finish_analysis(ownership)


def risk(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
    baseline: BaselineOption = None,
) -> None:
    """Report composite knowledge risk for the repository."""
    _risk_impl(json_output, baseline, output_format)


commands.command(name="risk")(risk)


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


commands.command(name="qualified-owners")(qualified_owners)


@commands.command()
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
    report = simulate_removal(ownership, config, identities)
    if json_output:
        _emit_json(simulation_payload(report), ownership)
    else:
        _render_simulation(report)
        _report_notice()
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
    if (diff is not None or pull_request is not None) and run_options().diff_only is not None:
        raise typer.BadParameter("Pass either --diff, --pr, or --diff-only.")
    if diff is not None or pull_request is not None:
        _emit_change(diff, pull_request, json_output=json_output, long=True)
        return
    _suggest_impl(json_output)


commands.command(name="suggest")(suggest)


@commands.command()
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
            help="Name ownership_score beside this report.",
        ),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Show historical activity confidence and qualified owner count over time."""
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

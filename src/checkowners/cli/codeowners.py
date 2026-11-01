"""Commands that read and write the CODEOWNERS file."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from checkowners.baseline import (
    DEFAULT_BASELINE_PATH,
    finding_payload,
    write_baseline,
)
from checkowners.cli.analysis import (
    _apply_extra_gaps,
    _run_analyze,
    detect_command_drift,
)
from checkowners.cli.common import (
    AllowBroadOption,
    DialectOption,
    ForceOption,
    FormatOption,
    JsonOption,
    _aggregate,
    _codeowners_rules,
    _finish_analysis,
    _generate_or_exit,
    _is_machine,
    _load_config,
    _output_format,
    _person,
    _policy_as_of,
    _read_codeowners,
    _repo_relative,
    _report_notice,
    _tracked_paths,
    codeowners_ownership,
    console,
    exit_with,
    resolve_baseline,
)
from checkowners.cli.emit import (
    _emit_json,
    _emit_machine,
    _emit_sync,
    _path_payload,
    _policy_payload,
)
from checkowners.cli.flows import (
    _commit_synced,
    _enforcement,
    _finish_sync_preview,
    _ratchet_or_exit,
)
from checkowners.cli.render import (
    _render_blocking,
    _render_completeness,
)
from checkowners.concentration import (
    compute_qualified_owners,
)
from checkowners.config import (
    find_codeowners_path,
)
from checkowners.drift import (
    evidence_gaps,
    reconciliation_body,
)
from checkowners.errors import ExitCode
from checkowners.generate import (
    codeowners_write_metrics,
    ensure_overwrite_safe,
)
from checkowners.github import (
    get_github_token,
    github_repository,
    open_or_update_reconciliation,
)
from checkowners.models import (
    DriftResult,
)
from checkowners.patterns import (
    matching_rules,
    parse_rules,
)
from checkowners.policy import (
    blocking_findings,
    invalid_owner_findings,
)
from checkowners.providers import require_dialect
from checkowners.report_formats import (
    combine_findings,
    findings_for_shadows,
    findings_for_validation,
)
from checkowners.validate import validate_codeowners

if TYPE_CHECKING:
    pass

commands = typer.Typer()
baseline = typer.Typer(help="Manage the accepted-findings baseline.")


@commands.command()
def generate(
    json_output: JsonOption = False,
    force: ForceOption = False,
    allow_broad_patterns: AllowBroadOption = False,
    dialect: DialectOption = "github",
) -> None:
    """Generate a CODEOWNERS file from inferred ownership."""
    config = _load_config()
    if allow_broad_patterns:
        config = replace(config, output=replace(config.output, allow_broad_patterns=True))
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    ensure_overwrite_safe(codeowners_path, config.output.header, force=force)
    ownership = _run_analyze(config, repo_root)
    ownership = codeowners_ownership(ownership, config)
    chosen = require_dialect(dialect)
    result = _generate_or_exit(
        repo_root,
        ownership,
        config,
        codeowners_path,
        force=force,
        dialect=chosen,
    )
    rel_path = codeowners_path.relative_to(repo_root)
    if json_output:
        _emit_json(
            {
                "path": rel_path.as_posix(),
                "content": result.content,
                "broad_patterns": [record.as_json() for record in result.broad_patterns],
                **codeowners_write_metrics(result.content, dialect=chosen),
            },
            ownership,
        )
    else:
        console.print(f"[green]Generated {rel_path}[/green]")
    _finish_analysis(ownership)


@commands.command(name="print")
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
        _report_notice()
    _finish_analysis(ownership)


@commands.command()
def validate(
    json_output: JsonOption = False,
    output_format: FormatOption = None,
    dialect: DialectOption = "github",
) -> None:
    """Validate CODEOWNERS file syntax."""
    fmt = _output_format(json_output, output_format)
    config = _load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    chosen = require_dialect(dialect)
    errors = validate_codeowners(repo_root, codeowners_path=codeowners_path, dialect=chosen)
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
        baseline=resolve_baseline(None, config),
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


@commands.command()
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


@baseline.command("create")
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
    result = detect_command_drift(repo_root, ownership, config, codeowners_path)
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


@commands.command()
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
    ensure_overwrite_safe(codeowners_path, config.output.header, force=force)
    ownership = _run_analyze(config, repo_root)
    ownership = codeowners_ownership(ownership, config)
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
    drift_result = detect_command_drift(repo_root, ownership, config, codeowners_path)
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
    opened = open_or_update_reconciliation(
        token=get_github_token(),
        repository=github_repository(repo_root),
        path=rel_path.as_posix(),
        content=result.content,
        body=reconciliation_body(drift_result, ownership, config),
    )
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

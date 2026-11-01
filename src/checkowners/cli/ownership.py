"""Commands that report who owns what."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape
from rich.table import Table

from checkowners.cli.analysis import (
    _analyze_target,
    _declared_owners,
    _fresh_or_scoped,
    _load_for_command,
    _load_or_analyze,
)
from checkowners.cli.common import (
    JsonOption,
    _aggregate,
    _analyze_analysis,
    _confidence_style,
    _finish_analysis,
    _load_config,
    _person,
    _repo_id,
    _report_notice,
    console,
    exit_with,
)
from checkowners.cli.emit import (
    _emit_change,
    _emit_json,
    _emit_transfer,
    _expertise_rank_payload,
    _onboarding_payload,
    _path_payload,
    _topology_payload,
)
from checkowners.cli.flows import (
    _build_or_load_graph,
    _graph_transfer,
    _run_owners,
)
from checkowners.cli.render import (
    _format_last_commit,
    _render_completeness,
    _render_exclusions_line,
    _render_explanation,
    _render_git_history_line,
    _render_ignore_revs_line,
    _render_mailmap_line,
    _render_ownership_table,
    _render_topology,
)
from checkowners.errors import ExitCode
from checkowners.expertise import rank_expertise
from checkowners.explain import (
    build_explanation,
    explanation_payload,
)
from checkowners.github import (
    declared_teams_from_github,
)
from checkowners.graph import (
    EXPORT_FORMATS,
    GraphExtraMissingError,
    for_display,
    format_analytics,
    graph_analytics,
    label_nodes,
    to_dot,
    to_gexf,
    to_graphml,
    to_json,
    to_text,
)
from checkowners.onboard import generate_onboarding_path
from checkowners.privacy import (
    scrub_text,
)
from checkowners.topology import (
    infer_topology,
)

if TYPE_CHECKING:
    pass

commands = typer.Typer()


@commands.command()
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
        _report_notice()
    _finish_analysis(ownership)


@commands.command()
def graph(
    export: Annotated[
        str | None,
        typer.Option(
            "--export",
            help=(
                "Export format. The documented default is dot. "
                f"Supported: {', '.join(EXPORT_FORMATS)}."
            ),
            case_sensitive=False,
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--from", help="Start of an ownership transfer path."),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--to", help="End of an ownership transfer path."),
    ] = None,
) -> None:
    """Render the ownership graph in the terminal."""
    config = _load_config()
    repo_root = Path.cwd()
    ownership = _load_or_analyze(config, repo_root)
    graph_obj = _build_or_load_graph(repo_root, ownership, config)
    fmt = None if export is None else export.strip().lower()
    if fmt is not None and fmt not in EXPORT_FORMATS:
        supported = ", ".join(EXPORT_FORMATS)
        console.print(f"[red]Unsupported export format: {export!r}; supported: {supported}[/red]")
        exit_with(ExitCode.CONFIG)
    shown = for_display(graph_obj, config, _repo_id())
    transfer = _graph_transfer(graph_obj, config, source, target)
    if fmt is None:
        typer.echo(to_text(shown))
        typer.echo(format_analytics(graph_analytics(shown, ownership, config)))
        _emit_transfer(transfer, config, stream="out")
    elif fmt == "json":
        analytics = graph_analytics(shown, ownership, config)
        if transfer is not None and transfer.nodes:
            labeled = label_nodes(transfer.nodes, config, _repo_id())
            analytics = replace(analytics, transfer_path=labeled)
        typer.echo(to_json(shown, analytics))
        _emit_transfer(transfer, config, stream="err", empty_only=True)
    else:
        writers = {"dot": to_dot, "graphml": to_graphml, "gexf": to_gexf}
        typer.echo(writers[fmt](shown))
        _emit_transfer(transfer, config, stream="err")
    _report_notice()
    _finish_analysis(ownership)


@commands.command("diff")
def diff_cmd(
    spec: Annotated[
        str,
        typer.Argument(help="base...head, or a revision compared with HEAD."),
    ],
    json_output: JsonOption = False,
) -> None:
    """Print ownership impact for the paths changed in SPEC."""
    _emit_change(spec, None, json_output=json_output, long=False)


@commands.command()
def topology(
    json_output: JsonOption = False,
    resolution: Annotated[
        float,
        typer.Option(
            "--resolution",
            help="Louvain resolution. Values above 1 favor smaller clusters.",
        ),
    ] = 1.0,
) -> None:
    """Infer repository topology. Clusters are inferred collaboration, not teams."""
    if resolution <= 0:
        console.print("[red]resolution must be positive[/red]")
        exit_with(ExitCode.CONFIG)
    config = _load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    declared = declared_teams_from_github(config)
    try:
        report = infer_topology(
            ownership,
            config,
            declared_teams=declared,
            resolution=resolution,
        )
    except GraphExtraMissingError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with(ExitCode.CONFIG)
    if json_output:
        _emit_json(_topology_payload(report), ownership)
        _finish_analysis(ownership)
        return
    _render_topology(report, config)
    _report_notice()
    _finish_analysis(ownership)


@commands.command()
def onboard(
    path: Annotated[str, typer.Argument(help="Path or directory to onboard into.")],
    json_output: JsonOption = False,
    markdown: Annotated[
        bool,
        typer.Option("--markdown", help="Emit a Markdown checklist."),
    ] = False,
) -> None:
    """List inferred onboarding suggestions for a codebase area."""
    config = _load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    report = generate_onboarding_path(
        ownership,
        config,
        target=path,
        repo_root=Path.cwd(),
    )
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
    table = Table(title=f"Onboarding suggestions: {path}")
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
    if report.unavailable_signals:
        console.print("Unavailable signals: " + ", ".join(report.unavailable_signals))
    if report.cycles:
        console.print("Cycle: " + ", ".join(report.cycles) + ". Those steps use path order.")
    if report.mentor_candidates and not _aggregate():
        names = ", ".join(_person(handle) for handle in report.mentor_candidates)
        console.print(f"Mentor candidates: {escape(names)}")
    _report_notice()
    _finish_analysis(ownership)


@commands.command()
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
        _report_notice()
        _finish_analysis(ownership)
        return
    if json_output:
        _emit_json(explanation_payload(explanation), ownership)
        _finish_analysis(ownership)
        return
    _render_explanation(explanation, ownership.last_analyzed)
    _render_completeness(ownership)
    _report_notice()
    _finish_analysis(ownership)


@commands.command("owners")
def owners_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to list owners for.")],
    json_output: JsonOption = False,
) -> None:
    """List inferred owners for a path."""
    _run_owners(path, json_output)


@commands.command()
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
        _report_notice()
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
    _report_notice()
    _finish_analysis(ownership)

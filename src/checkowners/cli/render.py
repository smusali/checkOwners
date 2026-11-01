"""Turning results into terminal output: the Rich tables and text lines."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import typer
from rich.markup import escape
from rich.table import Table

from checkowners.baseline import (
    RatchetOutcome,
)
from checkowners.cli.common import (
    _aggregate,
    _confidence_style,
    _days_since,
    _leader,
    _pathology_detail,
    _person,
    _repo_id,
    _report_notice,
    _risk_detail,
    _signal_label,
    console,
)
from checkowners.concentration import (
    BusFactorReport,
    SimulationReport,
    format_qualified_owner_count,
)
from checkowners.config import (
    Config,
    Suppression,
)
from checkowners.explain import (
    ExplainedOwner,
    PathExplanation,
    WhyNotResult,
)
from checkowners.models import (
    AnalysisCompleteness,
    DriftResult,
    Finding,
    OwnerEntry,
    OwnershipMap,
    completeness_label,
    run_completeness,
)
from checkowners.privacy import (
    scrub_text,
)
from checkowners.risk import (
    Pathology,
    RiskEntry,
    RiskReport,
)
from checkowners.state import (
    CacheInfo,
)
from checkowners.suggest import RECOMMENDATION_REASON, BalanceReport, ReviewLoad
from checkowners.topology import (
    TeamComparison,
    TopologyReport,
)
from checkowners.trends import (
    TrendReport,
    compared_axis_line,
)

if TYPE_CHECKING:
    pass


def _render_completeness(ownership: OwnershipMap) -> None:
    console.print(completeness_label(run_completeness(ownership)))
    for gap in ownership.analysis_completeness.gaps:
        console.print(gap.reason)


def _format_last_commit(value: datetime | None) -> str:
    return value.date().isoformat() if value else "-"


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


def print_expired_suppressions(expired: tuple[Suppression, ...]) -> None:
    for item in expired:
        expires = item.expires.isoformat() if item.expires is not None else ""
        console.print(
            f"[red]Expired suppression:[/red] {escape(item.path)} {item.rule} "
            f"expired {expires}: {escape(item.reason)}"
        )


def _render_blocking(blocking: tuple[Finding, ...]) -> None:
    if not blocking:
        console.print("Blocking: none")
        return
    shown = ", ".join(f"{item.rule} {escape(item.path)}" for item in blocking)
    console.print(f"[red]Blocking:[/red] {shown}")


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


_PATHOLOGY_LABELS = {
    "knowledge-vacuum": "Knowledge vacuum",
    "phantom-ownership": "Phantom ownership",
    "shadow-maintainer": "Shadow maintainer",
    "ownership-review-divergence": "Ownership/review divergence",
}


def _render_pathology(item: Pathology) -> None:
    console.print(f"{_PATHOLOGY_LABELS[item.kind]}  {escape(item.path)}")
    console.print(f"          {escape(_pathology_detail(item))}")


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


def _render_topology(report: TopologyReport, config: Config) -> None:
    console.print(
        f"Algorithm: {report.algorithm}  resolution: {report.resolution:g}  seed: {report.seed}"
    )
    console.print(f"Modularity: {report.modularity:.4f}")
    if report.stability_available:
        console.print(
            "Stability mean: "
            f"{report.stability_mean:.4f}  variance: {report.stability_variance:.4f}"
        )
    else:
        console.print("Stability unavailable: the person graph has no edges.")
    if report.signals_used:
        console.print("Signals used: " + ", ".join(report.signals_used))
    if report.signals_unavailable:
        console.print("Signals unavailable: " + ", ".join(report.signals_unavailable))
    if not report.clusters:
        console.print("[yellow]No clusters inferred.[/yellow]")
        return
    table = Table(title="Exploratory repository topology")
    table.add_column("Cluster", style="cyan")
    table.add_column("Members")
    table.add_column("Primary paths")
    table.add_column("Source")
    for cluster in report.clusters:
        source = "[green]declared[/green]" if cluster.declared else "[yellow]inferred[/yellow]"
        names = [*cluster.members, *cluster.affiliated_members]
        table.add_row(
            escape(cluster.name),
            escape(", ".join(_person(member) for member in names)) if not _aggregate() else "-",
            escape(", ".join(cluster.primary_paths)) or "-",
            source,
        )
    console.print(table)
    if report.comparisons:
        console.print()
        console.print("[bold]Declared teams:[/bold]")
        for item in report.comparisons:
            _print_comparison(item)
    elif report.mismatches:
        console.print()
        console.print("[bold]Mismatches:[/bold]")
        for line in report.mismatches:
            console.print(f"  - {scrub_text(line, config, _repo_id())}")


def _print_comparison(item: TeamComparison) -> None:
    metrics = (
        f"Jaccard {item.jaccard:.2f}  precision {item.precision:.2f}  "
        f"recall {item.recall:.2f}  coverage {item.coverage:.2f}"
    )
    console.print(f"{escape(item.team)} vs {escape(item.cluster or '-')}: {metrics}")
    if item.cross_team and not _aggregate():
        experts = ", ".join(_person(member) for member in item.cross_team)
        console.print(f"  Potential cross-team expert: {escape(experts)}")


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
    if explanation.logical_file_id:
        console.print(f"Logical file:                 {escape(explanation.logical_file_id)}")
    if explanation.lineage:
        console.print(f"Prior names:                  {escape(', '.join(explanation.lineage))}")
    if explanation.rename_confidence < 1.0:
        console.print(f"Rename confidence:            {explanation.rename_confidence:.2f}")
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


def _render_trends(report: TrendReport, cap: int, *, compared: bool) -> None:
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


def _print_cache_info(info: CacheInfo) -> None:
    typer.echo(f"path: {info['path']}")
    typer.echo(f"bytes: {info['bytes']}")
    typer.echo(f"state_files: {info['state_files']}")
    typer.echo(f"graph_files: {info['graph_files']}")
    typer.echo(f"blame_files: {info['blame_files']}")
    typer.echo(f"handles: {'yes' if info['handles'] else 'no'}")
    typer.echo(f"limit_bytes: {info['limit_bytes']}")
    if "current_repo_id" in info:
        typer.echo(f"current_repo_id: {info['current_repo_id']}")
        typer.echo(f"current_state_path: {info['current_state_path']}")
        exists = "yes" if info["current_state_exists"] else "no"
        typer.echo(f"current_state_exists: {exists}")
    for entry in info["entries"]:
        typer.echo(
            f"{entry['repo_id']}\t{entry['analysis_ref']}\t"
            f"{entry['last_analyzed']}\t{entry['bytes']}"
        )

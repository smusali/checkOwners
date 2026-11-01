"""Building the JSON each command prints, and writing it out."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from checkowners.baseline import (
    RatchetOutcome,
    finding_payload,
)
from checkowners.change import (
    build_change,
    change_payload,
    changed_paths,
    merge_base,
    render_diff,
    render_suggest,
)
from checkowners.cli.analysis import (
    _change_loader,
    _change_team_resolver,
    _pull_request_change,
)
from checkowners.cli.common import (
    _active_config,
    _finish_analysis,
    _guard_ownership,
    _load_config,
    _MachineFormat,
    _repo_id,
    _scrubbed,
    _stamp_output,
    console,
    err_console,
)
from checkowners.concentration import (
    BusFactorReport,
    classify,
    distribution_json,
    qualified_owner_count_fields,
)
from checkowners.config import (
    Config,
)
from checkowners.generate import (
    GenerateResult,
    codeowners_write_metrics,
)
from checkowners.github import (
    ReconciliationResult,
)
from checkowners.graph import (
    TransferResult,
    format_transfer,
    label_nodes,
)
from checkowners.models import (
    DecayWarning,
    ExpertiseRank,
    Finding,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.onboard import OnboardingPath
from checkowners.payloads import (
    DecayWarningJson,
    OwnerJson,
    PathOwnershipJson,
    owner_json,
    path_analysis_json,
    risk_from_scores,
)
from checkowners.policy import (
    effective_policy,
)
from checkowners.privacy import (
    sanitize,
    scrub_text,
)
from checkowners.report_formats import (
    ReportFinding,
    junit_document,
    sarif_document,
    workflow_annotations,
)
from checkowners.risk import (
    DecayReport,
)
from checkowners.suggest import BalanceReport, ReviewLoad
from checkowners.topology import (
    TeamComparison,
    TopologyReport,
)

if TYPE_CHECKING:
    pass


def public_payload(
    data: Mapping[str, object],
    config: Config | None = None,
) -> dict[str, object]:
    loaded = config if config is not None else _active_config()
    if loaded is None:
        return dict(data)
    return sanitize(data, loaded, _repo_id())


def _emit_json(
    data: Mapping[str, object],
    ownership: OwnershipMap | None = None,
    *,
    head_sha: str | None = None,
    generated_at: str | None = None,
) -> None:
    stamped = _stamp_output(data, ownership, head_sha=head_sha, generated_at=generated_at)
    typer.echo(json.dumps(public_payload(stamped), indent=2, sort_keys=True))


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
        typer.echo(json.dumps(public_payload(document), indent=2, sort_keys=True))
        return
    typer.echo(_scrubbed(junit_document(findings, command=command)))


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


def _expertise_rank_payload(rank: ExpertiseRank) -> dict[str, object]:
    return {
        "handle": rank.handle,
        "confidence": round(rank.confidence, 4),
        "commits": rank.commits,
        "last_commit": rank.last_commit.isoformat() if rank.last_commit else None,
    }


def _ratchet_json(outcome: RatchetOutcome) -> dict[str, object]:
    return {
        "baselined": outcome.counts.baselined,
        "stale_baseline": [finding_payload(item) for item in outcome.stale_baseline],
        "suppressed": outcome.counts.suppressed,
    }


def _policy_payload(config: Config, blocking: tuple[Finding, ...]) -> dict[str, object]:
    return {
        "policy": effective_policy(config),
        "blocking": [finding_payload(item) for item in blocking],
    }


def filter_bus_payload(
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


def _write_action_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(public_payload(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )


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


def _emit_transfer(
    result: TransferResult | None,
    config: Config,
    *,
    stream: Literal["out", "err"],
    empty_only: bool = False,
) -> None:
    if result is None:
        return
    if result.nodes and empty_only:
        return
    if result.nodes:
        labeled = TransferResult(nodes=label_nodes(result.nodes, config, _repo_id()))
        text = format_transfer(labeled)
    else:
        text = "No ownership path."
    if stream == "err":
        err_console.print(text)
        return
    typer.echo(text)


def _qualified_owners_payload(report: BusFactorReport, config: Config) -> dict[str, object]:
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


def _topology_payload(report: TopologyReport) -> dict[str, object]:
    return {
        "algorithm": report.algorithm,
        "resolution": report.resolution,
        "seed": report.seed,
        "modularity": report.modularity,
        "stability_mean": report.stability_mean,
        "stability_variance": report.stability_variance,
        "stability_available": report.stability_available,
        "signals_used": list(report.signals_used),
        "signals_unavailable": list(report.signals_unavailable),
        "clusters": [
            {
                "name": cluster.name,
                "members": list(cluster.members),
                "affiliated_members": list(cluster.affiliated_members),
                "primary_paths": list(cluster.primary_paths),
                "declared": cluster.declared,
                "parent": cluster.parent,
            }
            for cluster in report.clusters
        ],
        "comparisons": [_comparison_payload(item) for item in report.comparisons],
        "mismatches": list(report.mismatches),
    }


def _comparison_payload(item: TeamComparison) -> dict[str, object]:
    return {
        "team": item.team,
        "cluster": item.cluster,
        "jaccard": item.jaccard,
        "precision": item.precision,
        "recall": item.recall,
        "coverage": item.coverage,
        "cross_team": list(item.cross_team),
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
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        _guard_ownership(exc)
    if json_output:
        _emit_json(change_payload(report))
        return
    text = render_suggest(report) if long else render_diff(report)
    typer.echo(scrub_text(text, config, _repo_id()), nl=False)


def _onboarding_payload(report: OnboardingPath) -> dict[str, object]:
    return {
        "target": report.target,
        "unavailable_signals": list(report.unavailable_signals),
        "mentor_candidates": list(report.mentor_candidates),
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

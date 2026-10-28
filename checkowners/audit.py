from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypeGuard, get_args

from checkowners.busfactor import compute_qualified_owners
from checkowners.decay import detect_decay
from checkowners.github import AccessState, ReviewState, TeamDirectory
from checkowners.models import (
    Config,
    DriftResult,
    Finding,
    FindingRule,
    OwnershipMap,
    knowledge_concentration,
)
from checkowners.patterns import CodeownersRule, matching_rules, pattern_matches
from checkowners.report_formats import ReportFinding, finding_for_audit, findings_for_shadows
from checkowners.validate import (
    RemoteCodeownersError,
    ValidationFinding,
    compare_codeowners_syntax,
)

RowStatus = Literal["ok", "warn", "fail", "unverifiable"]
CODEOWNERS_SIZE_LIMIT = 3_145_728


@dataclass(frozen=True)
class AuditRow:
    rule: str
    status: RowStatus
    summary: str
    path: str = ""
    owners: tuple[str, ...] = ()
    source: str = ""
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class AuditReport:
    rows: tuple[AuditRow, ...]
    env: tuple[AuditRow, ...] = ()
    codeowners_uri: str = "CODEOWNERS"


@dataclass(frozen=True)
class AuditFacts:
    repo_root: Path
    config: Config
    codeowners_path: Path
    text: str
    rules: tuple[CodeownersRule, ...]
    tracked: tuple[str, ...]
    head_branch: str
    base_branch: str
    file_bytes: int
    teams: TeamDirectory | None
    ownership: OwnershipMap | None
    analysis_error: str
    errors: tuple[ValidationFinding, ...]
    remote_errors: tuple[RemoteCodeownersError, ...] | None
    private: bool
    ref: str
    default_branch: str
    access: Mapping[str, AccessState]
    review: ReviewState
    token_present: bool
    include_inference: bool
    drift: DriftResult | None = None


@dataclass(frozen=True)
class EnvironmentFacts:
    git_repo: bool
    full_history: bool
    token_present: bool
    token_scopes: tuple[str, ...]
    codeowners_path: str
    api_reachable: bool | None
    team_visibility: str
    review_history: str
    blame_ignore_revs: bool
    cache_writable: bool
    version: str
    config_parsed: bool


def build_audit(facts: AuditFacts) -> AuditReport:
    relative = _relative(facts.repo_root, facts.codeowners_path)
    rows: list[AuditRow] = [
        _branch_row(facts),
        _size_row(facts, relative),
        _syntax_row(facts, relative),
        _access_row(facts),
        *_team_rows(facts),
        _shadow_row(facts, relative),
        _coverage_row(facts),
        _dead_row(facts),
        _bus_row(facts),
    ]
    if facts.include_inference:
        rows.extend(_inference_rows(facts, relative))
    return AuditReport(rows=tuple(rows), codeowners_uri=relative or "CODEOWNERS")


def environment_rows(facts: EnvironmentFacts) -> tuple[AuditRow, ...]:
    scopes = ", ".join(facts.token_scopes) if facts.token_scopes else "none reported"
    token = _row(
        "token",
        "ok" if facts.token_present else "unverifiable",
        "Token is present." if facts.token_present else "No token is configured.",
    )
    scope_row = _row(
        "token-scopes",
        "ok" if facts.token_present else "unverifiable",
        f"Token scopes: {scopes}.",
    )
    if facts.api_reachable is True:
        api = _row("api", "ok", "GitHub API answered.")
    else:
        api = _row("api", "unverifiable", "GitHub API did not answer.")
    teams = _row(
        "team-visibility",
        "ok" if facts.team_visibility == "visible" else "unverifiable",
        f"Team visibility: {facts.team_visibility}.",
    )
    reviews = _row(
        "review-history",
        "ok" if facts.review_history == "available" else "unverifiable",
        f"Review history: {facts.review_history}.",
    )
    blame = "present" if facts.blame_ignore_revs else "absent"
    return (
        _row("git-repo", "ok" if facts.git_repo else "fail", _yes(facts.git_repo, "Git repo")),
        _row(
            "full-history",
            "ok" if facts.full_history else "warn",
            "History is complete." if facts.full_history else "Repository is shallow.",
        ),
        token,
        scope_row,
        _row("codeowners-path", "ok", facts.codeowners_path or "CODEOWNERS path is unset."),
        api,
        teams,
        reviews,
        _row("blame-ignore-revs", "ok", f".git-blame-ignore-revs is {blame}."),
        _row(
            "cache",
            "ok" if facts.cache_writable else "warn",
            "Cache is writable." if facts.cache_writable else "Cache is not writable.",
        ),
        _row("version", "ok", f"checkowners {facts.version}."),
        _row(
            "config",
            "ok" if facts.config_parsed else "fail",
            "Config parsed." if facts.config_parsed else "Config did not parse.",
        ),
    )


def format_audit(report: AuditReport) -> str:
    lines = [_line(row) for row in report.rows]
    if report.env:
        lines.append("environment:")
        lines.extend(_line(row) for row in report.env)
    return "\n".join(lines) + "\n"


def audit_body(report: AuditReport) -> dict[str, object]:
    payload: dict[str, object] = {"rows": [_row_payload(row) for row in report.rows]}
    if report.env:
        payload["env"] = [_row_payload(row) for row in report.env]
    return payload


def fail_findings(report: AuditReport) -> tuple[Finding, ...]:
    found: list[Finding] = []
    for row in report.rows:
        if row.status == "fail":
            found.extend(row.findings)
    return tuple(found)


def audit_report_findings(report: AuditReport) -> tuple[ReportFinding, ...]:
    rows: list[ReportFinding] = []
    for row in report.rows:
        if row.status not in {"warn", "fail"}:
            continue
        level: Literal["warning", "error"] = "error" if row.status == "fail" else "warning"
        subjects = row.findings
        if not subjects:
            parsed = _as_rule(row.rule)
            if parsed is None:
                continue
            subjects = (
                Finding(
                    rule=parsed,
                    path=row.path or report.codeowners_uri,
                    owners=row.owners,
                ),
            )
        for finding in subjects:
            emitted = finding_for_audit(
                finding.rule,
                level=level,
                message=row.summary,
                path=finding.path,
                owners=finding.owners,
                status=row.status,
                uri=finding.path or report.codeowners_uri,
                start_line=_start_line(finding.path),
            )
            if emitted is not None:
                rows.append(emitted)
    return tuple(rows)


def without_hidden(
    report: AuditReport,
    visible: set[tuple[str, str, tuple[str, ...]]],
) -> AuditReport:
    kept: list[AuditRow] = []
    for row in report.rows:
        if row.status in {"ok", "unverifiable"} or not row.findings:
            kept.append(row)
            continue
        findings = tuple(item for item in row.findings if item.identity() in visible)
        if findings:
            kept.append(replace(row, findings=findings, owners=findings[0].owners))
    return replace(report, rows=tuple(kept))


def _inference_rows(facts: AuditFacts, relative: str) -> tuple[AuditRow, ...]:
    return (
        _duplicate_row(facts),
        _broad_row(facts),
        _self_row(facts, relative),
        _ruleset_row(facts),
        _drift_row(facts),
        _concentration_row(facts),
        _decay_row(facts),
        _orphan_row(facts),
        _observed_row(facts),
    )


def _branch_row(facts: AuditFacts) -> AuditRow:
    if not facts.head_branch or not facts.base_branch:
        return _row("branch", "unverifiable", "The branch GitHub consults is unknown.")
    if facts.head_branch == facts.base_branch:
        return _row("branch", "ok", f"CODEOWNERS was read from {facts.head_branch}.")
    summary = f"CODEOWNERS was read from {facts.head_branch}; GitHub consults {facts.base_branch}."
    return _row("branch", "warn", summary)


def _size_row(facts: AuditFacts, relative: str) -> AuditRow:
    if facts.file_bytes > CODEOWNERS_SIZE_LIMIT:
        finding = _one("file-size", relative or "CODEOWNERS")
        return _row(
            "file-size",
            "fail",
            f"CODEOWNERS is {facts.file_bytes} bytes. The limit is {CODEOWNERS_SIZE_LIMIT}.",
            path=relative,
            findings=(finding,),
        )
    return _row("file-size", "ok", f"CODEOWNERS is {facts.file_bytes} bytes.")


def _syntax_row(facts: AuditFacts, relative: str) -> AuditRow:
    compared = compare_codeowners_syntax(
        facts.text,
        facts.remote_errors,
        private=facts.private,
        ref=facts.ref,
        default_branch=facts.default_branch,
    )
    if compared.inconclusive:
        return _row(
            "invalid-owner",
            "unverifiable",
            "Private non-default branch returned no errors. The check is inconclusive.",
            source="github",
        )
    if facts.errors:
        findings = tuple(_syntax_finding(relative, error) for error in facts.errors)
        message = facts.errors[0].message
        return _row(
            "invalid-owner",
            "fail",
            message,
            source=compared.source,
            findings=findings,
        )
    if compared.source == "github" and not compared.agrees:
        return _row(
            "invalid-owner",
            "fail",
            "Local syntax and the GitHub errors endpoint disagree.",
            source="github",
            findings=(_one("invalid-owner", relative or "CODEOWNERS"),),
        )
    extra = ""
    if compared.other_kinds:
        extra = " Other GitHub kinds: " + ", ".join(compared.other_kinds) + "."
    return _row(
        "invalid-owner",
        "ok",
        f"Syntax source is {compared.source}.{extra}",
        source=compared.source,
    )


def _access_row(facts: AuditFacts) -> AuditRow:
    owners = _declared(facts.rules)
    if not owners:
        return _row("owner-permission", "ok", "CODEOWNERS names no owners.")
    if not facts.token_present:
        return _row(
            "owner-permission",
            "unverifiable",
            "Write access requires a GitHub token.",
            owners=owners,
        )
    denied: list[str] = []
    unknown: list[str] = []
    for owner in owners:
        if _is_email(owner):
            unknown.append(owner)
            continue
        state = facts.access.get(owner, "unknown")
        if state in {"read", "invisible"}:
            denied.append(owner)
        elif state != "write":
            unknown.append(owner)
    if denied:
        findings = tuple(_one("owner-permission", owner, (owner,)) for owner in denied)
        return _row(
            "owner-permission",
            "fail",
            "Owners without write access: " + ", ".join(denied) + ".",
            owners=tuple(denied),
            findings=findings,
        )
    if unknown:
        return _row(
            "owner-permission",
            "unverifiable",
            "Write access could not be read for " + ", ".join(unknown) + ".",
            owners=tuple(unknown),
        )
    return _row("owner-permission", "ok", "Every owner has write access.")


def _team_rows(facts: AuditFacts) -> tuple[AuditRow, ...]:
    owners = tuple(owner for owner in _declared(facts.rules) if "/" in owner)
    if not owners:
        return (_row("empty-team", "ok", "No team owners."),)
    if facts.teams is None:
        return (
            _row(
                "invisible-team",
                "unverifiable",
                "Team membership requires the GitHub API.",
                owners=owners,
            ),
        )
    invisible: list[str] = []
    empty: list[str] = []
    for owner in owners:
        membership = facts.teams.membership_for(owner)
        if membership is None or not membership.found or not membership.visible:
            invisible.append(owner)
        elif not membership.nested and not membership.members:
            empty.append(owner)
    hidden = _team_status("invisible-team", invisible, "Team membership is hidden or missing.")
    vacant = _team_status("empty-team", empty, "A team has no members and no child teams.")
    return (hidden, vacant)


def _shadow_row(facts: AuditFacts, relative: str) -> AuditRow:
    shadows = findings_for_shadows(facts.rules, facts.tracked, relative or "CODEOWNERS")
    if not shadows:
        return _row("shadowed-rule", "ok", "No rule is shadowed.")
    findings = tuple(_one("shadowed-rule", item.path, item.owners) for item in shadows)
    return _row(
        "shadowed-rule",
        "warn",
        shadows[0].message,
        path=shadows[0].path,
        owners=shadows[0].owners,
        findings=findings,
    )


def _coverage_row(facts: AuditFacts) -> AuditRow:
    if not facts.tracked:
        return _row("unowned-file", "unverifiable", "No tracked files were listed.")
    uncovered = tuple(path for path in facts.tracked if not matching_rules(facts.rules, path))
    if not uncovered:
        return _row("unowned-file", "ok", "Every tracked file matches a rule.")
    findings = tuple(_one("unowned-file", path) for path in uncovered)
    share = len(facts.tracked) - len(uncovered)
    return _row(
        "unowned-file",
        "warn",
        f"{share} of {len(facts.tracked)} tracked files match a rule.",
        findings=findings,
    )


def _dead_row(facts: AuditFacts) -> AuditRow:
    dead = tuple(
        rule
        for rule in facts.rules
        if not any(pattern_matches(rule.pattern, path) for path in facts.tracked)
    )
    if not dead:
        return _row("stale", "ok", "Every pattern matches a tracked file.")
    findings = tuple(_one("stale", rule.pattern, rule.owners) for rule in dead)
    return _row(
        "stale",
        "warn",
        "Patterns that match no tracked file: " + ", ".join(rule.pattern for rule in dead) + ".",
        findings=findings,
    )


def _bus_row(facts: AuditFacts) -> AuditRow:
    if facts.ownership is None:
        reason = facts.analysis_error or "Analysis did not run."
        return _row("single-expert", "unverifiable", reason)
    report = compute_qualified_owners(facts.ownership, facts.config)
    critical = report.critical_paths
    if not critical:
        return _row("single-expert", "ok", "No path has a single qualified owner.")
    findings = tuple(_one("single-expert", path) for path in critical)
    return _row(
        "single-expert",
        "warn",
        f"{len(critical)} paths have a single qualified owner.",
        findings=findings,
    )


def _duplicate_row(facts: AuditFacts) -> AuditRow:
    seen: set[str] = set()
    duplicates: list[CodeownersRule] = []
    for rule in facts.rules:
        if rule.pattern in seen:
            duplicates.append(rule)
        else:
            seen.add(rule.pattern)
    if not duplicates:
        return _row("duplicate-rule", "ok", "Patterns are unique.")
    findings = tuple(_one("duplicate-rule", rule.pattern, rule.owners) for rule in duplicates)
    return _row(
        "duplicate-rule",
        "warn",
        "Repeated patterns: " + ", ".join(rule.pattern for rule in duplicates) + ".",
        findings=findings,
    )


def _broad_row(facts: AuditFacts) -> AuditRow:
    if not facts.tracked:
        return _row("broad-rule", "ok", "No tracked files to measure.")
    broad = tuple(
        rule
        for rule in facts.rules
        if sum(pattern_matches(rule.pattern, path) for path in facts.tracked) * 2
        >= len(facts.tracked)
    )
    if not broad:
        return _row("broad-rule", "ok", "No pattern covers half of the tracked files.")
    findings = tuple(_one("broad-rule", rule.pattern, rule.owners) for rule in broad)
    return _row(
        "broad-rule",
        "warn",
        "Broad patterns: " + ", ".join(rule.pattern for rule in broad) + ".",
        findings=findings,
    )


def _self_row(facts: AuditFacts, relative: str) -> AuditRow:
    if not relative or not facts.codeowners_path.is_file():
        return _row("self-protection", "unverifiable", "CODEOWNERS path is not a file.")
    if matching_rules(facts.rules, relative):
        return _row("self-protection", "ok", "A rule matches the CODEOWNERS path.")
    finding = _one("self-protection", relative)
    return _row(
        "self-protection",
        "warn",
        "No rule matches the CODEOWNERS path.",
        path=relative,
        findings=(finding,),
    )


def _ruleset_row(facts: AuditFacts) -> AuditRow:
    if not facts.token_present or facts.review == "unknown":
        return _row(
            "ruleset",
            "unverifiable",
            "Required code owner review could not be read.",
        )
    if facts.review == "required":
        return _row("ruleset", "ok", "Code owner review is required.")
    return _row(
        "ruleset",
        "warn",
        "Rulesets and branch protection do not require code owner review.",
        findings=(_one("ruleset", facts.base_branch or facts.head_branch or "branch"),),
    )


def _drift_row(facts: AuditFacts) -> AuditRow:
    if facts.ownership is None or facts.drift is None:
        reason = facts.analysis_error or "Drift was not compared."
        return _row("changed", "unverifiable", reason)
    findings: list[Finding] = []
    for entry in facts.drift.changed:
        findings.append(_one("changed", entry.path, entry.owners))
    for entry in facts.drift.missing:
        findings.append(_one("missing", entry.path))
    if not findings:
        return _row("changed", "ok", "No ownership drift.")
    return _row(
        "changed",
        "warn",
        f"{len(facts.drift.changed)} changed and {len(facts.drift.missing)} missing.",
        findings=tuple(findings),
    )


def _concentration_row(facts: AuditFacts) -> AuditRow:
    if facts.ownership is None:
        return _row("single-expert", "unverifiable", _analysis_reason(facts))
    threshold = facts.config.risk.truck_factor_thresholds[0]
    hot: list[str] = []
    for path, ownership in facts.ownership.paths.items():
        scores = tuple(owner.ownership_score for owner in ownership.owners)
        share = knowledge_concentration(scores, facts.config.risk.truck_factor_thresholds)
        if share.top_owner_share >= threshold:
            hot.append(path)
    if not hot:
        return _row("single-expert", "ok", "No path concentrates ownership past the truck factor.")
    findings = tuple(_one("single-expert", path) for path in hot)
    return _row(
        "single-expert",
        "warn",
        f"{len(hot)} paths exceed the truck-factor share.",
        findings=findings,
    )


def _decay_row(facts: AuditFacts) -> AuditRow:
    if facts.ownership is None:
        return _row("decayed-owner", "unverifiable", _analysis_reason(facts))
    declared = {_login(owner) for owner in _declared(facts.rules)}
    reports = tuple(
        item
        for item in detect_decay(facts.ownership, facts.config)
        if _login(item.warning.handle) in declared
    )
    if not reports:
        return _row("decayed-owner", "ok", "No declared owner is past the decay threshold.")
    findings = tuple(
        _one("decayed-owner", item.warning.path, (item.warning.handle,)) for item in reports
    )
    return _row(
        "decayed-owner",
        "warn",
        f"{len(reports)} declared owners are past the decay threshold.",
        findings=findings,
    )


def _orphan_row(facts: AuditFacts) -> AuditRow:
    if facts.ownership is None:
        return _row("orphaned-owner", "unverifiable", _analysis_reason(facts))
    former_top: list[tuple[str, str]] = []
    saw_former = False
    for path, ownership in facts.ownership.paths.items():
        if not ownership.owners:
            continue
        top = max(ownership.owners, key=lambda owner: (owner.ownership_score, owner.handle))
        if top.contributor_status == "former":
            saw_former = True
            former_top.append((path, top.handle))
        elif any(owner.contributor_status == "former" for owner in ownership.owners):
            saw_former = True
    if former_top:
        findings = tuple(_one("orphaned-owner", path, (handle,)) for path, handle in former_top)
        return _row(
            "orphaned-owner",
            "warn",
            "A former owner is the top observed expert.",
            findings=findings,
        )
    if not facts.config.contributor_roster and not saw_former:
        return _row(
            "orphaned-owner",
            "unverifiable",
            "No roster is configured, so former owners cannot be judged.",
        )
    return _row("orphaned-owner", "ok", "No former owner is the top observed expert.")


def _observed_row(facts: AuditFacts) -> AuditRow:
    if facts.ownership is None:
        return _row(
            "observed-unowned",
            "unverifiable",
            _analysis_reason(facts),
        )
    quiet = tuple(
        path
        for path, ownership in facts.ownership.paths.items()
        if ownership.qualified_owner_count == 0
    )
    if not quiet:
        return _row("observed-unowned", "ok", "Every scored path has a confident expert.")
    findings = tuple(_one("observed-unowned", path) for path in quiet)
    return _row(
        "observed-unowned",
        "warn",
        f"{len(quiet)} paths have no confident observed expert.",
        findings=findings,
    )


def _team_status(rule: str, owners: Sequence[str], problem: str) -> AuditRow:
    if not owners:
        return _row(rule, "ok", "None.")
    kind = _as_rule(rule)
    findings = () if kind is None else tuple(_one(kind, owner, (owner,)) for owner in owners)
    return _row(rule, "fail", problem, owners=tuple(owners), findings=findings)


def _row(
    rule: str,
    status: RowStatus,
    summary: str,
    *,
    path: str = "",
    owners: tuple[str, ...] = (),
    source: str = "",
    findings: tuple[Finding, ...] = (),
) -> AuditRow:
    return AuditRow(
        rule=rule,
        status=status,
        summary=summary,
        path=path,
        owners=owners,
        source=source,
        findings=findings,
    )


def _one(rule: FindingRule, path: str, owners: tuple[str, ...] = ()) -> Finding:
    return Finding(rule=rule, path=path or "CODEOWNERS", owners=owners)


def _declared(rules: tuple[CodeownersRule, ...]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for rule in rules:
        for owner in rule.owners:
            seen.setdefault(owner, owner)
    return tuple(seen)


def _relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _line(row: AuditRow) -> str:
    return f"{row.status}: {row.rule}: {row.summary}"


def _row_payload(row: AuditRow) -> dict[str, object]:
    return {
        "owners": list(row.owners),
        "path": row.path,
        "rule": row.rule,
        "source": row.source,
        "status": row.status,
        "summary": row.summary,
    }


def _start_line(path: str) -> int:
    _prefix, sep, raw = path.rpartition(":")
    if sep and raw.isdigit():
        return int(raw)
    return 1


def _analysis_reason(facts: AuditFacts) -> str:
    return facts.analysis_error or "Analysis did not run."


def _syntax_finding(relative: str, error: ValidationFinding) -> Finding:
    suffix = f":{error.line_number}" if error.line_number else ""
    return _one("invalid-owner", f"{relative}{suffix}")


def _login(owner: str) -> str:
    return owner.lstrip("@").casefold()


def _is_email(owner: str) -> bool:
    return "@" in owner and not owner.startswith("@")


def _yes(flag: bool, label: str) -> str:
    if flag:
        return f"{label}: yes."
    return f"{label}: no."


def _as_rule(rule: str) -> FindingRule | None:
    if _is_finding_rule(rule):
        return rule
    return None


def _is_finding_rule(rule: str) -> TypeGuard[FindingRule]:
    return rule in get_args(FindingRule)

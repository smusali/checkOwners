from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from xml.sax.saxutils import escape

from checkowners import __version__
from checkowners.busfactor import BusFactorReport
from checkowners.drift import compute_severity
from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    AnalysisGap,
    Config,
    DriftEntry,
    DriftResult,
    models_payload,
)
from checkowners.patterns import CodeownersRule, matching_rules
from checkowners.validate import ValidationFinding

RuleId = Literal[
    "checkowners/unowned-path",
    "checkowners/stale-owner",
    "checkowners/ownership-drift",
    "checkowners/invalid-owner",
    "checkowners/shadowed-rule",
    "checkowners/single-expert-risk",
    "checkowners/incomplete-analysis",
]
SarifLevel = Literal["note", "warning", "error"]
AnnotationLevel = Literal["notice", "warning", "failure"]
_BaselineRule = Literal[
    "missing",
    "stale",
    "changed",
    "invalid-owner",
    "single-expert",
    "shadowed-rule",
    "incomplete-analysis",
]

RULE_IDS: tuple[RuleId, ...] = (
    "checkowners/unowned-path",
    "checkowners/stale-owner",
    "checkowners/ownership-drift",
    "checkowners/invalid-owner",
    "checkowners/shadowed-rule",
    "checkowners/single-expert-risk",
    "checkowners/incomplete-analysis",
)
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"
ANNOTATION_LIMIT = 50
_INVALID_OWNER = "Invalid owner format: "
_INFORMATION_URI = "https://github.com/smusali/checkOwners"


@dataclass(frozen=True)
class _Rule:
    rule_id: RuleId
    name: str
    text: str
    default_level: SarifLevel


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "checkowners/unowned-path",
        "Unowned path",
        "A path with observed expertise has no CODEOWNERS rule.",
        "warning",
    ),
    _Rule(
        "checkowners/stale-owner",
        "Stale owner",
        "A CODEOWNERS rule matches no tracked file.",
        "warning",
    ),
    _Rule(
        "checkowners/ownership-drift",
        "Ownership drift",
        "Declared owners differ from observed expertise.",
        "warning",
    ),
    _Rule(
        "checkowners/invalid-owner",
        "Invalid owner",
        "A CODEOWNERS owner is not a GitHub handle or email.",
        "error",
    ),
    _Rule(
        "checkowners/shadowed-rule",
        "Shadowed rule",
        "An earlier CODEOWNERS rule never wins because a later rule matches the same paths.",
        "warning",
    ),
    _Rule(
        "checkowners/single-expert-risk",
        "Single expert risk",
        "One qualified owner holds the expertise for a path.",
        "error",
    ),
    _Rule(
        "checkowners/incomplete-analysis",
        "Incomplete analysis",
        "A source of ownership evidence was not collected.",
        "warning",
    ),
)
_RULE_INDEX = {rule.rule_id: index for index, rule in enumerate(_RULES)}


@dataclass(frozen=True)
class ReportFinding:
    rule_id: RuleId
    level: SarifLevel
    message: str
    uri: str
    start_line: int
    baseline_rule: _BaselineRule
    path: str
    owners: tuple[str, ...]
    properties: Mapping[str, object]


def sarif_level(severity: str) -> SarifLevel:
    if severity in {"critical", "high", "error"}:
        return "error"
    if severity in {"medium", "warning"}:
        return "warning"
    return "note"


def baseline_identity(rule: str, path: str, owners: Sequence[str]) -> str:
    folded = ",".join(sorted(owner.casefold() for owner in owners))
    return f"{rule}|{path}|{folded}"


def baseline_fingerprint(rule: str, path: str, owners: Sequence[str]) -> str:
    identity = baseline_identity(rule, path, owners)
    return hashlib.sha256(identity.encode()).hexdigest()


def findings_for_drift(
    result: DriftResult,
    *,
    codeowners_uri: str,
    config: Config | None,
) -> tuple[ReportFinding, ...]:
    rows: list[ReportFinding] = []
    rows.extend(
        _drift_rows(
            result.missing,
            "checkowners/unowned-path",
            "missing",
            codeowners_uri,
            config,
            on_file=True,
        )
    )
    rows.extend(
        _drift_rows(
            result.stale,
            "checkowners/stale-owner",
            "stale",
            codeowners_uri,
            config,
            on_file=False,
        )
    )
    rows.extend(
        _drift_rows(
            result.changed,
            "checkowners/ownership-drift",
            "changed",
            codeowners_uri,
            config,
            on_file=False,
        )
    )
    return tuple(rows)


def findings_for_bus(report: BusFactorReport) -> tuple[ReportFinding, ...]:
    critical = set(report.critical_paths)
    rows: list[ReportFinding] = []
    for entry in report.entries:
        if entry.path not in critical:
            continue
        owners = entry.contributors_above_threshold
        names = ", ".join(owners) if owners else "none"
        message = (
            f"Single expert risk on {entry.path}. "
            f"{entry.qualified_owner_count} qualified owner(s): {names}."
        )
        if entry.recommended_backups:
            message += f" Backups: {', '.join(entry.recommended_backups)}."
        rows.append(
            ReportFinding(
                rule_id="checkowners/single-expert-risk",
                level=sarif_level("critical"),
                message=message,
                uri=entry.path,
                start_line=1,
                baseline_rule="single-expert",
                path=entry.path,
                owners=owners,
                properties={
                    "qualified_owner_count": entry.qualified_owner_count,
                    "owners": list(owners),
                    "recommended_backups": list(entry.recommended_backups),
                },
            )
        )
    return tuple(rows)


def findings_for_validation(
    errors: Sequence[ValidationFinding],
    codeowners_uri: str,
    severity: str,
) -> tuple[ReportFinding, ...]:
    rows: list[ReportFinding] = []
    level = sarif_level(severity)
    for error in errors:
        if not error.message.startswith(_INVALID_OWNER) or error.line_number < 1:
            continue
        token = error.message.removeprefix(_INVALID_OWNER).strip()
        owners = (token,) if token else ()
        path = f"{codeowners_uri}:{error.line_number}"
        rows.append(
            ReportFinding(
                rule_id="checkowners/invalid-owner",
                level=level,
                message=error.message,
                uri=codeowners_uri,
                start_line=error.line_number,
                baseline_rule="invalid-owner",
                path=path,
                owners=owners,
                properties={"recommendation": "review_codeowners_rule"},
            )
        )
    return tuple(rows)


def findings_for_shadows(
    rules: tuple[CodeownersRule, ...],
    tracked: Sequence[str],
    codeowners_uri: str,
) -> tuple[ReportFinding, ...]:
    seen: set[int] = set()
    rows: list[ReportFinding] = []
    for path in tracked:
        matches = matching_rules(rules, path)
        if len(matches) < 2:
            continue
        winner = matches[-1]
        for earlier in matches[:-1]:
            if earlier.line_number in seen:
                continue
            seen.add(earlier.line_number)
            line = winner.line_number if winner.line_number > 0 else 1
            message = f"Pattern {earlier.pattern} is shadowed by line {winner.line_number}."
            rows.append(
                ReportFinding(
                    rule_id="checkowners/shadowed-rule",
                    level="warning",
                    message=message,
                    uri=codeowners_uri,
                    start_line=line,
                    baseline_rule="shadowed-rule",
                    path=earlier.pattern,
                    owners=earlier.owners,
                    properties={
                        "shadowed_pattern": earlier.pattern,
                        "shadowed_line": earlier.line_number,
                        "shadowing_pattern": winner.pattern,
                        "shadowing_line": winner.line_number,
                        "recommendation": "review_codeowners_rule",
                    },
                )
            )
    return tuple(rows)


def findings_for_gaps(
    gaps: Sequence[AnalysisGap],
    uri: str,
    severity: str,
) -> tuple[ReportFinding, ...]:
    level = sarif_level(severity)
    return tuple(
        ReportFinding(
            rule_id="checkowners/incomplete-analysis",
            level=level,
            message=gap.reason,
            uri=uri,
            start_line=1,
            baseline_rule="incomplete-analysis",
            path=gap.code,
            owners=(),
            properties={"code": gap.code},
        )
        for gap in gaps
    )


def combine_findings(*groups: Sequence[ReportFinding]) -> tuple[ReportFinding, ...]:
    seen: set[str] = set()
    kept: list[ReportFinding] = []
    for group in groups:
        for finding in group:
            key = baseline_fingerprint(finding.baseline_rule, finding.path, finding.owners)
            if key in seen:
                continue
            seen.add(key)
            kept.append(finding)
    return tuple(kept)


def sarif_document(
    findings: Sequence[ReportFinding],
    *,
    command: str,
    provenance: Mapping[str, object],
) -> dict[str, object]:
    version = provenance.get("checkowners_version")
    tool_version = version if isinstance(version, str) and version else __version__
    properties = dict(provenance)
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "automationDetails": {"id": f"checkowners/{command}"},
                "tool": {
                    "driver": {
                        "name": "checkowners",
                        "version": tool_version,
                        "semanticVersion": tool_version,
                        "informationUri": _INFORMATION_URI,
                        "rules": [_rule_descriptor(rule) for rule in _RULES],
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "properties": properties,
                    }
                ],
                "properties": properties,
                "results": [_result(finding) for finding in findings],
            }
        ],
    }


def junit_document(findings: Sequence[ReportFinding], *, command: str) -> str:
    safe_command = escape(command)
    if findings:
        cases = "".join(_testcase(finding, safe_command) for finding in findings)
        tests = len(findings)
        failures = len(findings)
    else:
        cases = f'<testcase classname="checkowners.{safe_command}" name="no findings"/>'
        tests = 1
        failures = 0
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites name="checkowners" tests="{tests}" failures="{failures}">'
        f'<testsuite name="{safe_command}" tests="{tests}" failures="{failures}" errors="0">'
        f"{cases}</testsuite></testsuites>"
    )


def workflow_annotations(findings: Sequence[ReportFinding]) -> str:
    lines = [_workflow_command(finding) for finding in findings[:ANNOTATION_LIMIT]]
    extra = len(findings) - ANNOTATION_LIMIT
    if extra > 0:
        lines.append(f"::notice::{extra} more findings are in the step summary.")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def check_annotations(findings: Sequence[ReportFinding]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for finding in findings[:ANNOTATION_LIMIT]:
        rows.append(
            {
                "path": finding.uri,
                "start_line": finding.start_line,
                "end_line": finding.start_line,
                "annotation_level": _check_level(finding.level),
                "message": finding.message[:65535],
            }
        )
    return rows


def provenance_stub() -> dict[str, object]:
    return {
        "schema_version": COMMAND_SCHEMA_VERSION,
        "checkowners_version": __version__,
        "models": models_payload(),
        "repository": "",
        "head_sha": "",
        "generated_at": "",
        "analysis_completeness": None,
    }


def _drift_rows(
    entries: tuple[DriftEntry, ...],
    rule_id: RuleId,
    baseline_rule: _BaselineRule,
    codeowners_uri: str,
    config: Config | None,
    *,
    on_file: bool,
) -> list[ReportFinding]:
    rows: list[ReportFinding] = []
    for entry in entries:
        lone = DriftResult(stale=(), missing=(), changed=(entry,), drift_detected=True)
        severity = compute_severity(lone, config)
        action = (
            "remove_stale_rule" if entry.drift_type == "stale_rule" else "review_codeowners_rule"
        )
        owners = () if baseline_rule == "missing" else entry.owners
        line = (
            1
            if on_file or entry.line_number is None or entry.line_number < 1
            else entry.line_number
        )
        uri = entry.path if on_file else codeowners_uri
        rows.append(
            ReportFinding(
                rule_id=rule_id,
                level=sarif_level(severity),
                message=_drift_message(entry, severity, action),
                uri=uri,
                start_line=line,
                baseline_rule=baseline_rule,
                path=entry.path,
                owners=owners,
                properties={
                    "recommendation": action,
                    "declared": list(entry.owners),
                    "observed": list(entry.observed_owners),
                    "confidence_delta": round(entry.confidence_delta, 4),
                    "drift_type": entry.drift_type,
                    "severity": severity,
                },
            )
        )
    return rows


def _drift_message(entry: DriftEntry, severity: str, action: str) -> str:
    reason = entry.reason or "Ownership differs from CODEOWNERS."
    if not reason.endswith("."):
        reason = f"{reason}."
    declared = ", ".join(entry.owners) if entry.owners else "none"
    observed = ", ".join(entry.observed_owners) if entry.observed_owners else "none"
    return (
        f"{reason} Declared: {declared}. Observed: {observed}. "
        f"Severity: {severity}. Recommendation: {action}."
    )


def _rule_descriptor(rule: _Rule) -> dict[str, object]:
    return {
        "id": rule.rule_id,
        "name": rule.name,
        "shortDescription": {"text": rule.text},
        "fullDescription": {"text": rule.text},
        "defaultConfiguration": {"level": rule.default_level},
        "help": {"text": rule.text},
    }


def _result(finding: ReportFinding) -> dict[str, object]:
    identity = baseline_identity(finding.baseline_rule, finding.path, finding.owners)
    properties: dict[str, object] = {
        "baselineIdentity": identity,
        **finding.properties,
    }
    return {
        "ruleId": finding.rule_id,
        "ruleIndex": _RULE_INDEX[finding.rule_id],
        "level": finding.level,
        "message": {"text": finding.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.uri},
                    "region": {"startLine": finding.start_line},
                }
            }
        ],
        "partialFingerprints": {
            "primaryLocationLineHash": baseline_fingerprint(
                finding.baseline_rule, finding.path, finding.owners
            )
        },
        "properties": properties,
    }


def _testcase(finding: ReportFinding, command: str) -> str:
    title = escape(_first_sentence(finding.message))
    return (
        "<testcase "
        f'classname="checkowners.{command}" '
        f'name="{escape(finding.rule_id)}:{escape(finding.path)}" '
        f'file="{escape(finding.uri)}" '
        f'line="{finding.start_line}">'
        f'<failure message="{title}" type="{escape(finding.rule_id)}">'
        f"{escape(finding.message)}</failure></testcase>"
    )


def _first_sentence(message: str) -> str:
    period = message.find(". ")
    if period == -1:
        return message
    return message[: period + 1]


def _workflow_command(finding: ReportFinding) -> str:
    kind = {"error": "error", "warning": "warning", "note": "notice"}[finding.level]
    path = _command_property(finding.uri)
    message = _command_message(finding.message)
    return f"::{kind} file={path},line={finding.start_line}::{message}"


def _command_property(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(",", "%2C")
        .replace(":", "%3A")
    )


def _command_message(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _check_level(level: SarifLevel) -> AnnotationLevel:
    if level == "error":
        return "failure"
    if level == "warning":
        return "warning"
    return "notice"

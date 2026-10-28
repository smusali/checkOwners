from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import jsonschema
import pytest
from typer.testing import CliRunner

from checkowners import __version__
from checkowners.busfactor import BusFactor, BusFactorReport
from checkowners.cli import app
from checkowners.models import (
    AnalysisGap,
    BusFactorConfig,
    Config,
    DriftEntry,
    DriftResult,
)
from checkowners.patterns import CodeownersRule
from checkowners.report_formats import (
    RULE_IDS,
    baseline_fingerprint,
    baseline_identity,
    check_annotations,
    combine_findings,
    finding_for_audit,
    findings_for_bus,
    findings_for_drift,
    findings_for_gaps,
    findings_for_shadows,
    findings_for_validation,
    junit_document,
    provenance_stub,
    sarif_document,
    sarif_level,
    workflow_annotations,
)
from checkowners.validate import ValidationFinding
from tests.conftest import init_git_repo

_SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "sarif-2.1.0.json"


def test_rule_ids_stay_stable() -> None:
    assert RULE_IDS == (
        "checkowners/unowned-path",
        "checkowners/stale-owner",
        "checkowners/ownership-drift",
        "checkowners/invalid-owner",
        "checkowners/shadowed-rule",
        "checkowners/single-expert-risk",
        "checkowners/incomplete-analysis",
        "checkowners/owner-permission",
        "checkowners/invisible-team",
        "checkowners/empty-team",
        "checkowners/file-size",
        "checkowners/unowned-file",
        "checkowners/observed-unowned",
        "checkowners/duplicate-rule",
        "checkowners/broad-rule",
        "checkowners/self-protection",
        "checkowners/ruleset",
        "checkowners/decayed-owner",
        "checkowners/orphaned-owner",
    )


def test_sarif_level_maps_one_place() -> None:
    assert sarif_level("critical") == "error"
    assert sarif_level("high") == "error"
    assert sarif_level("error") == "error"
    assert sarif_level("medium") == "warning"
    assert sarif_level("warning") == "warning"
    assert sarif_level("low") == "note"


def test_fingerprint_matches_baseline_identity() -> None:
    owners = ("@Bob", "@alice")
    identity = baseline_identity("stale", "src/", owners)
    assert identity == "stale|src/|@alice,@bob"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    assert baseline_fingerprint("stale", "src/", owners) == digest


def _document() -> dict[str, object]:
    drift = DriftResult(
        stale=(
            DriftEntry(
                path="old/",
                confidence_delta=0.1,
                reason="pattern matches no tracked file",
                owners=("@alice",),
                drift_type="stale_rule",
                line_number=4,
            ),
        ),
        missing=(
            DriftEntry(
                path="src/new.py",
                confidence_delta=0.4,
                reason="no CODEOWNERS owner",
                drift_type="owner_mismatch",
            ),
        ),
        changed=(
            DriftEntry(
                path="src/pay.py",
                confidence_delta=0.8,
                reason="owners differ",
                owners=("@alice",),
                observed_owners=("@bob",),
                drift_type="owner_mismatch",
                line_number=9,
            ),
        ),
        drift_detected=True,
    )
    bus = BusFactorReport(
        entries=(
            BusFactor(
                path="src/solo.py",
                qualified_owner_count=1,
                contributors_above_threshold=("@carol",),
                recommended_backups=("@dave",),
            ),
        ),
        qualified_owner_count_cap=3,
        config=BusFactorConfig(),
    )
    rules = (
        CodeownersRule(pattern="src/solo.py", owners=("@old",), line_number=1),
        CodeownersRule(pattern="src/solo.py", owners=("@carol",), line_number=2),
    )
    findings = (
        *findings_for_drift(drift, codeowners_uri="CODEOWNERS", config=Config()),
        *findings_for_bus(bus),
        *findings_for_validation(
            (
                ValidationFinding(
                    line_number=3,
                    line="* not-an-owner",
                    message="Invalid owner format: not-an-owner",
                ),
            ),
            "CODEOWNERS",
            "error",
        ),
        *findings_for_shadows(rules, ("src/solo.py",), "CODEOWNERS"),
        *findings_for_gaps(
            (AnalysisGap(code="absent_token", reason="No token."),),
            ".github/checkowners.yml",
            "warning",
        ),
    )
    return sarif_document(findings, command="drift", provenance=provenance_stub())


def test_sarif_document_matches_schema_and_locations() -> None:
    document = _document()
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    validator.validate(document)
    runs = document["runs"]
    assert isinstance(runs, list)
    run = runs[0]
    assert isinstance(run, dict)
    tool = run["tool"]
    assert isinstance(tool, dict)
    driver = tool["driver"]
    assert isinstance(driver, dict)
    rules = driver["rules"]
    assert isinstance(rules, list)
    assert [rule["id"] for rule in rules] == list(RULE_IDS)
    results = run["results"]
    assert isinstance(results, list)
    by_rule = {item["ruleId"]: item for item in results}
    expected = {
        "checkowners/unowned-path": ("note", "src/new.py", 1),
        "checkowners/stale-owner": ("warning", "CODEOWNERS", 4),
        "checkowners/ownership-drift": ("error", "CODEOWNERS", 9),
        "checkowners/invalid-owner": ("error", "CODEOWNERS", 3),
        "checkowners/shadowed-rule": ("warning", "CODEOWNERS", 2),
        "checkowners/single-expert-risk": ("error", "src/solo.py", 1),
        "checkowners/incomplete-analysis": ("warning", ".github/checkowners.yml", 1),
    }
    assert set(by_rule) == set(expected)
    for rule_id, (level, uri, line) in expected.items():
        item = by_rule[rule_id]
        assert item["level"] == level
        locations = item["locations"]
        assert isinstance(locations, list)
        physical = locations[0]["physicalLocation"]
        assert physical["artifactLocation"]["uri"] == uri
        assert physical["region"]["startLine"] == line
        fingerprints = item["partialFingerprints"]
        assert isinstance(fingerprints["primaryLocationLineHash"], str)
        assert len(fingerprints["primaryLocationLineHash"]) == 64


def test_junit_shape_and_annotations() -> None:
    findings = (
        *findings_for_drift(
            DriftResult(
                stale=(),
                missing=(
                    DriftEntry(
                        path="src/new.py",
                        confidence_delta=0.4,
                        reason="no CODEOWNERS owner",
                    ),
                ),
                changed=(),
                drift_detected=True,
            ),
            codeowners_uri="CODEOWNERS",
            config=Config(),
        ),
    )
    xml = junit_document(findings, command="drift")
    root = ElementTree.fromstring(xml)  # noqa: S314
    assert root.tag == "testsuites"
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.attrib["tests"] == "1"
    assert suite.attrib["failures"] == "1"
    case = suite.find("testcase")
    assert case is not None
    assert case.attrib["classname"] == "checkowners.drift"
    assert case.attrib["name"].startswith("checkowners/unowned-path:")
    assert case.attrib["file"] == "src/new.py"
    failure = case.find("failure")
    assert failure is not None
    assert failure.attrib["type"] == "checkowners/unowned-path"
    clean = ElementTree.fromstring(junit_document((), command="risk"))  # noqa: S314
    clean_case = clean.find("testsuite/testcase")
    assert clean_case is not None
    assert clean_case.attrib["name"] == "no findings"
    assert clean.find("testsuite/testcase/failure") is None
    notes = workflow_annotations(findings)
    assert notes.startswith("::notice file=src/new.py,line=1::")
    assert check_annotations(findings)[0]["annotation_level"] == "notice"


def test_annotation_cap_names_the_remainder() -> None:
    findings = findings_for_gaps(
        tuple(AnalysisGap(code=f"gap-{index}", reason=f"Gap {index}.") for index in range(51)),
        "CODEOWNERS",
        "warning",
    )
    text = workflow_annotations(findings)
    assert text.count("::warning ") == 50
    assert "::notice::1 more findings are in the step summary." in text
    assert len(check_annotations(findings)) == 50


def test_validate_format_junit_and_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_git_repo(tmp_path)
    (tmp_path / "CODEOWNERS").write_text("* @alice\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    junit = runner.invoke(app, ["validate", "--format", "junit"])
    assert junit.exit_code == 0
    assert "<testsuites" in junit.stdout
    conflict = runner.invoke(app, ["validate", "--json", "--format", "sarif"])
    assert conflict.exit_code == 2


def test_format_edges() -> None:
    assert workflow_annotations(()) == ""
    rules = (
        CodeownersRule(pattern="src/solo.py", owners=("@old",), line_number=3),
        CodeownersRule(pattern="src/solo.py", owners=("@new",), line_number=0),
    )
    shadows = findings_for_shadows(rules, ("src/solo.py", "src/solo.py"), "CODEOWNERS")
    assert len(shadows) == 1
    assert shadows[0].start_line == 1
    skipped = findings_for_validation(
        (
            ValidationFinding(line_number=0, line="x", message="Invalid owner format: z"),
            ValidationFinding(line_number=2, line="x", message="syntax error"),
            ValidationFinding(line_number=4, line="x", message="Invalid owner format: "),
        ),
        "CODEOWNERS",
        "warning",
    )
    assert len(skipped) == 1
    assert skipped[0].owners == ()
    assert skipped[0].level == "warning"
    gap = AnalysisGap(code="absent_token", reason="No period")
    repeated = findings_for_gaps((gap, gap), "CODEOWNERS", "low")
    assert len(combine_findings(repeated, repeated)) == 1
    assert check_annotations(repeated)[0]["annotation_level"] == "notice"
    bus = BusFactorReport(
        entries=(
            BusFactor(
                path="src/ok.py",
                qualified_owner_count=3,
                contributors_above_threshold=("@a",),
                recommended_backups=(),
            ),
            BusFactor(
                path="src/solo.py",
                qualified_owner_count=1,
                contributors_above_threshold=(),
                recommended_backups=(),
            ),
        ),
        qualified_owner_count_cap=3,
        config=BusFactorConfig(),
    )
    solo = findings_for_bus(bus)
    assert len(solo) == 1
    assert "none" in solo[0].message
    assert "Backups" not in solo[0].message
    assert check_annotations(solo)[0]["annotation_level"] == "failure"
    done = findings_for_drift(
        DriftResult(
            stale=(
                DriftEntry(
                    path="old/",
                    confidence_delta=0.1,
                    reason="Already done.",
                    owners=(),
                    drift_type="stale_rule",
                    line_number=0,
                ),
            ),
            missing=(),
            changed=(),
            drift_detected=True,
        ),
        codeowners_uri="CODEOWNERS",
        config=Config(),
    )
    assert done[0].message.startswith("Already done.")
    assert done[0].start_line == 1
    blank = sarif_document((), command="drift", provenance={})
    numbered = sarif_document((), command="drift", provenance={"checkowners_version": 3})
    runs = blank["runs"]
    other = numbered["runs"]
    assert isinstance(runs, list) and isinstance(other, list)
    assert runs[0]["tool"]["driver"]["version"] == __version__
    assert other[0]["tool"]["driver"]["version"] == __version__
    weird = findings_for_gaps((gap,), "dir/a b:c,%\n", "note")
    command = workflow_annotations(weird)
    assert "%2C" in command or "%3A" in command
    assert "%0A" in command
    assert "%25" in command
    xml = junit_document(weird, command="drift")
    assert "No period" in xml


def test_finding_for_audit_skips_rules_without_a_sarif_id() -> None:
    assert (
        finding_for_audit(
            "not-a-rule",
            level="warning",
            message="no",
            path="CODEOWNERS",
            owners=(),
            status="warn",
            uri="CODEOWNERS",
            start_line=1,
        )
        is None
    )
    assert (
        finding_for_audit(
            "incomplete-analysis",
            level="note",
            message="gap",
            path="CODEOWNERS",
            owners=(),
            status="warn",
            uri="",
            start_line=1,
        )
        is None
    )

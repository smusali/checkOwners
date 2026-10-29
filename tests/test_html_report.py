"""Offline HTML report."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners.cli import app
from checkowners.html_report import (
    HTML_SIZE_BUDGET,
    LIMITATIONS_SUMMARY,
    REMOVAL_PATH,
    ReportDocument,
    ReportFile,
    ReportOwner,
    render_report,
)
from checkowners.models import Config
from checkowners.privacy import KNOWLEDGE_RISK_NOTICE
from tests.conftest import GitRepo

_EMAIL = "secret-owner@example.com"


def _owner(handle: str = _EMAIL) -> ReportOwner:
    return ReportOwner(
        handle=handle,
        ownership_score=0.82,
        evidence_quality=0.64,
        commits=3,
        last_commit="2026-01-01T00:00:00+00:00",
        signals=(("recency", 0.9), ("blame", 0.5)),
    )


def _document(files: tuple[ReportFile, ...]) -> ReportDocument:
    return ReportDocument(
        head_sha="abc123def456",
        generated_at="2026-01-01T00:00:00+00:00",
        analysis_completeness=0.91,
        models=(
            ("ownership", "ownership-v2"),
            ("risk", "risk-v1"),
            ("topology", "topology-v2"),
        ),
        overview=(("unowned-file", "ok", "Every tracked file matches a rule."),),
        files=files,
        timeline=(("2026-01-01T00:00:00+00:00", 0.4),),
        comparisons=(),
        clusters=(("inferred-1", False, ()),),
        teams_reason="Declared teams were not available.",
        ownership_coverage=1.0,
        critical_components=0,
        drift_count=0,
        finding="src/pay.py is low: concentrated",
    )


def _file(path: str, handle: str = _EMAIL) -> ReportFile:
    return ReportFile(
        path=path,
        change_events=2,
        risk="low",
        rule=f"{path} {handle}",
        owners=(_owner(handle),),
    )


def test_report_command_is_offline(
    git_repo: GitRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_repo.commit_files(
        {"src/pay.py": "amount = 1\n", "CODEOWNERS": "* @alice\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T00:00:00+00:00",
    )
    monkeypatch.chdir(git_repo.path)
    destination = tmp_path / "checkowners.html"
    result = CliRunner().invoke(app, ["report", "--html", str(destination)])
    assert result.exit_code == 0, result.output
    html = destination.read_text(encoding="utf-8")
    assert "http://" not in html
    assert "https://" not in html
    assert 'id="overview"' in html
    assert 'id="treemap"' in html
    assert 'id="ownership-map"' in html
    assert 'id="timeline"' in html
    assert 'id="team-alignment"' in html
    assert 'id="drilldown"' in html
    assert "evidence_quality" in html
    assert "ownership_score" in html
    assert "historical-v1" in html
    assert KNOWLEDGE_RISK_NOTICE in html
    assert LIMITATIONS_SUMMARY in html
    assert REMOVAL_PATH in html
    assert "Declared teams were not available." in html


def test_anonymized_report_hides_identity() -> None:
    document = _document((_file("src/pay.py"),))
    config = replace(Config(), output=replace(Config().output, anonymize=True))
    html = render_report(document, config, "fixture")
    assert _EMAIL not in html
    assert "ownership_score" in html
    assert "evidence_quality" in html
    assert "abc123def456" in html
    assert "0.9100" in html


def test_aggregate_report_omits_identity() -> None:
    document = _document((_file("src/pay.py"),))
    config = replace(Config(), output=replace(Config().output, aggregate_only=True))
    html = render_report(document, config, "fixture")
    assert _EMAIL not in html
    assert "Identities are omitted in this privacy mode." in html


def test_report_bytes_repeat_and_stay_within_budget() -> None:
    files = tuple(
        _file(f"dir{index // 100}/file{index}.py", f"p{index}@example.com")
        for index in range(12000)
    )
    document = _document(files)
    config = Config()
    first = render_report(document, config, "fixture")
    second = render_report(document, config, "fixture")
    assert first == second
    assert len(first.encode("utf-8")) <= HTML_SIZE_BUDGET


def test_report_command_shows_fixture_provenance(
    git_repo: GitRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_repo.commit_files(
        {"README.md": "hello\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T00:00:00+00:00",
    )
    monkeypatch.chdir(git_repo.path)
    destination = tmp_path / "nested" / "report.html"
    result = CliRunner().invoke(app, ["report", "--html", str(destination)])
    assert result.exit_code == 0, result.output
    html = destination.read_text(encoding="utf-8")
    assert "head-sha" in html
    assert "analysis-completeness" in html
    assert "ownership-v2" in html

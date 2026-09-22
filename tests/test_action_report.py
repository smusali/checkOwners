from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from checkowners.action_report import (
    DIAGNOSTIC,
    SOLO_LINE,
    _completeness_lines,
    _write_multiline_output,
    build,
    entry_lines,
    fmt_delta,
    has_actionable_findings,
    has_actionable_knowledge_risk,
    knowledge_risk_lines,
    load,
    md_cell,
    publish_outputs,
    qualified_humans,
    summarize_balance,
    summarize_bus_factor,
    summarize_decay,
    summarize_drift,
    write_step_summary,
)
from checkowners.privacy import KNOWLEDGE_RISK_NOTICE

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(_TOOLS))

from comment_on_pr import MARKER, _existing_id  # noqa: E402


def test_completeness_lines_skip_unreadable_gaps() -> None:
    assert _completeness_lines({}) == []
    assert _completeness_lines({"analysis_completeness": True}) == []
    assert _completeness_lines({"analysis_completeness": 1}) == ["analysis completeness: 100%"]
    lines = _completeness_lines(
        {
            "analysis_completeness": 0.5,
            "analysis_gaps": [
                "skip",
                {"code": "missing_mailmap"},
                {"reason": ""},
                {"reason": "Missing .mailmap."},
            ],
        }
    )
    assert lines == ["analysis completeness: 50%", "Missing .mailmap."]


def test_md_cell_escapes_backticks_pipes_newlines_and_html() -> None:
    assert md_cell("foo`bar") == "foo'bar"
    assert md_cell("a|b") == "a/b"
    assert md_cell("a\nb") == "a b"
    assert md_cell("a\r\nb") == "a b"
    assert md_cell("<script>") == "(script)"
    long = "x" * 100
    shown = md_cell(long, 10)
    assert shown.endswith("…")
    assert len(shown) == 10


def test_build_overflow_points_at_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAX_OUTPUT_ENTRIES", "2")
    monkeypatch.setenv("CHECKOWNERS_ARTIFACT_NAME", "checkowners-reports")
    drift = {
        "drift_detected": True,
        "notes": ["alpha", "beta", "gamma"],
        "stale": [
            {"path": "a.py", "confidence_delta": 0.1, "reason": "old"},
            {"path": "b.py", "confidence_delta": 0.2},
            {"path": "c.py", "confidence_delta": 0.3},
        ],
        "missing": [],
        "changed": [],
    }
    (tmp_path / "drift.json").write_text(json.dumps(drift), encoding="utf-8")
    text = build()
    assert "note: alpha" in text
    assert "note: beta" in text
    assert "note: gamma" not in text
    assert "`a.py`" in text
    assert "`b.py`" in text
    assert "`c.py`" not in text
    assert "and 2 more. Full report is in the checkowners-reports artifact." in text
    assert KNOWLEDGE_RISK_NOTICE in text


def test_action_report_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load("missing.json") is None
    (tmp_path / "not-object.json").write_text("[1]", encoding="utf-8")
    assert load("not-object.json") is None
    assert fmt_delta(None) == "0.00"
    assert fmt_delta("x") == "0.00"
    assert fmt_delta("1.5") == "1.50"
    assert qualified_humans(None) == []
    assert summarize_bus_factor({"entries": "nope"}, 1)["counts"]["entries"] == 0
    assert summarize_decay({"reports": "nope"}, 1)["counts"]["reports"] == 0
    assert has_actionable_findings(None, None, None) is False
    assert has_actionable_findings({"drift_detected": True}, None, None) is True
    assert has_actionable_findings({"drift_detected": False}, None, None) is False
    assert has_actionable_knowledge_risk(None, {"reports": [{"path": "a.py"}]}) is False
    assert (
        has_actionable_knowledge_risk(
            {"entries": [{"contributors_above_threshold": ["@only"]}]},
            {"reports": [{"path": "a.py"}]},
        )
        is False
    )
    lines, _cut = entry_lines([("stale", {"path": 1})])
    assert lines == []
    assert has_actionable_knowledge_risk(
        {
            "entries": [
                {
                    "path": "a.py",
                    "contributors_above_threshold": ["@a", "@b"],
                    "recommended_backups": [],
                },
                {
                    "path": "b.py",
                    "contributors_above_threshold": ["@a"],
                    "recommended_backups": ["@b"],
                },
            ]
        },
        None,
    )
    solo = {
        "entries": [
            {"path": "only.py", "contributors_above_threshold": ["alice"]},
        ]
    }
    assert knowledge_risk_lines(solo, None) == ([SOLO_LINE], False)
    assert knowledge_risk_lines(None, None) == ([], False)
    risk, _cut = knowledge_risk_lines(
        {
            "entries": [
                {
                    "path": "shared.py",
                    "contributors_above_threshold": ["@owner", "@other"],
                    "recommended_backups": [],
                },
                {
                    "path": "solo.py",
                    "contributors_above_threshold": ["alice"],
                    "recommended_backups": [],
                },
                {
                    "path": "",
                    "contributors_above_threshold": ["@owner"],
                },
            ]
        },
        {
            "reports": [
                {"handle": "@x", "path": "d.py", "days_since_last_commit": "n/a"},
                {"handle": "@y", "path": "e.py"},
            ]
        },
    )
    assert "No candidate backup reviewers." in risk
    assert "Only @alice" in "\n".join(risk)
    assert "a while ago" in "\n".join(risk)
    many = {
        "entries": [
            {
                "path": f"p{index}.py",
                "contributors_above_threshold": ["@one", "@two"],
                "recommended_backups": [],
            }
            for index in range(2)
        ]
        + [
            {
                "path": f"s{index}.py",
                "contributors_above_threshold": ["@one"],
                "recommended_backups": ["bot[bot]"],
            }
            for index in range(10)
        ]
    }
    extra_lines, extra_cut = knowledge_risk_lines(many, None)
    assert extra_cut is False
    assert any("more single-owner paths" in line for line in extra_lines)
    summary = summarize_bus_factor(
        {"entries": [{"tier": "other"}], "qualified_owner_count_cap": True},
        2,
    )
    assert summary["qualified_owner_count_cap"] == 3
    assert summary["critical_paths"] == []
    (tmp_path / "drift.json").write_text(
        json.dumps(
            {
                "drift_detected": False,
                "stale": "nope",
                "notes": 1,
                "analysis_completeness": 0.73,
                "analysis_gaps": [{"code": "missing_mailmap", "reason": "Missing .mailmap."}],
            }
        ),
        encoding="utf-8",
    )
    summary = build(limit=5)
    assert "No drift detected." in summary
    assert "analysis completeness: 73%" in summary
    assert "Missing .mailmap." in summary
    assert "Baselined: 0. Suppressed: 0. Stale baseline: 0." in build(limit=5)
    (tmp_path / "bus_factor.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "path": "shared.py",
                        "contributors_above_threshold": ["@a", "@b"],
                    },
                    {
                        "path": "x" * 90,
                        "contributors_above_threshold": ["@a"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "decay.json").write_text(
        json.dumps(
            {
                "reports": [{"path": "skip-me"}]
                + [
                    {
                        "handle": f"@d{index}",
                        "path": f"d{index}.py",
                        "days_since_last_commit": 1,
                    }
                    for index in range(10)
                ]
            }
        ),
        encoding="utf-8",
    )
    risk_text = build()
    assert "### Knowledge risk" in risk_text
    assert "more continuity-risk warnings." in risk_text
    assert "Full report is in the checkowners-reports artifact." in risk_text
    assert KNOWLEDGE_RISK_NOTICE in risk_text
    (tmp_path / "drift.json").unlink()
    assert build() == DIAGNOSTIC
    assert KNOWLEDGE_RISK_NOTICE in DIAGNOSTIC
    write_step_summary("plain")
    assert (tmp_path / "checkowners-report.md").read_text(encoding="utf-8") == "plain"
    (tmp_path / "checkowners-report.md").unlink()
    (tmp_path / "checkowners-report.md").mkdir()
    write_step_summary("ignored")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    publish_outputs(limit=1)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    hexes = iter(["aaa", "bbb"])
    with patch(
        "checkowners.action_report.secrets.token_hex",
        side_effect=lambda _n: next(hexes),
    ):
        _write_multiline_output("name", "hello\nghadelim_aaa\nworld")
    written = (tmp_path / "out").read_text(encoding="utf-8")
    assert "ghadelim_bbb" in written
    assert "ghadelim_aaa\nworld" in written
    monkeypatch.setenv("MAX_OUTPUT_ENTRIES", "nope")
    with pytest.raises(SystemExit, match="positive integer"):
        publish_outputs()
    monkeypatch.setenv("MAX_OUTPUT_ENTRIES", "0")
    with pytest.raises(SystemExit, match="positive integer"):
        publish_outputs()


def _json_resp(payload: object) -> MagicMock:
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode("utf-8")
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_existing_id_finds_marker_on_second_page() -> None:
    page1 = [{"id": index, "body": f"other {index}"} for index in range(100)]
    page2 = [{"id": 4242, "body": f"{MARKER}\nreport"}]

    def side_effect(req: urllib.request.Request, timeout: int = 30) -> MagicMock:  # noqa: ARG001
        if "page=2" in req.full_url:
            return _json_resp(page2)
        return _json_resp(page1)

    with patch("comment_on_pr.urllib.request.urlopen", side_effect=side_effect) as mocked:
        found = _existing_id("token", "owner", "repo", "7")
    assert found == 4242
    assert mocked.call_count == 2
    assert "page=2" in mocked.call_args_list[1][0][0].full_url


def test_summaries_include_ratchet_counts() -> None:
    drift = summarize_drift(
        {
            "missing": [{"path": "a.py"}],
            "stale": [],
            "changed": [],
            "notes": [],
            "baselined": 4,
            "suppressed": 2,
            "stale_baseline": [{"rule": "missing", "path": "gone.py", "owners": []}],
        },
        50,
    )
    assert drift["counts"]["baselined"] == 4
    assert drift["counts"]["suppressed"] == 2
    assert drift["counts"]["stale_baseline"] == 1
    bus = summarize_bus_factor(
        {"entries": [], "baselined": 4, "suppressed": 2, "stale_baseline": []},
        50,
    )
    assert bus["counts"]["baselined"] == 4
    assert bus["counts"]["suppressed"] == 2
    assert bus["counts"]["stale_baseline"] == 0


def test_summarize_balance_trims_each_list() -> None:
    loads = [{"handle": "@a", "reviews": 1}, {"handle": "@b", "reviews": 2}]
    overloaded = [{"handle": "@a", "reviews": 9}]
    suggestions = [{"overloaded": "@a", "candidate": "@b"}]
    all_trimmed = summarize_balance(
        {"loads": loads, "overloaded": overloaded, "suggestions": suggestions},
        1,
    )
    assert all_trimmed["truncated"] is True
    assert all_trimmed["counts"] == {"loads": 2, "overloaded": 1, "suggestions": 1}
    suggestions_only = summarize_balance({"suggestions": suggestions}, 0)
    assert suggestions_only["truncated"] is True
    assert suggestions_only["loads"] == []
    none_trimmed = summarize_balance({"loads": loads[:1]}, 5)
    assert none_trimmed["truncated"] is False
    overloaded_only = summarize_balance(
        {"loads": loads[:1], "overloaded": [*overloaded, *overloaded]},
        1,
    )
    assert overloaded_only["truncated"] is True


def test_publish_outputs_writes_balance_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    (tmp_path / "balance.json").write_text(
        json.dumps({"loads": [{"handle": "@a", "reviews": 3}], "average": 3}),
        encoding="utf-8",
    )
    publish_outputs(limit=1)
    assert "balance_summary" in output.read_text(encoding="utf-8")


def test_balance_summary_is_absent_without_balance_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    publish_outputs(limit=1)
    assert "balance_summary" not in output.read_text(encoding="utf-8")


def test_existing_id_returns_none_after_short_page() -> None:
    page = [{"id": 1, "body": "nope"}]

    def side_effect(req: urllib.request.Request, timeout: int = 30) -> MagicMock:  # noqa: ARG001
        return _json_resp(page)

    with patch("comment_on_pr.urllib.request.urlopen", side_effect=side_effect) as mocked:
        found = _existing_id("token", "owner", "repo", "7")
    assert found is None
    assert mocked.call_count == 1

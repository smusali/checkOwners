"""The report the composite Action writes for a run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from checkowners.action_report import (
    build,
    entry_lines,
    knowledge_risk_lines,
    md_cell,
    publish_outputs,
    summarize_balance,
    summarize_bus_factor,
    summarize_drift,
    write_step_summary,
)
from checkowners.privacy import KNOWLEDGE_RISK_NOTICE
from tests.conftest import Payload


def test_completeness_lines_skip_unreadable_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("drift.json").write_text("{}", encoding="utf-8")
    assert "analysis completeness" not in build()
    Path("drift.json").write_text(json.dumps({"analysis_completeness": True}), encoding="utf-8")
    assert "analysis completeness" not in build()
    Path("drift.json").write_text(json.dumps({"analysis_completeness": 1}), encoding="utf-8")
    assert "analysis completeness: 100%" in build()
    Path("drift.json").write_text(
        json.dumps(
            {
                "analysis_completeness": 0.5,
                "analysis_gaps": [
                    "skip",
                    {"code": "missing_mailmap"},
                    {"reason": ""},
                    {"reason": "Missing .mailmap."},
                ],
            }
        ),
        encoding="utf-8",
    )
    text = build()
    assert "analysis completeness: 50%" in text
    assert "Missing .mailmap." in text


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
    drift_counts = Payload(drift).child("counts")
    assert drift_counts.whole("baselined") == 4
    assert drift_counts.whole("suppressed") == 2
    assert drift_counts.whole("stale_baseline") == 1
    bus = summarize_bus_factor(
        {"entries": [], "baselined": 4, "suppressed": 2, "stale_baseline": []},
        50,
    )
    bus_counts = Payload(bus).child("counts")
    assert bus_counts.whole("baselined") == 4
    assert bus_counts.whole("suppressed") == 2
    assert bus_counts.whole("stale_baseline") == 0


def test_summarize_balance_trims_each_list() -> None:
    loads = [{"handle": "@a", "reviews": 1}, {"handle": "@b", "reviews": 2}]
    overloaded = [{"handle": "@a", "reviews": 9}]
    suggestions = [{"overloaded": "@a", "candidate": "@b"}]
    all_trimmed = summarize_balance(
        {"loads": loads, "overloaded": overloaded, "suggestions": suggestions},
        1,
    )
    assert all_trimmed["truncated"] is True
    assert all_trimmed["counts"] == {
        "loads": 2,
        "overloaded": 1,
        "suggestions": 1,
        "review_concentration": 0,
        "divergence": 0,
    }
    assert "load_signal" in all_trimmed
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
    concentration = [{"path": "a.py", "share": 0.8}, {"path": "b.py", "share": 0.9}]
    divergence = [{"path": "a.py", "writer": "@a"}, {"path": "b.py", "writer": "@b"}]
    trimmed = summarize_balance(
        {
            "load_signal": "github_completed_reviews",
            "review_concentration": concentration,
            "divergence": divergence,
        },
        1,
    )
    assert trimmed["truncated"] is True
    assert trimmed["review_concentration"] == concentration[:1]
    assert trimmed["divergence"] == divergence[:1]
    trimmed_counts = Payload(trimmed).child("counts")
    assert trimmed_counts.whole("review_concentration") == 2
    assert trimmed_counts.whole("divergence") == 2


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


def test_build_lists_reviewers_when_load_is_real(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "drift.json").write_text(
        json.dumps(
            {
                "drift_detected": False,
                "notes": [],
                "stale": [],
                "missing": [],
                "changed": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "balance.json").write_text(
        json.dumps(
            {
                "load_signal": "github_completed_reviews",
                "loads": [
                    {"handle": "@alice", "load_band": "high"},
                    {"handle": "@bob", "load_band": "low"},
                ],
            }
        ),
        encoding="utf-8",
    )
    text = build()
    assert "### Recommended reviewers" in text
    assert "`@bob` (low)" in text
    assert "@alice" not in text
    (tmp_path / "balance.json").write_text(
        json.dumps({"load_signal": "git_authorship_proxy", "loads": []}),
        encoding="utf-8",
    )
    assert "### Recommended reviewers" not in build()


def test_reviewer_lines_skip_unusable_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "drift.json").write_text(
        json.dumps(
            {
                "drift_detected": False,
                "notes": [],
                "stale": [],
                "missing": [],
                "changed": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "balance.json").write_text(
        json.dumps({"load_signal": "github_completed_reviews", "loads": "nope"}),
        encoding="utf-8",
    )
    assert "### Recommended reviewers" not in build()
    (tmp_path / "balance.json").write_text(
        json.dumps(
            {
                "load_signal": "external",
                "loads": [
                    "nope",
                    {"handle": "", "load_band": "low"},
                    {"handle": 1, "load_band": "low"},
                    {"handle": "@high", "load_band": "high"},
                    {"handle": "@a", "load_band": "low"},
                    {"handle": "@b", "load_band": "typical"},
                ],
            }
        ),
        encoding="utf-8",
    )
    text = build(limit=1)
    assert "`@a` (low)" in text
    assert "@b" not in text
    (tmp_path / "balance.json").write_text(
        json.dumps(
            {
                "load_signal": "github_completed_reviews",
                "loads": [{"handle": "@high", "load_band": "high"}],
            }
        ),
        encoding="utf-8",
    )
    assert "### Recommended reviewers" not in build()
    lines, _cut = entry_lines(
        [
            (
                "stale",
                {
                    "path": "a.py",
                    "declared": "nope",
                    "observed": {"owners": [1, "", "@a"], "teams": "no"},
                },
            ),
            ("changed", {"path": "b.py", "reason": "because", "confidence_delta": 0.2}),
            ("missing", {"path": "c.py", "declared": {"teams": ["@t"]}}),
        ]
    )
    rendered = "\n".join(lines)
    assert "Observed: @a" in rendered
    assert "Declared: @t" in rendered
    assert "because" in rendered


def test_decay_lines_name_a_former_contributor() -> None:
    lines, _truncated = knowledge_risk_lines(
        {
            "entries": [
                {"path": "a.py", "contributors_above_threshold": ["@ada"]},
                {"path": "b.py", "contributors_above_threshold": ["@bob"]},
            ]
        },
        {
            "reports": [
                {
                    "handle": "alice@example.com",
                    "path": "src/a.py",
                    "days_since_last_commit": 3,
                    "contributor_status": "former",
                }
            ]
        },
    )
    assert any("former contributor" in line for line in lines)


def test_step_summary_ignores_os_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "checkowners-report.md").mkdir()
    write_step_summary("hi")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "checkowners-report.md"))
    write_step_summary("hi")

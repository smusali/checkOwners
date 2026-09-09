from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(_TOOLS))

from comment_on_pr import MARKER, _existing_id  # noqa: E402
from render_job_summary import build, md_cell  # noqa: E402


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


def _json_resp(payload: object) -> MagicMock:
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode("utf-8")
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_existing_id_finds_marker_on_second_page() -> None:
    page1 = [{"id": index, "body": f"other {index}"} for index in range(100)]
    page2 = [{"id": 4242, "body": f"{MARKER}\nreport"}]

    def side_effect(req: urllib.request.Request, timeout: int = 30) -> MagicMock:
        if "page=2" in req.full_url:
            return _json_resp(page2)
        return _json_resp(page1)

    with patch("comment_on_pr.urllib.request.urlopen", side_effect=side_effect) as mocked:
        found = _existing_id("token", "owner", "repo", "7")
    assert found == 4242
    assert mocked.call_count == 2
    assert "page=2" in mocked.call_args_list[1][0][0].full_url


def test_existing_id_returns_none_after_short_page() -> None:
    page = [{"id": 1, "body": "nope"}]

    def side_effect(req: urllib.request.Request, timeout: int = 30) -> MagicMock:
        return _json_resp(page)

    with patch("comment_on_pr.urllib.request.urlopen", side_effect=side_effect) as mocked:
        found = _existing_id("token", "owner", "repo", "7")
    assert found is None
    assert mocked.call_count == 1

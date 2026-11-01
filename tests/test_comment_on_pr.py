"""Tests for the pull-request comment tool. Not part of the checkowners package."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

_SPEC = importlib.util.spec_from_file_location(
    "comment_on_pr",
    Path(__file__).resolve().parents[1] / "tools" / "comment_on_pr.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
comment_on_pr = importlib.util.module_from_spec(_SPEC)
sys.modules["comment_on_pr"] = comment_on_pr
_SPEC.loader.exec_module(comment_on_pr)
MARKER = comment_on_pr.MARKER
_existing_id = comment_on_pr._existing_id


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


def test_existing_id_returns_none_after_short_page() -> None:
    page = [{"id": 1, "body": "nope"}]

    def side_effect(req: urllib.request.Request, timeout: int = 30) -> MagicMock:  # noqa: ARG001
        return _json_resp(page)

    with patch("comment_on_pr.urllib.request.urlopen", side_effect=side_effect) as mocked:
        found = _existing_id("token", "owner", "repo", "7")
    assert found is None
    assert mocked.call_count == 1

"""Tests for checkowners.github module."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from checkowners.analyze import Contribution, review_window_start, score_owners
from checkowners.github import (
    _REVIEW_PAGE_SIZE,
    PullRequestFilesError,
    ReconciliationError,
    _graphql,
    _GraphqlResult,
    _mapping,
    _pull_ref,
    _require_mapping,
    _require_sha,
    _span_days,
    begin_api_budget,
    build_review_coverage,
    clear_api_budget,
    clear_api_evidence,
    collection_gaps,
    collection_timestamp,
    create_team_resolver,
    external_evidence_payload,
    get_github_client,
    get_github_token,
    github_repository,
    note_api_call,
    note_collection_gap,
    open_or_update_reconciliation,
    publish_check_run,
    pull_request_base,
    pull_request_paths,
    resolve_handles,
    resolve_noreply_handle,
    review_was_omitted,
    review_window,
    set_offline,
    team_snapshot_hash,
)
from checkowners.models import (
    API_BUDGET_REASON,
    API_RATE_LIMIT_REASON,
    SEARCH_RATE_LIMIT_REASON,
    AnalysisConfig,
    AnalysisGap,
    Config,
    ScoringConfig,
    completeness_score,
)
from checkowners.privacy import email_token
from checkowners.state import read_handle_cache, write_handle_cache


def test_get_github_token_present() -> None:
    with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"}):
        assert get_github_token() == "ghp_test123"


def test_get_github_token_missing() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert get_github_token() == ""


_AS_OF = datetime(2026, 10, 24, tzinfo=UTC)
_SINCE = _AS_OF - timedelta(days=365)
_HANDLES = {"alice@example.com": "@alice", "bob@example.com": "@bob"}


def _stamp(days_ago: int) -> str:
    return (_AS_OF - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _pull_node(
    number: int,
    updated_days: int,
    closed_days: int,
    reviewers: list[str],
    paths: list[str],
    *,
    files_cursor: str | None = None,
    reviews_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "updatedAt": _stamp(updated_days),
        "closedAt": _stamp(closed_days),
        "reviews": {
            "pageInfo": {
                "hasNextPage": reviews_cursor is not None,
                "endCursor": reviews_cursor,
            },
            "nodes": [{"author": {"login": login}} for login in reviewers],
        },
        "files": {
            "pageInfo": {"hasNextPage": files_cursor is not None, "endCursor": files_cursor},
            "nodes": [{"path": path} for path in paths],
        },
    }


def _page(nodes: list[dict[str, object]], *, has_next: bool, cursor: str | None) -> _GraphqlResult:
    return _GraphqlResult(
        {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        },
        False,
    )


def _paged(
    nodes: list[dict[str, object]],
    calls: list[str],
) -> Callable[[str, str, Mapping[str, object]], _GraphqlResult]:
    def respond(_token: str, query: str, variables: Mapping[str, object]) -> _GraphqlResult:
        calls.append(query)
        raw_cursor = variables.get("cursor")
        index = int(raw_cursor) if isinstance(raw_cursor, str) and raw_cursor.isdigit() else 0
        start = index * _REVIEW_PAGE_SIZE
        chunk = nodes[start : start + _REVIEW_PAGE_SIZE]
        has_next = start + _REVIEW_PAGE_SIZE < len(nodes)
        return _page(chunk, has_next=has_next, cursor=str(index + 1) if has_next else None)

    return respond


def _collect(
    emails: set[str],
    query: Callable[[str, str, Mapping[str, object]], _GraphqlResult],
    *,
    handles: dict[str, str] | None = None,
    since: datetime | None = _SINCE,
) -> dict[str, dict[str, float]]:
    with (
        patch("checkowners.github.get_github_client", return_value=MagicMock()),
        patch("checkowners.github.resolve_handles", return_value=handles or {}),
        patch("checkowners.github._graphql", side_effect=query),
    ):
        return build_review_coverage("tok", "org/repo", emails, as_of=_AS_OF, since=since)


def _review_scores(coverage: dict[str, dict[str, float]]) -> dict[str, float]:
    owners = score_owners(
        {
            "alice@example.com": Contribution(commits=1, last_commit=_AS_OF, credit=1.0),
            "bob@example.com": Contribution(commits=1, last_commit=_AS_OF, credit=1.0),
        },
        {},
        coverage["a.py"],
        max_commits=1.0,
        scoring=ScoringConfig(),
        now=_AS_OF,
        blame_available=False,
        review_available=True,
    )
    scores: dict[str, float] = {}
    for owner in owners:
        breakdown = owner.score_breakdown
        if breakdown is not None:
            scores[owner.handle] = breakdown.review.score
    return scores


def _window_pulls() -> list[dict[str, object]]:
    return [
        _pull_node(1, 100, 100, ["alice"], ["a.py"]),
        _pull_node(2, 200, 200, ["alice"], ["a.py"]),
        _pull_node(3, 300, 300, ["bob"], ["a.py"]),
    ]


def test_build_review_coverage_maps_logins_to_emails() -> None:
    calls: list[str] = []

    def respond(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        calls.append(query)
        if "pullRequests(" in query:
            return _page(
                [
                    _pull_node(1, 10, 10, ["alice", "bob"], ["src/main.py"]),
                    _pull_node(2, 20, 20, ["alice"], ["src/main.py"], files_cursor="more"),
                ],
                has_next=False,
                cursor=None,
            )
        return _GraphqlResult(
            {
                "repository": {
                    "p0": {
                        "files": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"path": "src/util.py"}],
                        }
                    }
                }
            },
            False,
        )

    coverage = _collect(
        {"alice@example.com", "bob@example.com"},
        respond,
        handles=_HANDLES,
    )
    assert coverage["src/main.py"]["alice@example.com"] == 2 / 3
    assert coverage["src/main.py"]["bob@example.com"] == 1 / 3
    assert coverage["src/util.py"]["alice@example.com"] == 1.0
    assert len(calls) == 2


def test_build_review_coverage_omits_when_the_review_query_is_rate_limited() -> None:
    clear_api_evidence()

    def respond(_token: str, _query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        return _GraphqlResult(None, True)

    coverage = _collect({"a@example.com"}, respond, handles={"a@example.com": "@a"})
    assert coverage == {}
    assert review_was_omitted()
    assert any(gap.reason == API_RATE_LIMIT_REASON for gap in collection_gaps())
    assert review_window().sampled == 0
    clear_api_evidence()


def test_review_window_truncation_warns_and_lowers_completeness() -> None:
    clear_api_evidence()
    begin_api_budget(1)

    def respond(_token: str, _query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        return _page([_pull_node(1, 1, 1, ["a"], ["a.py"])], has_next=True, cursor="next")

    coverage = _collect({"a@example.com"}, respond, handles={"a@example.com": "@a"})
    assert coverage["a.py"]["a@example.com"] == 1.0
    sample = review_window()
    assert sample.sampled == 1
    assert sample.window_days < 365
    assert any(gap.reason.startswith("Review window truncated:") for gap in collection_gaps())
    assert completeness_score(collection_gaps()) < completeness_score(())
    assert review_was_omitted() is False
    clear_api_evidence()


def test_resolve_handles_omits_when_search_budget_is_exhausted() -> None:
    clear_api_evidence()
    client = MagicMock()
    client.get_rate_limit.return_value.search.remaining = 0
    with (
        patch("checkowners.github.get_github_client", return_value=client),
        patch("checkowners.github.read_handle_cache", return_value={}),
    ):
        result = resolve_handles({"budget@example.com"}, "ghp_test")
    assert result == {}
    assert client.search_users.called is False
    assert any(gap.reason == SEARCH_RATE_LIMIT_REASON for gap in collection_gaps())
    clear_api_evidence()


def test_collection_gap_keeps_the_first_reason_until_replaced() -> None:
    clear_api_evidence()
    note_collection_gap(AnalysisGap("api_rate_limit", "first"))
    note_collection_gap(AnalysisGap("api_rate_limit", "second"))
    assert collection_gaps()[0].reason == "first"
    note_collection_gap(AnalysisGap("api_rate_limit", "third"), replace=True)
    assert collection_gaps()[0].reason == "third"
    clear_api_evidence()


def test_resolve_handles_stops_when_the_request_budget_is_already_spent() -> None:
    clear_api_evidence()
    begin_api_budget(0)
    client = MagicMock()
    with (
        patch("checkowners.github.get_github_client", return_value=client),
        patch("checkowners.github.read_handle_cache", return_value={}),
    ):
        result = resolve_handles({"spent@example.com"}, "ghp_test")
    assert result == {}
    assert client.get_rate_limit.called is False
    assert any(gap.reason == API_BUDGET_REASON for gap in collection_gaps())
    clear_api_evidence()


def test_resolve_handles_stops_mid_lookup_when_the_request_budget_is_spent() -> None:
    clear_api_evidence()
    begin_api_budget(1)
    client = MagicMock()
    client.get_rate_limit.return_value.search.remaining = 5
    with (
        patch("checkowners.github.get_github_client", return_value=client),
        patch("checkowners.github.read_handle_cache", return_value={}),
    ):
        result = resolve_handles({"one@example.com", "two@example.com"}, "ghp_test")
    assert result == {}
    assert client.search_users.called is False
    assert any(gap.reason == API_BUDGET_REASON for gap in collection_gaps())
    clear_api_evidence()


def test_search_rate_limit_failure_still_looks_up_handles() -> None:
    clear_api_evidence()
    client = MagicMock()
    client.get_rate_limit.side_effect = RuntimeError("down")
    user = MagicMock()
    user.login = "ada"
    client.search_users.return_value = [user]
    with (
        patch("checkowners.github.get_github_client", return_value=client),
        patch("checkowners.github.read_handle_cache", return_value={}),
    ):
        result = resolve_handles({"ada@example.com"}, "ghp_test")
    assert result == {"ada@example.com": "@ada"}
    clear_api_evidence()


def test_identical_review_behavior_matches_across_velocity() -> None:
    clear_api_evidence()
    slow_calls: list[str] = []
    fast_calls: list[str] = []
    outside = [_pull_node(1000 + index, 1, 400, ["bob"], ["a.py"]) for index in range(200)]
    fast_nodes = [*outside, *_window_pulls(), _pull_node(9, 400, 400, ["carol"], ["a.py"])]
    slow = _collect(
        {"alice@example.com", "bob@example.com"},
        _paged(_window_pulls(), slow_calls),
        handles=_HANDLES,
    )
    fast = _collect(
        {"alice@example.com", "bob@example.com"},
        _paged(fast_nodes, fast_calls),
        handles=_HANDLES,
    )
    assert slow["a.py"] == fast["a.py"]
    assert _review_scores(slow) == _review_scores(fast)
    assert review_window().sampled == 3
    assert review_window().window_days == 300
    rest_calls = 2 * (len(outside) + len(_window_pulls()))
    assert len(fast_calls) == (len(fast_nodes) + _REVIEW_PAGE_SIZE - 1) // _REVIEW_PAGE_SIZE
    assert len(fast_calls) < rest_calls
    assert len(slow_calls) == 1
    clear_api_evidence()


def test_review_without_a_user_does_not_page_files() -> None:
    clear_api_evidence()
    calls: list[str] = []

    def respond(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        calls.append(query)
        node: dict[str, object] = {
            "number": 1,
            "updatedAt": _stamp(1),
            "closedAt": _stamp(1),
            "reviews": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"author": None}],
            },
            "files": {
                "pageInfo": {"hasNextPage": True, "endCursor": "more"},
                "nodes": [{"path": "a.py"}],
            },
        }
        return _page([node], has_next=False, cursor=None)

    coverage = _collect({"a@example.com"}, respond, handles={"a@example.com": "@a"})
    assert coverage == {}
    assert len(calls) == 1
    clear_api_evidence()


def test_api_request_budget_stops_before_the_review_query() -> None:
    clear_api_evidence()
    begin_api_budget(0)
    calls: list[str] = []
    coverage = _collect({"a@example.com"}, _paged([], calls), handles={"a@example.com": "@a"})
    assert coverage == {}
    assert calls == []
    assert review_was_omitted()
    assert any(gap.reason == API_BUDGET_REASON for gap in collection_gaps())
    clear_api_evidence()


def _graphql_response(body: bytes, remaining: object = "8") -> MagicMock:
    response = MagicMock()
    response.read.return_value = body
    response.headers.get.return_value = remaining
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_review_coverage_ignores_a_repository_slug_and_a_failed_query() -> None:
    clear_api_evidence()
    with (
        patch("checkowners.github.get_github_client", return_value=MagicMock()),
        patch("checkowners.github.resolve_handles", return_value={"a@example.com": "@a"}),
    ):
        assert (
            build_review_coverage("tok", "noslug", {"a@example.com"}, as_of=_AS_OF, since=_SINCE)
            == {}
        )
    failed = _collect(
        {"a@example.com"},
        lambda _token, _query, _variables: _GraphqlResult(None, False),
        handles={"a@example.com": "@a"},
    )
    assert failed == {}
    assert review_was_omitted() is False
    missing = _collect(
        {"a@example.com"},
        lambda _token, _query, _variables: _GraphqlResult({"repository": None}, False),
        handles={"a@example.com": "@a"},
    )
    assert missing == {}
    empty = _collect(
        {"a@example.com"},
        lambda _token, _query, _variables: _page([], has_next=False, cursor=None),
        handles={"a@example.com": "@a"},
    )
    assert empty == {}
    assert review_window().sampled == 0
    assert collection_gaps() == ()
    clear_api_evidence()


def test_review_scan_keeps_closed_pulls_inside_the_window() -> None:
    future = (_AS_OF + timedelta(days=2)).isoformat().replace("+00:00", "Z")
    nodes: list[dict[str, object]] = [
        _pull_node(1, 10, 10, ["a"], ["a.py"]),
        {
            "number": True,
            "updatedAt": _stamp(3),
            "closedAt": _stamp(3),
            "reviews": {"nodes": [{"author": {"login": "a"}}, {"author": {"login": 3}}, None]},
            "files": {"nodes": [{"path": "kept.py"}, {"path": 4}, "skip"]},
        },
        {"updatedAt": "nope"},
        {"closedAt": _stamp(2)},
        {
            "number": 4,
            "updatedAt": future,
            "closedAt": future,
            "reviews": {"nodes": [{"author": {"login": "a"}}]},
            "files": {"nodes": [{"path": "later.py"}]},
        },
        {
            "number": 5,
            "updatedAt": "2026-10-01T00:00:00",
            "closedAt": "2026-10-01T00:00:00",
            "reviews": {"nodes": [{"author": {"login": "a"}}]},
            "files": {"nodes": [{"path": "naive.py"}]},
        },
        _pull_node(6, 4, 4, ["zoe"], ["other.py"]),
        {
            "number": 7,
            "updatedAt": _stamp(5),
            "reviews": {
                "pageInfo": {"hasNextPage": True, "endCursor": 5},
                "nodes": None,
            },
        },
    ]
    coverage = _collect(
        {"a@example.com"},
        lambda _token, _query, _variables: _page(nodes, has_next=False, cursor=None),
        handles={"a@example.com": "@a"},
    )
    assert set(coverage) == {"a.py", "kept.py", "naive.py"}
    assert "later.py" not in coverage
    assert "other.py" not in coverage


def test_review_follow_up_stops_when_the_budget_or_cursor_repeats() -> None:
    clear_api_evidence()
    begin_api_budget(1)
    stopped = _collect(
        {"a@example.com"},
        lambda _token, _query, _variables: _page(
            [_pull_node(1, 1, 1, ["a"], ["a.py"], files_cursor="more")],
            has_next=True,
            cursor="next",
        ),
        handles={"a@example.com": "@a"},
    )
    assert stopped["a.py"]["a@example.com"] == 1.0
    assert any(gap.reason.startswith("Review window truncated:") for gap in collection_gaps())
    clear_api_evidence()

    def repeat(_token: str, _query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        return _page([_pull_node(1, 1, 1, ["a"], ["a.py"])], has_next=True, cursor="same")

    repeated = _collect({"a@example.com"}, repeat, handles={"a@example.com": "@a"})
    assert repeated["a.py"]["a@example.com"] == 1.0
    assert any(gap.reason.startswith("Review window truncated:") for gap in collection_gaps())
    clear_api_evidence()
    missing_cursor = _collect(
        {"a@example.com"},
        lambda _token, _query, _variables: _GraphqlResult(
            {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": True, "endCursor": None},
                        "nodes": [_pull_node(2, 1, 1, ["a"], ["a.py"])],
                    }
                }
            },
            False,
        ),
        handles={"a@example.com": "@a"},
    )
    assert missing_cursor["a.py"]["a@example.com"] == 1.0
    assert any(gap.reason.startswith("Review window truncated:") for gap in collection_gaps())
    clear_api_evidence()


def test_review_follow_up_reads_reviews_and_reports_a_short_window() -> None:
    clear_api_evidence()

    def both(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        if "pullRequests(" in query:
            return _page(
                [_pull_node(1, 1, 1, ["a"], ["a.py"], files_cursor="f", reviews_cursor="r")],
                has_next=False,
                cursor=None,
            )
        return _GraphqlResult(
            {
                "repository": {
                    "p0": {
                        "reviews": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"author": {"login": "b"}}],
                        },
                        "files": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"path": "extra.py"}],
                        },
                    }
                }
            },
            False,
        )

    covered = _collect(
        {"a@example.com", "b@example.com"},
        both,
        handles={"a@example.com": "@a", "b@example.com": "@b"},
    )
    assert set(covered) == {"a.py", "extra.py"}
    clear_api_evidence()
    begin_api_budget(2)

    def limited(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        if "pullRequests(" in query:
            return _page(
                [_pull_node(2, 2, 2, ["a"], ["a.py"], reviews_cursor="r")],
                has_next=True,
                cursor="next",
            )
        return _GraphqlResult(
            {
                "repository": {
                    "p0": {
                        "reviews": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "more"},
                            "nodes": [],
                        }
                    }
                }
            },
            True,
        )

    _collect({"a@example.com"}, limited, handles={"a@example.com": "@a"})
    assert any(gap.code == "api_rate_limit" for gap in collection_gaps())
    clear_api_evidence()

    def same_cursor(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        if "pullRequests(" in query:
            return _page(
                [_pull_node(3, 1, 1, ["a"], ["a.py"], reviews_cursor="r")],
                has_next=False,
                cursor=None,
            )
        return _GraphqlResult(
            {
                "repository": {
                    "p0": {
                        "reviews": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "r"},
                            "nodes": [{"author": {"login": "a"}}],
                        }
                    }
                }
            },
            False,
        )

    assert _collect({"a@example.com"}, same_cursor, handles={"a@example.com": "@a"})
    clear_api_evidence()

    def missing_repo(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        if "pullRequests(" in query:
            return _page(
                [_pull_node(4, 1, 1, ["a"], ["a.py"], reviews_cursor="r")],
                has_next=False,
                cursor=None,
            )
        return _GraphqlResult({"repository": None}, False)

    def missing_node(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        if "pullRequests(" in query:
            return _page(
                [_pull_node(5, 1, 1, ["a"], ["a.py"], files_cursor="f")],
                has_next=False,
                cursor=None,
            )
        return _GraphqlResult({"repository": {}}, False)

    def missing_page(_token: str, query: str, _variables: Mapping[str, object]) -> _GraphqlResult:
        if "pullRequests(" in query:
            return _page(
                [_pull_node(6, 1, 1, ["a"], ["a.py"], files_cursor="f", reviews_cursor="r")],
                has_next=False,
                cursor=None,
            )
        return _GraphqlResult({"repository": {"p0": {}}}, False)

    assert _collect({"a@example.com"}, missing_repo, handles={"a@example.com": "@a"})
    assert _collect({"a@example.com"}, missing_node, handles={"a@example.com": "@a"})
    assert _collect({"a@example.com"}, missing_page, handles={"a@example.com": "@a"})
    clear_api_evidence()


def test_open_ended_review_window_names_the_truncated_span() -> None:
    clear_api_evidence()
    begin_api_budget(1)
    _collect(
        {"a@example.com"},
        lambda _token, _query, _variables: _page(
            [_pull_node(1, 2, 2, ["a"], ["a.py"])],
            has_next=True,
            cursor="next",
        ),
        handles={"a@example.com": "@a"},
        since=None,
    )
    assert any(
        gap.reason == "Review window truncated: sample covers 2 days." for gap in collection_gaps()
    )
    clear_api_evidence()


def test_review_window_start_uses_an_explicit_day_count() -> None:
    explicit = Config(analysis=AnalysisConfig(lookback_days="adaptive", review_lookback_days=14))
    assert review_window_start(explicit, _AS_OF) == _AS_OF - timedelta(days=14)
    adaptive = Config(analysis=AnalysisConfig(lookback_days="adaptive"))
    assert review_window_start(adaptive, _AS_OF) is None
    assert _span_days(_AS_OF, _AS_OF + timedelta(days=1)) == 0


def test_graphql_review_query_reports_transport_and_payload_failures() -> None:
    clear_api_evidence()
    with patch.dict(os.environ, {"GITHUB_API_URL": "ftp://example.test"}):
        assert _graphql("tok", "query {}", {}).data is None
    payload = {"data": {"repository": {"name": "app"}}}
    with patch(
        "checkowners.github.urllib.request.urlopen",
        return_value=_graphql_response(json.dumps(payload).encode(), "4"),
    ):
        loaded = _graphql("tok", "query {}", {"cursor": None})
    assert loaded.data == payload["data"]
    assert loaded.rate_limited is False
    with patch(
        "checkowners.github.urllib.request.urlopen",
        return_value=_graphql_response(json.dumps(payload).encode(), "0"),
    ):
        assert _graphql("tok", "query {}", {}).rate_limited is True
    with patch(
        "checkowners.github.urllib.request.urlopen",
        return_value=_graphql_response(json.dumps(payload).encode(), 3),
    ):
        counted = _graphql("tok", "query {}", {})
    assert counted.rate_limited is False
    limited = json.dumps({"errors": [{"type": "RATE_LIMITED"}]}).encode()
    with patch(
        "checkowners.github.urllib.request.urlopen",
        return_value=_graphql_response(limited, "9"),
    ):
        assert _graphql("tok", "query {}", {}).rate_limited is True
    message = json.dumps(
        {"errors": [1, {"message": "API rate limit exceeded"}, {"type": "OTHER"}]}
    ).encode()
    with patch(
        "checkowners.github.urllib.request.urlopen",
        return_value=_graphql_response(message, "9"),
    ):
        assert _graphql("tok", "query {}", {}).rate_limited is True
    for body in (b"[]", b"\xff", b"{"):
        with patch(
            "checkowners.github.urllib.request.urlopen",
            return_value=_graphql_response(body, "9"),
        ):
            assert _graphql("tok", "query {}", {}).data is None
    plain = json.dumps({"errors": "nope"}).encode()
    with patch(
        "checkowners.github.urllib.request.urlopen",
        return_value=_graphql_response(plain, "9"),
    ):
        assert _graphql("tok", "query {}", {}).rate_limited is False
    other = json.dumps(
        {"data": {"repository": {}}, "errors": [{"message": 1}, {"message": "nope"}]}
    ).encode()
    with patch(
        "checkowners.github.urllib.request.urlopen",
        return_value=_graphql_response(other, "9"),
    ):
        assert _graphql("tok", "query {}", {}).rate_limited is False
    with patch(
        "checkowners.github.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "https://api.github.com/graphql",
            403,
            "no",
            Message(),
            BytesIO(b"no"),
        ),
    ):
        assert _graphql("tok", "query {}", {}).rate_limited is True
    with patch(
        "checkowners.github.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "https://api.github.com/graphql",
            500,
            "no",
            Message(),
            BytesIO(b"no"),
        ),
    ):
        assert _graphql("tok", "query {}", {}).rate_limited is False
    with patch(
        "checkowners.github.urllib.request.urlopen",
        side_effect=urllib.error.URLError("down"),
    ):
        assert _graphql("tok", "query {}", {}).data is None
    with patch(
        "checkowners.github.urllib.request.urlopen",
        side_effect=TimeoutError(),
    ):
        assert _graphql("tok", "query {}", {}).data is None
    clear_api_evidence()


def test_build_review_coverage_empty_without_client() -> None:
    with patch("checkowners.github.get_github_client", return_value=None):
        coverage = build_review_coverage(
            "tok",
            "org/repo",
            {"a@example.com"},
            as_of=_AS_OF,
            since=_SINCE,
        )
        assert coverage == {}


def test_build_review_coverage_empty_when_no_handles_resolve() -> None:
    coverage = _collect({"a@example.com"}, _paged([], []))
    assert coverage == {}


def test_get_github_client_with_token() -> None:
    with patch("github.Github") as mock_cls:
        client = get_github_client("ghp_test")
    mock_cls.assert_called_once_with("ghp_test")
    assert client is not None


def test_get_github_client_empty_token() -> None:
    assert get_github_client("") is None


def test_resolve_handles_no_token() -> None:
    result = resolve_handles({"alice@example.com"}, "")
    assert result == {}


def test_resolve_handles_success() -> None:
    mock_user = MagicMock()
    mock_user.login = "alice"
    mock_client = MagicMock()
    mock_client.search_users.return_value = [mock_user]
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"alice@example.com"}, "ghp_test")
    assert result == {"alice@example.com": "@alice"}


def test_resolve_handles_not_found() -> None:
    mock_client = MagicMock()
    mock_client.search_users.return_value = []
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"unknown@example.com"}, "ghp_test")
    assert result == {}


def test_resolve_handles_api_error() -> None:
    mock_client = MagicMock()
    mock_client.search_users.side_effect = Exception("rate limit")
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"alice@example.com"}, "ghp_test")
    assert result == {}


def test_create_team_resolver_all_in_one_team() -> None:
    mock_team = MagicMock()
    mock_team.slug = "backend"
    mock_team.get_members.return_value = [
        MagicMock(login="alice"),
        MagicMock(login="bob"),
    ]
    mock_org = MagicMock()
    mock_org.get_teams.return_value = [mock_team]
    mock_client = MagicMock()
    mock_client.get_organization.return_value = mock_org

    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolver = create_team_resolver("ghp_test", "myorg")
    assert resolver is not None
    assert resolver(("@alice", "@bob")) == "@myorg/backend"


def test_create_team_resolver_no_matching_team() -> None:
    mock_team = MagicMock()
    mock_team.slug = "backend"
    mock_team.get_members.return_value = [MagicMock(login="alice")]
    mock_org = MagicMock()
    mock_org.get_teams.return_value = [mock_team]
    mock_client = MagicMock()
    mock_client.get_organization.return_value = mock_org

    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolver = create_team_resolver("ghp_test", "myorg")
    assert resolver is not None
    assert resolver(("@alice", "@carol")) is None


def test_create_team_resolver_prefers_subteam() -> None:
    parent = MagicMock()
    parent.slug = "platform"
    parent.get_members.return_value = [
        MagicMock(login="alice"),
        MagicMock(login="bob"),
        MagicMock(login="carol"),
    ]
    child = MagicMock()
    child.slug = "platform/backend"
    child.get_members.return_value = [
        MagicMock(login="alice"),
        MagicMock(login="bob"),
    ]
    mock_org = MagicMock()
    mock_org.get_teams.return_value = [parent, child]
    mock_client = MagicMock()
    mock_client.get_organization.return_value = mock_org

    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolver = create_team_resolver("ghp_test", "myorg")
    assert resolver is not None
    assert resolver(("@alice", "@bob")) == "@myorg/platform/backend"


def test_create_team_resolver_no_token() -> None:
    assert create_team_resolver("", "myorg") is None


def test_create_team_resolver_no_org() -> None:
    assert create_team_resolver("ghp_test", "") is None


def test_resolve_noreply_handle_current_form() -> None:
    assert resolve_noreply_handle("12345+octo-cat@users.noreply.github.com") == "@octo-cat"


def test_resolve_noreply_handle_legacy_form() -> None:
    assert resolve_noreply_handle("octocat@users.noreply.github.com") == "@octocat"


def test_resolve_noreply_handle_rejects_other_emails() -> None:
    assert resolve_noreply_handle("alice@example.com") is None
    assert resolve_noreply_handle("x@users.noreply.github.com.evil.com") is None


def test_resolve_handles_noreply_without_token() -> None:
    """Noreply emails resolve locally: no token, no API, no client."""
    result = resolve_handles({"99+alice@users.noreply.github.com"}, "")
    assert result == {"99+alice@users.noreply.github.com": "@alice"}


def test_resolve_handles_uses_disk_cache_before_api() -> None:
    write_handle_cache({"alice@example.com": "@alice"})
    mock_client = MagicMock()
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"alice@example.com"}, "ghp_test")
    assert result == {"alice@example.com": "@alice"}
    mock_client.search_users.assert_not_called()


def test_collection_timestamp_uses_source_date_epoch() -> None:
    with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
        assert collection_timestamp() == datetime.fromtimestamp(1700000000, tz=UTC).isoformat()


def test_collection_timestamp_ignores_invalid_source_date_epoch() -> None:
    with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "not-a-timestamp"}):
        stamp = collection_timestamp()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None


def test_external_evidence_after_api_calls() -> None:
    clear_api_evidence()
    try:
        assert external_evidence_payload("abc") == {}
        with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            note_api_call()
        payload = external_evidence_payload("abc")
        assert payload == {
            "github_evidence_collected_at": datetime.fromtimestamp(1700000000, tz=UTC).isoformat(),
            "repository_head": "abc",
        }
        note_api_call({"backend": ["bob", "alice"]})
        with_team = external_evidence_payload("abc")
        assert with_team["github_evidence_collected_at"] == payload["github_evidence_collected_at"]
        assert with_team["team_snapshot"] == team_snapshot_hash({"backend": ["alice", "bob"]})
        note_api_call()
        kept = external_evidence_payload("def")
        assert kept["team_snapshot"] == with_team["team_snapshot"]
        assert kept["repository_head"] == "def"
    finally:
        clear_api_evidence()


def test_team_fetch_failure_records_api_evidence_without_snapshot() -> None:
    clear_api_evidence()
    try:
        mock_client = MagicMock()
        mock_client.get_organization.side_effect = RuntimeError("teams unavailable")
        with patch("checkowners.github.get_github_client", return_value=mock_client):
            assert create_team_resolver("ghp_test", "myorg") is None
        payload = external_evidence_payload("head")
        assert payload["repository_head"] == "head"
        assert "github_evidence_collected_at" in payload
        assert "team_snapshot" not in payload
    finally:
        clear_api_evidence()


def test_resolve_handles_remembers_misses() -> None:
    mock_client = MagicMock()
    mock_client.search_users.return_value = []
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        resolve_handles({"ghost@example.com"}, "ghp_test")
    assert read_handle_cache()[email_token("ghost@example.com")] == ""
    # Second run: the remembered miss short-circuits the API.
    mock_client.search_users.reset_mock()
    with patch("checkowners.github.get_github_client", return_value=mock_client):
        result = resolve_handles({"ghost@example.com"}, "ghp_test")
    assert result == {}
    mock_client.search_users.assert_not_called()


class _Body:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Api:
    def __init__(self) -> None:
        self.repo: object = {"default_branch": "main"}
        self.pulls: object = []
        self.blob: object = {"sha": "blob1"}
        self.patch_status: int | None = None
        self.fail: BaseException | None = None
        self.raw: bytes | None = None

    def __call__(self, req: urllib.request.Request, timeout: int = 30) -> _Body:
        assert timeout > 0
        if self.fail is not None:
            raise self.fail
        if self.raw is not None:
            raw = self.raw
            self.raw = None
            return _Body(raw)
        method = req.get_method()
        url = req.full_url
        if url.endswith("/repos/acme/app") and method == "GET":
            return _Body(json.dumps(self.repo).encode())
        if "/git/ref/heads/main" in url and method == "GET":
            return _Body(json.dumps({"object": {"sha": "base"}}).encode())
        if "/git/commits/base" in url and method == "GET":
            return _Body(json.dumps({"sha": "base", "tree": {"sha": "tree0"}}).encode())
        if url.endswith("/git/blobs") and method == "POST":
            return _Body(json.dumps(self.blob).encode())
        if url.endswith("/git/trees") and method == "POST":
            return _Body(json.dumps({"sha": "tree1"}).encode())
        if url.endswith("/git/commits") and method == "POST":
            return _Body(json.dumps({"sha": "commit1"}).encode())
        if "/pulls?" in url and method == "GET":
            return _Body(json.dumps(self.pulls).encode())
        if "/git/refs/heads/" in url and method == "PATCH":
            if self.patch_status is not None:
                raise urllib.error.HTTPError(
                    url,
                    self.patch_status,
                    "no",
                    Message(),
                    BytesIO(b"nope"),
                )
            return _Body(json.dumps({"object": {"sha": "commit1"}}).encode())
        if url.endswith("/git/refs") and method == "POST":
            return _Body(
                json.dumps({"ref": "refs/heads/checkowners/reconcile-codeowners"}).encode()
            )
        if url.endswith("/pulls") and method == "POST":
            return _Body(
                json.dumps({"number": 9, "html_url": "https://github.com/acme/app/pull/9"}).encode()
            )
        raise AssertionError(f"{method} {url}")


def _open(api: _Api) -> dict[str, object]:
    with patch("checkowners.github.urllib.request.urlopen", side_effect=api):
        return open_or_update_reconciliation(
            token="ghs_test",
            repository="acme/app",
            path=".github/CODEOWNERS",
            content="* @alice\n",
            body="body\n",
        )


def test_github_repository_uses_origin_when_env_is_absent(tmp_path: Path) -> None:
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "checkowners.github.repository_identity",
            return_value="origin:github.com/acme/app",
        ),
    ):
        assert github_repository(tmp_path) == "acme/app"


def test_github_repository_falls_through_invalid_env_to_origin(tmp_path: Path) -> None:
    with (
        patch.dict(os.environ, {"GITHUB_REPOSITORY": "not a slug"}, clear=True),
        patch(
            "checkowners.github.repository_identity",
            return_value="origin:github.com/acme/app",
        ),
    ):
        assert github_repository(tmp_path) == "acme/app"


@pytest.mark.parametrize("slug", ["", "acme", "/app", "acme/", "acme/app/extra"])
def test_github_repository_rejects_unusable_slug(tmp_path: Path, slug: str) -> None:
    with (
        patch.dict(os.environ, {"GITHUB_REPOSITORY": slug}, clear=True),
        patch("checkowners.github.repository_identity", return_value="path:/repos/app"),
        pytest.raises(ReconciliationError, match="pull-requests: write"),
    ):
        github_repository(tmp_path)


def test_github_repository_rejects_origin_that_is_not_a_slug(tmp_path: Path) -> None:
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "checkowners.github.repository_identity",
            return_value="origin:github.com/acme/app/extra",
        ),
        pytest.raises(ReconciliationError, match="pull-requests: write"),
    ):
        github_repository(tmp_path)


def test_open_or_update_requires_a_token() -> None:
    with pytest.raises(ReconciliationError, match="pull-requests: write"):
        open_or_update_reconciliation(
            token="",
            repository="acme/app",
            path="CODEOWNERS",
            content="x\n",
            body="y\n",
        )


def test_open_or_update_refuses_offline_mode() -> None:
    set_offline(True)
    try:
        with pytest.raises(ReconciliationError, match="pull-requests: write"):
            open_or_update_reconciliation(
                token="ghs_test",
                repository="acme/app",
                path="CODEOWNERS",
                content="x\n",
                body="y\n",
            )
    finally:
        set_offline(False)


@pytest.mark.parametrize("repository", ["acme", "/app", "acme/", "acme/app/extra"])
def test_open_or_update_rejects_bad_repository(repository: str) -> None:
    with pytest.raises(ReconciliationError, match="pull-requests: write"):
        open_or_update_reconciliation(
            token="ghs_test",
            repository=repository,
            path="CODEOWNERS",
            content="x\n",
            body="y\n",
        )


def test_open_or_update_rejects_a_non_http_api_url() -> None:
    with (
        patch.dict(os.environ, {"GITHUB_API_URL": "ftp://example.com"}),
        patch("checkowners.github.urllib.request.urlopen") as urlopen,
        pytest.raises(ReconciliationError, match="Could not open a pull request"),
    ):
        open_or_update_reconciliation(
            token="ghs_test",
            repository="acme/app",
            path="CODEOWNERS",
            content="x\n",
            body="y\n",
        )
    urlopen.assert_not_called()


@pytest.mark.parametrize("repo", [{}, {"default_branch": ""}, {"default_branch": 1}])
def test_open_or_update_rejects_missing_default_branch(repo: object) -> None:
    api = _Api()
    api.repo = repo
    with pytest.raises(ReconciliationError, match="Could not open a pull request"):
        _open(api)


def test_open_or_update_skips_malformed_pulls_then_creates() -> None:
    api = _Api()
    api.pulls = [
        1,
        {"number": "bad", "html_url": "https://github.com/acme/app/pull/1"},
        {"number": 1, "html_url": None},
        {"number": 1, "html_url": ""},
    ]
    opened = _open(api)
    assert opened == {
        "number": 9,
        "url": "https://github.com/acme/app/pull/9",
        "updated": False,
    }


def test_open_or_update_rejects_a_non_list_pull_payload() -> None:
    api = _Api()
    api.pulls = {"items": []}
    with pytest.raises(ReconciliationError, match="Could not open a pull request"):
        _open(api)


def test_open_or_update_reraises_when_branch_update_is_not_missing() -> None:
    api = _Api()
    api.patch_status = 500
    with pytest.raises(ReconciliationError, match="HTTP 500") as caught:
        _open(api)
    assert caught.value.status == 500


def test_open_or_update_reports_denied_http_status() -> None:
    api = _Api()
    api.fail = urllib.error.HTTPError(
        "https://api.github.com/repos/acme/app",
        401,
        "unauthorized",
        Message(),
        BytesIO(b"no"),
    )
    with pytest.raises(ReconciliationError, match="pull-requests: write") as caught:
        _open(api)
    assert caught.value.status == 401


def test_open_or_update_reports_url_and_os_errors() -> None:
    refused = _Api()
    refused.fail = urllib.error.URLError("timed out")
    with pytest.raises(ReconciliationError, match="timed out"):
        _open(refused)
    broken = _Api()
    broken.fail = OSError("boom")
    with pytest.raises(ReconciliationError, match="boom"):
        _open(broken)


def test_open_or_update_reports_invalid_and_empty_responses() -> None:
    invalid = _Api()
    invalid.raw = b"{"
    with pytest.raises(ReconciliationError, match="Could not open a pull request"):
        _open(invalid)
    empty = _Api()
    empty.raw = b""
    with pytest.raises(ReconciliationError, match="Could not open a pull request"):
        _open(empty)


def test_reconciliation_parsers_reject_incomplete_payloads() -> None:
    with pytest.raises(ReconciliationError):
        _pull_ref({"number": "x", "html_url": "https://github.com/acme/app/pull/1"})
    with pytest.raises(ReconciliationError):
        _pull_ref({"number": 1, "html_url": None})
    with pytest.raises(ReconciliationError):
        _pull_ref({"number": 1, "html_url": ""})
    with pytest.raises(ReconciliationError):
        _require_sha({})
    with pytest.raises(ReconciliationError):
        _require_sha({"sha": ""})
    with pytest.raises(ReconciliationError):
        _require_sha({"sha": 1})
    with pytest.raises(ReconciliationError):
        _require_mapping(None)
    with pytest.raises(ReconciliationError):
        _require_mapping([])
    assert _mapping("no") is None
    assert _mapping({1: "a"}) is None


class _Response:
    def __init__(self, raw: bytes, status: int) -> None:
        self._raw = raw
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _http_error(status: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        status,
        "no",
        Message(),
        BytesIO(body),
    )


def test_pull_request_files_and_check_runs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_offline(False)
    clear_api_budget()
    try:
        with pytest.raises(PullRequestFilesError, match="Offline"):
            set_offline(True)
            pull_request_paths("acme/app", 1, "tok")
        set_offline(False)
        with pytest.raises(PullRequestFilesError, match="token"):
            pull_request_paths("acme/app", 1, "")
        with pytest.raises(PullRequestFilesError, match="token"):
            pull_request_base("", 1, "tok")
        first = [{"status": "removed", "filename": "gone.py"} for _ in range(98)]
        first.append({"status": "added", "filename": ""})
        first.append({"filename": "x.py"})
        second = [
            {"status": "copied", "filename": "copy.py"},
            {"status": "changed", "filename": "chg.py"},
            {"status": "renamed", "filename": "ren.py"},
            {"status": "modified", "filename": 1},
            1,
        ]
        pages = [json.dumps(first).encode(), json.dumps(second).encode()]

        def open_pages(_req: urllib.request.Request, timeout: int = 30) -> _Response:
            del timeout
            return _Response(pages.pop(0), 200)

        with patch("checkowners.github.urllib.request.urlopen", side_effect=open_pages):
            assert pull_request_paths("acme/app", 7, "tok") == ("copy.py", "chg.py", "ren.py")
        with patch(
            "checkowners.github.urllib.request.urlopen",
            return_value=_Response(b"", 200),
        ):
            assert pull_request_paths("acme/app", 7, "tok") == ()
        with (
            patch(
                "checkowners.github.urllib.request.urlopen",
                return_value=_Response(b"\xff", 200),
            ),
            pytest.raises(PullRequestFilesError, match="pull-requests: read"),
        ):
            pull_request_paths("acme/app", 7, "tok")
        with (
            patch(
                "checkowners.github.urllib.request.urlopen",
                return_value=_Response(b"{", 200),
            ),
            pytest.raises(PullRequestFilesError, match="pull-requests: read"),
        ):
            pull_request_paths("acme/app", 7, "tok")
        with (
            patch(
                "checkowners.github.urllib.request.urlopen",
                return_value=_Response(b"{}", 200),
            ),
            pytest.raises(PullRequestFilesError, match="pull-requests: read"),
        ):
            pull_request_paths("acme/app", 7, "tok")
        with (
            patch(
                "checkowners.github.urllib.request.urlopen",
                side_effect=_http_error(500, b"no"),
            ),
            pytest.raises(PullRequestFilesError, match="HTTP 500"),
        ):
            pull_request_paths("acme/app", 7, "tok")
        with (
            patch(
                "checkowners.github.urllib.request.urlopen",
                side_effect=_http_error(404, b"no"),
            ),
            pytest.raises(PullRequestFilesError, match="pull-requests: read"),
        ):
            pull_request_base("acme/app", 7, "tok")
        with (
            patch(
                "checkowners.github.urllib.request.urlopen",
                side_effect=urllib.error.URLError("down"),
            ),
            pytest.raises(PullRequestFilesError, match="pull-requests: read"),
        ):
            pull_request_base("acme/app", 7, "tok")
        with patch(
            "checkowners.github.urllib.request.urlopen",
            return_value=_Response(json.dumps({"base": {"sha": "abc"}}).encode(), 200),
        ):
            assert pull_request_base("acme/app", 7, "tok") == "abc"
        for body in (
            b"[]",
            b"{}",
            json.dumps({"base": {}}).encode(),
            json.dumps({"base": {"sha": ""}}).encode(),
        ):
            with (
                patch(
                    "checkowners.github.urllib.request.urlopen",
                    return_value=_Response(body, 200),
                ),
                pytest.raises(PullRequestFilesError, match="pull-requests: read"),
            ):
                pull_request_base("acme/app", 7, "tok")
        begin_api_budget(0)
        with (
            patch("checkowners.github.urllib.request.urlopen") as blocked,
            pytest.raises(PullRequestFilesError, match="pull-requests: read"),
        ):
            pull_request_paths("acme/app", 7, "tok")
        blocked.assert_not_called()
        clear_api_budget()
        for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_SHA"):
            monkeypatch.delenv(name, raising=False)
        assert publish_check_run(conclusion="failure", summary="ok", annotations=[]) is False
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
        monkeypatch.setenv("GITHUB_SHA", "abc")
        monkeypatch.setenv("GITHUB_API_URL", "ftp://example.test")
        with patch("checkowners.github.urllib.request.urlopen") as blocked:
            assert publish_check_run(conclusion="success", summary="ok", annotations=[]) is False
        blocked.assert_not_called()
        monkeypatch.delenv("GITHUB_API_URL", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        captured: list[bytes] = []

        def open_check(req: urllib.request.Request, timeout: int = 30) -> _Response:
            del timeout
            assert req.data is not None
            captured.append(req.data)
            return _Response(b"{}", 201)

        notes = [{"path": f"p{i}.py", "message": "m", "start_line": 1} for i in range(51)]
        with patch("checkowners.github.urllib.request.urlopen", side_effect=open_check):
            assert publish_check_run(conclusion="success", summary="  ", annotations=notes) is True
        body = json.loads(captured[0])
        assert body["output"]["summary"] == "checkOwners"
        assert len(body["output"]["annotations"]) == 50
        assert body["conclusion"] == "success"
        long_summary = "x" * 70_000
        with patch("checkowners.github.urllib.request.urlopen", side_effect=open_check):
            assert publish_check_run(conclusion="failure", summary=long_summary, annotations=[])
        assert len(json.loads(captured[1])["output"]["summary"]) == 65535
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with patch(
            "checkowners.github.urllib.request.urlopen",
            side_effect=_http_error(401, b"no"),
        ):
            assert publish_check_run(conclusion="success", summary="ok", annotations=[]) is False
        assert "::notice::checkOwners check run was not created." in capsys.readouterr().err
        with patch(
            "checkowners.github.urllib.request.urlopen",
            side_effect=_http_error(500, b"no"),
        ):
            assert publish_check_run(conclusion="success", summary="ok", annotations=[]) is False
        assert capsys.readouterr().err == ""
        with patch(
            "checkowners.github.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ):
            assert publish_check_run(conclusion="success", summary="ok", annotations=[]) is False
        assert "::notice::" in capsys.readouterr().err
        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        with patch("checkowners.github.urllib.request.urlopen") as blocked:
            assert (
                publish_check_run(conclusion="success", summary="ok", annotations=[cycle]) is False
            )
        blocked.assert_not_called()
        assert "::notice::" in capsys.readouterr().err
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with patch(
            "checkowners.github.urllib.request.urlopen",
            side_effect=_http_error(403, b"no"),
        ):
            assert publish_check_run(conclusion="success", summary="ok", annotations=[]) is False
        assert capsys.readouterr().err == ""
    finally:
        set_offline(False)
        clear_api_budget()
        clear_api_evidence()

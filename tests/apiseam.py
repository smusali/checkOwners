"""The one mocked seam: checkowners.github.request."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

from checkowners.github import ApiResponse

Handler = Callable[[str, str, "Mapping[str, object] | None"], "ApiResponse | None"]


@dataclass(frozen=True)
class Route:
    """One canned reply, matched on the method and a path prefix."""

    method: str
    prefix: str
    status: int = 200
    body: object = None
    headers: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class Call:
    """One request the fake seam received."""

    method: str
    path: str
    token: str
    payload: Mapping[str, object] | None


class FakeApi:
    """Replacement for `checkowners.github.request`, the only mocked seam.

    Routes are matched in order on the method and a path prefix. Unmatched
    requests answer 404. `calls` records every request.
    """

    def __init__(self, routes: Sequence[Route], handler: Handler | None = None) -> None:
        self.routes = list(routes)
        self.handler = handler
        self.calls: list[Call] = []

    def __call__(
        self,
        method: str,
        path: str,
        token: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> ApiResponse:
        self.calls.append(Call(method, path, token, payload))
        if self.handler is not None:
            answered = self.handler(method, path, payload)
            if answered is not None:
                return answered
        for route in self.routes:
            if route.method == method and path.startswith(route.prefix):
                body = b"" if route.body is None else json.dumps(route.body).encode("utf-8")
                return ApiResponse(route.status, body, dict(route.headers), route.reason)
        return ApiResponse(404, b"", {}, "")

    def paths(self, method: str = "") -> list[str]:
        """Return the requested paths, optionally only for one method."""
        return [call.path for call in self.calls if not method or call.method == method]

    def payloads(self, method: str = "") -> list[Mapping[str, object] | None]:
        """Return the sent payloads, optionally only for one method."""
        return [call.payload for call in self.calls if not method or call.method == method]


@contextmanager
def fake_api(*routes: Route, handler: Handler | None = None) -> Iterator[FakeApi]:
    """Patch the GitHub HTTP seam with canned replies for the block.

    `handler` answers first when given, so a stateful flow can reply without
    a route for every step. Returning None from it falls back to the routes.
    """
    api = FakeApi(routes, handler)
    with patch("checkowners.github.request", side_effect=api):
        yield api


def reply(payload: object = None, status: int = 200) -> ApiResponse:
    """Return one JSON reply, for use from a `fake_api` handler."""
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return ApiResponse(status, body, {})

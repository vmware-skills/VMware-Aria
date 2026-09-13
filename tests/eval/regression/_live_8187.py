"""Shared fixtures for the 2026-09-13 Aria Operations 8.18.7 findings.

Every response here was captured from a real appliance (see the ``_note`` in
each JSON file), so the tests exercise the shapes the product actually sends
rather than the shapes the code was written to expect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vmware_aria.connection import AriaApiError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aria_8187"


def body(name: str) -> Any:
    """The captured response body of one fixture file."""
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data["body"]


def api_error(status: int, path: str, response_body: Any = None) -> AriaApiError:
    return AriaApiError(
        f"Aria Operations returned HTTP {status}.",
        status_code=status,
        method="GET",
        path=path,
        body=response_body,
    )


class RoutedClient:
    """A client whose ``get`` answers per path; an exception value is raised."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = dict(routes)
        self.calls: list[str] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(path)
        if path not in self._routes:
            raise AssertionError(f"unexpected GET {path}")
        answer = self._routes[path]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def startup_routes() -> dict[str, Any]:
    """The appliance as it was measured: node 503, six of seven services OK."""
    return {
        "/deployment/node/status": api_error(
            503, "/deployment/node/status", body("node_status_503.json")
        ),
        "/deployment/node/services/info": body("services_info.json"),
        "/versions/current": body("versions_current.json"),
    }

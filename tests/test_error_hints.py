"""An error about the request must not send the model to check connectivity.

2026-10-24 conversation round: a non-UUID alert_id, and a 404 that the error
text itself explained as "this endpoint needs VCF Operations 9.0+", both came
back with "Run 'vmware-aria doctor' to verify connectivity." Doctor reports
green for both, so the hint pointed at the one thing that was not wrong.
"""

from __future__ import annotations

import asyncio
import json
import ssl

import pytest

from vmware_aria.config import ConfigError
from vmware_aria.connection import AriaApiError
from vmware_aria.mcp_server import server
from vmware_aria.mcp_server._shared import _DOCTOR_HINT, _FIX_IN_MESSAGE_HINT, _hint_for


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_request_statuses_say_the_message_has_the_fix(status: int) -> None:
    assert _hint_for(AriaApiError("x", status_code=status)) == _FIX_IN_MESSAGE_HINT


@pytest.mark.parametrize("status", [None, 401, 403, 500, 503])
def test_login_server_and_transport_failures_keep_doctor(status) -> None:
    assert _hint_for(AriaApiError("x", status_code=status)) == _DOCTOR_HINT


@pytest.mark.parametrize("exc", [ValueError("alert_id must be a UUID"), ConfigError("set VMWARE_ARIA_X_PASSWORD")])
def test_authored_request_errors_say_the_message_has_the_fix(exc: Exception) -> None:
    assert _hint_for(exc) == _FIX_IN_MESSAGE_HINT


@pytest.mark.parametrize(
    "exc",
    [KeyError("resourceId"), ssl.SSLError("bad cert"), json.JSONDecodeError("Expecting value", "<html>", 0), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x"), RuntimeError("boom")],
)
def test_errors_this_skill_did_not_write_keep_doctor(exc: Exception) -> None:
    assert _hint_for(exc) == _DOCTOR_HINT


def test_a_malformed_alert_id_through_the_mcp_layer(monkeypatch) -> None:
    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    result = asyncio.run(server.mcp.call_tool("investigate_alert", {"alert_id": "e1fdab74-"}))
    content = result[0] if isinstance(result, tuple) else result
    payload = json.loads(content[0].text)
    assert "8-4-4-4-12" in payload["error"]
    assert payload["hint"] == _FIX_IN_MESSAGE_HINT

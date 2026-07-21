"""MCP server parity + error-surface regression evals.

Two past incidents pinned here:

* 踩坑 #34 — the MCP tool surface drifted from the documented tool table
  (CLI had clone/migrate/snapshot, MCP silently didn't). The parity tests
  assert the registered tool count and read/write split match SKILL.md's
  declaration ("MCP Tools (28 — 21 read, 7 write)").

* v1.5.34 follow-up — the connection layer's teaching errors (AriaApiError:
  "404 → list the parent collection first", "503 → platform booting") were
  collapsed to "AriaApiError: operation failed." by _safe_error's allowlist,
  so agents never saw the remediation hint.
"""
from __future__ import annotations

import asyncio

import pytest

EXPECTED_TOOL_COUNT = 28
EXPECTED_READ_COUNT = 21
EXPECTED_WRITE_COUNT = 7


def _list_tools():
    from vmware_aria.mcp_server.server import mcp

    return asyncio.run(mcp.list_tools())


def test_mcp_tool_count_matches_skill_md_declaration() -> None:
    tools = _list_tools()
    assert len(tools) == EXPECTED_TOOL_COUNT, (
        f"MCP server exposes {len(tools)} tools but SKILL.md declares "
        f"{EXPECTED_TOOL_COUNT} — update both together (踩坑 #34)"
    )


def test_mcp_read_write_split_matches_skill_md_declaration() -> None:
    tools = _list_tools()
    read = [t.name for t in tools if t.annotations and t.annotations.readOnlyHint]
    write = [t.name for t in tools if not (t.annotations and t.annotations.readOnlyHint)]
    assert len(read) == EXPECTED_READ_COUNT, f"read tools drifted: {sorted(read)}"
    assert len(write) == EXPECTED_WRITE_COUNT, f"write tools drifted: {sorted(write)}"


def test_safe_error_passes_aria_api_error_hint_through() -> None:
    from vmware_aria.mcp_server.server import _safe_error
    from vmware_aria.connection import AriaApiError, _hint_for_status

    exc = AriaApiError(
        "Aria Operations GET /resources/bad-uuid returned HTTP 404. "
        + _hint_for_status(404),
        status_code=404,
        method="GET",
        path="/resources/bad-uuid",
    )
    message = _safe_error(exc, "get_resource")
    assert "404" in message
    assert "list the parent" in message, (
        "AriaApiError teaching hints must pass through _safe_error, not "
        "collapse to 'AriaApiError: operation failed.'"
    )


def test_mcp_tool_error_path_contains_404_hint(monkeypatch) -> None:
    # End-to-end through a registered tool: the dict an agent receives must
    # carry the teaching hint, not an opaque failure line.
    import vmware_aria.mcp_server.server as server
    from vmware_aria.connection import AriaApiError, _hint_for_status

    def boom(target=None):
        raise AriaApiError(
            "Aria Operations GET /resources/bad-uuid returned HTTP 404. "
            + _hint_for_status(404),
            status_code=404,
            method="GET",
            path="/resources/bad-uuid",
        )

    monkeypatch.setattr(server, "_get_connection", boom)
    result = server.get_resource("bad-uuid")

    assert "error" in result
    assert "404" in result["error"]
    assert "list the parent" in result["error"]


def test_a_returned_error_is_audited_as_a_failure(monkeypatch) -> None:
    """A tool that catches and returns must still be recorded as having failed.

    ``@vmware_tool`` marks a call failed when an exception reaches it, or when
    the payload it returns is the family's error envelope — a dict carrying a
    truthy ``error``. Every tool in this skill catches and returns exactly that
    envelope, so no explicit ``report_tool_failure`` call is needed here.

    That is a property of this surface, not a guarantee, which is why it is
    pinned: a tool rewritten to hand back an error *string* would look identical
    to a success, and the audit row would say ``ok`` for an operation that
    failed, an undo token would be written for a change that never happened, and
    the circuit breaker would be told the call succeeded.
    """
    import vmware_aria.mcp_server.server as server
    from vmware_aria.connection import AriaApiError

    assert getattr(server.get_resource, "_is_vmware_tool", False), (
        "get_resource is not wrapped by @vmware_tool — this test would pass "
        "without exercising the audit path it exists to check"
    )

    rows: list[dict] = []

    class _Recorder:
        def log(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: _Recorder())

    def boom(target=None):
        raise AriaApiError("Aria Operations returned HTTP 404.", status_code=404)

    monkeypatch.setattr(server, "_get_connection", boom)
    result = server.get_resource("bad-uuid")

    assert result.get("error"), "the tool did not return the error envelope"
    assert rows, "the wrapper recorded no audit row at all"
    assert rows[0]["status"] == "error", (
        f"a failed call was audited as {rows[0]['status']!r} — the audit log, "
        "the undo store and the circuit breaker all read this field"
    )


def test_safe_error_passes_connection_error_through() -> None:
    # issue #10: a dropped connection must surface its teaching hint through
    # MCP instead of being masked as a generic "operation failed".
    from vmware_aria.mcp_server.server import _safe_error

    hint = "Connection to aria-prod dropped. Run 'vmware-aria doctor'."
    message = _safe_error(ConnectionError(hint), "get_resource")
    assert hint in message
    assert "operation failed" not in message


def test_safe_error_still_masks_unexpected_exceptions() -> None:
    from vmware_aria.mcp_server.server import _safe_error

    message = _safe_error(RuntimeError("internal /etc/path host:443 detail"), "get_resource")
    assert "internal" not in message
    assert message == "RuntimeError: operation failed."


def test_cli_command_releases_connection_token(monkeypatch) -> None:
    # issue #10: each CLI command builds a fresh ConnectionManager via
    # _get_connection; without cleanup its per-invocation auth token is never
    # released, accumulating server-side. The _friendly_errors wrapper must
    # close every manager opened during the command.
    from vmware_aria import cli

    closed: list[object] = []

    class FakeManager:
        def disconnect_all(self) -> None:
            closed.append(self)

    monkeypatch.setattr(cli, "_OPEN_MANAGERS", [])

    @cli._friendly_errors
    def fake_command() -> str:
        # Simulate _get_connection registering a manager for cleanup.
        cli._OPEN_MANAGERS.append(FakeManager())
        return "ok"

    assert fake_command() == "ok"
    assert len(closed) == 1, "command must release its ConnectionManager token"
    assert cli._OPEN_MANAGERS == [], "registry must be drained after the command"


def test_connection_cleanup_runs_even_on_error(monkeypatch) -> None:
    # Cleanup lives in a finally, so a failing command still releases tokens.
    import typer

    from vmware_aria import cli
    from vmware_aria.connection import AriaApiError

    closed: list[object] = []

    class FakeManager:
        def disconnect_all(self) -> None:
            closed.append(self)

    monkeypatch.setattr(cli, "_OPEN_MANAGERS", [])

    @cli._friendly_errors
    def failing_command() -> None:
        cli._OPEN_MANAGERS.append(FakeManager())
        raise AriaApiError("boom", status_code=503, method="GET", path="/x")

    with pytest.raises(typer.Exit):
        failing_command()
    assert len(closed) == 1, "token must be released even when the command errors"

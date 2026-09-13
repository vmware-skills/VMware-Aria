"""The two platform self-check reads, as an agent and an operator reach them.

MCP: registered, read-only, [READ]-prefixed, every parameter described, errors
returned as the family envelope. CLI: ``vmware-aria health node`` and
``vmware-aria health adapters`` exist and render the live fixtures.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.eval.regression.test_platform_adapters import FakeClient as AdapterClient
from tests.eval.regression.test_platform_adapters import adapters_body
from tests.eval.regression.test_platform_node_resources import FakeClient as NodeClient
from tests.eval.regression.test_platform_node_resources import live_routes
from vmware_aria.connection import AriaApiError
from vmware_aria.mcp_server import server

NEW_TOOLS = ("get_aria_node_resources", "list_adapters")


def _tools() -> dict[str, Any]:
    from vmware_aria.mcp_server.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_registered_read_only_and_documented(name: str) -> None:
    tool = _tools()[name]
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.description.startswith("[READ]")
    for param, schema in tool.inputSchema["properties"].items():
        assert schema.get("description"), f"{name}.{param} has no description"
    assert "target" in tool.inputSchema["properties"]


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_returns_the_error_envelope(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def boom(target: Any = None) -> Any:
        raise AriaApiError("Aria Operations returned HTTP 404. list the parent collection first", status_code=404)

    monkeypatch.setattr(server, "_get_connection", boom)
    result = getattr(server, name)()
    assert "HTTP 404" in result["error"]
    assert result["hint"]


def test_invalid_window_reaches_the_agent_as_teaching_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_get_connection", lambda target=None: NodeClient(*live_routes()))
    result = server.get_aria_node_resources(window_hours=0)
    assert "window_hours" in result["error"]


def test_mcp_tools_return_the_live_readings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_get_connection", lambda target=None: NodeClient(*live_routes()))
    nodes = server.get_aria_node_resources()
    assert nodes["nodes"][0]["memory_pressure"]["level"] == "NORMAL"

    status = {"status": "ONLINE", "systemTime": 1789287196733}
    client = AdapterClient({"/adapters": adapters_body(), "/deployment/node/status": status})
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    assert server.list_adapters(adapter_kind="VMWARE")["items"][0]["name"] == "new VC"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["node", "adapters"])
def test_cli_help(command: str) -> None:
    from vmware_aria.cli import app

    result = CliRunner().invoke(app, ["health", command, "--help"])
    assert result.exit_code == 0, result.output


def test_cli_health_node_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_aria import cli

    monkeypatch.setattr(cli, "_get_connection", lambda target, config=None: (NodeClient(*live_routes()), None))
    result = CliRunner().invoke(cli.app, ["health", "node"])
    assert result.exit_code == 0, result.output
    assert "NORMAL" in result.output
    assert "15.61" in result.output
    assert "Analytics" in result.output


def test_cli_health_adapters_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_aria import cli

    status = {"status": "ONLINE", "systemTime": 1789287196733}
    client = AdapterClient({"/adapters": adapters_body(), "/deployment/node/status": status})
    monkeypatch.setattr(cli, "_get_connection", lambda target, config=None: (client, None))
    result = CliRunner().invoke(cli.app, ["health", "adapters"])
    assert result.exit_code == 0, result.output
    assert "new VC" in result.output


def test_cli_health_node_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_aria import cli

    monkeypatch.setattr(cli, "_get_connection", lambda target, config=None: (NodeClient(*live_routes()), None))
    result = CliRunner().invoke(cli.app, ["health", "node", "--json"])
    assert result.exit_code == 0, result.output
    assert '"memory_pressure"' in result.output

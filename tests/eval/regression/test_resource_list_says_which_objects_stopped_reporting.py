"""``resource list`` says which objects stopped reporting.

2026-09-14, live Aria Operations 8.18.7: the alert "Objects are not receiving
data from adapter instance" was active on the "new VC" adapter instance, and the
skill could not say which objects it meant. ``resource list`` showed every row
as ``STARTED`` — that column is ``resourceState``, Aria's lifecycle state for the
object, and a powered-off VM is STARTED too. The answer was one field over:
``resource get`` on "VMware vCenter Server" (GREY, -1) read
``{"resourceState": "STARTED", "resourceStatus": "NO_DATA_RECEIVING"}``, while
vcsa read ``DATA_RECEIVING``. The listing dropped exactly that field.

The DTO shapes below are the ones read off the appliance that day.
"""

from __future__ import annotations

import asyncio

import pytest

from vmware_aria.ops.resources import list_resources


def _dto(rid: str, name: str, state: str | None, status: str | None) -> dict:
    states = [] if state is None else [{"resourceState": state, "resourceStatus": status}]
    return {
        "identifier": rid,
        "resourceKey": {"name": name, "resourceKindKey": "VirtualMachine", "adapterKindKey": "VMWARE"},
        "badges": [],
        "resourceStatusStates": states,
    }


LIVE_ROWS = [
    _dto("694b4c53-fafc-4e7d-bf4c-0d8edd42d58d", "VMware vCenter Server", "STARTED", "NO_DATA_RECEIVING"),
    _dto("c996e529-35fb-46e1-ae37-11186cb97751", "vcsa", "STARTED", "DATA_RECEIVING"),
    _dto("b010ea38-9921-49a7-b3a2-45ae8a332226", "vRealize-Operations", "STARTED", "DATA_RECEIVING"),
]


class _Client:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.params: list[dict] = []

    def get(self, path: str, params: dict | None = None, **_kw) -> dict:
        assert path == "/resources", path
        self.params.append(dict(params or {}))
        return {"resourceList": self.rows, "pageInfo": {"totalCount": len(self.rows)}}


def test_each_row_carries_the_collection_status_beside_the_aria_state():
    rows = {r["name"]: r for r in list_resources(_Client(LIVE_ROWS))["items"]}
    assert rows["VMware vCenter Server"]["collection_status"] == "NO_DATA_RECEIVING"
    assert rows["vcsa"]["collection_status"] == "DATA_RECEIVING"
    assert all(r["aria_state"] == "STARTED" for r in rows.values())
    # Kept for existing callers: `status` still carries resourceState.
    assert all(r["status"] == "STARTED" for r in rows.values())


def test_the_filter_returns_only_the_objects_not_receiving_data():
    result = list_resources(_Client(LIVE_ROWS), collection_status="no_data_receiving")
    assert [r["name"] for r in result["items"]] == ["VMware vCenter Server"]
    # Applied client-side, so the server's count describes the unfiltered set.
    assert result["total"] is None


def test_a_filter_that_matches_nothing_names_the_statuses_it_saw():
    """Empty because nothing matched is not the same as empty because nothing is wrong."""
    result = list_resources(_Client(LIVE_ROWS), collection_status="COLLECTOR_DOWN")
    assert result["items"] == []
    note = result["note"]
    assert "COLLECTOR_DOWN" in note
    assert "DATA_RECEIVING" in note and "NO_DATA_RECEIVING" in note


def test_an_unreported_status_is_unknown_and_never_matches():
    rows = LIVE_ROWS + [_dto("0000", "no-states", None, None)]
    by_name = {r["name"]: r for r in list_resources(_Client(rows))["items"]}
    assert by_name["no-states"]["collection_status"] is None
    matched = list_resources(_Client(rows), collection_status="DATA_RECEIVING")["items"]
    assert "no-states" not in {r["name"] for r in matched}


def test_all_kinds_sends_no_resource_kind():
    """An adapter instance's objects are VMs, hosts and datastores at once."""
    client = _Client(LIVE_ROWS)
    list_resources(client, resource_kind=None, collection_status="NO_DATA_RECEIVING")
    assert "resourceKind" not in client.params[0]


def test_cli_shows_collection_status_and_filters_by_it(monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from vmware_aria import cli

    client = _Client(LIVE_ROWS)
    monkeypatch.setattr(cli, "_get_connection", lambda *_a, **_k: (client, None))
    monkeypatch.setenv("COLUMNS", "200")
    result = CliRunner().invoke(
        cli.app, ["resource", "list", "--kind", "all", "--collection-status", "NO_DATA_RECEIVING"]
    )
    assert result.exit_code == 0, result.output
    assert "VMware vCenter Server" in result.output
    assert "NO_DATA_RECEIVING" in result.output
    assert "Aria state" in result.output
    assert "vcsa" not in result.output
    assert "resourceKind" not in client.params[0]


def test_mcp_tool_takes_the_collection_status_filter():
    from vmware_aria.mcp_server.server import mcp

    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "list_resources")
    assert "collection_status" in tool.inputSchema["properties"]

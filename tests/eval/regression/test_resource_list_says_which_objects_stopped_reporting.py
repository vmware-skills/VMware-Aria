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


# ── review, 2026-09-15 ─────────────────────────────────────────────────────
# The filter used to stop scanning after `limit` ROWS — with the CLI default of
# 50 that was the first 1000-object page — and then say no object matched.


class _PagedClient:
    """GET /resources honouring page and pageSize, as the appliance does."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.pages: list[int] = []

    def get(self, path: str, params: dict | None = None, **_kw) -> dict:
        assert path == "/resources", path
        page, size = params["page"], min(params["pageSize"], 1000)
        self.pages.append(page)
        return {
            "resourceList": self.rows[page * size : (page + 1) * size],
            "pageInfo": {"totalCount": len(self.rows)},
        }


def _estate(n: int, bad: int | None) -> list[dict]:
    return [
        _dto(f"id-{i}", f"obj{i}", "STARTED", "NO_DATA_RECEIVING" if i == bad else "DATA_RECEIVING")
        for i in range(n)
    ]


def test_a_match_past_the_first_page_is_found_with_a_small_limit():
    client = _PagedClient(_estate(2500, bad=2300))
    result = list_resources(client, resource_kind=None, limit=50, collection_status="NO_DATA_RECEIVING")
    assert [r["name"] for r in result["items"]] == ["obj2300"]
    assert not result.get("note")
    assert client.pages == [0, 1, 2]


def test_a_name_match_past_the_first_page_is_found_too():
    result = list_resources(_PagedClient(_estate(2500, bad=None)), limit=50, name_filter="obj2300")
    assert [r["name"] for r in result["items"]] == ["obj2300"]


def test_a_scan_cut_by_the_safety_cap_does_not_claim_nothing_matched(monkeypatch):
    from vmware_aria.ops import resources

    monkeypatch.setattr(resources, "_RESOURCES_MAX_TOTAL", 1000)
    result = list_resources(_PagedClient(_estate(2500, bad=2300)), collection_status="NO_DATA_RECEIVING")
    assert result["items"] == []
    assert result["scan_complete"] is False
    assert "first 1000 of 2500" in result["note"]
    assert "No object has" not in result["note"]


def test_an_unreported_status_is_named_in_the_note():
    rows = [_dto("0000", "no-states", None, None)]
    note = list_resources(_Client(rows), collection_status="NO_DATA_RECEIVING")["note"]
    assert "not reported" in note


def test_the_note_describes_what_was_read_even_when_a_name_filter_removed_it():
    note = list_resources(_Client(LIVE_ROWS), name_filter="zzz", collection_status="COLLECTOR_DOWN")["note"]
    assert "DATA_RECEIVING" in note
    assert "no objects were listed" not in note


def test_the_note_only_suggests_widening_the_kind_when_it_was_narrowed():
    narrowed = list_resources(_Client(LIVE_ROWS), collection_status="COLLECTOR_DOWN")["note"]
    everything = list_resources(_Client(LIVE_ROWS), resource_kind=None, collection_status="COLLECTOR_DOWN")["note"]
    assert "widen resource_kind" in narrowed
    assert "widen resource_kind" not in everything


def test_mcp_tool_maps_all_and_passes_the_filter(monkeypatch):
    from vmware_aria.mcp_server import server
    from vmware_aria.mcp_server.tools import resources as tools

    client = _Client(LIVE_ROWS)
    monkeypatch.setattr(server, "_get_connection", lambda *_a, **_k: client)
    out = tools.list_resources(resource_kind="ALL", collection_status="no_data_receiving")
    assert [r["name"] for r in out["items"]] == ["VMware vCenter Server"]
    assert "resourceKind" not in client.params[0]


def test_cli_prints_the_note_and_the_kind_column(monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from vmware_aria import cli

    monkeypatch.setattr(cli, "_get_connection", lambda *_a, **_k: (_Client(LIVE_ROWS), None))
    monkeypatch.setenv("COLUMNS", "300")
    result = CliRunner().invoke(cli.app, ["resource", "list", "--kind", "all", "--collection-status", "COLLECTOR_DOWN"])
    assert result.exit_code == 0, result.output
    assert "No object has collection_status COLLECTOR_DOWN" in " ".join(result.output.split())
    listing = CliRunner().invoke(cli.app, ["resource", "list", "--kind", "all"])
    assert "Kind" in listing.output and "VirtualMachine" in listing.output

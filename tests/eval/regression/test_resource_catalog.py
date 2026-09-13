"""Metric keys, properties and relationships: look before you query.

2026-09-13, real Aria Operations 8.18.7. The skill's own docs named 13 metric
keys that do not exist on Aria, and ``cpu|demand_average`` is not defined for
VMs at all. Power state, ``config|extraConfig|mem_hotadd`` and the parent host
were needed the same day and were only reachable by hand, as was VM → host →
datastore navigation. These three reads close that gap.

Every one of them has two ways to be empty, and they must not look alike:

* the resource really has no keys / properties / relationships, and
* the read failed or came back in a shape this code does not understand.

A failed or unrecognised primary read raises (the MCP tool turns that into its
error envelope), so it can never arrive as ``items: []``. A failed *secondary*
read — the kind's definitions behind a key, the parent/child labels behind a
relationship — leaves the rows in place and says the join is undetermined.

A measured trap pinned below: ``GET /resources/{id}/statkeys`` for a resource
id that does not exist answers **200 with ``{"stat-key": []}``**, so "no keys"
is only trusted after ``GET /resources/{id}`` has confirmed the resource.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.eval.regression._live_8187 import RoutedClient, api_error, body
from vmware_aria.connection import AriaApiError

VM = "f654322a-50c3-4a9a-937c-2d4bae8a2b36"
HOST = "12adc0ff-f9ff-4a43-b0e1-22664a058376"
VM_KIND_DEFS = "/adapterkinds/VMWARE/resourcekinds/VirtualMachine/statkeys"
ENVELOPE_KEYS = ("items", "returned", "limit", "total", "truncated", "hint", "next_offset")


def _vm_key_routes(**overrides: Any) -> dict[str, Any]:
    routes = {
        f"/resources/{VM}": body("vm_resource_test_llm.json"),
        f"/resources/{VM}/statkeys": body("vm_statkeys_180.json"),
        VM_KIND_DEFS: body("vm_statkey_definitions_full.json"),
    }
    routes.update(overrides)
    return routes


def _by_key(result: dict) -> dict[str, dict]:
    return {row["key"]: row for row in result["items"]}


def _defs() -> dict[str, dict]:
    return {d["key"]: d for d in body("vm_statkey_definitions_full.json")["resourceTypeAttributes"]}


# ═══ list_metric_keys ═════════════════════════════════════════════════════════


@pytest.mark.unit
def test_resource_keys_are_joined_with_the_kinds_definitions():
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient(_vm_key_routes())
    result = list_metric_keys(client, resource_id=VM, limit=500)

    for key in ENVELOPE_KEYS:
        assert key in result
    assert result["total"] == result["returned"] == 180
    assert result["truncated"] is False and result["next_offset"] is None
    assert result["source"] == "resource"
    assert result["resource_kind"] == "VirtualMachine" and result["adapter_kind"] == "VMWARE"
    assert result["definitions_status"] == "read" and result["definitions_note"] is None
    assert [r["key"] for r in result["items"]] == sorted(r["key"] for r in result["items"])

    rows = _by_key(result)
    defs = _defs()
    demand = rows["mem|host_demand"]
    assert demand["definition"] == "found"
    assert demand["name"] == defs["mem|host_demand"]["name"]
    assert demand["unit"] == (defs["mem|host_demand"].get("unit") or None)

    boot = rows["guestfilesystem:/boot|usage"]
    assert boot["definition"] == "found_by_instance", "an instanced key joins with its instance stripped"
    assert boot["name"] == defs["guestfilesystem|usage"]["name"]

    assert result["unjoined_count"] == 0 and result["unjoined_keys"] == []
    assert sum(r["definition"] == "found_by_instance" for r in result["items"]) == 34


@pytest.mark.unit
def test_a_key_the_kind_does_not_define_is_named_not_hidden():
    from vmware_aria.ops.catalog import list_metric_keys

    keys = {"stat-key": [{"key": "mem|host_demand"}, {"key": "custom|made_up"}]}
    client = RoutedClient(_vm_key_routes(**{f"/resources/{VM}/statkeys": keys}))
    result = list_metric_keys(client, resource_id=VM)

    rows = _by_key(result)
    assert rows["custom|made_up"]["definition"] == "not_defined_for_kind"
    assert rows["custom|made_up"]["name"] is None and rows["custom|made_up"]["unit"] is None
    assert result["unjoined_count"] == 1
    assert result["unjoined_keys"] == ["custom|made_up"]


@pytest.mark.unit
def test_filter_matches_key_or_name_case_insensitively():
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient(_vm_key_routes())
    result = list_metric_keys(client, resource_id=VM, key_filter="MEM|", limit=500)

    assert result["items"], "the VM reports memory keys"
    for row in result["items"]:
        assert "mem|" in row["key"].lower() or "mem|" in (row["name"] or "").lower()
    expected = [
        k["key"] for k in body("vm_statkeys_180.json")["stat-key"]
        if "mem|" in k["key"].lower()
        or "mem|" in (_defs().get(k["key"], {}).get("name") or "").lower()
    ]
    assert result["total"] >= len({k for k in expected if "mem|" in k.lower()})


@pytest.mark.unit
def test_keys_page_with_offset_and_next_offset():
    from vmware_aria.ops.catalog import list_metric_keys

    first = list_metric_keys(RoutedClient(_vm_key_routes()), resource_id=VM, limit=50)
    assert (first["returned"], first["total"], first["truncated"], first["next_offset"]) == (50, 180, True, 50)

    last = list_metric_keys(RoutedClient(_vm_key_routes()), resource_id=VM, limit=50, offset=150)
    assert (last["returned"], last["next_offset"]) == (30, None)
    assert not {r["key"] for r in first["items"]} & {r["key"] for r in last["items"]}


@pytest.mark.unit
@pytest.mark.parametrize("limit, offset", [(0, 0), (501, 0), (-1, 0), (10, -1)])
def test_out_of_range_page_arguments_are_rejected(limit, offset):
    from vmware_aria.ops.catalog import list_metric_keys

    with pytest.raises(ValueError):
        list_metric_keys(RoutedClient(_vm_key_routes()), resource_id=VM, limit=limit, offset=offset)


@pytest.mark.unit
def test_kind_mode_lists_definitions_without_touching_a_resource():
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient({VM_KIND_DEFS: body("vm_statkey_definitions_full.json")})
    result = list_metric_keys(client, resource_kind="VirtualMachine", limit=10)

    assert result["source"] == "resource_kind"
    assert result["total"] == 911 and result["returned"] == 10 and result["next_offset"] == 10
    assert client.calls == [VM_KIND_DEFS]
    assert set(result["items"][0]) >= {"key", "name", "unit"}


@pytest.mark.unit
@pytest.mark.parametrize("kwargs", [{}, {"resource_id": VM, "resource_kind": "VirtualMachine"}])
def test_exactly_one_of_resource_id_or_resource_kind(kwargs):
    from vmware_aria.ops.catalog import list_metric_keys

    with pytest.raises(ValueError, match="list_resources"):
        list_metric_keys(RoutedClient({}), **kwargs)


@pytest.mark.unit
def test_empty_statkeys_for_an_id_that_does_not_exist_is_an_error_not_no_keys():
    """Measured: statkeys for a nonexistent id answers 200 {"stat-key": []}; only the resource GET 404s."""
    from vmware_aria.ops.catalog import list_metric_keys

    bogus = "00000000-0000-4000-8000-000000000000"
    client = RoutedClient({
        f"/resources/{bogus}": api_error(404, f"/resources/{bogus}"),
        f"/resources/{bogus}/statkeys": {"stat-key": []},
    })
    with pytest.raises(AriaApiError) as exc:
        list_metric_keys(client, resource_id=bogus)
    assert exc.value.status_code == 404


@pytest.mark.unit
def test_an_existing_resource_with_no_keys_says_so():
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient(_vm_key_routes(**{f"/resources/{VM}/statkeys": {"stat-key": []}}))
    result = list_metric_keys(client, resource_id=VM)

    assert result["items"] == [] and result["total"] == 0
    assert result["hint"] and "no stat keys" in result["hint"]


@pytest.mark.unit
def test_a_failed_statkeys_read_raises_not_empty():
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient(_vm_key_routes(**{
        f"/resources/{VM}/statkeys": api_error(500, f"/resources/{VM}/statkeys"),
    }))
    with pytest.raises(AriaApiError):
        list_metric_keys(client, resource_id=VM)


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        {"statKeys": [{"key": "cpu|usage_average"}]},
        {"stat-key": {"key": "cpu|usage_average"}},
        {"stat-key": [{"statKey": {"key": "cpu|usage_average"}}]},
        {"stat-key": [{"key": "mem|guest_demand"}, "cpu|usage_average"]},
        [{"key": "cpu|usage_average"}],
    ],
    ids=["no-stat-key", "not-a-list", "rows-without-key", "some-rows-unreadable", "body-is-a-list"],
)
def test_an_unrecognised_statkeys_shape_raises_not_empty(answer):
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient(_vm_key_routes(**{f"/resources/{VM}/statkeys": answer}))
    with pytest.raises(AriaApiError, match="statkeys"):
        list_metric_keys(client, resource_id=VM)


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        api_error(500, VM_KIND_DEFS),
        {"attributes": []},
        {"resourceTypeAttributes": [{"name": "no key"}]},
    ],
    ids=["http-500", "no-container", "rows-without-key"],
)
def test_unreadable_definitions_leave_keys_in_place_and_the_join_undetermined(answer):
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient(_vm_key_routes(**{VM_KIND_DEFS: answer}))
    result = list_metric_keys(client, resource_id=VM, limit=500)

    assert result["total"] == 180, "the keys the resource reports are still listed"
    assert result["definitions_status"] == "undetermined"
    assert "resourcekinds" in result["definitions_note"]
    assert all(r["definition"] == "not_read" for r in result["items"])
    assert all(r["name"] is None and r["unit"] is None for r in result["items"])
    assert result["unjoined_count"] is None, "an unread join is not 'zero keys unjoined'"
    assert result["unjoined_keys"] is None


@pytest.mark.unit
def test_an_unrecognised_kind_definition_body_raises_in_kind_mode():
    from vmware_aria.ops.catalog import list_metric_keys

    client = RoutedClient({VM_KIND_DEFS: {"attributes": []}})
    with pytest.raises(AriaApiError, match="resourcekinds"):
        list_metric_keys(client, resource_kind="VirtualMachine")


@pytest.mark.unit
def test_metric_key_strings_are_sanitized():
    from vmware_aria.ops.catalog import list_metric_keys

    keys = {"stat-key": [{"key": "cpu|evil\x07\x1b[2J"}]}
    defs = {"resourceTypeAttributes": [{"key": "cpu|evil\x07\x1b[2J", "name": "CPU\x00|Evil", "unit": "%\x1b"}]}
    client = RoutedClient(_vm_key_routes(**{f"/resources/{VM}/statkeys": keys, VM_KIND_DEFS: defs}))
    row = list_metric_keys(client, resource_id=VM)["items"][0]

    for field in ("key", "name", "unit"):
        assert not any(ord(c) < 32 for c in row[field]), (field, row[field])


# ═══ get_resource_properties ══════════════════════════════════════════════════


@pytest.mark.unit
def test_properties_from_the_live_shape():
    from vmware_aria.ops.catalog import get_resource_properties

    client = RoutedClient({f"/resources/{VM}/properties": body("vm_properties.json")})
    result = get_resource_properties(client, VM, limit=500)

    for key in ENVELOPE_KEYS:
        assert key in result
    assert result["total"] == result["returned"] == 64
    assert result["resource_id"] == VM
    props = {p["name"]: p["value"] for p in result["items"]}
    assert props["config|extraConfig|vcpu_hotadd"] == "false"
    assert props["summary|MOID"] == "vm-2001"
    assert [p["name"] for p in result["items"]] == sorted(props)


@pytest.mark.unit
def test_property_filter_and_paging():
    from vmware_aria.ops.catalog import get_resource_properties

    client = RoutedClient({f"/resources/{VM}/properties": body("vm_properties.json")})
    result = get_resource_properties(client, VM, name_filter="SUMMARY|", limit=2)

    matching = [p for p in body("vm_properties.json")["property"] if "summary|" in p["name"].lower()]
    assert result["total"] == len(matching) > 2
    assert result["returned"] == 2 and result["next_offset"] == 2
    assert all("summary|" in p["name"].lower() for p in result["items"])


@pytest.mark.unit
def test_a_failed_property_read_raises_not_empty():
    from vmware_aria.ops.catalog import get_resource_properties

    client = RoutedClient({f"/resources/{VM}/properties": api_error(404, f"/resources/{VM}/properties")})
    with pytest.raises(AriaApiError):
        get_resource_properties(client, VM)


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        {"resourceId": VM, "properties": []},
        {"resourceId": VM, "property": {"name": "a", "value": "b"}},
        {"resourceId": VM, "property": [{"key": "a", "value": "b"}]},
        [{"name": "a", "value": "b"}],
    ],
    ids=["no-property", "not-a-list", "rows-without-name", "body-is-a-list"],
)
def test_an_unrecognised_property_shape_raises_not_empty(answer):
    from vmware_aria.ops.catalog import get_resource_properties

    client = RoutedClient({f"/resources/{VM}/properties": answer})
    with pytest.raises(AriaApiError, match="properties"):
        get_resource_properties(client, VM)


@pytest.mark.unit
def test_a_resource_with_no_properties_says_so():
    from vmware_aria.ops.catalog import get_resource_properties

    client = RoutedClient({f"/resources/{VM}/properties": {"resourceId": VM, "property": []}})
    result = get_resource_properties(client, VM)
    assert result["items"] == [] and result["total"] == 0
    assert result["hint"] and "no properties" in result["hint"]


@pytest.mark.unit
def test_property_names_and_values_are_sanitized():
    from vmware_aria.ops.catalog import get_resource_properties

    answer = {"resourceId": VM, "property": [{"name": "summary|x\x1b[31m", "value": "v\x07" + "y" * 900}]}
    row = get_resource_properties(RoutedClient({f"/resources/{VM}/properties": answer}), VM)["items"][0]
    assert not any(ord(c) < 32 for c in row["name"] + row["value"])
    assert len(row["value"]) <= 500


# ═══ get_resource_relationships ═══════════════════════════════════════════════


def _rel_routes(rid: str, label: str, **overrides: Any) -> dict[str, Any]:
    routes = {
        f"/resources/{rid}/relationships": body(f"{label}_relationships_all.json"),
        f"/resources/{rid}/relationships/PARENT": body(f"{label}_relationships_parent.json"),
        f"/resources/{rid}/relationships/CHILD": body(f"{label}_relationships_child.json"),
    }
    routes.update(overrides)
    return routes


@pytest.mark.unit
def test_all_relationships_carry_their_direction():
    from vmware_aria.ops.catalog import get_resource_relationships

    result = get_resource_relationships(RoutedClient(_rel_routes(VM, "vm")), VM)

    for key in ENVELOPE_KEYS:
        assert key in result
    assert result["total"] == result["returned"] == 4
    rows = {r["name"]: r for r in result["items"]}
    assert rows["192.168.60.15"] == {
        "id": HOST, "name": "192.168.60.15", "kind": "HostSystem",
        "adapter_kind": "VMWARE", "direction": "parent",
    }
    assert rows["datastore1"]["direction"] == "child"
    assert rows["datastore1"]["kind"] == "Datastore"
    assert result["relationship_type"] == "ALL" and result["direction_note"] is None


@pytest.mark.unit
def test_a_single_type_is_one_call_and_labels_itself():
    from vmware_aria.ops.catalog import get_resource_relationships

    client = RoutedClient(_rel_routes(VM, "vm"))
    result = get_resource_relationships(client, VM, relationship_type="PARENT")

    assert client.calls == [f"/resources/{VM}/relationships/PARENT"]
    assert result["total"] == 3
    assert {r["direction"] for r in result["items"]} == {"parent"}


@pytest.mark.unit
def test_relationships_page_with_offset():
    from vmware_aria.ops.catalog import get_resource_relationships

    result = get_resource_relationships(RoutedClient(_rel_routes(HOST, "host")), HOST, limit=5, offset=5)
    assert (result["total"], result["returned"], result["next_offset"]) == (11, 5, 10)


class _PagingClient:
    """Serves a captured relationship list back in server-side pages."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.pages: list[int] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        page, size = params["page"], params["pageSize"]
        self.pages.append(page)
        chunk = self._rows[page * size : (page + 1) * size]
        return {"pageInfo": {"totalCount": len(self._rows), "page": page, "pageSize": size},
                "relationshipType": "CHILD", "resourceList": chunk}


@pytest.mark.unit
def test_every_server_page_is_walked(monkeypatch):
    from vmware_aria.ops import catalog

    monkeypatch.setattr(catalog, "_REL_PAGE_SIZE", 2)
    rows = body("host_relationships_child.json")["resourceList"]
    client = _PagingClient(rows)
    result = catalog.get_resource_relationships(client, HOST, relationship_type="CHILD", limit=500)

    assert result["total"] == len(rows) == 9
    assert len({r["id"] for r in result["items"]}) == 9
    assert client.pages == [0, 1, 2, 3, 4]


@pytest.mark.unit
def test_a_failed_relationship_read_raises_not_empty():
    from vmware_aria.ops.catalog import get_resource_relationships

    path = f"/resources/{VM}/relationships"
    client = RoutedClient(_rel_routes(VM, "vm", **{path: api_error(404, path)}))
    with pytest.raises(AriaApiError):
        get_resource_relationships(client, VM)


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        {"pageInfo": {"totalCount": 1}, "resources": []},
        {"resourceList": {"identifier": HOST}},
        {"resourceList": [{"resourceKey": {"name": "no id"}}]},
        [],
    ],
    ids=["no-resourceList", "not-a-list", "rows-without-identifier", "body-is-a-list"],
)
def test_an_unrecognised_relationship_shape_raises_not_empty(answer):
    from vmware_aria.ops.catalog import get_resource_relationships

    client = RoutedClient(_rel_routes(VM, "vm", **{f"/resources/{VM}/relationships": answer}))
    with pytest.raises(AriaApiError, match="relationships"):
        get_resource_relationships(client, VM)


@pytest.mark.unit
@pytest.mark.parametrize("label_answer", [api_error(500, "x"), {"resources": []}], ids=["http-500", "unrecognised"])
def test_an_unreadable_direction_label_keeps_the_rows_and_says_so(label_answer):
    from vmware_aria.ops.catalog import get_resource_relationships

    client = RoutedClient(_rel_routes(VM, "vm", **{f"/resources/{VM}/relationships/PARENT": label_answer}))
    result = get_resource_relationships(client, VM)

    assert result["total"] == 4
    assert all(r["direction"] is None for r in result["items"]), (
        "with PARENT unread, a row missing from CHILD could be a parent or something else"
    )
    assert "PARENT" in result["direction_note"]


@pytest.mark.unit
def test_a_row_in_neither_parent_nor_child_is_other_not_dropped():
    from vmware_aria.ops.catalog import get_resource_relationships

    extra = {"identifier": "99999999-0000-4000-8000-000000000000",
             "resourceKey": {"name": "odd", "adapterKindKey": "X", "resourceKindKey": "Y"}}
    all_body = body("vm_relationships_all.json")
    widened = {**all_body, "resourceList": [*all_body["resourceList"], extra],
               "pageInfo": {**all_body["pageInfo"], "totalCount": 5}}
    result = get_resource_relationships(
        RoutedClient(_rel_routes(VM, "vm", **{f"/resources/{VM}/relationships": widened})), VM
    )
    assert {r["name"]: r["direction"] for r in result["items"]}["odd"] == "other"
    assert result["total"] == 5


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["parent", "ANCESTOR", "DESCENDANT", ""])
def test_relationship_type_is_a_closed_set(bad):
    from vmware_aria.ops.catalog import get_resource_relationships

    with pytest.raises(ValueError, match="ALL, PARENT, CHILD"):
        get_resource_relationships(RoutedClient({}), VM, relationship_type=bad)


@pytest.mark.unit
def test_no_relationships_says_so():
    from vmware_aria.ops.catalog import get_resource_relationships

    empty = {"pageInfo": {"totalCount": 0, "page": 0, "pageSize": 1000}, "relationshipType": "CHILD", "resourceList": []}
    client = RoutedClient({f"/resources/{VM}/relationships/CHILD": empty})
    result = get_resource_relationships(client, VM, relationship_type="CHILD")
    assert result["items"] == [] and result["total"] == 0
    assert result["hint"] and "no CHILD relationships" in result["hint"]


@pytest.mark.unit
def test_relationship_names_are_sanitized():
    from vmware_aria.ops.catalog import get_resource_relationships

    row = {"identifier": HOST, "resourceKey": {"name": "evil\x1b[2J\x07", "adapterKindKey": "V\x00", "resourceKindKey": "H\x08"}}
    answer = {"pageInfo": {"totalCount": 1}, "resourceList": [row]}
    client = RoutedClient({f"/resources/{VM}/relationships/CHILD": answer})
    item = get_resource_relationships(client, VM, relationship_type="CHILD")["items"][0]
    assert not any(ord(c) < 32 for c in item["name"] + item["kind"] + item["adapter_kind"])


# ═══ MCP + CLI surfaces ═══════════════════════════════════════════════════════


@pytest.mark.unit
@pytest.mark.parametrize(
    "tool, kwargs",
    [
        ("list_metric_keys", {"resource_id": VM}),
        ("get_resource_properties", {"resource_id": VM}),
        ("get_resource_relationships", {"resource_id": VM}),
    ],
)
def test_mcp_tool_turns_an_unreadable_body_into_an_error_not_items(monkeypatch, tool, kwargs):
    import vmware_aria.mcp_server.server as server

    client = RoutedClient({
        f"/resources/{VM}": body("vm_resource_test_llm.json"),
        f"/resources/{VM}/statkeys": {"unexpected": True},
        f"/resources/{VM}/properties": {"unexpected": True},
        f"/resources/{VM}/relationships": {"unexpected": True},
    })
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    result = getattr(server, tool)(**kwargs)

    assert result.get("error"), result
    assert "items" not in result


@pytest.mark.unit
def test_mcp_tool_passes_the_live_shape_through(monkeypatch):
    import vmware_aria.mcp_server.server as server

    client = RoutedClient(_rel_routes(VM, "vm"))
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    result = server.get_resource_relationships(resource_id=VM)
    assert result["total"] == 4 and "error" not in result


@pytest.mark.unit
@pytest.mark.parametrize("command", ["keys", "properties", "relationships"])
def test_cli_commands_exist_under_resource(command):
    from typer.testing import CliRunner

    from vmware_aria.cli import app

    outcome = CliRunner().invoke(app, ["resource", command, "--help"])
    assert outcome.exit_code == 0, outcome.output


@pytest.mark.unit
def test_cli_relationships_prints_rows(monkeypatch):
    from typer.testing import CliRunner

    from vmware_aria import cli
    from vmware_aria.cli import app

    client = RoutedClient(_rel_routes(VM, "vm"))
    monkeypatch.setattr(cli, "_get_connection", lambda target=None, config=None: (client, None))
    outcome = CliRunner().invoke(app, ["resource", "relationships", VM])
    assert outcome.exit_code == 0, outcome.output
    assert "192.168.60.15" in outcome.output and "parent" in outcome.output


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ["resource", "relationships", VM, "--type", "parent"],
        ["resource", "properties", VM, "--limit", "0"],
        ["resource", "keys"],
    ],
    ids=["lower-case-type", "limit-zero", "no-id-no-kind"],
)
def test_cli_bad_input_is_one_red_line_not_a_traceback(monkeypatch, argv):
    """Live 2026-09-13: `--type parent` printed a full traceback."""
    from typer.testing import CliRunner

    from vmware_aria import cli
    from vmware_aria.cli import app

    client = RoutedClient(_rel_routes(VM, "vm"))
    monkeypatch.setattr(cli, "_get_connection", lambda target=None, config=None: (client, None))
    outcome = CliRunner().invoke(app, argv)
    assert outcome.exit_code == 2, outcome.output
    assert "Error:" in outcome.output
    assert outcome.exception is None or isinstance(outcome.exception, SystemExit)

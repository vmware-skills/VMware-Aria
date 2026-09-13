"""RESOURCE CATALOG tools (3, read-only).

list_metric_keys, get_resource_properties, get_resource_relationships — what a
resource reports, what it is, and what it is attached to. Registered on the
shared ``mcp`` instance; connection and error helpers resolve through
``vmware_aria.mcp_server.server`` at call time, like every other tool module.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp

_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
_DOCTOR_HINT = "Run 'vmware-aria doctor' to verify connectivity."


@mcp.tool(annotations=_READ)
@vmware_tool(risk_level="low")
def list_metric_keys(
    resource_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    adapter_kind: str = "VMWARE",
    key_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] Look up metric keys before get_resource_metrics or get_top_consumers; do not guess (cpu|demand_average does not exist on VMs).

    resource_id: keys that resource reports, with name/unit from its kind; definition is found, found_by_instance, not_defined_for_kind (see unjoined_keys) or not_read (definitions_status undetermined: name/unit unknown, not absent). resource_kind: keys the kind defines, not all collected.

    Paginated envelope with next_offset: pass it back as offset until null. An unreadable key list is an error, never empty.

    Args:
        resource_id: Resource UUID (from list_resources). This or resource_kind, not both.
        resource_kind: Kind key, e.g. VirtualMachine, HostSystem, Datastore.
        adapter_kind: Adapter kind for resource_kind. Default VMWARE.
        key_filter: Case-insensitive substring of key or name, e.g. "mem|".
        limit: Page size, 1-500 (default 100).
        offset: Rows to skip; the previous next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.catalog import list_metric_keys as _keys

        return _keys(
            server._get_connection(target), resource_id=resource_id, resource_kind=resource_kind,
            adapter_kind=adapter_kind, key_filter=key_filter, limit=limit, offset=offset,
        )
    except Exception as e:
        return {"error": server._safe_error(e, "list_metric_keys"), "hint": _DOCTOR_HINT}


@mcp.tool(annotations=_READ)
@vmware_tool(risk_level="low")
def get_resource_properties(
    resource_id: str,
    name_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] Current property values Aria holds for one resource: power state, parent host and vCenter (summary|parentHost, summary|parentVcenter), configured CPU/memory, extraConfig flags such as config|extraConfig|mem_hotadd. Use this for configuration facts; use get_resource_metrics for time-series values.

    Returns a paginated envelope of {name, value} rows sorted by name (value is a string, null when the property carries none), with next_offset — pass it back as offset until null. A failed or unrecognised read is an error, never an empty list; a missing resource is a 404.

    Args:
        resource_id: Resource UUID (from list_resources).
        name_filter: Case-insensitive substring of the property name, e.g. "summary|".
        limit: Page size, 1-500 (default 100). Out-of-range is rejected.
        offset: Rows to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.catalog import get_resource_properties as _props

        return _props(server._get_connection(target), resource_id, name_filter=name_filter, limit=limit, offset=offset)
    except Exception as e:
        return {"error": server._safe_error(e, "get_resource_properties"), "hint": _DOCTOR_HINT}


@mcp.tool(annotations=_READ)
@vmware_tool(risk_level="low")
def get_resource_relationships(
    resource_id: str,
    relationship_type: str = "ALL",
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] Navigate the inventory: resources related to one resource (VM -> host, datastore, folder; host -> VMs, datacenter). Each row: id, name, kind, adapter_kind, direction (parent, child, both, other). Call again on a returned id to walk further up or down.

    Returns a paginated envelope with next_offset — pass it back as offset until null. With ALL, direction is null when the PARENT/CHILD labels could not be read, and direction_note says why. A failed or unrecognised read is an error, never an empty list.

    Args:
        resource_id: Resource UUID (from list_resources).
        relationship_type: Exactly ALL, PARENT or CHILD (upper case). Default ALL; ANCESTOR/DESCENDANT are rejected.
        limit: Page size, 1-500 (default 100). Out-of-range is rejected.
        offset: Rows to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.catalog import get_resource_relationships as _rels

        return _rels(
            server._get_connection(target), resource_id,
            relationship_type=relationship_type, limit=limit, offset=offset,
        )
    except Exception as e:
        return {"error": server._safe_error(e, "get_resource_relationships"), "hint": _DOCTOR_HINT}

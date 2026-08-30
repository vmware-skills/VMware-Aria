"""ALERT read tools (4, read-only).

list_alerts, get_alert, list_alert_definitions, list_symptom_definitions.

The write/destructive alert tools (acknowledge_alert, cancel_alert,
create_alert_definition, set_alert_definition_state, delete_alert_definition)
keep their definitions in ``vmware_aria/mcp_server/server.py`` because their confirmed-gate
preview contract is asserted there by AST inspection in
``tests/test_no_destructive_ops.py``.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_alerts(
    active_only: bool = True,
    criticality: Optional[str] = None,
    resource_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List alerts from Aria Operations.

    Returns alert summaries: name, criticality, status, impact, resource_id,
    timestamps, and control state. The Alert model does not carry a resource
    name — resolve it via get_resource(resource_id).

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint, next_offset. Check
    truncated before calling this the complete set.

    Page it: limit is the page size (1-500; 0, negatives and anything above 500
    are rejected, not clamped), offset is how many rows to skip, and
    next_offset is the offset of the next page — pass it back as offset and
    stop when it is null. Do not loop on truncated: that says this page is not
    the whole collection, so it stays true on the last page of a walk.

    Args:
        active_only: Return only active (non-cancelled) alerts. Default True.
        criticality: Filter by criticality: INFORMATION, WARNING, IMMEDIATE, CRITICAL.
        resource_id: Scope alerts to a specific resource UUID.
        limit: Page size, 1–500 (default 100). Out-of-range is rejected.
        offset: Alerts to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alerts import list_alerts as _list

        return _list(server._get_connection(target), active_only=active_only, criticality=criticality, resource_id=resource_id, limit=limit, offset=offset)
    except Exception as e:
        return {"error": server._safe_error(e, "list_alerts"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_alert(alert_id: str, target: Optional[str] = None) -> dict:
    """[READ] Get full details for one alert by UUID, including its contributing (triggered) symptoms. Use this after list_alerts to drill into a single alert; use list_alerts to discover or filter them. Returns one alert object: name, criticality, status, impact, resource_id, start/update/cancel timestamps, control state, and symptoms (each with the condition that triggered it). Gotcha: an empty symptoms list normally means the alert has no triggered symptoms, but if a "symptoms_note" key is present the list is UNKNOWN rather than empty — the response shape was unrecognised or the lookup failed, so do not tell the user the alert fired for no reason. The Alert model does not carry a resource name — resolve it via get_resource(resource_id), or call investigate_alert to do that correlation in one step. Recommendations hang off the alert definition, not the alert. To act on the alert afterwards, use acknowledge_alert or cancel_alert.

    Args:
        alert_id: The alert UUID (from list_alerts).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alerts import get_alert as _get

        return _get(server._get_connection(target), alert_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_alert"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_alert_definitions(
    name_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List alert definitions (templates that generate alerts when triggered).

    criticality is the max severity across the definition's states[] (the
    AlertDefinition model has no top-level criticality or enabled field).
    Pass a returned id to set_alert_definition_state to enable or disable it.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint, next_offset. Check
    truncated before calling this the complete set.

    Page it: limit is the page size (1-500; 0, negatives and anything above 500
    are rejected, not clamped), offset is how many rows to skip, and
    next_offset is the offset of the next page — pass it back as offset and
    stop when it is null. Do not loop on truncated: that says this page is not
    the whole collection, so it stays true on the last page of a walk.

    Args:
        name_filter: Substring filter on definition name (case-insensitive).
        limit: Page size, 1–500 (default 100). Out-of-range is rejected.
        offset: Definitions to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alerts import list_alert_definitions as _list

        return _list(server._get_connection(target), name_filter=name_filter, limit=limit, offset=offset)
    except Exception as e:
        return {"error": server._safe_error(e, "list_alert_definitions"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_symptom_definitions(
    name_filter: Optional[str] = None,
    resource_kind: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List symptom definitions — use the returned IDs when calling create_alert_definition.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint, next_offset. Check
    truncated before calling this the complete set.

    Page it: limit is the page size (1-500; 0, negatives and anything above 500
    are rejected, not clamped), offset is how many rows to skip, and
    next_offset is the offset of the next page — pass it back as offset and
    stop when it is null. Do not loop on truncated: that says this page is not
    the whole collection, so it stays true on the last page of a walk.

    Args:
        name_filter: Substring filter on symptom name (case-insensitive).
        resource_kind: Optional resource kind filter, e.g. VirtualMachine, HostSystem.
        limit: Page size, 1–500 (default 100). Out-of-range is rejected.
        offset: Definitions to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alerts import list_symptom_definitions as _list

        return _list(server._get_connection(target), name_filter=name_filter, resource_kind=resource_kind, limit=limit, offset=offset)
    except Exception as e:
        return {"error": server._safe_error(e, "list_symptom_definitions"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def investigate_alert(alert_id: str, target: Optional[str] = None) -> dict:
    """[READ] Resolve one alert to its affected resource in a single call — use this instead of chaining get_alert then get_resource by hand.

    Does the whole alert-to-object correlation server-side: fetches the alert,
    reads its resourceId, fetches that resource, and confirms the resource name
    and kind before suggesting anything downstream.

    Returns five always-present keys: alert (Aria's values verbatim), resource
    (or null), correlation (both UUIDs labelled, plus confirmed name, kind and
    a confirmed flag), next_step (which vmware-monitor tool to call next, or
    null), and warnings (empty on success).

    Gotchas: alert_id is the alert UUID from list_alerts, NOT the resource
    UUID — mixing them up is the most common error here; the correlation block
    labels each. An unresolvable resource degrades to a warning plus nulls
    rather than an error, so the alert is never lost. Never match the resource
    against vCenter inventory unless correlation.confirmed is true.

    Args:
        alert_id: The alert UUID from list_alerts (not the resource UUID).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.investigate import investigate_alert as _investigate

        return _investigate(server._get_connection(target), alert_id)
    except Exception as e:
        return {"error": server._safe_error(e, "investigate_alert"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

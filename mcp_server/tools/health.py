"""HEALTH tools (2, read-only).

get_aria_health, list_collector_groups.
"""

from typing import Optional

from vmware_policy import vmware_tool

from mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_aria_health(target: Optional[str] = None) -> dict:
    """[READ] Check Aria Operations platform node status (ONLINE/OFFLINE).

    Returns overall_status ("ONLINE" when all internal services run, else
    "OFFLINE" — the endpoint itself answers 503 when offline), healthy bool,
    system_time_ms, and details. Use this to verify Aria Operations is
    functioning before investigating monitoring blind spots; per-service
    breakdown is not exposed by the public API.

    Args:
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.health import get_aria_health as _get

        return _get(server._get_connection(target))
    except Exception as e:
        return {"error": server._safe_error(e, "get_aria_health"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_collector_groups(target: Optional[str] = None) -> dict:
    """[READ] List Aria Operations collector groups and their member collector status.

    Collectors are remote agents that gather metrics from vSphere and other adapters.
    Check this when resources appear missing from Aria Operations or metrics are stale.
    Groups list member collector IDs; details (name, state UP/DOWN, local) are
    enriched via one extra collectors call.

    Returns a result envelope: the rows under `items`, plus `returned`,
    `limit`, `total` (null when the API reports no collection size),
    `truncated` and `hint`. Check `truncated` before describing this as the
    complete set — when it is true, more rows exist beyond this page.

    Args:
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.health import list_collector_groups as _list

        return _list(server._get_connection(target))
    except Exception as e:
        return {"error": server._safe_error(e, "list_collector_groups"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

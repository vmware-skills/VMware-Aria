"""HEALTH tools (2, read-only).

get_aria_health, list_collector_groups.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_aria_health(target: Optional[str] = None) -> dict:
    """[READ] Check Aria Operations platform health, per service, plus its version.

    Returns assessment: HEALTHY, DEGRADED (some services OK, some ERROR — the
    platform still answers), DOWN (no service OK) or UNKNOWN (breakdown
    unreadable). overall_status is the node's own flag: OFFLINE whenever any
    one service is not running, so OFFLINE alone is not an outage. Also
    services (name, health, details; null when unreadable), services_not_ok,
    healthy, system_time_ms, details, and product_version / product_line
    ("8.x", "9.x") / release_name — check the line before assuming 9.x-only
    tools (fleet_*, findings_list, promql_query) exist. A 503 is reported,
    never raised. If HEALTHY but data looks stale, check list_collector_groups.

    Args:
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

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
    enriched via one extra collectors call. A DOWN collector means
    list_resources and the metric tools see stale or missing data for
    everything behind it.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint. Check truncated
    before calling this the complete set.

    Args:
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.health import list_collector_groups as _list

        return _list(server._get_connection(target))
    except Exception as e:
        return {"error": server._safe_error(e, "list_collector_groups"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

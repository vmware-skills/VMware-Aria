"""CAPACITY tools (4, read-only).

get_capacity_overview, get_remaining_capacity, get_time_remaining,
list_rightsizing_recommendations.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_capacity_overview(cluster_id: str, target: Optional[str] = None) -> dict:
    """[READ] Returns a capacity overview for a cluster — the group-level remaining-capacity percentage (capacity_remaining_pct, which only exists at group level) plus per-dimension (cpu/mem/diskspace) absolute remaining capacity and projected days-until-full, from the OnlineCapacityAnalytics metrics. Values are None while capacity analytics are still warming up on a fresh instance. Start here when assessing overall cluster capacity health; for absolute headroom values use get_remaining_capacity, and for just the exhaustion projections use get_time_remaining.

    Args:
        cluster_id: The cluster resource UUID (ClusterComputeResource, from list_resources).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.capacity import get_capacity_overview as _get

        return _get(server._get_connection(target), cluster_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_capacity_overview"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_remaining_capacity(resource_id: str, target: Optional[str] = None) -> dict:
    """[READ] Get remaining capacity headroom for a cluster or host — how much more workload fits before hitting limits. Returns the group-level capacity_remaining_pct (only available at group level) plus one entry per capacity dimension (cpu, mem, diskspace) with remaining_value (absolute, unit per dimension e.g. MHz/KB), from the OnlineCapacityAnalytics demand model. Values are None while capacity analytics warm up. Use get_capacity_overview for the combined view, or get_time_remaining for projected days-until-full.

    Args:
        resource_id: The resource UUID — a ClusterComputeResource or HostSystem (from list_resources).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.capacity import get_remaining_capacity as _get

        return _get(server._get_connection(target), resource_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_remaining_capacity"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_time_remaining(resource_id: str, target: Optional[str] = None) -> dict:
    """[READ] Predict when a cluster will exhaust its capacity based on usage trends.

    Returns `time_remaining`: one entry per capacity dimension (cpu, mem,
    diskspace) with projected days until full. Use get_capacity_overview
    instead when you also want current headroom — this tool returns only the
    projections. Days are None while capacity analytics warm up on a fresh
    instance, and None does not mean unlimited.

    Args:
        resource_id: The resource UUID (typically ClusterComputeResource).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.capacity import get_time_remaining as _get

        return _get(server._get_connection(target), resource_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_time_remaining"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_rightsizing_recommendations(
    resource_id: Optional[str] = None,
    limit: int = 50,
    target: Optional[str] = None,
) -> dict:
    """[READ] List VM rightsizing data — recommended CPU/memory size per VM.

    Reads the OnlineCapacityAnalytics recommendedSize metrics (the public
    API's rightsizing signal; the UI Rightsize page uses internal APIs).
    Compare against the VM's provisioned size to find over/under-provisioning.
    Values are None while capacity analytics warm up. One stats call per VM —
    keep limit modest. Get VM UUIDs from list_resources.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint. Check truncated
    before calling this the complete set.

    Args:
        resource_id: Optional VM resource UUID to scope to a single VM.
        limit: Maximum VMs to evaluate when listing (1–100). Default 50.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.capacity import list_rightsizing_recommendations as _list

        return _list(server._get_connection(target), resource_id=resource_id, limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "list_rightsizing_recommendations"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

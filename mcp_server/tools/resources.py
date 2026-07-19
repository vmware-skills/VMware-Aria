"""RESOURCE tools (5, read-only).

list_resources, get_resource, get_resource_metrics, get_resource_health,
get_top_consumers.

Each tool registers on the shared ``mcp`` instance and resolves the
connection/error helpers through ``mcp_server.server`` at call time, so the
single patch surface ``server._get_connection`` / ``server._safe_error``
(re-exported from ``_shared``) governs every tool exactly as the former
monolithic ``server.py`` did.
"""

from typing import Optional

from vmware_policy import vmware_tool

from mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_resources(
    resource_kind: str = "VirtualMachine",
    limit: int = 100,
    name_filter: Optional[str] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] List resources in Aria Operations filtered by kind.

    Returns a result envelope: the rows under `items`, plus `returned`,
    `limit`, `total` (null when the API reports no collection size),
    `truncated` and `hint`. Check `truncated` before describing this as the
    complete set — when it is true, more rows exist beyond this page.

    Args:
        resource_kind: Resource kind to list. Common values: VirtualMachine,
            HostSystem, ClusterComputeResource, Datastore, Datacenter.
        limit: Maximum number of results. Default 100. Results are paginated
            automatically, so a larger limit returns more than one page.
        name_filter: Optional substring to filter by resource name (case-insensitive).
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.resources import list_resources as _list

        return _list(server._get_connection(target), resource_kind=resource_kind, limit=limit, name_filter=name_filter)
    except Exception as e:
        return {"error": server._safe_error(e, "list_resources"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_resource(resource_id: str, target: Optional[str] = None) -> dict:
    """[READ] Get full details for one resource by UUID, including health, risk, and efficiency badges (each a color plus 0-100 score), resource kind, adapter kind, identifiers, and status states. Use after list_resources to inspect a single resource in depth; use list_resources (not this tool) to discover UUIDs by kind or name. For just the health score use get_resource_health; for time-series metrics use get_resource_metrics.

    Args:
        resource_id: The resource UUID (from list_resources).
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.resources import get_resource as _get

        return _get(server._get_connection(target), resource_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_resource"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_resource_metrics(
    resource_id: str,
    metric_keys: list[str],
    hours: int = 1,
    rollup_type: str = "AVG",
    target: Optional[str] = None,
) -> dict:
    """[READ] Fetch time-series metric statistics for a resource.

    Args:
        resource_id: The resource UUID.
        metric_keys: List of metric keys to fetch, e.g. ["cpu|usage_average", "mem|usage_average"].
            Common keys: cpu|usage_average, mem|usage_average, disk|usage_average,
            net|usage_average, cpu|demand_average, mem|workload.
        hours: Number of hours of history to retrieve. Default 1.
        rollup_type: Aggregation type: AVG, MAX, MIN, SUM, COUNT, LATEST. Default AVG.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        import time as _time

        from vmware_aria.ops.resources import get_resource_metrics as _get_metrics

        client = server._get_connection(target)
        end_ms = int(_time.time() * 1000)
        begin_ms = end_ms - (hours * 3_600_000)
        return _get_metrics(client, resource_id, metric_keys, begin_time_ms=begin_ms, end_time_ms=end_ms, rollup_type=rollup_type)
    except Exception as e:
        return {"error": server._safe_error(e, "get_resource_metrics"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_resource_health(resource_id: str, target: Optional[str] = None) -> dict:
    """[READ] Get the health, risk, and efficiency badge scores for a resource.

    Badges come from the resource's badges[] array. Scores are 0–100
    (higher = healthier for HEALTH; -1 = unknown) with a color per badge.

    Args:
        resource_id: The resource UUID.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.resources import get_resource_health as _get_health

        return _get_health(server._get_connection(target), resource_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_resource_health"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_top_consumers(
    metric_key: str = "cpu|usage_average",
    resource_kind: str = "VirtualMachine",
    top_n: int = 10,
    target: Optional[str] = None,
) -> dict:
    """[READ] Query resources with highest consumption of a given metric.

    Returns a result envelope: the rows under `items`, plus `returned`,
    `limit`, `total` (null when the API reports no collection size),
    `truncated` and `hint`. Check `truncated` before describing this as the
    complete set — when it is true, more rows exist beyond this page.

    Args:
        metric_key: The metric to rank by. Common values: cpu|usage_average,
            mem|usage_average, disk|usage_average, net|usage_average.
        resource_kind: Resource kind to scope the query. Default VirtualMachine.
        top_n: Number of top consumers to return (max 50). Default 10.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.resources import get_top_consumers as _get_top

        return _get_top(server._get_connection(target), metric_key=metric_key, resource_kind=resource_kind, top_n=top_n)
    except Exception as e:
        return {"error": server._safe_error(e, "get_top_consumers"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

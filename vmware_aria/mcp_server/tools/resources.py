"""RESOURCE tools (5, read-only).

list_resources, get_resource, get_resource_metrics, get_resource_health,
get_top_consumers.

Each tool registers on the shared ``mcp`` instance and resolves the
connection/error helpers through ``vmware_aria.mcp_server.server`` at call time, so the
single patch surface ``server._get_connection`` / ``server._safe_error``
(re-exported from ``_shared``) governs every tool exactly as the former
monolithic ``server.py`` did.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_resources(
    resource_kind: str = "VirtualMachine",
    limit: int = 100,
    name_filter: Optional[str] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] List resources in Aria Operations filtered by kind. Start here: this turns a name or kind into the UUID other resource tools need. Then call get_resource for detail on one row.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint. Check truncated
    before calling this the complete set.

    Args:
        resource_kind: e.g. VirtualMachine, HostSystem,
            ClusterComputeResource, Datastore, Datacenter.
        limit: Maximum number of results. Default 100. Paginated
            automatically, so a larger limit spans more than one page.
        name_filter: Substring filter on resource name (case-insensitive).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.resources import list_resources as _list

        return _list(server._get_connection(target), resource_kind=resource_kind, limit=limit, name_filter=name_filter)
    except Exception as e:
        return {"error": server._safe_error(e, "list_resources"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_resource(resource_id: str, target: Optional[str] = None) -> dict:
    """[READ] Returns one resource object: id, name, kind, adapter kind, identifiers, status states, and the health/risk/efficiency badges (each a color plus 0-100 score, null when Aria has not scored that badge). Use this after list_resources to inspect a single UUID in depth — it does not accept a name, so use list_resources to discover UUIDs by kind or name. For just the badge scores use get_resource_health; for time-series metrics use get_resource_metrics.

    Args:
        resource_id: The resource UUID (from list_resources).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

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

    Returns a dict keyed by metric key, each mapping to a list of
    {timestamp_ms, value} points — not an envelope. Use this for history; for
    a single current score use get_resource_health instead. A key the API has
    no data for does not appear in the result at all, so check which keys came
    back before reporting a metric as zero.

    Args:
        resource_id: The resource UUID.
        metric_keys: Metric keys to fetch, e.g. ["cpu|usage_average",
            "mem|usage_average", "disk|usage_average", "net|usage_average"].
        hours: Number of hours of history to retrieve. Default 1.
        rollup_type: Aggregation type: AVG, MAX, MIN, SUM, COUNT, LATEST. Default AVG.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

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

    Returns the three scores and their colors, from the resource's badges[]
    array. Scores are 0–100 (higher = healthier for HEALTH). Use this when the
    scores are all you need; use get_resource for the whole object, or
    list_alerts(resource_id=...) for what drove a low score. A score is null
    (or -1) when Aria has not computed that badge — that does not mean healthy.

    Args:
        resource_id: The resource UUID.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

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
    """[READ] Query resources with highest consumption of a given metric. Then call get_resource_metrics on a returned id for its history.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint. Check truncated
    before calling this the complete set.

    Args:
        metric_key: Metric to rank by, e.g. cpu|usage_average,
            mem|usage_average, disk|usage_average.
        resource_kind: Resource kind to scope the query. Default VirtualMachine.
        top_n: Number of top consumers to return (max 50). Default 10.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.resources import get_top_consumers as _get_top

        return _get_top(server._get_connection(target), metric_key=metric_key, resource_kind=resource_kind, top_n=top_n)
    except Exception as e:
        return {"error": server._safe_error(e, "get_top_consumers"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

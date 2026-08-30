"""ANOMALY tools (2, read-only).

list_anomalies, get_resource_riskbadge.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_anomalies(
    resource_id: Optional[str] = None,
    limit: int = 50,
    target: Optional[str] = None,
) -> dict:
    """[READ] Report per-resource anomaly counts (System Attributes|total_alarms metric).

    The suite-api does not expose the UI's anomalous-metrics list; this is the
    Total Anomalies metric — active symptoms, events and DT violations on the
    object and its children. With resource_id: that resource's count. Without:
    ranks every VM in the environment and returns the worst `limit` of them.
    For root cause, follow up with list_alerts(resource_id=...).

    `limit` bounds the answer, not the scan — raising it does not widen the
    search, and lowering it does not hide worse objects. The whole inventory is
    read either way, in bulk pages, because the ranking metric is not a field
    the appliance can sort on.

    Returns a paginated envelope: flagged rows worst-first under items, plus
    returned, limit, total, truncated, hint, scanned (objects examined),
    vm_total and scan_complete. Only flagged VMs are returned, so a short list
    is not by itself proof the environment is clean. When scan_complete is
    true, total is the number of anomalous objects found and truncated is
    exact; when it is false the scan hit its cap, total is the environment's VM
    count, and a note says the ranking is partial — an unexamined object could
    outrank every row shown.

    Args:
        resource_id: Optional resource UUID to scope to a single resource.
        limit: Maximum ranked rows to return (1–500). Default 50. Rejected, not
            clamped, when out of range.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.anomaly import list_anomalies as _list

        return _list(server._get_connection(target), resource_id=resource_id, limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "list_anomalies"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_resource_riskbadge(resource_id: str, target: Optional[str] = None) -> dict:
    """[READ] Get the risk badge score for a resource (0–100, higher = more risk of future problems).

    The risk badge predicts likelihood of performance degradation or
    availability issues based on current trends and workload patterns.
    Returns `risk_score` and `risk_color` for the one resource. Use this when
    the risk number is all you want; get_resource_health returns health and
    efficiency alongside it. The score is null when Aria has not computed a
    risk badge, and the badge does not say what is wrong — use
    list_alerts(resource_id=...) for the contributing alerts.

    Args:
        resource_id: The resource UUID.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.anomaly import get_resource_riskbadge as _get

        return _get(server._get_connection(target), resource_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_resource_riskbadge"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

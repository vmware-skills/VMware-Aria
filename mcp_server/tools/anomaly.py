"""ANOMALY tools (2, read-only).

list_anomalies, get_resource_riskbadge.
"""

from typing import Optional

from vmware_policy import vmware_tool

from mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_anomalies(
    resource_id: Optional[str] = None,
    limit: int = 50,
    target: Optional[str] = None,
) -> dict:
    """[READ] Report per-resource anomaly counts (System Attributes|total_alarms metric).

    The public suite-api does not expose the UI's anomalous-metrics list; this
    returns the Total Anomalies metric — active anomalies (symptoms, events,
    DT violations) on the object and its children. With
    resource_id: that resource's count. Without: scans up to `limit` VMs and
    returns those with non-zero counts, sorted descending. For root cause,
    follow up with list_alerts(resource_id=...). One stats call per VM when
    listing — keep limit modest.

    Returns a result envelope: the flagged rows under `items`, plus
    `returned`, `limit`, `total`, `truncated`, `hint`, and `scanned` (how
    many VMs were examined). A short list is not proof the environment is
    clean — check `truncated`, which is true whenever VMs went unscanned.

    Args:
        resource_id: Optional resource UUID to scope to a single resource.
        limit: Maximum VMs to scan when listing (1–100). Default 50.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.anomaly import list_anomalies as _list

        return _list(server._get_connection(target), resource_id=resource_id, limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "list_anomalies"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_resource_riskbadge(resource_id: str, target: Optional[str] = None) -> dict:
    """[READ] Get the risk badge score for a resource (0–100, higher = more risk of future problems).

    The risk badge predicts likelihood of performance degradation or availability issues
    based on current trends and workload patterns.

    Args:
        resource_id: The resource UUID.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.anomaly import get_resource_riskbadge as _get

        return _get(server._get_connection(target), resource_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_resource_riskbadge"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

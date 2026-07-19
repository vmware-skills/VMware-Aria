"""REPORT tools (4: 3 read + generate_report write).

list_report_definitions, generate_report, list_reports, get_report.

delete_report keeps its definition in ``mcp_server/server.py`` because its
confirmed-gate preview contract is asserted there by AST inspection in
``tests/test_no_destructive_ops.py``.
"""

from typing import Optional

from vmware_policy import vmware_tool

from mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_report_definitions(
    name_filter: Optional[str] = None,
    limit: int = 100,
    target: Optional[str] = None,
) -> dict:
    """[READ] List available report definition templates in Aria Operations.

    Returns a result envelope: the rows under `items`, plus `returned`,
    `limit`, `total` (null when the API reports no collection size),
    `truncated` and `hint`. Check `truncated` before describing this as the
    complete set — when it is true, more rows exist beyond this page.

    Args:
        name_filter: Optional substring to filter by report name (case-insensitive).
        limit: Maximum number of definitions to return (1–500). Default 100.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.reports import list_report_definitions as _list

        return _list(server._get_connection(target), name_filter=name_filter, limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "list_report_definitions"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def generate_report(
    definition_id: str,
    resource_ids: Optional[list[str]] = None,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Trigger generation of a report from a report definition template.

    Returns immediately with a report_id and PENDING status.
    Poll get_report(report_id) until status == COMPLETED, then use download_url.

    Args:
        definition_id: Report definition (template) UUID from list_report_definitions.
        resource_ids: REQUIRED — at least one resource UUID. The Report API
            generates against a single root resource (first ID is used); pass
            a cluster/datacenter UUID to cover its children. Find IDs via
            list_resources.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.reports import generate_report as _generate

        return _generate(
            server._get_connection(target),
            definition_id=definition_id,
            resource_ids=resource_ids,
            audit_logger=server._audit,
            target_name=server._target_name(target),
        )
    except Exception as e:
        return {"error": server._safe_error(e, "generate_report"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_reports(
    definition_id: Optional[str] = None,
    limit: int = 50,
    target: Optional[str] = None,
) -> dict:
    """[READ] List generated reports, optionally filtered by report definition.

    Returns a result envelope: the rows under `items`, plus `returned`,
    `limit`, `total` (null when the API reports no collection size),
    `truncated` and `hint`. Check `truncated` before describing this as the
    complete set — when it is true, more rows exist beyond this page.

    Args:
        definition_id: Optional report definition UUID to filter results.
        limit: Maximum number of reports to return (1–200). Default 50.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.reports import list_reports as _list

        return _list(server._get_connection(target), definition_id=definition_id, limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "list_reports"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_report(
    report_id: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get status and download URLs for a generated report.

    Args:
        report_id: The report UUID (from generate_report or list_reports).
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from mcp_server import server

    try:
        from vmware_aria.ops.reports import get_report as _get

        return _get(server._get_connection(target), report_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_report"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

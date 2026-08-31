"""REPORT tools (4: 3 read + generate_report write).

list_report_definitions, generate_report, list_reports, get_report.

delete_report keeps its definition in ``vmware_aria/mcp_server/server.py`` because its
confirmed-gate preview contract is asserted there by AST inspection in
``tests/test_no_destructive_ops.py``.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_report_definitions(
    name_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List available report definition templates in Aria Operations. Pass a returned id to generate_report to run one.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint, next_offset. Check
    truncated before calling this the complete set.

    Page it: limit is the page size (1-500; 0, negatives and anything above 500
    are rejected, not clamped), offset is how many rows to skip, and
    next_offset is the offset of the next page — pass it back as offset and
    stop when it is null. Do not loop on truncated: that says this page is not
    the whole collection, so it stays true on the last page of a walk.

    Args:
        name_filter: Substring filter on report name (case-insensitive).
        limit: Page size, 1–500 (default 100). Out-of-range is rejected.
        offset: Definitions to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.reports import list_report_definitions as _list

        return _list(server._get_connection(target), name_filter=name_filter, limit=limit, offset=offset)
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

    Returns immediately with a report_id and PENDING status; it does not wait
    for the file. Poll get_report(report_id) until status == COMPLETED, then
    use download_url.

    Args:
        definition_id: Report definition (template) UUID from list_report_definitions.
        resource_ids: REQUIRED — at least one resource UUID. The Report API
            generates against a single root resource (first ID is used); pass
            a cluster/datacenter UUID to cover its children. Find IDs via
            list_resources.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

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
    """[READ] List generated reports, optionally filtered by report definition. Pass a returned id to get_report for its status and download URLs.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint. Check truncated
    before calling this the complete set. This one has no offset — it is
    bounded by limit alone, so a truncated page cannot be walked past.

    Args:
        definition_id: Optional report definition UUID to filter results.
        limit: Max reports to return (1–200). Default 50.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

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

    Returns id, name (the report's title, fetched from its definition — null if
    that definition is gone; this endpoint carries no title of its own),
    description (the definition's explanatory blurb, which is NOT the title),
    status (PENDING, RUNNING, COMPLETED, FAILED),
    definition_id, completion_time (the appliance's own rendering, e.g.
    "Sun Aug 30 04:40:08 UTC 2026"), completion_time_ms (epoch ms, or null when
    the appliance sent a date string), download_url (PDF) and csv_url. Use
    this to poll after generate_report. The URLs are always constructed, so a
    download_url is present even while the report is still PENDING — check
    status before fetching it.

    Args:
        report_id: The report UUID (from generate_report or list_reports).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.reports import get_report as _get

        return _get(server._get_connection(target), report_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_report"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

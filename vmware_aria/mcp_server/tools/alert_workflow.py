"""ALERT WORKFLOW tools (3): list_alert_notes (read), add_alert_note (write), get_alert_recommendations (read).

add_alert_note is ``risk_level="low"`` with no confirmed gate: a note changes
neither the alert nor monitoring, it only records text beside the alert — the
same judgement the family made for VDI's session_send_message. It is still a
[WRITE], audited, and @guarded on the CLI.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp

_HINT = "Run 'vmware-aria doctor' to verify connectivity."


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_alert_notes(
    alert_id: str,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List the notes on one alert — who is handling it and what was done. Each row: id, note text, type (USER or SYSTEM), user_name, user_id, created_time_ms.

    Use add_alert_note to record a new one. An unknown alert id is an error
    (HTTP 404), not an empty list.

    Returns a paginated envelope: items, returned, limit, total (null when the
    API reports no size), truncated, hint, next_offset, and notes_note — null
    when the answer was read; otherwise items is UNKNOWN, not empty, and must
    not be reported as "no notes".

    Page it: limit is the page size (1-500; 0, negatives and anything above 500
    are rejected, not clamped), offset is how many rows to skip, and
    next_offset is the offset of the next page — pass it back as offset and
    stop when it is null. Do not loop on truncated.

    Args:
        alert_id: The alert UUID from list_alerts (not the resource UUID).
        limit: Page size, 1-500 (default 100). Out-of-range is rejected.
        offset: Notes to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alert_notes import list_alert_notes as _list

        return _list(server._get_connection(target), alert_id, limit=limit, offset=offset)
    except Exception as e:
        return {"error": server._safe_error(e, "list_alert_notes"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="low")
def add_alert_note(
    alert_id: str,
    note: str,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Add a note to an alert to record who is handling it and what was done. Does not change the alert's status or ownership — use acknowledge_alert to take ownership.

    Not idempotent: calling twice adds two notes. Returns created (the stored
    note) and confirmation_note — when created is null the appliance did not
    confirm the note; run list_alert_notes before retrying. There is no undo
    tool for notes.

    Args:
        alert_id: The alert UUID from list_alerts (not the resource UUID).
        note: Note text, e.g. "Taking this: rebooting esx-03". Empty is rejected.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alert_notes import add_alert_note as _add

        return _add(
            server._get_connection(target),
            alert_id,
            note,
            audit_logger=server._audit,
            target_name=server._target_name(target),
        )
    except Exception as e:
        return {"error": server._safe_error(e, "add_alert_note"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_alert_recommendations(alert_id: str, target: Optional[str] = None) -> dict:
    """[READ] Get the prioritized recommendations for one alert — what Aria suggests doing about it — by resolving the alert to its alert definition and the recommendation text.

    Returns alert_id, alert_name, criticality, alert_definition_id,
    state_severity, status, recommendations (each: id, priority — lower is more
    important, description, action, lookup) and note. status: "found";
    "partial" (some text could not be read — ids and priorities are still
    listed, description null means unknown, not blank); "none_defined" (the
    definition defines none — recommendations is []); "unknown" (the definition
    could not be read — recommendations is null and must NOT be reported as "no
    recommendations"). An unreadable alert is an error.

    Args:
        alert_id: The alert UUID from list_alerts (not the resource UUID).
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alert_recommendations import get_alert_recommendations as _get

        return _get(server._get_connection(target), alert_id)
    except Exception as e:
        return {"error": server._safe_error(e, "get_alert_recommendations"), "hint": _HINT}

"""MAINTENANCE tools (3): start / end resource maintenance (write), list_maintenance_schedules (read).

The two writes carry a ``confirmed=False`` preview gate like acknowledge_alert
(same risk: reversible, but alerting on the resource stops), and declare each
other as their undo. ``tests/test_no_destructive_ops.py`` finds the gate here by
searching the whole ``mcp_server`` tree.
"""

from typing import Any, Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp

_HINT = "Run 'vmware-aria doctor' to verify connectivity."


def _changed(result: Any) -> bool:
    """True only for a result that reports an executed write."""
    return isinstance(result, dict) and not result.get("preview") and not result.get("error")


def _undo_start(params: dict, result: Any) -> Optional[dict]:
    if not _changed(result):
        return None
    return {
        "tool": "end_resource_maintenance",
        "params": {"resource_id": params.get("resource_id"), "confirmed": True, "target": params.get("target")},
        "skill": "aria",
        "note": "Inverse of start_resource_maintenance: take the resource out of maintenance.",
    }


def _undo_end(params: dict, result: Any) -> Optional[dict]:
    if not _changed(result):
        return None
    return {
        "tool": "start_resource_maintenance",
        "params": {"resource_id": params.get("resource_id"), "confirmed": True, "target": params.get("target")},
        "skill": "aria",
        "note": (
            "Inverse of end_resource_maintenance: put the resource back in maintenance. It re-enters as "
            "MAINTAINED_MANUAL (no end) — the end of a timed window is not restored."
        ),
    }


def _describe_window(duration_minutes: Optional[int], end_time_ms: Optional[int]) -> str:
    if duration_minutes is not None:
        return f"for {duration_minutes} minutes"
    if end_time_ms is not None:
        return f"until {end_time_ms} (epoch ms)"
    return "with NO end (MAINTAINED_MANUAL — stays until end_resource_maintenance)"


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="medium", undo=_undo_start)
def start_resource_maintenance(
    resource_id: str,
    duration_minutes: Optional[int] = None,
    end_time_ms: Optional[int] = None,
    confirmed: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Put one resource in maintenance so Aria stops alerting on it and collecting its data — use before planned work such as powering a VM or host off.

    Pass duration_minutes OR end_time_ms for a timed window (state MAINTAINED;
    the resource returns to its prior state when it expires). Pass neither for
    manual maintenance (MAINTAINED_MANUAL) that lasts until
    end_resource_maintenance — easy to forget, so prefer a window. Returns the
    state before and after, confirmed (true / false / null when the after-state
    could not be read — null is unknown, not failure) and a note. Default
    confirmed=False returns a preview without connecting. Undo:
    end_resource_maintenance.

    Args:
        resource_id: Resource UUID from list_resources (not the resource name).
        duration_minutes: Window length in whole minutes, 1-525600. Not with end_time_ms.
        end_time_ms: Window end as epoch MILLISECONDS in the future. Not with duration_minutes.
        confirmed: Must be True to actually start maintenance. Default False = preview only.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        if not confirmed:
            from vmware_aria.ops.maintenance import maintenance_window_params

            maintenance_window_params(duration_minutes, end_time_ms)
            return {
                "preview": True,
                "action": "start_resource_maintenance",
                "resource_id": resource_id,
                "message": (
                    f"[preview] Would put resource {resource_id} in maintenance "
                    f"{_describe_window(duration_minutes, end_time_ms)}; alerting and collection stop. "
                    "Re-invoke with confirmed=True to execute."
                ),
            }
        from vmware_aria.ops.maintenance import start_resource_maintenance as _start

        return _start(
            server._get_connection(target),
            resource_id,
            duration_minutes=duration_minutes,
            end_time_ms=end_time_ms,
            audit_logger=server._audit,
            target_name=server._target_name(target),
        )
    except Exception as e:
        return {"error": server._safe_error(e, "start_resource_maintenance"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="medium", undo=_undo_end)
def end_resource_maintenance(
    resource_id: str,
    confirmed: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Take one resource out of maintenance so Aria resumes alerting on it and collecting its data.

    Refuses a resource whose state was read and is not MAINTAINED /
    MAINTAINED_MANUAL (nothing to end). Returns the state before and after,
    confirmed (true / false / null when unknown) and a note. Default
    confirmed=False returns a preview without connecting. Undo:
    start_resource_maintenance (re-enters as manual maintenance).

    Args:
        resource_id: Resource UUID from list_resources (not the resource name).
        confirmed: Must be True to actually end maintenance. Default False = preview only.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    if not confirmed:
        return {
            "preview": True,
            "action": "end_resource_maintenance",
            "resource_id": resource_id,
            "message": (
                f"[preview] Would take resource {resource_id} out of maintenance; alerting and collection "
                "resume. Re-invoke with confirmed=True to execute."
            ),
        }
    try:
        from vmware_aria.ops.maintenance import end_resource_maintenance as _end

        return _end(
            server._get_connection(target),
            resource_id,
            audit_logger=server._audit,
            target_name=server._target_name(target),
        )
    except Exception as e:
        return {"error": server._safe_error(e, "end_resource_maintenance"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_maintenance_schedules(
    resource_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List recurring maintenance schedules: name, schedule type (ONCE/DAILY/WEEKLY/MONTHLY/YEARLY), recurrence, start hour/minute, duration in minutes, time zone, start and expiry.

    A schedule does not list the resources it applies to — pass resource_id to
    see only the schedules for one resource. To put a resource in maintenance
    now, use start_resource_maintenance.

    Returns a paginated envelope: items, returned, limit, total (null when the
    API reports no size), truncated, hint, next_offset, and schedules_note —
    null when the answer was read; otherwise items is UNKNOWN, not empty, and
    must not be reported as "no schedules".

    Page it: limit is the page size (1-500; 0, negatives and anything above 500
    are rejected, not clamped), offset is how many rows to skip, and
    next_offset is the offset of the next page — pass it back as offset and
    stop when it is null. Do not loop on truncated.

    Args:
        resource_id: Only schedules that apply to this resource UUID.
        limit: Page size, 1-500 (default 100). Out-of-range is rejected.
        offset: Schedules to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.maintenance import list_maintenance_schedules as _list

        return _list(server._get_connection(target), resource_id=resource_id, limit=limit, offset=offset)
    except Exception as e:
        return {"error": server._safe_error(e, "list_maintenance_schedules"), "hint": _HINT}

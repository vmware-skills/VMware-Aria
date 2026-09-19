"""MAINTENANCE tools (3): start / end resource maintenance (write), list_maintenance_schedules (read).

The two writes carry the ``confirm=False`` blast-radius gate (HLD §7) like
acknowledge_alert (same risk: reversible, but alerting on the resource stops),
and declare each other as their undo — recorded only when the before-state
shows the undo would restore it (start: was not in maintenance; end: was in
maintenance). ``tests/test_no_destructive_ops.py`` finds the gate here by
searching the whole ``mcp_server`` tree.
"""

from typing import Any, Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp

_HINT = "Run 'vmware-aria doctor' to verify connectivity."
_STATE_NEXT = "Check the resource with get_resource and retry."

#: Actions that report a change nothing was sent for.
_NOT_A_CHANGE = frozenset({"preview", "noop"})


def _changed(result: Any) -> bool:
    """True only for a result that reports an executed write."""
    return (
        isinstance(result, dict)
        and result.get("action") not in _NOT_A_CHANGE
        and not result.get("error")
    )


def _before_in_maintenance(result: Any) -> Optional[bool]:
    """``before.in_maintenance`` from an executed write: True / False / None (unknown)."""
    before = result.get("before") if isinstance(result, dict) else None
    return before.get("in_maintenance") if isinstance(before, dict) else None


def _undo_start(params: dict, result: Any) -> Optional[dict]:
    # Only a call that opened the window may close it. Already in maintenance
    # (True) means ending it would close someone else's window; unknown (None)
    # cannot rule that out. Either way, record no undo.
    if not _changed(result) or _before_in_maintenance(result) is not False:
        return None
    return {
        "tool": "end_resource_maintenance",
        "params": {"resource_id": params.get("resource_id"), "confirm": True, "target": params.get("target")},
        "skill": "aria",
        "note": "Inverse of start_resource_maintenance: take the resource out of maintenance.",
    }


def _undo_end(params: dict, result: Any) -> Optional[dict]:
    # The undo re-enters maintenance with no end. Record it only when the
    # resource is known to have been in maintenance: from an unknown (None)
    # before-state, replaying it could put a resource that never was in
    # maintenance into indefinite maintenance.
    if not _changed(result) or _before_in_maintenance(result) is not True:
        return None
    return {
        "tool": "start_resource_maintenance",
        "params": {"resource_id": params.get("resource_id"), "confirm": True, "target": params.get("target")},
        "skill": "aria",
        "note": (
            "Inverse of end_resource_maintenance: put the resource back in maintenance. It re-enters as "
            "MAINTAINED_MANUAL (no end) — the end of a timed window is not restored."
        ),
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="medium", undo=_undo_start)
def start_resource_maintenance(
    resource_id: str,
    duration_minutes: Optional[int] = None,
    end_time_ms: Optional[int] = None,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Put one resource in maintenance so Aria stops alerting on it and collecting its data — use before planned work such as powering a VM or host off.

    Pass duration_minutes OR end_time_ms for a timed window (state MAINTAINED;
    the resource returns to its prior state when it expires). Pass neither for
    manual maintenance (MAINTAINED_MANUAL) that lasts until
    end_resource_maintenance — easy to forget, so prefer a window.

    Without confirm=True this only previews: it reads the resource and returns
    blast_radius (resource name and kind, each adapter's state, whether it is
    in maintenance now, and the requested window) and changes nothing. Show
    that to the user and get their explicit decision. Do not set confirm=True
    on your own because the user asked earlier: they have not seen what it
    changes yet. Refused with confirm=True: a resource already in maintenance
    (starting again would replace that window) and a resource whose state
    cannot be read.

    Acting returns the state before and after, confirmed (true / false / null
    when the after-state could not be read — null is unknown, not failure) and
    a note. Undo: end_resource_maintenance, recorded only when the resource was
    known not to be in maintenance before.

    Args:
        resource_id: Resource UUID from list_resources (not the resource name).
        duration_minutes: Window length in whole minutes, 1-525600. Not with end_time_ms.
        end_time_ms: Window end as epoch MILLISECONDS in the future. Not with duration_minutes.
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server
    from vmware_aria.ops import gate_measures
    from vmware_aria.ops.maintenance import maintenance_window_params
    from vmware_aria.ops.maintenance import start_resource_maintenance as _start
    from vmware_aria.ops.write_gate import gate, resolve_confirm

    decision = resolve_confirm(confirm, confirmed)

    def run() -> dict:
        maintenance_window_params(duration_minutes, end_time_ms)  # refuse a bad window unconnected
        client = server._get_connection(target)
        radius = gate_measures.measure_maintenance_start(client, resource_id, duration_minutes, end_time_ms)
        return gate(
            "start_resource_maintenance", f"resource {radius['resource_id']}", radius,
            act=decision.act, next_step=_STATE_NEXT,
            apply=lambda: _start(
                client, resource_id, duration_minutes=duration_minutes, end_time_ms=end_time_ms,
                audit_logger=server._audit, target_name=server._target_name(target),
            ),
        )

    return server._gated("start_resource_maintenance", decision.deprecated, run)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="medium", undo=_undo_end)
def end_resource_maintenance(
    resource_id: str,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Take one resource out of maintenance so Aria resumes alerting on it and collecting its data.

    Without confirm=True this only previews: it reads the resource and returns
    blast_radius (resource name and kind, each adapter's state, the maintenance
    mode it is in) and changes nothing. Show that to the user and get their
    explicit decision. Do not set confirm=True on your own because the user
    asked earlier: they have not seen what it changes yet. Refused with
    confirm=True: a resource known not to be in maintenance (an adapter reports
    a state such as STARTED or STOPPED — nothing to end), and a resource whose
    state is unknown (unreadable, or reported as UNKNOWN / NONE) — end that one
    from the Aria UI or the CLI after checking it.

    Acting returns the state before and after, confirmed (true / false / null
    when unknown) and a note. Undo: start_resource_maintenance (re-enters as
    manual maintenance), recorded only when the resource was known to be in
    maintenance before.

    Args:
        resource_id: Resource UUID from list_resources (not the resource name).
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server
    from vmware_aria.ops import gate_measures
    from vmware_aria.ops.maintenance import end_resource_maintenance as _end
    from vmware_aria.ops.write_gate import gate, resolve_confirm

    decision = resolve_confirm(confirm, confirmed)

    def run() -> dict:
        client = server._get_connection(target)
        radius = gate_measures.measure_maintenance_end(client, resource_id)
        return gate(
            "end_resource_maintenance", f"resource {radius['resource_id']}", radius,
            act=decision.act, next_step=_STATE_NEXT,
            apply=lambda: _end(
                client, resource_id, audit_logger=server._audit, target_name=server._target_name(target),
            ),
        )

    return server._gated("end_resource_maintenance", decision.deprecated, run)


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

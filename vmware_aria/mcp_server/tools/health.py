"""HEALTH tools (4, read-only).

get_aria_health, list_collector_groups, get_aria_node_resources, list_adapters.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_aria_health(target: Optional[str] = None) -> dict:
    """[READ] Check Aria Operations platform health, per service, plus its version.

    Returns assessment: HEALTHY, DEGRADED (some services OK, some ERROR — the
    platform still answers), DOWN (no service OK) or UNKNOWN (breakdown
    unreadable). overall_status is the node's own flag: OFFLINE whenever any
    one service is not running, so OFFLINE alone is not an outage. Also
    services (name, health, details; null when unreadable), services_not_ok,
    healthy, system_time_ms, details, and product_version / product_line
    ("8.x", "9.x") / release_name — check the line before assuming 9.x-only
    tools (fleet_*, findings_list, promql_query) exist. A 503 is reported,
    never raised. If HEALTHY but data looks stale, check list_collector_groups.

    Args:
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.health import get_aria_health as _get

        return _get(server._get_connection(target))
    except Exception as e:
        return {"error": server._safe_error(e, "get_aria_health"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_collector_groups(target: Optional[str] = None) -> dict:
    """[READ] List Aria Operations collector groups and their member collector status.

    Collectors are remote agents that gather metrics from vSphere and other adapters.
    Check this when resources appear missing from Aria Operations or metrics are stale.
    Groups list member collector IDs; details (name, state UP/DOWN, local) are
    enriched via one extra collectors call. A DOWN collector means
    list_resources and the metric tools see stale or missing data for
    everything behind it.

    Returns a paginated envelope: items, returned, limit, total (null
    when the API reports no size), truncated, hint. Check truncated
    before calling this the complete set.

    Args:
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.health import list_collector_groups as _list

        return _list(server._get_connection(target))
    except Exception as e:
        return {"error": server._safe_error(e, "list_collector_groups"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def get_aria_node_resources(window_hours: int = 24, target: Optional[str] = None) -> dict:
    """[READ] Memory, swap, heap and watchdog restarts of the Aria Operations node(s) themselves.

    When: get_aria_health shows a service in ERROR, or the Aria UI/API is slow —
    this reads Aria's self-monitoring objects (vC-Ops-Node, vC-Ops-Watchdog) to
    show whether the node is starved of memory. For data that stopped arriving
    from vCenter or another source, use list_adapters instead.

    What: per node — memory (mem|total, mem|used, mem|free, mem|actualFree,
    mem|actualUsed), swap (swap|total, swap|used, swap|free), heap
    (heap|MaxHeapSize, heap|CurrentHeapSize, heap|CommittedMemory,
    heap|NodeHeapMemoryRemaining), heap_components (committed heap per
    component: Analytics, SuiteAPI, Collector, ...) and watchdog_restarts per
    service. Each value has latest, latest_time_ms, unit (from Aria's own statkey
    definitions, on 8.18.7: GB for mem/swap, MB for the heap sizes and
    heap_components, % for heap|NodeHeapMemoryRemaining; null when undefined)
    and window {min, avg, max, points} over window_hours of 5-minute averages.
    memory_pressure.level is an indicator, not a diagnosis: HIGH when actual
    free memory is below 10% of total, ELEVATED below 20%, NORMAL at 20% or
    more, UNKNOWN when the readings do not settle it; basis shows the numbers.

    Gotchas: a key with no value is in the node's missing list (reason
    not_reported, no_data or undetermined) and is never shown as zero.
    watchdog_restarts null means unknown (see watchdog_note), not zero restarts.
    nodes null with nodes_error means the node objects were not recognised.
    units_error, latest_error, window_error and watchdog_error name reads that
    failed; when set, the affected values are unknown.

    Args:
        window_hours: Hours of history for window min/avg/max, 1-720 (default 24). Latest values are read regardless.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.platform import get_aria_node_resources as _get

        return _get(server._get_connection(target), window_hours=window_hours)
    except Exception as e:
        return {"error": server._safe_error(e, "get_aria_node_resources"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_adapters(
    adapter_kind: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List Aria Operations adapter instances and when each last collected.

    When: an alert such as "Objects are not receiving data", or metrics that
    stopped updating — this shows which adapter instance (vCenter, NSX, the
    self-monitoring adapter, ...) stopped collecting and since when. For the
    collector processes behind them use list_collector_groups; for the Aria
    node's own memory use get_aria_node_resources.

    What: rows with id, name, adapter_kind, resource_kind, collector_id,
    collector_group_id (null when none), monitoring_interval_min,
    resources_collected, metrics_collected, last_collected_ms and
    last_heartbeat_ms with their ages in seconds, message (the adapter's own
    status text) and stale / stale_basis. stale is true when lastCollected is
    older than 3 of the adapter's monitoring intervals and at least 15 minutes,
    false when within that, null when the fields cannot support a verdict.
    Ages use the appliance clock (reference_clock "appliance") when node status
    carries it, otherwise this machine's ("local"). The envelope also names
    stale_adapters and staleness_unknown for the filtered set, and
    adapter_kinds_present.

    Returns a paginated envelope: items, returned, limit, total, truncated,
    hint, next_offset. GET /adapters is unpaged, so total is exact; pass
    next_offset back as offset and stop when it is null.

    Gotchas: last_collected being recent does not prove every object behind the
    adapter receives data — it is the instance's last collection cycle. An
    unrecognised or empty answer is returned as an error, never as no adapters.

    Args:
        adapter_kind: Case-insensitive exact adapter kind key, e.g. "VMWARE" for vCenter. Omit for all.
        limit: Page size, 1-500 (default 100). Out-of-range is rejected.
        offset: Rows to skip; pass the previous response's next_offset.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.platform import list_adapters as _list

        return _list(server._get_connection(target), adapter_kind=adapter_kind, limit=limit, offset=offset)
    except Exception as e:
        return {"error": server._safe_error(e, "list_adapters"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

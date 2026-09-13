"""Aria Operations platform self-check: the node's own resources, and adapter collection.

Two reads the health check (``ops/health.py``) cannot answer:

* :func:`get_aria_node_resources` — memory, swap, heap and watchdog restarts
  of the Aria node(s), from Aria's self-monitoring objects (adapter kind
  ``vCenter Operations Adapter``; resource kinds ``vC-Ops-Node`` and
  ``vC-Ops-Watchdog``). On 2026-09-13 these were the only evidence for why
  LOCATOR stopped responding on an 8 GB appliance.
* :func:`list_adapters` — every adapter instance with when it last collected,
  for alerts such as "Objects are not receiving data".

A value that was not read is reported as not read — never as zero, never as
"no adapters". All API strings pass through ``sanitize()``.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

from vmware_aria.connection import AriaApiError, NonJsonBodyError
from vmware_aria.ops._paging import next_offset, paginate, validate_page_args

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

SELF_MONITORING_ADAPTER_KIND = "vCenter Operations Adapter"
NODE_RESOURCE_KIND = "vC-Ops-Node"
WATCHDOG_RESOURCE_KIND = "vC-Ops-Watchdog"

DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 720
#: The window is read as AVG rollups of this many minutes; min/avg/max are
#: computed over those points, so a spike shorter than this is smoothed.
WINDOW_INTERVAL_MINUTES = 5

#: Memory pressure is HIGH when actual free memory (``mem|actualFree``: free
#: plus caches and buffers, per the statkey definition) is below this percent
#: of ``mem|total``. The 2026-09-13 incident read 0.47 of 7.75 GB = 6%.
MEMORY_PRESSURE_HIGH_FREE_PCT = 10.0
#: ELEVATED below this percent (and at or above the HIGH threshold).
MEMORY_PRESSURE_ELEVATED_FREE_PCT = 20.0

#: An adapter is stale when it last collected more than this many of its own
#: monitoring intervals ago ...
ADAPTER_STALE_MISSED_INTERVALS = 3
#: ... and never sooner than this many minutes, so a 1-minute adapter is not
#: flagged for one slow cycle.
ADAPTER_STALE_MIN_AGE_MINUTES = 15

MEMORY_KEYS = ("mem|total", "mem|used", "mem|free", "mem|actualFree", "mem|actualUsed")
SWAP_KEYS = ("swap|total", "swap|used", "swap|free")
HEAP_KEYS = ("heap|MaxHeapSize", "heap|CurrentHeapSize", "heap|CommittedMemory", "heap|NodeHeapMemoryRemaining")
_EXPECTED_KEYS = MEMORY_KEYS + SWAP_KEYS + HEAP_KEYS
_HEAP_COMPONENT_PREFIX = "heap|ComponentCommittedMemory|"
_SERVICE_PREFIX = "Service:"
_RESTARTS_SUFFIX = "|Restarts"

_RESOURCE_PAGE_SIZE = 1000
_HOUR_MS = 3_600_000
_MINUTE_MS = 60_000


def _local_now_ms() -> int:
    return int(time.time() * 1000)


def _describe_failure(exc: AriaApiError) -> str:
    if isinstance(exc, NonJsonBodyError):
        return f"HTTP {exc.status_code}, but the body is not JSON"
    return f"HTTP {exc.status_code}" if exc.status_code is not None else "no HTTP response"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Self-monitoring objects
# ---------------------------------------------------------------------------


def _kind_of(resource: dict) -> tuple[Any, Any]:
    key = resource.get("resourceKey")
    if not isinstance(key, dict):
        return None, None
    return key.get("adapterKindKey"), key.get("resourceKindKey")


def _identifier(resource: dict, name: str) -> str | None:
    key = resource.get("resourceKey") if isinstance(resource.get("resourceKey"), dict) else {}
    for ident in key.get("resourceIdentifiers") or []:
        if isinstance(ident, dict) and (ident.get("identifierType") or {}).get("name") == name:
            value = ident.get("value")
            return sanitize(str(value)) if isinstance(value, str) and value else None
    return None


def _list_self_resources(client: AriaClient, kind: str) -> tuple[list[dict] | None, str | None]:
    """Self-monitoring resources of ``kind``, or ``(None, why)``. HTTP errors propagate.

    Every Aria Operations node has a ``vC-Ops-Node`` and a ``vC-Ops-Watchdog``
    object, so an empty or unrecognised listing is reported as unread rather
    than as a deployment with no nodes.
    """
    what = f"GET /resources?resourceKind={kind}&adapterKind={SELF_MONITORING_ADAPTER_KIND}"
    params = {"resourceKind": kind, "adapterKind": SELF_MONITORING_ADAPTER_KIND, "pageSize": _RESOURCE_PAGE_SIZE}
    data = client.get("/resources", params=params)
    rows = data.get("resourceList") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None, f"{what} answered in an unrecognised shape (no 'resourceList' list)."
    matched = [
        r
        for r in rows
        if isinstance(r, dict)
        and _kind_of(r) == (SELF_MONITORING_ADAPTER_KIND, kind)
        and isinstance(r.get("identifier"), str)
        and r["identifier"]
    ]
    if not matched:
        return None, (
            f"{what} returned no {kind} object with an identifier ({len(rows)} row(s)). Every Aria "
            f"Operations node has one, so this is an unrecognised answer, not a deployment without nodes."
        )
    return matched, None


def _read_watchdogs(client: AriaClient) -> tuple[dict[str, dict] | None, str | None]:
    """Watchdog objects keyed by their NODEID identifier, or ``(None, why)``."""
    try:
        rows, error = _list_self_resources(client, WATCHDOG_RESOURCE_KIND)
    except AriaApiError as exc:
        return None, f"GET /resources?resourceKind={WATCHDOG_RESOURCE_KIND} failed ({_describe_failure(exc)})."
    if rows is None:
        return None, error
    by_node: dict[str, dict] = {}
    for row in rows:
        node_id = _identifier(row, "NODEID")
        if node_id:
            by_node[node_id] = row
    return by_node, None


def _read_units(client: AriaClient) -> tuple[dict[str, str | None], str | None]:
    """statKey -> unit from the resource-kind definitions; the error names any unread kind."""
    units: dict[str, str | None] = {}
    errors = []
    for kind in (NODE_RESOURCE_KIND, WATCHDOG_RESOURCE_KIND):
        # Literal paths at every call site: the spec-conformance scan resolves a
        # variable argument to the module's last same-named assignment.
        path = f"/adapterkinds/{SELF_MONITORING_ADAPTER_KIND}/resourcekinds/{kind}/statkeys"
        try:
            data = client.get(f"/adapterkinds/{SELF_MONITORING_ADAPTER_KIND}/resourcekinds/{kind}/statkeys")
        except AriaApiError as exc:
            errors.append(f"GET {path} failed ({_describe_failure(exc)})")
            continue
        rows = data.get("resourceTypeAttributes") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            errors.append(f"GET {path} answered in an unrecognised shape (no 'resourceTypeAttributes' list)")
            continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("key"), str):
                unit = row.get("unit")
                units[row["key"]] = sanitize(unit) if isinstance(unit, str) and unit else None
    if not errors:
        return units, None
    return units, "; ".join(errors) + ". Units of those keys are unknown (null), not unitless."


def _unit_for(units: dict[str, str | None], key: str) -> str | None:
    """Exact definition, else the uninstanced one (``Service:api|Restarts`` -> ``Service|Restarts``)."""
    if key in units:
        return units[key]
    return units.get("|".join(segment.split(":", 1)[0] for segment in key.split("|")))


def _reported_keys(client: AriaClient, resource_id: str) -> tuple[set[str] | None, str | None]:
    path = f"/resources/{resource_id}/statkeys"
    try:
        data = client.get(f"/resources/{resource_id}/statkeys")
    except AriaApiError as exc:
        return None, f"GET {path} failed ({_describe_failure(exc)})"
    rows = data.get("stat-key") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None, f"GET {path} answered in an unrecognised shape (no 'stat-key' list)"
    keys = [r["key"] for r in rows if isinstance(r, dict) and isinstance(r.get("key"), str)]
    if len(keys) != len(rows):
        return None, f"GET {path} answered with {len(rows) - len(keys)} 'stat-key' row(s) in an unrecognised form"
    return set(keys), None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

_Series = dict[str, dict[str, tuple[list, list]]]


def _parse_stats(data: Any, what: str) -> tuple[_Series | None, str | None]:
    """resourceId -> statKey -> (timestamps, data), or ``(None, why)`` for any unrecognised part."""
    values = data.get("values") if isinstance(data, dict) else None
    unrecognised = f"{what} answered in an unrecognised shape"
    if not isinstance(values, list):
        return None, f"{unrecognised} (no 'values' list); the values are unknown, not zero."
    out: _Series = {}
    for entry in values:
        if not isinstance(entry, dict) or not isinstance(entry.get("resourceId"), str):
            return None, f"{unrecognised} (a value without a resourceId)."
        container = entry.get("stat-list") or entry.get("statList")
        stats = container.get("stat") if isinstance(container, dict) else None
        if not isinstance(stats, list):
            return None, f"{unrecognised} (a value without a 'stat-list.stat' list)."
        per_resource = out.setdefault(entry["resourceId"], {})
        for stat in stats:
            stat_key = stat.get("statKey") if isinstance(stat, dict) else None
            key = stat_key.get("key") if isinstance(stat_key, dict) else None
            points = stat.get("data") if isinstance(stat, dict) else None
            if not isinstance(key, str) or not isinstance(points, list):
                return None, f"{unrecognised} (a stat without a string statKey.key and a data list)."
            stamps = stat.get("timestamps")
            per_resource[key] = (stamps if isinstance(stamps, list) else [], points)
    return out, None


def _read_latest(client: AriaClient, ids: list[str], keys: list[str]) -> tuple[_Series | None, str | None]:
    path = "/resources/stats/latest"
    try:
        data = client.get("/resources/stats/latest", params={"resourceId": ids, "statKey": keys})
    except AriaApiError as exc:
        return None, f"GET {path} failed ({_describe_failure(exc)}); latest values are unknown."
    return _parse_stats(data, f"GET {path}")


def _read_window(
    client: AriaClient, ids: list[str], keys: list[str], begin_ms: int, end_ms: int
) -> tuple[_Series | None, str | None]:
    path = "/resources/stats/query"
    payload = {
        "resourceId": ids,
        "statKey": keys,
        "begin": begin_ms,
        "end": end_ms,
        "rollUpType": "AVG",
        "intervalType": "MINUTES",
        "intervalQuantifier": WINDOW_INTERVAL_MINUTES,
    }
    try:
        data = client.post("/resources/stats/query", json_data=payload, retries=1)
    except AriaApiError as exc:
        return None, f"POST {path} failed ({_describe_failure(exc)}); window min/avg/max are unknown."
    return _parse_stats(data, f"POST {path}")


def _metric(key: str, latest: dict | None, window: dict | None, units: dict[str, str | None]) -> dict | None:
    """One reading, or ``None`` when neither read produced a numeric value for ``key``."""
    value = stamp = None
    if latest and key in latest:
        stamps, points = latest[key]
        for i, point in enumerate(points):
            if _is_number(point):
                value, stamp = point, stamps[i] if i < len(stamps) else None
    summary = None
    if window and key in window:
        numbers = [p for p in window[key][1] if _is_number(p)]
        if numbers:
            summary = {
                "min": min(numbers),
                "avg": sum(numbers) / len(numbers),
                "max": max(numbers),
                "points": len(numbers),
            }
    if value is None and summary is None:
        return None
    return {"latest": value, "latest_time_ms": _int_or_none(stamp), "unit": _unit_for(units, key), "window": summary}


def _missing(key: str, reported: set[str] | None, reported_error: str | None, reads_ok: bool) -> dict:
    if reported is not None and key not in reported:
        return {"key": sanitize(key), "reason": "not_reported", "detail": "Absent from this object's stat-key list."}
    if reported is not None and reads_ok:
        return {
            "key": sanitize(key),
            "reason": "no_data",
            "detail": "Reported, but no value came back, latest or in the window.",
        }
    why = reported_error or "a stats read failed (see latest_error / window_error)"
    return {
        "key": sanitize(key),
        "reason": "undetermined",
        "detail": f"No value, and {why}, so not-reported cannot be told from no-data.",
    }


# ---------------------------------------------------------------------------
# Pressure indicator
# ---------------------------------------------------------------------------


def assess_memory_pressure(total: dict | None, actual_free: dict | None) -> dict:
    """HIGH / ELEVATED / NORMAL / UNKNOWN from the latest mem|total and mem|actualFree.

    HIGH below :data:`MEMORY_PRESSURE_HIGH_FREE_PCT` percent free, ELEVATED
    below :data:`MEMORY_PRESSURE_ELEVATED_FREE_PCT`, NORMAL at or above it.
    UNKNOWN whenever the two readings do not settle it: a value or unit is
    missing, the units differ, or free is negative or larger than total.
    """

    def unknown(basis: str) -> dict:
        return {"level": "UNKNOWN", "actual_free_pct": None, "basis": basis}

    if total is None or actual_free is None:
        return unknown("mem|total or mem|actualFree was not read for this node.")
    t, f = total.get("latest"), actual_free.get("latest")
    if not _is_number(t) or not _is_number(f):
        return unknown("There is no latest numeric value for both mem|total and mem|actualFree.")
    t_unit, f_unit = total.get("unit"), actual_free.get("unit")
    if t_unit is None or f_unit is None or t_unit != f_unit:
        return unknown(f"The units of mem|total ({t_unit}) and mem|actualFree ({f_unit}) are not both known and equal.")
    if t <= 0 or f < 0 or f > t:
        return unknown(f"mem|actualFree {f} against mem|total {t} {t_unit} is not a consistent reading.")
    pct = f / t * 100
    basis = (
        f"Actual free {f:.2f} of {t:.2f} {t_unit} ({pct:.1f}%); HIGH below "
        f"{MEMORY_PRESSURE_HIGH_FREE_PCT:g}%, ELEVATED below {MEMORY_PRESSURE_ELEVATED_FREE_PCT:g}%."
    )
    if pct < MEMORY_PRESSURE_HIGH_FREE_PCT:
        return {"level": "HIGH", "actual_free_pct": pct, "basis": basis}
    if pct < MEMORY_PRESSURE_ELEVATED_FREE_PCT:
        return {"level": "ELEVATED", "actual_free_pct": pct, "basis": basis}
    if pct >= MEMORY_PRESSURE_ELEVATED_FREE_PCT:
        return {"level": "NORMAL", "actual_free_pct": pct, "basis": basis}
    return unknown(f"The free percentage {pct} could not be compared with the thresholds.")


# ---------------------------------------------------------------------------
# get_aria_node_resources
# ---------------------------------------------------------------------------


def _validate_window(window_hours: Any) -> None:
    if not isinstance(window_hours, int) or isinstance(window_hours, bool) or not 1 <= window_hours <= MAX_WINDOW_HOURS:
        raise ValueError(
            f"Invalid window_hours {window_hours!r}: pass a whole number of hours from 1 to "
            f"{MAX_WINDOW_HOURS} (default {DEFAULT_WINDOW_HOURS}). It sets how far back min/avg/max look; "
            f"latest values are read regardless."
        )


def _plan_node(client: AriaClient, node: dict, watchdogs: dict[str, dict] | None, units: dict) -> dict:
    """What to read for one node: its keys, its watchdog and the watchdog's restart keys."""
    reported, reported_error = _reported_keys(client, node["identifier"])
    known = reported if reported is not None else set(units)
    components = sorted(k for k in known if k.startswith(_HEAP_COMPONENT_PREFIX))
    plan = {
        "node": node,
        "id": node["identifier"],
        "node_id": _identifier(node, "NODEID"),
        "keys": list(_EXPECTED_KEYS) + components,
        "reported": reported,
        "reported_error": reported_error,
        "watchdog_id": None,
        "watchdog_keys": [],
        "watchdog_reported": None,
        "watchdog_error": None,
        "watchdog_note": None,
    }
    if watchdogs is None:
        plan["watchdog_note"] = "The watchdog objects were not read (see watchdog_error); restarts are unknown."
        return plan
    watchdog = watchdogs.get(plan["node_id"]) if plan["node_id"] else None
    if watchdog is None:
        plan["watchdog_note"] = (
            "No vC-Ops-Watchdog object carries this node's NODEID identifier, so no watchdog was "
            "paired with it; restarts are unknown."
        )
        return plan
    wd_reported, wd_error = _reported_keys(client, watchdog["identifier"])
    plan.update(watchdog_id=watchdog["identifier"], watchdog_reported=wd_reported, watchdog_error=wd_error)
    if wd_reported is None:
        plan["watchdog_note"] = f"{wd_error}, so the watchdog's restart keys are unknown."
        return plan
    plan["watchdog_keys"] = sorted(
        k for k in wd_reported if k.startswith(_SERVICE_PREFIX) and k.endswith(_RESTARTS_SUFFIX)
    )
    if not plan["watchdog_keys"]:
        plan["watchdog_note"] = (
            "The watchdog reports no 'Service:<name>|Restarts' keys; restart counts are unknown, not zero."
        )
    return plan


def _series_for(stats: _Series | None, resource_id: str | None) -> dict | None:
    if stats is None or resource_id is None:
        return None
    return stats.get(resource_id, {})


def _build_node(plan: dict, latest: _Series | None, window: _Series | None, units: dict) -> dict:
    reads_ok = latest is not None and window is not None
    lat, win = _series_for(latest, plan["id"]), _series_for(window, plan["id"])
    metrics = {k: _metric(k, lat, win, units) for k in plan["keys"]}
    missing = [_missing(k, plan["reported"], plan["reported_error"], reads_ok) for k, m in metrics.items() if m is None]

    restarts, note = None, plan["watchdog_note"]
    if plan["watchdog_keys"]:
        wlat, wwin = _series_for(latest, plan["watchdog_id"]), _series_for(window, plan["watchdog_id"])
        readings = {k: _metric(k, wlat, wwin, units) for k in plan["watchdog_keys"]}
        missing += [
            _missing(k, plan["watchdog_reported"], plan["watchdog_error"], reads_ok)
            for k, m in readings.items()
            if m is None
        ]
        restarts = {
            sanitize(k[len(_SERVICE_PREFIX) : -len(_RESTARTS_SUFFIX)]): m for k, m in readings.items() if m is not None
        } or None
        if restarts is None:
            note = "No restart value was read for any watchdog service (see missing); restarts are unknown."

    states = plan["node"].get("resourceStatusStates")
    first_state = states[0] if isinstance(states, list) and states and isinstance(states[0], dict) else {}
    key = plan["node"].get("resourceKey") or {}
    return {
        "name": sanitize(str(key.get("name") or "")),
        "resource_id": sanitize(plan["id"]),
        "node_id": plan["node_id"],
        "node_host": _identifier(plan["node"], "NODEHOST"),
        "collection_status": sanitize(str(first_state.get("resourceStatus") or "")) or None,
        "memory": {k: metrics[k] for k in MEMORY_KEYS if metrics[k] is not None},
        "swap": {k: metrics[k] for k in SWAP_KEYS if metrics[k] is not None},
        "heap": {k: metrics[k] for k in HEAP_KEYS if metrics[k] is not None},
        "heap_components": {
            sanitize(k[len(_HEAP_COMPONENT_PREFIX) :]): m
            for k, m in metrics.items()
            if k.startswith(_HEAP_COMPONENT_PREFIX) and m is not None
        },
        "watchdog_resource_id": sanitize(plan["watchdog_id"]) if plan["watchdog_id"] else None,
        "watchdog_restarts": restarts,
        "watchdog_note": note,
        "missing": missing,
        "memory_pressure": assess_memory_pressure(metrics.get("mem|total"), metrics.get("mem|actualFree")),
    }


def get_aria_node_resources(client: AriaClient, window_hours: int = DEFAULT_WINDOW_HOURS) -> dict:
    """Resource pressure on the Aria Operations node(s) themselves.

    Reads the self-monitoring objects: each ``vC-Ops-Node`` (memory, swap, heap
    overall and per component) and the ``vC-Ops-Watchdog`` paired with it by
    NODEID (``Service:<name>|Restarts``). Units come from the resource-kind
    statkey definitions (8.18.7: GB for mem|/swap|, MB for heap sizes, % for
    NodeHeapMemoryRemaining), never assumed.

    Per value: ``latest`` / ``latest_time_ms`` (GET /resources/stats/latest),
    ``unit``, and ``window`` = min/avg/max/points over the last
    ``window_hours`` of 5-minute AVG points (POST /resources/stats/query), or
    ``None`` when the window has no points.

    ``memory_pressure`` is an indicator from the latest mem|actualFree against
    mem|total: HIGH below 10% free, ELEVATED below 20%, NORMAL at 20% or more
    (:data:`MEMORY_PRESSURE_HIGH_FREE_PCT`,
    :data:`MEMORY_PRESSURE_ELEVATED_FREE_PCT`), UNKNOWN when the readings do not
    settle it. ``basis`` carries the numbers.

    A key with no value is listed in the node's ``missing`` with a reason —
    ``not_reported``, ``no_data`` or ``undetermined`` — and is absent from the
    readings, never zero. ``watchdog_restarts`` is ``None`` (with
    ``watchdog_note``) when restarts are unknown. ``nodes`` is ``None`` with
    ``nodes_error`` when the node objects are unrecognised; ``units_error``,
    ``latest_error``, ``window_error`` and ``watchdog_error`` name failed reads.
    An HTTP failure listing the nodes raises ``AriaApiError``.

    Raises:
        ValueError: ``window_hours`` is not an integer from 1 to 720.
    """
    _validate_window(window_hours)
    end_ms = _local_now_ms()
    begin_ms = end_ms - window_hours * _HOUR_MS
    result = {
        "adapter_kind": SELF_MONITORING_ADAPTER_KIND,
        "window_hours": window_hours,
        "window_begin_ms": begin_ms,
        "window_end_ms": end_ms,
        "window_rollup": f"AVG over {WINDOW_INTERVAL_MINUTES}-minute intervals",
        "thresholds": {
            "memory_pressure_high_below_actual_free_pct": MEMORY_PRESSURE_HIGH_FREE_PCT,
            "memory_pressure_elevated_below_actual_free_pct": MEMORY_PRESSURE_ELEVATED_FREE_PCT,
        },
        "nodes": None,
        "nodes_error": None,
        "units_error": None,
        "latest_error": None,
        "window_error": None,
        "watchdog_error": None,
    }
    nodes, nodes_error = _list_self_resources(client, NODE_RESOURCE_KIND)
    if nodes is None:
        return {**result, "nodes_error": nodes_error, "details": nodes_error}

    watchdogs, watchdog_error = _read_watchdogs(client)
    units, units_error = _read_units(client)
    plans = [_plan_node(client, node, watchdogs, units) for node in nodes]
    ids = [p["id"] for p in plans] + [p["watchdog_id"] for p in plans if p["watchdog_keys"]]
    keys = sorted({k for p in plans for k in p["keys"] + p["watchdog_keys"]})
    latest, latest_error = _read_latest(client, ids, keys)
    window, window_error = _read_window(client, ids, keys, begin_ms, end_ms)
    rows = [_build_node(plan, latest, window, units) for plan in plans]

    errors = [e for e in (units_error, latest_error, window_error, watchdog_error) if e]
    summary = "; ".join(f"{r['name']}: memory pressure {r['memory_pressure']['level']}" for r in rows)
    return {
        **result,
        "nodes": rows,
        "units_error": units_error,
        "latest_error": latest_error,
        "window_error": window_error,
        "watchdog_error": watchdog_error,
        "details": f"{len(rows)} node(s) read. {summary}." + (" " + " ".join(errors) if errors else ""),
    }


# ---------------------------------------------------------------------------
# list_adapters
# ---------------------------------------------------------------------------


def _reference_time(client: AriaClient) -> tuple[int, str]:
    """The appliance's own clock (node status ``systemTime``, also in its 503 body), else the local one."""
    try:
        data = client.get("/deployment/node/status", retries=0)
    except AriaApiError as exc:
        data = exc.body if exc.status_code == 503 else None
    system_time = data.get("systemTime") if isinstance(data, dict) else None
    if _int_or_none(system_time) is not None and system_time > 0:
        return system_time, "appliance"
    return _local_now_ms(), "local"


def _is_positive_number(value: Any) -> bool:
    return _is_number(value) and value > 0


def _staleness(last_collected: int | None, interval: Any, reference_ms: int) -> tuple[bool | None, str]:
    if last_collected is None:
        return None, "No lastCollected timestamp, so staleness is unknown."
    if not _is_positive_number(interval):
        return None, "No usable monitoringInterval, so the expected cadence (and staleness) is unknown."
    threshold_min = max(ADAPTER_STALE_MISSED_INTERVALS * interval, ADAPTER_STALE_MIN_AGE_MINUTES)
    age_ms = reference_ms - last_collected
    if age_ms < 0:
        return None, "lastCollected is later than the reference clock; staleness is unknown."
    rule = (
        f"threshold {threshold_min:g} min = max({ADAPTER_STALE_MISSED_INTERVALS} x {interval:g}-min "
        f"interval, {ADAPTER_STALE_MIN_AGE_MINUTES} min)"
    )
    age = f"last collected {age_ms / _MINUTE_MS:.1f} min ago"
    if age_ms > threshold_min * _MINUTE_MS:
        return True, f"Stale: {age}, over the {rule}."
    if age_ms <= threshold_min * _MINUTE_MS:
        return False, f"Current: {age}, within the {rule}."
    return None, "The collection age could not be compared with the threshold."


def _age_s(timestamp: int | None, reference_ms: int) -> int | None:
    return None if timestamp is None else (reference_ms - timestamp) // 1000


def _text(value: Any, max_len: int = 500) -> str:
    return sanitize(value, max_len=max_len) if isinstance(value, str) else ""


def _adapter_row(adapter: dict, reference_ms: int) -> dict:
    key = adapter.get("resourceKey") if isinstance(adapter.get("resourceKey"), dict) else {}
    interval = adapter.get("monitoringInterval")
    last_collected = _int_or_none(adapter.get("lastCollected"))
    last_heartbeat = _int_or_none(adapter.get("lastHeartbeat"))
    stale, basis = _staleness(last_collected, interval, reference_ms)
    message = adapter.get("messageFromAdapterInstance")
    return {
        "id": _text(adapter.get("id")),
        "name": _text(key.get("name")),
        "adapter_kind": _text(key.get("adapterKindKey")),
        "resource_kind": _text(key.get("resourceKindKey")),
        "collector_id": _int_or_none(adapter.get("collectorId")),
        "collector_group_id": _text(adapter.get("collectorGroupId")) or None,
        "monitoring_interval_min": interval if _is_positive_number(interval) else None,
        "resources_collected": _int_or_none(adapter.get("numberOfResourcesCollected")),
        "metrics_collected": _int_or_none(adapter.get("numberOfMetricsCollected")),
        "last_collected_ms": last_collected,
        "last_collected_age_s": _age_s(last_collected, reference_ms),
        "last_heartbeat_ms": last_heartbeat,
        "last_heartbeat_age_s": _age_s(last_heartbeat, reference_ms),
        "message": _text(message, max_len=300) if isinstance(message, str) else None,
        "stale": stale,
        "stale_basis": basis,
    }


def list_adapters(
    client: AriaClient,
    adapter_kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List adapter instances and when each last collected.

    ``GET /adapters`` is unpaged, so ``total`` is exact. Each row: id, name,
    adapter_kind, resource_kind, collector_id, collector_group_id (null when
    the instance names none), monitoring_interval_min, resources_collected,
    metrics_collected, last_collected_ms / last_heartbeat_ms and their ages in
    seconds, message (the adapter's own message), ``stale`` and ``stale_basis``.

    ``stale`` is True when lastCollected is older than
    :data:`ADAPTER_STALE_MISSED_INTERVALS` monitoring intervals and at least
    :data:`ADAPTER_STALE_MIN_AGE_MINUTES` minutes; False when within that;
    None when the fields cannot support a verdict. Ages are measured against
    the appliance's clock (``reference_clock`` "appliance") when node status
    carries it, else this machine's ("local").

    Args:
        client: Authenticated Aria Operations API client.
        adapter_kind: Case-insensitive exact adapter kind key, e.g. "VMWARE".
        limit: Page size, 1-500.
        offset: Rows to skip; pass back ``next_offset``.

    Raises:
        ValueError: A blank adapter_kind or an out-of-range page window.
        AriaApiError: The read failed, or its answer is unrecognised or empty —
            every deployment runs a self-monitoring adapter, so an empty list
            is not reported as "no adapters".
    """
    if adapter_kind is not None and (not isinstance(adapter_kind, str) or not adapter_kind.strip()):
        raise ValueError(
            "adapter_kind must be an adapter kind key such as 'VMWARE' or 'vCenter Operations Adapter', "
            "or omitted to list every adapter instance."
        )
    validate_page_args(limit, offset)
    data = client.get("/adapters")
    rows = data.get("adapterInstancesInfoDto") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise AriaApiError(
            "GET /adapters answered without an 'adapterInstancesInfoDto' list, a shape this skill does "
            "not recognise; the adapter instances are unknown, not absent.",
            method="GET",
            path="/adapters",
        )
    objects = [r for r in rows if isinstance(r, dict)]
    if not objects:
        raise AriaApiError(
            "GET /adapters answered with no adapter instance objects. Every Aria Operations deployment "
            "runs its own self-monitoring adapter, so this is not read as 'no adapters'. Run "
            "'vmware-aria doctor' and retry.",
            method="GET",
            path="/adapters",
        )
    reference_ms, clock = _reference_time(client)
    all_rows = [_adapter_row(r, reference_ms) for r in objects]
    wanted = adapter_kind.strip().upper() if adapter_kind else None
    selected = [r for r in all_rows if wanted is None or r["adapter_kind"].upper() == wanted]
    page = paginate(selected, limit, offset)
    return paginated(
        page,
        limit=limit,
        total=len(selected),
        next_offset=next_offset(len(page), limit, offset, len(selected)),
        adapter_kind_filter=sanitize(adapter_kind.strip()) if adapter_kind else None,
        adapter_kinds_present=sorted({r["adapter_kind"] for r in all_rows if r["adapter_kind"]}),
        reference_time_ms=reference_ms,
        reference_clock=clock,
        stale_threshold={
            "missed_intervals": ADAPTER_STALE_MISSED_INTERVALS,
            "min_age_minutes": ADAPTER_STALE_MIN_AGE_MINUTES,
        },
        stale_adapters=[r["name"] for r in selected if r["stale"] is True],
        staleness_unknown=[r["name"] for r in selected if r["stale"] is None],
        unrecognized_rows=len(rows) - len(objects),
    )

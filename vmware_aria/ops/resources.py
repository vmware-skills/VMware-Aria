"""Aria Operations resource queries: list, get details, metrics, health, top consumers.

All API responses pass through sanitize() to strip control characters and limit length.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.resources")

# Valid resource kinds recognised by Aria Operations
_VALID_RESOURCE_KINDS = {
    "VirtualMachine",
    "HostSystem",
    "ClusterComputeResource",
    "Datastore",
    "Datacenter",
    "ResourcePool",
    "vSphere World",
}

_VALID_SORT_METRICS = {
    "cpu|usage_average",
    "mem|usage_average",
    "cpu|demand_average",
    "mem|workload",
    "disk|usage_average",
    "net|usage_average",
}

# URL-length protection for GET /resources/stats/topn: each resourceId adds
# ~40 chars to the query string; >100 IDs risks HTTP 414 (URI Too Long).
_TOPN_MAX_RESOURCE_IDS = 100

# Server-side page size for GET /resources pagination. The suite-api caps a
# single response at ~1000 resources regardless of a larger pageSize, so a
# large environment must be walked page by page (page index is 0-based). 1000
# is the documented max page size — fewer round trips than a smaller value.
_RESOURCES_PAGE_SIZE = 1000

# Safety cap on total resources fetched across all pages. A vCenter with tens
# of thousands of VMs could otherwise pull unbounded data into context; when
# the cap is hit before totalCount, we stop and log a warning (consistent with
# the family "search over list" rule — narrow with name_filter / resource_kind).
_RESOURCES_MAX_TOTAL = 50000


def _badges_by_type(dto: dict) -> dict[str, dict]:
    """Index the ResourceDto badges[] array ({type, color, score}) by type.

    The wire field is ``badges`` (array, type enum HEALTH/RISK/EFFICIENCY) —
    there is no singular ``badge`` object (2026-06-08 spec audit).
    """
    return {b.get("type", ""): b for b in dto.get("badges") or []}



# ---------------------------------------------------------------------------
# list_resources
# ---------------------------------------------------------------------------


def _summarize_resource(r: dict) -> dict:
    """Project one ResourceDto onto the high-signal summary fields we return."""
    health = _badges_by_type(r).get("HEALTH", {})
    return {
        "id": sanitize(r.get("identifier", "")),
        "name": sanitize(r.get("resourceKey", {}).get("name", "")),
        "kind": sanitize(r.get("resourceKey", {}).get("resourceKindKey", "")),
        "adapter_kind": sanitize(r.get("resourceKey", {}).get("adapterKindKey", "")),
        "health_color": sanitize(health.get("color", "")),
        "health_score": health.get("score", None),
        # guard: API may return "resourceStatusStates": [] (key present, empty)
        "status": sanitize((r.get("resourceStatusStates") or [{}])[0].get("resourceState", "")),
    }


def list_resources(
    client: AriaClient,
    resource_kind: str = "VirtualMachine",
    limit: int | None = None,
    name_filter: str | None = None,
) -> list[dict]:
    """List resources of a given kind from Aria Operations, following pagination.

    GET /resources caps a single response at ~1000 resources (the suite-api
    server-side page limit) and returns the rest across successive 0-based
    pages with a ``pageInfo`` block carrying ``totalCount``. The previous
    implementation requested one page with ``pageSize=limit`` and never read
    ``pageInfo``, so any environment with more than ~500–1000 resources of a
    kind was silently truncated (2026-06-09 user report: "maximum of 500
    resources returned"). This walks every page until ``totalCount`` is reached
    (or ``limit`` is satisfied), so large environments are fully enumerated.

    Args:
        client: Authenticated Aria Operations API client.
        resource_kind: Resource kind to list (e.g. VirtualMachine, HostSystem).
        limit: Maximum number of results to return. ``None`` (default) returns
            all resources of the kind, walking every page up to an internal
            safety cap. Pass an int to stop early.
        name_filter: Optional substring filter on resource name (case-insensitive).

    Returns:
        List of resource summary dicts with id, name, kind, and health badge.
    """
    if resource_kind not in _VALID_RESOURCE_KINDS:
        _log.warning("Unknown resource_kind '%s', proceeding anyway", resource_kind)

    # Hard ceiling on rows fetched: the explicit limit if given, otherwise the
    # safety cap. name_filter is applied client-side, so we keep paging until
    # the unfiltered totalCount is exhausted rather than stopping at `limit`
    # filtered matches. NOTE: GET /resources exposes a server-side `name` regex
    # param, but we keep the client-side case-insensitive substring match for
    # predictable semantics; this is correct, just walks the full unfiltered
    # collection (bounded by the safety cap) rather than pushing the filter down.
    fetch_cap = _RESOURCES_MAX_TOTAL if limit is None else min(limit, _RESOURCES_MAX_TOTAL)
    filter_lc = name_filter.lower() if name_filter else None

    results: list[dict] = []
    fetched = 0
    page = 0
    while True:
        params: dict[str, Any] = {
            "resourceKind": resource_kind,
            "page": page,
            "pageSize": _RESOURCES_PAGE_SIZE,
        }
        data = client.get("/resources", params=params)
        items = data.get("resourceList", []) or []
        if not items:
            break

        for r in items:
            fetched += 1
            summary = _summarize_resource(r)
            if filter_lc and filter_lc not in summary["name"].lower():
                continue
            results.append(summary)
            if limit is not None and len(results) >= limit:
                return results

        page_info = data.get("pageInfo") or {}
        total_count = page_info.get("totalCount")
        # Termination: a short page (fewer than a full pageSize) means the last
        # page; an exhausted totalCount means we've seen everything. Either
        # without the other is enough — guard both for servers that omit pageInfo.
        if len(items) < _RESOURCES_PAGE_SIZE:
            break
        if total_count is not None and fetched >= total_count:
            break
        if fetched >= fetch_cap:
            _log.warning(
                "list_resources hit the %d-resource safety cap for kind '%s' "
                "(totalCount=%s); results truncated. Narrow with name_filter or "
                "a smaller resource_kind.",
                fetch_cap,
                resource_kind,
                total_count,
            )
            break
        page += 1

    return results


# ---------------------------------------------------------------------------
# get_resource
# ---------------------------------------------------------------------------


def get_resource(client: AriaClient, resource_id: str) -> dict:
    """Get full details for a specific resource.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: The resource UUID.

    Returns:
        Dict with resource key, identifiers, health badge, and relationships.
    """
    if not resource_id:
        raise ValueError("resource_id must not be empty")

    data = client.get(f"/resources/{resource_id}")
    key = data.get("resourceKey", {})
    badges = _badges_by_type(data)
    health = badges.get("HEALTH", {})
    risk = badges.get("RISK", {})
    efficiency = badges.get("EFFICIENCY", {})
    return {
        "id": sanitize(data.get("identifier", "")),
        "name": sanitize(key.get("name", "")),
        "kind": sanitize(key.get("resourceKindKey", "")),
        "adapter_kind": sanitize(key.get("adapterKindKey", "")),
        "description": sanitize(data.get("description", ""), max_len=1000),
        "health_color": sanitize(health.get("color", "")),
        "health_score": health.get("score", None),
        "risk_color": sanitize(risk.get("color", "")),
        "risk_score": risk.get("score", None),
        "efficiency_color": sanitize(efficiency.get("color", "")),
        "efficiency_score": efficiency.get("score", None),
        "identifiers": {
            sanitize(ident.get("identifierType", {}).get("name", "")): sanitize(ident.get("value", ""))
            for ident in data.get("resourceKey", {}).get("resourceIdentifiers", [])
        },
        "status_states": [
            {
                "state": sanitize(s.get("resourceState", "")),
                "status": sanitize(s.get("resourceStatus", "")),
            }
            for s in data.get("resourceStatusStates", [])
        ],
    }


# ---------------------------------------------------------------------------
# get_resource_metrics
# ---------------------------------------------------------------------------


def get_resource_metrics(
    client: AriaClient,
    resource_id: str,
    metric_keys: list[str],
    begin_time_ms: int | None = None,
    end_time_ms: int | None = None,
    rollup_type: str = "AVG",
    interval_type: str = "MINUTES",
    interval_quantity: int = 5,
) -> dict:
    """Fetch time-series metric stats for a resource.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: The resource UUID.
        metric_keys: List of metric keys, e.g. ["cpu|usage_average", "mem|usage_average"].
        begin_time_ms: Start of query window (epoch ms). Defaults to 1 hour ago.
        end_time_ms: End of query window (epoch ms). Defaults to now.
        rollup_type: Aggregation type: AVG, MAX, MIN, SUM, COUNT, LATEST.
        interval_type: MINUTES, HOURS, DAYS, WEEKS, MONTHS.
        interval_quantity: Number of interval_type units per data point.

    Returns:
        Dict keyed by metric_key mapping to a list of {timestamp, value} points.
    """
    if not resource_id:
        raise ValueError("resource_id must not be empty")
    if not metric_keys:
        raise ValueError("metric_keys must not be empty")

    import time as _time

    if end_time_ms is None:
        end_time_ms = int(_time.time() * 1000)
    if begin_time_ms is None:
        begin_time_ms = end_time_ms - 3_600_000  # 1 hour

    # Per StatQuery spec: statKey is an array of plain strings, and the
    # interval count field is `intervalQuantifier` (2026-06-08 user report:
    # we sent [{"key": ...}] objects and `intervalQuantity`, both rejected).
    payload: dict[str, Any] = {
        "resourceId": [resource_id],
        "statKey": list(metric_keys),
        "begin": begin_time_ms,
        "end": end_time_ms,
        "rollUpType": rollup_type.upper(),
        "intervalType": interval_type.upper(),
        "intervalQuantifier": interval_quantity,
    }

    # Pure query endpoint — idempotent, safe to retry transient gateway errors.
    data = client.post(f"/resources/{resource_id}/stats/query", json_data=payload, retries=1)

    # Response nests stats under values[].stat-list.stat[] (hyphenated wire
    # key; some renderings show statList — parse both defensively).
    result: dict[str, list[dict]] = {}
    for value_entry in data.get("values", []):
        stat_container = value_entry.get("stat-list") or value_entry.get("statList") or {}
        for stat in stat_container.get("stat", []):
            key = sanitize(stat.get("statKey", {}).get("key", ""))
            timestamps = stat.get("timestamps", [])
            values = stat.get("data", [])
            result[key] = [
                {"timestamp_ms": ts, "value": v}
                for ts, v in zip(timestamps, values)
            ]
    return result


# ---------------------------------------------------------------------------
# latest_stats_bulk  (shared: replaces per-resource stats/latest N+1 loops)
# ---------------------------------------------------------------------------


def latest_stats_bulk(
    client: AriaClient,
    resource_ids: list[str],
    stat_keys: list[str],
) -> dict[str, dict[str, float | None]]:
    """Fetch the latest value of each statKey for many resources in ONE request.

    Replaces the per-resource ``GET /resources/{id}/stats/latest`` loop (an N+1
    that fired one HTTP round-trip per VM — up to ~100 for a listing) with a
    single bulk ``POST /resources/stats/query`` taking a ``resourceId`` array.
    ``rollUpType=LATEST`` over a short trailing window preserves the "latest
    value" semantics of the per-resource endpoint it replaces.

    Args:
        client: Authenticated Aria Operations API client.
        resource_ids: Resource UUIDs to query (empty entries are dropped).
        stat_keys: Metric wire keys to fetch, e.g. ["System Attributes|total_alarms"].

    Returns:
        Dict of resource_id -> {stat_key: latest value}. The value is None when a
        metric has no data for that resource; resources absent from the response
        are omitted (callers default missing entries themselves).
    """
    ids = [rid for rid in resource_ids if rid]
    if not ids or not stat_keys:
        return {}

    import time as _time

    end_ms = int(_time.time() * 1000)
    # Same StatQuery shape as get_resource_metrics (statKey string array +
    # intervalQuantifier), but with a resourceId ARRAY for the bulk endpoint.
    payload: dict[str, Any] = {
        "resourceId": ids,
        "statKey": list(stat_keys),
        "begin": end_ms - 3_600_000,  # 1 hour trailing window
        "end": end_ms,
        "rollUpType": "LATEST",
        "intervalType": "MINUTES",
        "intervalQuantifier": 5,
    }

    # Pure query endpoint — idempotent, safe to retry transient gateway errors.
    data = client.post("/resources/stats/query", json_data=payload, retries=1)

    result: dict[str, dict[str, float | None]] = {}
    # Response nests per-resource stats under values[].{resourceId, stat-list.stat[]}
    # (hyphenated wire key; some renderings show statList — parse both).
    for value_entry in data.get("values", []):
        rid = value_entry.get("resourceId", "")
        stat_container = value_entry.get("stat-list") or value_entry.get("statList") or {}
        per_resource = result.setdefault(rid, {})
        for stat in stat_container.get("stat", []):
            key = stat.get("statKey", {}).get("key", "")
            if not key:
                continue
            points = stat.get("data", [])
            per_resource[key] = points[-1] if points else None
    return result


# ---------------------------------------------------------------------------
# get_resource_health
# ---------------------------------------------------------------------------


def get_resource_health(client: AriaClient, resource_id: str) -> dict:
    """Get health badge scores for a resource (health, risk, efficiency).

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: The resource UUID.

    Returns:
        Dict with health, risk, and efficiency scores and colors.
    """
    if not resource_id:
        raise ValueError("resource_id must not be empty")

    # suite-api has no /resources/{id}/badge/* endpoints — badges come back
    # as the badges[] array on the ResourceDto (2026-06-08 spec audit).
    data = client.get(f"/resources/{resource_id}")
    badges = {b.get("type", ""): b for b in data.get("badges", [])}

    def _badge(kind: str) -> dict:
        b = badges.get(kind, {})
        return {"score": b.get("score", None), "color": sanitize(b.get("color", ""))}

    health = _badge("HEALTH")
    risk = _badge("RISK")
    efficiency = _badge("EFFICIENCY")
    return {
        "resource_id": resource_id,
        "health_score": health["score"],
        "health_color": health["color"],
        "risk_score": risk["score"],
        "risk_color": risk["color"],
        "efficiency_score": efficiency["score"],
        "efficiency_color": efficiency["color"],
    }


# ---------------------------------------------------------------------------
# get_top_consumers
# ---------------------------------------------------------------------------


def get_top_consumers(
    client: AriaClient,
    metric_key: str = "cpu|usage_average",
    resource_kind: str = "VirtualMachine",
    top_n: int = 10,
) -> list[dict]:
    """Query the resources with highest consumption of a given metric.

    Args:
        client: Authenticated Aria Operations API client.
        metric_key: The metric to rank by (e.g. cpu|usage_average, mem|usage_average).
        resource_kind: Resource kind to scope the query.
        top_n: Number of top consumers to return (max 50).

    Returns:
        List of dicts with resource id, name, and latest metric value, sorted descending.
    """
    top_n = max(1, min(top_n, 50))

    # POST /resources/query/topn does not exist in the suite-api (2026-06-08
    # user report). The real endpoint is GET /resources/stats/topn, which has
    # no resourceKind parameter — resolve candidate resource IDs first, then
    # rank them. The candidate collection is walked across ALL pages via
    # list_resources: the old single pageSize=200 page silently dropped every
    # candidate beyond the first 200, so in a large environment the true top
    # consumer could fall outside the ranked set entirely.
    candidates = list_resources(client, resource_kind=resource_kind)
    if not candidates:
        return []
    if len(candidates) > _TOPN_MAX_RESOURCE_IDS:
        _log.warning(
            "topn candidate set has %d resources but GET /resources/stats/topn "
            "can rank at most %d resourceIds per call (URL length — HTTP 414 "
            "risk); ranking the first %d only. Narrow with a more specific "
            "resource_kind to rank the full set.",
            len(candidates),
            _TOPN_MAX_RESOURCE_IDS,
            _TOPN_MAX_RESOURCE_IDS,
        )
        candidates = candidates[:_TOPN_MAX_RESOURCE_IDS]
    names = {c["id"]: c["name"] for c in candidates if c["id"]}

    import time as _time

    end_ms = int(_time.time() * 1000)
    params: dict[str, Any] = {
        "resourceId": list(names.keys()),
        "statKey": metric_key,
        "topN": top_n,
        "begin": end_ms - 3_600_000,
        "end": end_ms,
        "rollUpType": "AVG",
        "intervalType": "MINUTES",
        "intervalQuantifier": 5,
        "sortOrder": "DESCENDING",
        "groupBy": "RESOURCE",
    }
    data = client.get("/resources/stats/topn", params=params)

    results = []
    for group in data.get("resourceStatGroups", []):
        rid = group.get("groupKey", "")
        latest_value = None
        # Each resourceStats[] element is {resourceId, stat: {statKey,
        # timestamps, data}} — the data array nests under `stat`.
        for entry in group.get("resourceStats", []):
            points = entry.get("stat", {}).get("data", [])
            if points:
                latest_value = points[-1]
        results.append(
            {
                "id": sanitize(rid),
                "name": names.get(rid, ""),
                "metric_key": metric_key,
                "value": latest_value,
            }
        )
    return results[:top_n]

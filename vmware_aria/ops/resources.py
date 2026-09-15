"""Aria Operations resource queries: list, get details, metrics, health, top consumers.

All API responses pass through sanitize() to strip control characters and limit length.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

from vmware_aria.connection import AriaApiError
from vmware_aria.ops._ids import require_uuid
from vmware_aria.ops.metric_summary import summarize_series
from vmware_aria.ops.service_state import is_service_kind, read_service_state

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.resources")

_RESOURCE_ID_HINT = (
    "Run list_resources (filter with name= or resource_kind=, e.g. "
    "resource_kind='VirtualMachine') and copy an exact 'id' value."
)

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
    """Project one ResourceDto onto the high-signal summary fields we return.

    ``resourceStatusStates`` carries two different things. ``resourceState`` is
    Aria's lifecycle state for the object — STARTED for a powered-off VM too —
    and ``resourceStatus`` is whether data is arriving (``DATA_RECEIVING`` /
    ``NO_DATA_RECEIVING``, both read on 8.18.7). ``status`` has always carried
    the first; ``collection_status`` is the one that answers "which objects
    stopped reporting".
    """
    health = _badges_by_type(r).get("HEALTH", {})
    # guard: API may return "resourceStatusStates": [] (key present, empty)
    first_state = (r.get("resourceStatusStates") or [{}])[0]
    aria_state = sanitize(first_state.get("resourceState", ""))
    collection = first_state.get("resourceStatus")
    return {
        "id": sanitize(r.get("identifier", "")),
        "name": sanitize(r.get("resourceKey", {}).get("name", "")),
        "kind": sanitize(r.get("resourceKey", {}).get("resourceKindKey", "")),
        "adapter_kind": sanitize(r.get("resourceKey", {}).get("adapterKindKey", "")),
        "health_color": sanitize(health.get("color", "")),
        "health_score": health.get("score", None),
        "status": aria_state,
        "aria_state": aria_state,
        "collection_status": sanitize(str(collection)) if collection else None,
    }


def _resource_page(
    results: list[dict],
    limit: int | None,
    total_count: int | None,
    client_side: bool,
    wanted_status: str | None,
    seen_statuses: set[str],
    resource_kind: str | None = None,
    *,
    scanned: int = 0,
    capped: bool = False,
) -> dict:
    """Wrap a listing; say what was seen when a status filter matched nothing.

    An empty answer to "which objects are NO_DATA_RECEIVING" reads as "none",
    and a misspelt status produces exactly that answer. Naming the statuses the
    listing did contain is what tells the two apart.
    """
    extra: dict[str, object] = {}
    if wanted_status and not results:
        seen = ", ".join(sorted(seen_statuses)) or "none — no objects were listed"
        if capped:
            # "No object has it" would be a claim about objects nobody read.
            extra["note"] = (
                f"No object with collection_status {wanted_status} among the first "
                f"{scanned} of {total_count} objects: the scan stopped at its "
                f"{_RESOURCES_MAX_TOTAL}-object safety cap, so the rest were not "
                f"checked. Narrow resource_kind or name_filter. Statuses seen: {seen}."
            )
        else:
            widen = "" if resource_kind is None else ", or widen resource_kind ('all' lists every kind)"
            extra["note"] = (
                f"No object has collection_status {wanted_status}. Statuses seen in "
                f"this listing: {seen}. Check the spelling{widen}."
            )
    if capped:
        extra["scan_complete"] = False
    return paginated(results, limit=limit, total=None if client_side else total_count, **extra)


def list_resources(
    client: AriaClient,
    resource_kind: str | None = "VirtualMachine",
    limit: int | None = None,
    name_filter: str | None = None,
    collection_status: str | None = None,
) -> dict:
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
            ``None`` lists every kind — the objects under one adapter instance
            are VMs, hosts and datastores at once.
        limit: Maximum number of results to return. ``None`` (default) returns
            all resources of the kind, walking every page up to an internal
            safety cap. Pass an int to stop early.
        name_filter: Optional substring filter on resource name (case-insensitive).
        collection_status: Optional ``resourceStatus`` to keep, compared
            case-insensitively (e.g. ``NO_DATA_RECEIVING``). Applied client-side
            like ``name_filter``. A row with no reported status never matches.

    Returns:
        Result envelope with resource summary dicts under ``items``, each with
        id, name, kind, health badge, ``aria_state`` and ``collection_status``
        (``status`` is kept and equals ``aria_state``). ``total`` carries the
        kind's ``pageInfo.totalCount``, except under a name_filter or a
        collection_status filter — both are applied client-side, so the
        server's count describes the unfiltered collection, not this result.
        When a collection_status filter matches nothing, ``note`` names the
        statuses the listing contained.
    """
    if resource_kind is not None and resource_kind not in _VALID_RESOURCE_KINDS:
        _log.warning("Unknown resource_kind '%s', proceeding anyway", resource_kind)

    # Hard ceiling on rows fetched: the explicit limit if given, otherwise the
    # safety cap. name_filter is applied client-side, so we keep paging until
    # the unfiltered totalCount is exhausted rather than stopping at `limit`
    # filtered matches. NOTE: GET /resources exposes a server-side `name` regex
    # param, but we keep the client-side case-insensitive substring match for
    # predictable semantics; this is correct, just walks the full unfiltered
    # collection (bounded by the safety cap) rather than pushing the filter down.
    filter_lc = name_filter.lower() if name_filter else None
    # Client-side too: the 8.6 and 9.1 operation indexes list GET /resources but
    # not its query parameters, so no server-side status filter is assumed.
    wanted_status = collection_status.strip().upper() if collection_status else None
    client_side = bool(filter_lc or wanted_status)
    seen_statuses: set[str] = set()
    # A client-side filter runs after the page is read, so `limit` counts matches,
    # not rows. Capping the scan at `limit` rows stopped after the first page and
    # answered "no object matched" for an estate it had not finished reading.
    fetch_cap = (
        _RESOURCES_MAX_TOTAL if limit is None or client_side else min(limit, _RESOURCES_MAX_TOTAL)
    )

    results: list[dict] = []
    fetched = 0
    page = 0
    total_count: int | None = None
    capped = False
    while True:
        params: dict[str, Any] = {"page": page, "pageSize": _RESOURCES_PAGE_SIZE}
        if resource_kind is not None:
            # Omitted, the collection is every kind.
            params["resourceKind"] = resource_kind
        data = client.get("/resources", params=params)
        items = data.get("resourceList", []) or []
        # Read the count before consuming the page: an explicit `limit` returns
        # from inside the item loop below, and the envelope still needs it.
        total_count = (data.get("pageInfo") or {}).get("totalCount", total_count)
        if not items:
            break

        for r in items:
            fetched += 1
            summary = _summarize_resource(r)
            if wanted_status:
                status = (summary["collection_status"] or "").upper()
                # Recorded before the name filter: the note describes what was read.
                seen_statuses.add(status or "not reported")
            if filter_lc and filter_lc not in summary["name"].lower():
                continue
            if wanted_status:
                # An object with no reported status is unknown, and unknown
                # never satisfies a filter for a specific status.
                if status != wanted_status:
                    continue
            results.append(summary)
            if limit is not None and len(results) >= limit:
                return _resource_page(
                    results, limit, total_count, client_side, wanted_status, seen_statuses,
                    resource_kind, scanned=fetched,
                )

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
            capped = True
            break
        page += 1

    return _resource_page(
        results, limit, total_count, client_side, wanted_status, seen_statuses,
        resource_kind, scanned=fetched, capped=capped,
    )


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
    resource_id = require_uuid(resource_id, "resource_id", "resource", _RESOURCE_ID_HINT)

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
    summary: bool = False,
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
        summary: Return per-metric summaries (``summary``) instead of every
            point (``metrics``). The raw series is the default.

    Returns:
        ``metrics``: metric key -> list of ``{timestamp_ms, value}`` points, only
        for keys that returned at least one point. ``missing``: one entry per
        requested key that did not, with ``reason`` —
        ``not_collected_for_resource`` (the resource never reports that key;
        ``similar_keys`` lists its keys in the same group),
        ``no_data_in_window`` (it reports the key, no points in the window),
        ``resource_reports_no_stat_keys``, or ``undetermined`` (the key list
        could not be read) — and ``detail``. ``stat_keys_on_resource`` is how
        many keys the resource reports (``None`` when not read: it is only read
        when something is missing). Also ``resource_id``, ``window_begin_ms``,
        ``window_end_ms``. A resource id that does not exist is a 404 and raises.
        ``mode`` is ``raw`` or ``summary``; in summary mode ``metrics`` is
        replaced by ``summary``: metric key -> :func:`summarize_series` of its
        points (n, min, max, avg, latest, change points).
    """
    resource_id = require_uuid(resource_id, "resource_id", "resource", _RESOURCE_ID_HINT)
    if not metric_keys:
        raise ValueError(
            "metric_keys must be a non-empty list of Aria statKeys, e.g. "
            "['cpu|usage_average', 'mem|usage_average']. Specify at least one — "
            "there is no 'all metrics' mode. At the CLI these are the "
            "comma-separated values of --metrics/-m."
        )

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

    metrics = {key: points for key, points in _parse_stat_series(data).items() if points}
    # A key that is not collected and a key with no points in the window both
    # come back as simply absent — live 8.18.7 answers {"values": []} for
    # either (2026-09-13). Only the resource's stat-key list tells them apart,
    # so it is read on this path alone.
    absent = [key for key in dict.fromkeys(metric_keys) if key not in metrics]
    missing, key_count = _explain_missing(client, resource_id, absent) if absent else ([], None)
    series: dict[str, Any] = (
        {"summary": {key: summarize_series(points) for key, points in metrics.items()}}
        if summary
        else {"metrics": metrics}
    )
    return {
        "resource_id": resource_id,
        "window_begin_ms": begin_time_ms,
        "window_end_ms": end_time_ms,
        "mode": "summary" if summary else "raw",
        **series,
        "missing": missing,
        "stat_keys_on_resource": key_count,
    }


def _parse_stat_series(data: dict) -> dict[str, list[dict]]:
    """Map statKey -> points from a stats/query body.

    Response nests stats under values[].stat-list.stat[] (hyphenated wire key;
    some renderings show statList — parse both defensively).
    """
    result: dict[str, list[dict]] = {}
    for value_entry in data.get("values", []):
        stat_container = value_entry.get("stat-list") or value_entry.get("statList") or {}
        for stat in stat_container.get("stat", []):
            key = sanitize(stat.get("statKey", {}).get("key", ""))
            timestamps = stat.get("timestamps", [])
            values = stat.get("data", [])
            result[key] = [{"timestamp_ms": ts, "value": v} for ts, v in zip(timestamps, values)]
    return result


#: At most this many ``similar_keys`` per missing metric.
_MAX_SIMILAR_KEYS = 10

#: A statKey longer than this is not offered as a suggestion. Real keys are
#: short (the longest of the 1,690 VM and host keys 8.18.7 defines is well
#: under 100 characters); a suggestion is something to pass back verbatim, so
#: a truncated one would be a key that does not exist.
_MAX_SUGGESTED_KEY_LEN = 200


def _resource_stat_keys(client: AriaClient, resource_id: str) -> tuple[list[str] | None, str | None]:
    """The stat keys a resource reports, or ``(None, why it could not be read)``.

    Only a ``stat-key`` list whose every row carries a string ``key`` is read
    as the resource's keys. A row in any other form means the body is not the
    shape this understands, and a partial or empty read of it would be
    reported as "this resource never reports that key" — a confident answer
    from an unread body (形态 #1). Those cases return ``None`` instead.
    """
    path = f"/resources/{resource_id}/statkeys"
    try:
        data = client.get(path)
    except AriaApiError as exc:
        status = f"HTTP {exc.status_code}" if exc.status_code is not None else "no HTTP response"
        return None, f"GET {path} failed ({status})"
    rows = data.get("stat-key") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None, f"GET {path} answered without a 'stat-key' list"
    keys = [r["key"] for r in rows if isinstance(r, dict) and isinstance(r.get("key"), str)]
    if len(keys) != len(rows):
        return None, (
            f"GET {path} answered with {len(rows) - len(keys)} of {len(rows)} "
            f"'stat-key' rows in an unrecognised form (no string 'key')"
        )
    return keys, None


def _similar_keys(available: list[str], key: str) -> tuple[list[str], int]:
    """Keys in ``key``'s group to suggest, and how many were withheld as unsafe.

    A key is offered only if sanitizing it changes nothing and it fits
    ``_MAX_SUGGESTED_KEY_LEN``: control characters or an oversized string
    cannot be passed back as a real key, and must not reach the agent verbatim.
    """
    group = key.split("|", 1)[0] + "|"
    in_group = sorted({k for k in available if k.startswith(group)})
    safe = [k for k in in_group if sanitize(k, max_len=_MAX_SUGGESTED_KEY_LEN) == k]
    return safe[:_MAX_SIMILAR_KEYS], len(in_group) - len(safe)


def _explain_missing(
    client: AriaClient, resource_id: str, keys: list[str]
) -> tuple[list[dict], int | None]:
    """Say, per requested key with no points, which kind of empty it is."""
    available, error = _resource_stat_keys(client, resource_id)
    if available is None:
        detail = (
            f"{error}, so a key this resource does not report cannot be told "
            f"apart from one with no points in the window."
        )
        return [_missing(k, "undetermined", detail) for k in keys], None
    have = set(available)
    entries = []
    for key in keys:
        if not available:
            entries.append(_missing(key, "resource_reports_no_stat_keys", (
                "This resource reports no stat keys at all: it may be newly "
                "discovered, not collected, or not the resource you meant."
            )))
        elif key in have:
            entries.append(_missing(key, "no_data_in_window", (
                "The resource reports this key but has no points in the window. "
                "Widen hours, or check its collector with list_collector_groups."
            )))
        else:
            similar, withheld = _similar_keys(available, key)
            detail = (
                f"This resource has never reported this key ({len(available)} keys "
                f"reported). Try one of similar_keys."
            )
            if withheld:
                detail += (
                    f" {withheld} key(s) in the same group were omitted: they contain "
                    f"control characters or exceed {_MAX_SUGGESTED_KEY_LEN} characters."
                )
            entries.append(_missing(key, "not_collected_for_resource", detail, similar))
    return entries, len(available)


def _missing(key: str, reason: str, detail: str, similar: list[str] | None = None) -> dict:
    return {"metric_key": sanitize(key), "reason": reason, "detail": detail, "similar_keys": similar or []}


# ---------------------------------------------------------------------------
# latest_stats_bulk  (shared: replaces per-resource stats/latest N+1 loops)
# ---------------------------------------------------------------------------


def latest_stats_bulk(
    client: AriaClient,
    resource_ids: list[str],
    stat_keys: list[str],
    window_ms: int = 3_600_000,
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
        "begin": end_ms - window_ms,  # trailing window; 1 h default
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
    resource_id = require_uuid(resource_id, "resource_id", "resource", _RESOURCE_ID_HINT)

    # suite-api has no /resources/{id}/badge/* endpoints — badges come back
    # as the badges[] array on the ResourceDto (2026-06-08 spec audit).
    data = client.get(f"/resources/{resource_id}")
    badges = {b.get("type", ""): b for b in data.get("badges", [])}
    key = data.get("resourceKey") if isinstance(data.get("resourceKey"), dict) else {}
    kind = sanitize(str(key.get("resourceKindKey") or ""))

    def _badge(badge_type: str) -> dict:
        b = badges.get(badge_type, {})
        return {"score": b.get("score", None), "color": sanitize(b.get("color", ""))}

    health = _badge("HEALTH")
    risk = _badge("RISK")
    efficiency = _badge("EFFICIENCY")
    return {
        "resource_id": resource_id,
        "name": sanitize(str(key.get("name") or "")),
        "kind": kind,
        "health_score": health["score"],
        "health_color": health["color"],
        "risk_score": risk["score"],
        "risk_color": risk["color"],
        "efficiency_score": efficiency["score"],
        "efficiency_color": efficiency["color"],
        # A service object's badges are not its service state (8.18.7: mem
        # service GREEN 100 with SERVICE|AVAILABILITY 0) — read that state too.
        "service": read_service_state(client, resource_id) if is_service_kind(kind) else None,
    }


# ---------------------------------------------------------------------------
# get_top_consumers
# ---------------------------------------------------------------------------


def get_top_consumers(
    client: AriaClient,
    metric_key: str = "cpu|usage_average",
    resource_kind: str = "VirtualMachine",
    top_n: int = 10,
) -> dict:
    """Query the resources with highest consumption of a given metric.

    Args:
        client: Authenticated Aria Operations API client.
        metric_key: The metric to rank by (e.g. cpu|usage_average, mem|usage_average).
        resource_kind: Resource kind to scope the query.
        top_n: Number of top consumers to return (max 50).

    Returns:
        Result envelope with dicts under ``items`` carrying resource id, name,
        ``value`` — the average of the window's points, which is what Aria ranks
        by (last hour, 5-minute AVG rollup; live 8.18.7 orders by it, not by the
        last point) — and ``latest_value``, the most recent point. Items keep
        Aria's order, descending by ``value``. A ranking is a top-N slice
        of an unbounded set, so ``total`` is None and a full page is flagged
        truncated.
    """
    top_n = max(1, min(top_n, 50))

    # POST /resources/query/topn does not exist in the suite-api (2026-06-08
    # user report). The real endpoint is GET /resources/stats/topn, which has
    # no resourceKind parameter — resolve candidate resource IDs first, then
    # rank them. The candidate collection is walked across ALL pages via
    # list_resources: the old single pageSize=200 page silently dropped every
    # candidate beyond the first 200, so in a large environment the true top
    # consumer could fall outside the ranked set entirely.
    candidates = list_resources(client, resource_kind=resource_kind)["items"]
    if not candidates:
        return {
            **paginated([], limit=top_n),
            "hint": (
                f"No {resource_kind} resources were found, so nothing was ranked. "
                f"Check the kind name with list_resources."
            ),
        }
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

    groups = data.get("resourceStatGroups", [])[:top_n]
    results = []
    excluded = 0
    for group in groups:
        rid = group.get("groupKey", "")
        points: list[float] = []
        # Each resourceStats[] element is {resourceId, stat: {statKey,
        # timestamps, data}} — the data array nests under `stat`.
        for entry in group.get("resourceStats", []):
            points.extend(entry.get("stat", {}).get("data", []) or [])
        if not points:
            # Listed with no points: not a zero, so not a rank.
            excluded += 1
            continue
        results.append(
            {
                "id": sanitize(rid),
                "name": names.get(rid, ""),
                "metric_key": metric_key,
                # The ranking is by the window average; reporting the last point
                # as the value made a correct ranking read as unsorted.
                "value": sum(points) / len(points),
                "latest_value": points[-1],
            }
        )
    envelope = {**paginated(results, limit=top_n), "excluded_no_data": excluded}
    if excluded and len(groups) >= top_n:
        # Every slot the API gave was used, some by no-data rows, so the
        # resources ranked just below are unseen — more may have data.
        return {
            **envelope,
            "truncated": True,
            "hint": (
                f"{excluded} of the {len(groups)} resources the ranking returned had "
                f"no points for '{metric_key}' and were left out, not ranked at zero. "
                f"Others below them may have data: raise top_n to see them."
            ),
        }
    hint = _ranking_gap_hint(len(results), len(names), top_n, metric_key, resource_kind)
    return {**envelope, "hint": hint} if hint and not envelope["truncated"] else envelope


def _ranking_gap_hint(ranked: int, candidates: int, top_n: int, metric_key: str, kind: str) -> str | None:
    """Explain a ranking shorter than both top_n and the candidate set.

    Resources with no points for the key are left out of the ranking — the
    topn answer omits them (live 8.18.7: an unknown key returns
    ``resourceStatGroups: []``), and a group it does list with empty data is
    dropped by the caller — so a short or empty ranking means "not reported",
    never "consuming nothing".
    """
    if ranked >= min(top_n, candidates):
        return None
    if ranked == 0:
        return (
            f"None of the {candidates} {kind} resources returned data for "
            f"'{metric_key}' in the last hour, so nothing was ranked — not a sign "
            f"that nothing is consuming. Call get_resource_metrics on one of them: "
            f"its 'missing' field says whether the key is not collected or had no points."
        )
    return (
        f"Only {ranked} of {candidates} {kind} resources returned data for "
        f"'{metric_key}' in the last hour; the other {candidates - ranked} were left "
        f"out of the ranking, not ranked at zero. get_resource_metrics on one of them says why."
    )

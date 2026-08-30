"""Aria Operations anomaly signals: per-resource anomaly metric and risk badge.

2026-06-08 spec audit: the suite-api has NO anomaly listing endpoints —
the previously used /anomalies and /resources/{id}/anomalies paths never
existed (the UI's "anomalous metrics" view is not part of the public API),
and /resources/{id}/badge/* endpoints don't exist either. The real signals
are the "System Attributes|total_alarms" Total Anomalies metric (counts
active anomalies — symptoms, events, and DT violations on the object and
its children) and the badges[] array on the ResourceDto.

All API responses pass through sanitize() to strip control characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vmware_policy import paginated, sanitize

from vmware_aria.ops._paging import CollectionTotal, iter_collection, validate_page_args
from vmware_aria.ops.resources import latest_stats_bulk

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.anomaly")

# Total Anomalies metric wire key — "System Attributes|anomaly" does not
# exist; the metric counts active anomalies (symptoms/events/DT violations
# on the object and its children).
_TOTAL_ANOMALIES_STAT_KEY = "System Attributes|total_alarms"

#: Page requested from GET /resources while enumerating the objects to rank.
#: This is the appliance's page size, deliberately unrelated to the caller's
#: ``limit`` — conflating the two is the defect this module was fixed for.
_LISTING_PAGE_SIZE = 1000

#: Resource ids per bulk ``POST /resources/stats/query``. Ranking the estate
#: means reading the metric for every object, and the whole inventory in one
#: request body is not a request an appliance should be asked to answer. One
#: request per 500 objects keeps the fan-out bulk (踩坑 #31: never one round
#: trip per object) without an unbounded body.
_STATS_CHUNK = 500

#: Objects examined before the ranking is declared partial. Ranking requires
#: the metric for every candidate, so the cost of a complete answer is
#: ``ceil(N/1000)`` listing requests plus ``ceil(N/500)`` metric requests — at
#: this cap, 5 + 10. Past it the scan stops and the envelope says the ranking
#: covers only what was scanned, which is the honest answer; silently ranking
#: a prefix of the estate is the answer this module used to give.
_MAX_SCAN = 5000

_PARTIAL_SCAN_NOTE = (
    "partial ranking — {scanned} of {vm_total} objects were examined before "
    "the {cap}-object scan cap, so an unexamined object could outrank every "
    "row here. Scope the query with resource_id, or read this as 'the worst "
    "among the first {scanned}' rather than 'the worst'."
)


# ---------------------------------------------------------------------------
# list_anomalies
# ---------------------------------------------------------------------------


def list_anomalies(
    client: AriaClient,
    resource_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Report per-resource anomaly counts from the System Attributes metric.

    The public suite-api does not expose the UI's anomalous-metrics list;
    the available signal is the "System Attributes|total_alarms" Total Anomalies metric (count
    of active anomalies — symptoms, events, DT violations — on the object
    and its children). With resource_id, returns the
    count for that resource; without it, ranks every VirtualMachine resource in
    the environment by anomaly count and returns the worst ``limit`` of them.
    For root-cause detail, follow up with get_alert/list_alerts.

    ``limit`` bounds the answer, not the scan. Until 2026-08-30 it was pushed
    down as the listing's ``pageSize``, so the sort ran over one page rather
    than over the environment: asked for the top 3 on a real VCF Operations 9.1
    estate, the tool returned three plausible rows and left out the worst
    object in the fleet (the vCenter adapter instance, 18 anomalies), because
    that object was not on the page that happened to come back. There is no
    server-side alternative — the ranking key is a metric from
    POST /resources/stats/query, not a field on GET /resources, so the
    appliance cannot sort by it and the objects must be enumerated to be
    ranked.

    That has a price, and it is bounded rather than hidden: the inventory is
    walked in 1000-object pages and the metric is read in 500-object bulk
    queries, so ranking N objects costs ceil(N/1000) + ceil(N/500) requests and
    no per-object round trip (踩坑 #31). Past ``_MAX_SCAN`` objects the scan
    stops and the envelope says the ranking is partial.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Optional resource UUID to scope the query.
        limit: Maximum number of ranked rows to return (1–500). A page size,
            not a scan bound: out-of-range values are rejected, not clamped.

    Returns:
        Result envelope with dicts under ``items`` carrying resource id, name,
        and anomaly_count (latest value; None when the metric has no data for
        the resource), sorted worst first, plus ``scanned`` (objects examined),
        ``vm_total`` (the environment's VM ``pageInfo.totalCount``) and
        ``scan_complete``. ``total`` is the number of flagged objects when the
        scan was complete — so ``truncated`` is exact and the hint names the
        real number — and the environment's VM count when it was not, where an
        unexamined object could outrank everything shown; ``scan_complete``
        says which of the two is being reported, and a partial ranking also
        carries a ``note``.
    """
    validate_page_args(limit, 0)

    vm_total: int | None = None
    if resource_id:
        targets = {resource_id: ""}
    else:
        targets, vm_total = _rank_targets(client)

    # Bulk POST /resources/stats/query, chunked — replaces the old per-VM
    # GET /resources/{id}/stats/latest loop (an N+1 firing one round-trip per
    # VM). Chunking keeps that property while ranking an estate rather than a
    # page: the request count grows with the estate, not with the object count.
    ids = [rid for rid in targets if rid]
    stats_by_resource: dict[str, dict[str, float | None]] = {}
    for start in range(0, len(ids), _STATS_CHUNK):
        stats_by_resource.update(
            latest_stats_bulk(client, ids[start : start + _STATS_CHUNK], [_TOTAL_ANOMALIES_STAT_KEY])
        )

    results = []
    for rid, name in targets.items():
        if not rid:
            continue
        count = stats_by_resource.get(rid, {}).get(_TOTAL_ANOMALIES_STAT_KEY)
        results.append(
            {
                "resource_id": sanitize(rid),
                "resource_name": name,
                "anomaly_count": count,
                "metric_key": _TOTAL_ANOMALIES_STAT_KEY,
            }
        )

    scanned = len(targets)
    if resource_id:
        return paginated(results, scanned=scanned)

    flagged = [r for r in results if r["anomaly_count"]]
    flagged.sort(key=lambda r: r["anomaly_count"] or 0, reverse=True)
    # Rank first, cut second. The cut is the last thing that happens to the
    # list, which is the whole of the fix.
    page = flagged[:limit]

    scan_complete = vm_total is None or scanned >= vm_total
    if scan_complete:
        # Every object was ranked, so the size of the answer set is known:
        # reporting it makes `truncated` exact and lets the hint name the
        # number of anomalous objects the caller has not seen.
        return paginated(
            page,
            limit=limit,
            total=len(flagged),
            scanned=scanned,
            vm_total=vm_total,
            scan_complete=True,
        )
    # Objects went unexamined, so the flagged count is a floor, not a total.
    # The environment's VM count goes in `total` instead: it is the number that
    # makes `truncated` true, which is the claim that must survive being read
    # quickly — an unexamined object could outrank every row here.
    return paginated(
        page,
        limit=limit,
        total=vm_total,
        scanned=scanned,
        vm_total=vm_total,
        scan_complete=False,
        note=_PARTIAL_SCAN_NOTE.format(scanned=scanned, vm_total=vm_total, cap=_MAX_SCAN),
    )


def _rank_targets(client: AriaClient) -> tuple[dict[str, str], int | None]:
    """Enumerate the VirtualMachine resources to rank, and the environment's count.

    Walks GET /resources in appliance-sized pages up to :data:`_MAX_SCAN`. The
    count is read from ``pageInfo.totalCount`` through a sink because the walk
    can stop before the collection ends, and the envelope needs to know that it
    did.
    """
    sink = CollectionTotal()
    targets: dict[str, str] = {}
    for row in iter_collection(
        client,
        "/resources",
        "resourceList",
        extra_params={"resourceKind": "VirtualMachine"},
        page_size=_LISTING_PAGE_SIZE,
        max_total=_MAX_SCAN,
        total_sink=sink,
    ):
        rid = row.get("identifier", "")
        if rid:
            targets[rid] = sanitize(row.get("resourceKey", {}).get("name", ""))
        if len(targets) >= _MAX_SCAN:
            _log.warning(
                "anomaly ranking stopped at the %d-object scan cap; the result "
                "is the worst among those, not the worst overall.",
                _MAX_SCAN,
            )
            break
    return targets, sink.value


# ---------------------------------------------------------------------------
# get_resource_riskbadge
# ---------------------------------------------------------------------------


def get_resource_riskbadge(client: AriaClient, resource_id: str) -> dict:
    """Get the risk badge score for a resource.

    The risk badge reflects the likelihood of a future performance or
    availability problem, scored 0–100 (higher = more risk; -1 = unknown).
    Badges come from the badges[] array on the ResourceDto — there is no
    /badge/risk endpoint in the suite-api.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: The resource UUID.

    Returns:
        Dict with risk score and color. For contributing causes, inspect the
        resource's active alerts via list_alerts(resource_id=...).
    """
    if not resource_id:
        raise ValueError(
            "resource_id must be a non-empty Aria resource UUID. Run list_resources "
            "(filter with name= or resource_kind=) and copy an exact 'id' value."
        )

    data = client.get(f"/resources/{resource_id}")
    risk = next(
        (b for b in data.get("badges", []) if b.get("type") == "RISK"),
        {},
    )
    return {
        "resource_id": resource_id,
        "risk_score": risk.get("score", None),
        "risk_color": sanitize(risk.get("color", "")),
    }

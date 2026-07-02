"""Aria Operations capacity planning: overview, remaining capacity, time remaining, rightsizing.

2026-06-08 spec audit: the suite-api has NO dedicated capacity endpoints —
the previously used /resources/{id}/recommendations, /remainingcapacity and
/timeremaining paths never existed and returned 404 against real instances.
Capacity analytics are delivered exclusively as metrics through the stats
endpoints, under the ``OnlineCapacityAnalytics|*`` statKey family
(per-dimension: cpu / mem / diskspace, each with demand/alloc variants).

All API responses pass through sanitize() to strip control characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vmware_policy import sanitize

from vmware_aria.ops.resources import latest_stats_bulk

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.capacity")

_CAPACITY_DIMENSIONS = ("cpu", "mem", "diskspace")

# The remaining-capacity PERCENTAGE exists only at group level — there is
# no per-dimension OnlineCapacityAnalytics|{dim}|demand|capacityRemainingPercentage
# key (2026-06-08 spec audit). Per-dimension keys that ARE real on
# cluster/host/datacenter: |{dim}|demand|capacityRemaining and
# |{dim}|demand|timeRemaining.
_GROUP_REMAINING_PCT_KEY = "OnlineCapacityAnalytics|capacityRemainingPercentage"


def _latest_stats(client: AriaClient, resource_id: str, stat_keys: list[str]) -> dict[str, float | None]:
    """Fetch the latest value for each statKey via GET /resources/{id}/stats/latest.

    Returns a dict statKey -> latest value (None when the metric has no data,
    e.g. capacity analytics still warming up on a fresh resource).
    """
    data = client.get(
        f"/resources/{resource_id}/stats/latest",
        params={"statKey": stat_keys},
    )
    values: dict[str, float | None] = {k: None for k in stat_keys}
    for value_entry in data.get("values", []):
        stat_container = value_entry.get("stat-list") or value_entry.get("statList") or {}
        for stat in stat_container.get("stat", []):
            key = stat.get("statKey", {}).get("key", "")
            points = stat.get("data", [])
            if key in values and points:
                values[key] = points[-1]
    return values


# ---------------------------------------------------------------------------
# get_capacity_overview
# ---------------------------------------------------------------------------


def get_capacity_overview(client: AriaClient, cluster_id: str) -> dict:
    """Get a capacity utilization overview for a cluster.

    Combines the group-level remaining-capacity percentage with per-dimension
    (cpu/mem/diskspace) absolute remaining capacity and time-remaining
    projections from the OnlineCapacityAnalytics metrics. The percentage
    exists only at group level — there is no per-dimension percentage key.

    Args:
        client: Authenticated Aria Operations API client.
        cluster_id: The cluster resource UUID.

    Returns:
        Dict with group-level capacity_remaining_pct plus per-dimension
        capacity_remaining and time_remaining_days.
        Values are None when capacity analytics have no data yet.
    """
    if not cluster_id:
        raise ValueError("cluster_id must not be empty")

    stat_keys = [_GROUP_REMAINING_PCT_KEY] + [
        f"OnlineCapacityAnalytics|{dim}|demand|{metric}"
        for dim in _CAPACITY_DIMENSIONS
        for metric in ("capacityRemaining", "timeRemaining")
    ]
    values = _latest_stats(client, cluster_id, stat_keys)

    dimensions = []
    for dim in _CAPACITY_DIMENSIONS:
        dimensions.append(
            {
                "dimension": dim,
                "capacity_remaining": values[f"OnlineCapacityAnalytics|{dim}|demand|capacityRemaining"],
                "time_remaining_days": values[f"OnlineCapacityAnalytics|{dim}|demand|timeRemaining"],
            }
        )
    return {
        "resource_id": cluster_id,
        "capacity_remaining_pct": values[_GROUP_REMAINING_PCT_KEY],
        "dimensions": dimensions,
    }


# ---------------------------------------------------------------------------
# get_remaining_capacity
# ---------------------------------------------------------------------------


def get_remaining_capacity(client: AriaClient, resource_id: str) -> dict:
    """Get remaining capacity metrics for a resource (cluster or host).

    Reports how much additional workload can be added before running out of
    CPU, memory, or disk capacity, from the OnlineCapacityAnalytics demand
    model metrics.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: The resource UUID (typically a ClusterComputeResource).

    Returns:
        Dict with group-level capacity_remaining_pct and per-dimension
        absolute remaining capacity. The percentage exists only at group
        level — there is no per-dimension percentage key.
        Values are None when capacity analytics have no data yet.
    """
    if not resource_id:
        raise ValueError("resource_id must not be empty")

    stat_keys = [_GROUP_REMAINING_PCT_KEY] + [
        f"OnlineCapacityAnalytics|{dim}|demand|capacityRemaining"
        for dim in _CAPACITY_DIMENSIONS
    ]
    values = _latest_stats(client, resource_id, stat_keys)

    return {
        "resource_id": resource_id,
        "capacity_remaining_pct": values[_GROUP_REMAINING_PCT_KEY],
        "remaining_capacity": [
            {
                "dimension": dim,
                "remaining_value": values[f"OnlineCapacityAnalytics|{dim}|demand|capacityRemaining"],
            }
            for dim in _CAPACITY_DIMENSIONS
        ],
    }


# ---------------------------------------------------------------------------
# get_time_remaining
# ---------------------------------------------------------------------------


def get_time_remaining(client: AriaClient, resource_id: str) -> dict:
    """Get time-remaining-until-full predictions for a resource.

    Aria Operations projects when each capacity dimension (CPU, memory, disk)
    will be exhausted based on current usage trends. Value is in days.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: The resource UUID (typically a ClusterComputeResource).

    Returns:
        Dict with predicted days-until-exhaustion per capacity dimension.
        Values are None when capacity analytics have no data yet.
    """
    if not resource_id:
        raise ValueError("resource_id must not be empty")

    stat_keys = [
        f"OnlineCapacityAnalytics|{dim}|demand|timeRemaining"
        for dim in _CAPACITY_DIMENSIONS
    ]
    values = _latest_stats(client, resource_id, stat_keys)

    return {
        "resource_id": resource_id,
        "time_remaining": [
            {
                "dimension": dim,
                "time_remaining_days": values[f"OnlineCapacityAnalytics|{dim}|demand|timeRemaining"],
            }
            for dim in _CAPACITY_DIMENSIONS
        ],
    }


# ---------------------------------------------------------------------------
# list_rightsizing_recommendations
# ---------------------------------------------------------------------------


def list_rightsizing_recommendations(
    client: AriaClient,
    resource_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List VM rightsizing data (recommended vs provisioned size).

    The suite-api exposes rightsizing exclusively as per-VM metrics
    (OnlineCapacityAnalytics recommendedSize); the UI "Rightsize" page uses
    internal APIs. This queries the recommended-size metrics for the given
    VM, or for up to ``limit`` VMs when no resource_id is given.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Optional VM resource UUID to scope the query.
        limit: Maximum number of VMs to evaluate when listing (1–100).

    Returns:
        List of dicts with VM id, name, and recommended cpu/mem sizes.
        Values are None for VMs where capacity analytics have no data.
    """
    limit = max(1, min(limit, 100))

    if resource_id:
        targets = {resource_id: ""}
    else:
        listing = client.get(
            "/resources", params={"resourceKind": "VirtualMachine", "pageSize": limit}
        )
        targets = {
            r.get("identifier", ""): sanitize(r.get("resourceKey", {}).get("name", ""))
            for r in listing.get("resourceList", [])
        }

    # VM-published rightsizing keys have NO demand segment (spec audit):
    # OnlineCapacityAnalytics|{cpu,mem}|recommendedSize.
    stat_keys = [
        "OnlineCapacityAnalytics|cpu|recommendedSize",
        "OnlineCapacityAnalytics|mem|recommendedSize",
    ]

    # One bulk POST /resources/stats/query for every target — replaces the old
    # per-VM GET /resources/{id}/stats/latest loop (an N+1 firing up to one
    # round-trip per VM, ~101 for a full listing).
    stats_by_resource = latest_stats_bulk(client, list(targets), stat_keys)

    results = []
    for rid, name in targets.items():
        if not rid:
            continue
        values = stats_by_resource.get(rid, {})
        results.append(
            {
                "id": sanitize(rid),
                "name": name,
                "recommended_cpu": values.get("OnlineCapacityAnalytics|cpu|recommendedSize"),
                "recommended_memory": values.get("OnlineCapacityAnalytics|mem|recommendedSize"),
            }
        )
    return results

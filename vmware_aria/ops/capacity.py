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
import math
from typing import TYPE_CHECKING

from vmware_policy import paginated, sanitize

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
        raise ValueError(
            "cluster_id must be a non-empty Aria resource UUID for a cluster. Run "
            "list_resources with resource_kind='ClusterComputeResource' and copy an "
            "exact 'id' value."
        )

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
        raise ValueError(
            "resource_id must be a non-empty Aria resource UUID. Run list_resources "
            "(filter with name= or resource_kind=) and copy an exact 'id' value."
        )

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
        raise ValueError(
            "resource_id must be a non-empty Aria resource UUID. Run list_resources "
            "(filter with name= or resource_kind=) and copy an exact 'id' value."
        )

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
) -> dict:
    """List VM rightsizing data from the capacity engine's published metrics.

    The suite-api has no rightsizing endpoint. Broadcom's Capacity Analytics
    metric list publishes it on the VM as three keys —
    ``OnlineCapacityAnalytics|{cpu,mem,diskspace}|recommendedSize`` — and those
    are what this reads, on both the 8.x and 9.x lines.

    **This is not the number the UI shows.** The product's own field engineering
    states the UI does not give the recommended absolute vCPU or memory; the
    Rightsize view presents allocated plus a suggested delta. An operator
    comparing this row against that screen will see two different numbers, both
    correct, describing different things.

    Reading the values (KB 379521, behaviour introduced after 8.17):

    * a value above zero is a recommended size;
    * **zero is not a recommended size** — the engine publishes 0 continuously
      while it considers the VM reclaimable, so a raw pass-through would tell a
      caller to size a VM down to nothing. Zero is reported as
      ``sizing_status: "reclaimable"`` with the recommendation left ``None``;
    * **nothing published is not "no data"** — a VM that needs no resizing
      publishes no metric at all, and so does one the analytics have never
      scored. The appliance does not distinguish those two, so neither does
      this: both are ``sizing_status: "none_published"``, which says the
      ambiguity out loud rather than picking a side.

    Precedence, for the case where dimensions disagree: any dimension carrying a
    size makes the row a ``recommendation``, and a zero sitting beside it is not
    reported. KB 379521 describes reclaimable as a state of the VM rather than
    of one dimension, so the three should move together and this should not
    arise; it is written down because the code cannot tell that it never does.
    ``scripts/probe_aria_rightsizing.py`` counts the three outcomes per key on a
    real estate, which is what would show it happening.

    Units and direction (verified on a live 8.18.7, 2026-09-13). The raw
    ``recommended_*`` numbers are MHz / KB / GB — ``recommended_units`` says so on
    every row. CPU MHz is recommended cores times the host's MHz per core, so it
    is converted with the VM's own ``cpu|speed`` property (total Hz across its
    vCPUs) into ``recommended_vcpus``, rounded up. ``cpu_direction`` /
    ``memory_direction`` compare that against ``config|hardware|numCpu`` and
    ``config|hardware|memoryKB``: ``oversized`` / ``undersized`` / ``right_sized``,
    or ``None`` when either side is not published. Noise is not a direction:
    MHz within ``CPU_VCPU_ROUNDING_EPSILON`` of a core rounds to that core, and
    memory within ``MEMORY_DIRECTION_TOLERANCE`` (1%) of the configured size is
    ``right_sized`` — so ``actionable`` cannot be triggered by drift (seen live:
    +3 MB on 8 GiB read as undersized). Disk carries no direction:
    what baseline the engine sizes disk against is unverified, and a virtual
    disk cannot be shrunk in place.

    Powered-off VMs and templates are **labelled, not dropped**: dropping them
    would break ``total``/``truncated`` (one row per VM evaluated), and a
    powered-off VM is itself a reclamation candidate. ``actionable`` is True only
    for a powered-on non-template VM whose CPU or memory is off its
    recommendation; every other row carries ``caveats`` saying why.

    Vendor appliances cannot be identified reliably: ``summary|config|productName``
    is published only for OVF deployments that carry a vApp product (present on
    the Aria appliance, absent on a vCenter appliance in the same estate). So
    ``product_name`` is reported when published, and every reduction carries a
    caveat to check the vendor minimum size — no name-based guessing.

    ``aria_verdict`` surfaces the engine's own ``summary|oversized|*`` /
    ``summary|undersized|*`` statistics; when they point a different way from
    ``recommendedSize`` (seen live: 2 -> 1 vCPU recommended while
    ``summary|oversized|vcpus`` is 0) a caveat names both.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Optional VM resource UUID to scope the query.
        limit: Maximum number of VMs to evaluate when listing (1–100).

    Returns:
        Result envelope whose ``items`` carry VM id and name, the three raw
        ``recommended_*`` sizes with ``recommended_units``, ``sizing_status`` (one
        of ``recommendation`` / ``reclaimable`` / ``none_published``), current
        configuration and direction, power state, template flag, product name,
        ``aria_verdict``, ``actionable`` and ``caveats``. One row per VM
        evaluated, so ``total`` carries the environment's VM
        ``pageInfo.totalCount`` — a run that evaluated every VM reads as
        complete, a capped one as truncated.
    """
    limit = max(1, min(limit, 100))

    vm_total: int | None = None
    if resource_id:
        targets = {resource_id: ""}
    else:
        listing = client.get(
            "/resources", params={"resourceKind": "VirtualMachine", "pageSize": limit}
        )
        vm_total = (listing.get("pageInfo") or {}).get("totalCount")
        targets = {
            r.get("identifier", ""): sanitize(r.get("resourceKey", {}).get("name", ""))
            for r in listing.get("resourceList", [])
        }

    # One bulk POST /resources/stats/query for every target — replaces the old
    # per-VM GET /resources/{id}/stats/latest loop (an N+1 firing up to one
    # round-trip per VM, ~101 for a full listing).
    #
    # 25-hour window rather than the helper's 1-hour default. The capacity
    # engine publishes on its own cadence, which no document commits to; behind
    # a 1-hour window any cadence longer than an hour reads every VM as
    # "none_published" on an estate that has recommendations. LATEST rollup
    # still returns one newest point per key, and a day-old recommendation is
    # current for a signal that moves as slowly as capacity. The probe reports
    # per-key presence against the catalogue, which is what would expose a
    # window still too short.
    ids = [rid for rid in targets if rid]
    stats_by_resource = latest_stats_bulk(
        client, ids, _RIGHTSIZING_STAT_KEYS, window_ms=25 * 3_600_000
    )
    # Current configuration, power state and template flag are properties, not
    # stats — one bulk query for the page, same no-N+1 rule.
    props_by_resource = _latest_properties_bulk(client, ids, _RIGHTSIZING_PROPERTY_KEYS)

    results = [
        _rightsizing_row(rid, name, stats_by_resource.get(rid, {}), props_by_resource.get(rid, {}))
        for rid, name in targets.items()
        if rid
    ]
    if resource_id:
        return paginated(results)
    return paginated(results, limit=limit, total=vm_total)


# ---------------------------------------------------------------------------
# rightsizing helpers
# ---------------------------------------------------------------------------

_DIMENSIONS = ("cpu", "mem", "diskspace")

#: VM-published rightsizing keys have NO demand segment (spec audit), and
#: Broadcom's Capacity Analytics metric list names three of them.
_RECOMMENDED_KEYS = {dim: f"OnlineCapacityAnalytics|{dim}|recommendedSize" for dim in _DIMENSIONS}

#: Units per the 8.18.7 VirtualMachine statkey catalogue, and confirmed against
#: vCenter: cpu == cores x host MHz/core, mem == configured-style KB.
_RECOMMENDED_UNITS = {"cpu": "MHz", "memory": "KB", "diskspace": "GB"}

#: The engine's own verdict (8.18.7 catalogue; memory amounts in KB).
_VERDICT_KEYS = {
    "oversized": "summary|oversized",
    "oversized_vcpus": "summary|oversized|vcpus",
    "oversized_memory_kb": "summary|oversized|memory",
    "undersized": "summary|undersized",
    "undersized_vcpus": "summary|undersized|vcpus",
    "undersized_memory_kb": "summary|undersized|memory",
}

_RIGHTSIZING_STAT_KEYS = list(_RECOMMENDED_KEYS.values()) + list(_VERDICT_KEYS.values())

# Property keys, each seen answered on a live 8.18.7 before use (踩坑 #36).
_PROP_NUM_CPU = "config|hardware|numCpu"
_PROP_MEMORY_KB = "config|hardware|memoryKB"
_PROP_CPU_SPEED_HZ = "cpu|speed"  # total Hz across the VM's vCPUs (== vCenter host hz x numCpu)
_PROP_POWER_STATE = "summary|runtime|powerState"  # "Powered On" / "Powered Off"
_PROP_IS_TEMPLATE = "summary|config|isTemplate"  # "true" / "false"
_PROP_PRODUCT_NAME = "summary|config|productName"  # only OVF deployments with a vApp product

_RIGHTSIZING_PROPERTY_KEYS = [
    _PROP_NUM_CPU,
    _PROP_MEMORY_KB,
    _PROP_CPU_SPEED_HZ,
    _PROP_POWER_STATE,
    _PROP_IS_TEMPLATE,
    _PROP_PRODUCT_NAME,
]

#: A recommended MHz within this fraction of one vCPU above a whole core counts
#: as that core. On 8.18.7 the engine publishes whole cores (all 10 live values
#: were exact core multiples, off by < 1e-7 relative: its float32 MHz against the
#: VM's Hz-precise ``cpu|speed``). 1% of a core (~28 MHz at 2.8 GHz) is ~10^5 times
#: that noise, and a real fractional demand still rounds UP (1.5 cores -> 2).
#: Without it, a ratio of 1.0000036 would ceil to 2 and read an oversized VM as
#: right-sized.
CPU_VCPU_ROUNDING_EPSILON = 0.01

#: A memory recommendation within this fraction of the configured size is
#: ``right_sized``. Live 8.18.7: the Aria appliance's recommendation moved
#: 8391702 -> 8391681 -> 8391656 KB within one day against 8388608 configured
#: (+0.04%) — drift, not a resize, yet it read as undersized and actionable. 1%
#: leaves a 25x margin over that drift and a 7x margin under the smallest genuine
#: change in the same estate (vcsa, -7.4%). The engine's own summary|*|memory
#: rounds to whole GiB, so it cannot serve as the threshold (it calls +3 MB
#: "undersized by 1 GiB").
MEMORY_DIRECTION_TOLERANCE = 0.01

_POWERED_ON = "Powered On"
_POWERED_OFF = "Powered Off"


def _latest_properties_bulk(
    client: AriaClient, resource_ids: list[str], property_keys: list[str]
) -> dict[str, dict[str, object]]:
    """Latest property values for many resources in ONE POST.

    ``POST /resources/properties/latest/query`` (in both the 8.6 and 9.1 operation
    indexes). Observed 8.18.7 reply: ``values[].property-contents.property-content[]``
    with ``statKey`` as a plain string, numeric values under ``data`` and string
    values under ``values``; a key the resource does not publish is omitted.
    """
    if not resource_ids:
        return {}
    data = client.post(
        "/resources/properties/latest/query",
        json_data={"resourceIds": list(resource_ids), "propertyKeys": list(property_keys)},
        retries=1,
    )
    result: dict[str, dict[str, object]] = {}
    for entry in data.get("values", []) or []:
        contents = entry.get("property-contents") or entry.get("propertyContents") or {}
        per_resource = result.setdefault(entry.get("resourceId", ""), {})
        for prop in contents.get("property-content") or contents.get("propertyContent") or []:
            key = prop.get("statKey")
            points = prop.get("data") or prop.get("values") or []
            if isinstance(key, str) and points:
                per_resource[key] = points[-1]
    return result


def _as_float(value: object) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_bool(value: object) -> bool | None:
    text = str(value).strip().lower() if value is not None else ""
    return {"true": True, "false": False}.get(text)


def _direction(
    current: float | None, recommended: float | None, tolerance: float = 0.0
) -> str | None:
    """oversized / undersized / right_sized; within ``tolerance`` (relative) is right_sized."""
    if current is None or recommended is None:
        return None
    if current > 0 and abs(recommended - current) <= tolerance * current:
        return "right_sized"
    if recommended < current:
        return "oversized"
    if recommended > current:
        return "undersized"
    return "right_sized"


def _cpu_sizing(props: dict, recommended_mhz: float | None) -> dict:
    """Current vCPUs, MHz per vCPU and the MHz recommendation in vCPUs."""
    num_cpu = _as_float(props.get(_PROP_NUM_CPU))
    speed_hz = _as_float(props.get(_PROP_CPU_SPEED_HZ))
    mhz_per_vcpu = speed_hz / num_cpu / 1_000_000 if num_cpu and speed_hz else None
    recommended_vcpus = None
    if recommended_mhz is not None and mhz_per_vcpu:
        # Round up — a real fraction of a core is demand — but first absorb the
        # float noise between the engine's MHz and cpu|speed (see the constant).
        recommended_vcpus = max(
            1, math.ceil(recommended_mhz / mhz_per_vcpu - CPU_VCPU_ROUNDING_EPSILON)
        )
    current_vcpus = int(num_cpu) if num_cpu is not None else None
    return {
        "current_vcpus": current_vcpus,
        "cpu_mhz_per_vcpu": round(mhz_per_vcpu, 2) if mhz_per_vcpu else None,
        "recommended_vcpus": recommended_vcpus,
        "cpu_direction": _direction(current_vcpus, recommended_vcpus),
    }


def _aria_verdict(stats: dict) -> dict | None:
    raw = {field: _as_float(stats.get(key)) for field, key in _VERDICT_KEYS.items()}
    if all(v is None for v in raw.values()):
        return None
    return {
        field: (None if v is None else v > 0) if field in ("oversized", "undersized") else v
        for field, v in raw.items()
    }


def _engine_direction(verdict: dict | None, unit: str) -> str | None:
    if verdict is None:
        return None
    over, under = verdict.get(f"oversized_{unit}"), verdict.get(f"undersized_{unit}")
    if over is None or under is None:
        return None
    return "oversized" if over > 0 else "undersized" if under > 0 else "right_sized"


def _rightsizing_caveats(row: dict, props_published: bool) -> list[str]:
    caveats: list[str] = []
    power, template = row["power_state"], row["is_template"]
    if power is None or template is None:
        caveats.append(
            "power state / template flag not published for this VM — cannot tell "
            "whether it is a running workload"
        )
    elif template:
        caveats.append("template: not a running workload — size the VMs deployed from it instead")
    elif power == _POWERED_OFF:
        caveats.append(
            "powered off: the recommendation reflects past (or no) demand, not a "
            "running workload — decide whether the VM is needed before sizing it"
        )
    elif power != _POWERED_ON:
        caveats.append(f"power state is '{power}', not {_POWERED_ON}")
    if row["current_vcpus"] is None or row["current_memory_kb"] is None:
        if props_published or row["sizing_status"] == "recommendation":
            caveats.append(
                "current configuration (config|hardware|numCpu / memoryKB / cpu|speed) "
                "not published — direction cannot be stated"
            )
    for dim, unit, key in (("cpu", "vcpus", "vcpus"), ("memory", "memory_kb", "memory")):
        ours, engine = row[f"{dim}_direction"], _engine_direction(row["aria_verdict"], unit)
        if ours and engine and ours != engine:
            caveats.append(
                f"recommendedSize reads {dim} as {ours} but the engine's own "
                f"summary|oversized|{key} / summary|undersized|{key} read {engine} — "
                "confirm in the Aria UI before acting"
            )
    if "oversized" in (row["cpu_direction"], row["memory_direction"]):
        product = f" Aria reports product '{row['product_name']}'." if row["product_name"] else ""
        caveats.append(
            "before reducing, check the vendor minimum size for this guest — vendor "
            "appliances publish floors, and appliances cannot be reliably identified "
            f"from the API.{product}"
        )
    return caveats


def _rightsizing_row(rid: str, name: str, stats: dict, props: dict) -> dict:
    raw = {dim: stats.get(_RECOMMENDED_KEYS[dim]) for dim in _DIMENSIONS}
    # A published 0 means "reclaimable", not "recommend zero" (KB 379521),
    # so it must not reach the caller in a field named recommended_*.
    sized = {d: float(v) for d, v in raw.items() if v is not None and float(v) > 0}
    if sized:
        status = "recommendation"
    elif any(v is not None for v in raw.values()):
        status = "reclaimable"
    else:
        status = "none_published"
    power = props.get(_PROP_POWER_STATE)
    product = props.get(_PROP_PRODUCT_NAME)
    current_memory_kb = _as_float(props.get(_PROP_MEMORY_KB))
    row = {
        "id": sanitize(rid),
        "name": name,
        "recommended_cpu": sized.get("cpu"),
        "recommended_memory": sized.get("mem"),
        "recommended_diskspace": sized.get("diskspace"),
        "recommended_units": dict(_RECOMMENDED_UNITS),
        "sizing_status": status,
        **_cpu_sizing(props, sized.get("cpu")),
        "current_memory_kb": current_memory_kb,
        "memory_direction": _direction(
            current_memory_kb, sized.get("mem"), MEMORY_DIRECTION_TOLERANCE
        ),
        "power_state": sanitize(str(power)) if power is not None else None,
        "is_template": _as_bool(props.get(_PROP_IS_TEMPLATE)),
        "product_name": sanitize(str(product)) if product else None,
        "aria_verdict": _aria_verdict(stats),
    }
    row["actionable"] = bool(
        row["power_state"] == _POWERED_ON
        and row["is_template"] is False
        and {"oversized", "undersized"} & {row["cpu_direction"], row["memory_direction"]}
    )
    row["caveats"] = _rightsizing_caveats(row, props_published=bool(props))
    return row

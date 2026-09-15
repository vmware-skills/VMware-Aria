"""The state a service object reports about itself, beside its badges.

Found 2026-09-15 on Aria Operations 8.18.7 monitoring vCenter 8.0.3: the
``VCENTER_APPLIANCE_HEALTH_SERVICES`` children ``mem`` and ``system`` of the
vCenter app object answered HEALTH GREEN 100 while their ``SERVICE|STATUS``
property was ``orange`` and ``SERVICE|AVAILABILITY`` had been 0 for 72 hours.
The badge scores the alerts attached to the object itself; "vCenter app health
is affected" is raised on the parent, so the failing service's badge stayed
green.

Availability is read as a verdict only for the two values the appliance was
seen to use: 1 (every green service, e.g. ``applmgmt``, 635 of 635 points) and
0 (both orange services). Any other value, or no value, leaves ``available``
unknown — never "available" by exclusion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize

from vmware_aria.connection import AriaApiError

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

STATUS_PROPERTY = "SERVICE|STATUS"
AVAILABILITY_KEY = "SERVICE|AVAILABILITY"

_AVAILABLE = 1.0
_UNAVAILABLE = 0.0

BADGE_NOTE = (
    "The health/risk/efficiency badges score the alerts attached to this object, not the "
    "service's own state: a service whose SERVICE|AVAILABILITY is 0 can keep HEALTH GREEN 100 "
    "because the alert about it is raised on its parent. Read status and available here."
)


def is_service_kind(kind: str | None) -> bool:
    """Whether a resource kind key names a service object (e.g. VCENTER_APPLIANCE_HEALTH_SERVICES)."""
    return bool(kind) and "SERVICE" in str(kind).upper()


def _describe(exc: AriaApiError) -> str:
    return f"HTTP {exc.status_code}" if exc.status_code is not None else "no HTTP response"


def _read_status(client: AriaClient, resource_id: str, errors: list[str]) -> str | None:
    path = f"/resources/{resource_id}/properties"
    try:
        data = client.get(f"/resources/{resource_id}/properties")
    except AriaApiError as exc:
        errors.append(f"GET {path} failed ({_describe(exc)}), so {STATUS_PROPERTY} is unknown.")
        return None
    rows = data.get("property") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        errors.append(f"GET {path} answered without a 'property' list, so {STATUS_PROPERTY} is unknown.")
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("name") == STATUS_PROPERTY:
            text = sanitize(str(row.get("value") or ""))
            if text:
                return text
            errors.append(f"{STATUS_PROPERTY} is present with no value, so status is unknown.")
            return None
    errors.append(f"Aria holds no {STATUS_PROPERTY} property for this object, so status is unknown.")
    return None


def _read_availability(client: AriaClient, resource_id: str, errors: list[str]) -> float | None:
    # Imported here: resources imports this module for get_resource_health.
    from vmware_aria.ops.resources import latest_stats_bulk

    try:
        latest = latest_stats_bulk(client, [resource_id], [AVAILABILITY_KEY])
    except AriaApiError as exc:
        errors.append(f"POST /resources/stats/query failed ({_describe(exc)}), so {AVAILABILITY_KEY} is unknown.")
        return None
    value = latest.get(resource_id, {}).get(AVAILABILITY_KEY)
    if value is None:
        errors.append(f"No {AVAILABILITY_KEY} point in the last hour, so availability is unknown.")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{AVAILABILITY_KEY} answered a non-numeric value, so availability is unknown.")
        return None
    return float(value)


def _available(availability: float | None) -> bool | None:
    if availability == _AVAILABLE:
        return True
    if availability == _UNAVAILABLE:
        return False
    return None


def read_service_state(client: AriaClient, resource_id: str) -> dict[str, Any]:
    """Read a service object's ``SERVICE|STATUS`` and latest ``SERVICE|AVAILABILITY``.

    Returns:
        ``status`` (the property verbatim, e.g. ``green`` / ``orange``, or
        ``None``), ``availability`` (latest point, or ``None``), ``available``
        (``True`` for 1, ``False`` for 0, ``None`` otherwise), ``read_errors``
        (why any of those is unknown; empty when both were read) and ``note``
        (why the badges are not this state).
    """
    errors: list[str] = []
    status = _read_status(client, resource_id, errors)
    availability = _read_availability(client, resource_id, errors)
    return {
        "status": status,
        "availability": availability,
        "available": _available(availability),
        "read_errors": errors,
        "note": BADGE_NOTE,
    }

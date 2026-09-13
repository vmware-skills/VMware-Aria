"""Resource maintenance mode (WRITE) and maintenance schedules (READ).

``PUT /resources/{id}/maintained`` puts a resource in maintenance: Aria stops
alerting on it and collecting its data. Per the VCF Operations 9.1 OpenAPI, a
``duration`` (minutes) or ``end`` (epoch ms) query parameter yields
``MAINTAINED`` and the resource returns to its prior state when the window
expires; with neither it is ``MAINTAINED_MANUAL`` and stays until
``DELETE /resources/{id}/maintained``. Both operations exist in the 8.6 and 9.1
indexes.

Writes read the state before and after and return both. A read that fails is
reported as unknown (``in_maintenance: None``), never as "not in maintenance".

The write paths here have been exercised only against recording clients; no
maintenance change was sent to a real appliance while writing them.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

from vmware_aria.ops._collection import (
    describe_read_failure,
    int_or_none,
    list_or_none,
    read_window,
    text_or_none,
)
from vmware_aria.ops._paging import next_offset, validate_page_args

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient
    from vmware_aria.notify.audit import AuditLogger

_log = logging.getLogger("vmware-aria.ops.maintenance")

MAINTENANCE_STATES = frozenset({"MAINTAINED", "MAINTAINED_MANUAL"})

#: ``resource-status-state.resourceState`` enum, VCF Operations 9.1.0.0 OpenAPI.
KNOWN_RESOURCE_STATES = frozenset({
    "STOPPED", "STARTING", "STARTED", "STOPPING", "UPDATING", "FAILED",
    "MAINTAINED", "MAINTAINED_MANUAL", "REMOVING", "NOT_EXISTING", "NONE", "UNKNOWN",
})

#: One year. The API field is int32 minutes; this is our bound, chosen so a
#: typo of extra digits is refused rather than silencing a resource for decades.
MAX_DURATION_MINUTES = 525_600

#: Epoch milliseconds for 2001-09-09. A smaller "end" is almost certainly
#: seconds, which the appliance would read as a moment in January 1970.
_MIN_EPOCH_MS = 1_000_000_000_000

_ID_HINT = (
    "Run list_resources (or `vmware-aria resource list`) and copy the exact 'id' — "
    "the resource UUID, not its name."
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_resource_id(resource_id: Any) -> str:
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ValueError(f"resource_id must be a non-empty Aria resource UUID. {_ID_HINT}")
    return resource_id.strip()


def maintenance_window_params(
    duration_minutes: int | None = None,
    end_time_ms: int | None = None,
) -> dict[str, int] | None:
    """Validate a maintenance window and return its query parameters.

    Args:
        duration_minutes: Whole minutes, 1 to ``MAX_DURATION_MINUTES``.
        end_time_ms: Epoch milliseconds in the future.

    Returns:
        ``{"duration": n}``, ``{"end": ms}``, or ``None`` for manual
        maintenance (no end).

    Raises:
        ValueError: Both given, or either out of range.
    """
    if duration_minutes is not None and end_time_ms is not None:
        raise ValueError(
            "Pass duration_minutes OR end_time_ms, not both — the suite-api takes one window. "
            "Omit both for manual maintenance that lasts until end_resource_maintenance."
        )
    if duration_minutes is not None:
        if not _is_int(duration_minutes) or not 1 <= duration_minutes <= MAX_DURATION_MINUTES:
            raise ValueError(
                f"Invalid duration_minutes {duration_minutes!r}: pass whole minutes from 1 to "
                f"{MAX_DURATION_MINUTES} (one year)."
            )
        return {"duration": duration_minutes}
    if end_time_ms is not None:
        if not _is_int(end_time_ms) or end_time_ms < _MIN_EPOCH_MS:
            raise ValueError(
                f"Invalid end_time_ms {end_time_ms!r}: pass epoch MILLISECONDS (13 digits, e.g. "
                f"1789300000000). A 10-digit value is seconds — multiply it by 1000."
            )
        now_ms = int(time.time() * 1000)
        if end_time_ms <= now_ms:
            raise ValueError(
                f"end_time_ms {end_time_ms} is not in the future (now is {now_ms}). "
                f"Pass a later time, or duration_minutes instead."
            )
        return {"end": end_time_ms}
    return None


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------


def _verdict(raw: Any, adapter_states: list[dict]) -> tuple[bool | None, str | None, str | None]:
    """``(in_maintenance, maintenance_mode, note)`` from the adapter states."""
    if not isinstance(raw, list):
        return None, None, "The resource answered without a resourceStatusStates list, so its maintenance state is unknown."
    if not adapter_states or len(adapter_states) != len(raw):
        return None, None, "No adapter reported a readable state for this resource, so its maintenance state is unknown."
    states = [s["state"] for s in adapter_states]
    unrecognised = sorted({s for s in states if s not in KNOWN_RESOURCE_STATES})
    if unrecognised:
        return None, None, f"Unrecognised resource state {unrecognised}, so the maintenance state is unknown."
    maintained = [s for s in states if s in MAINTENANCE_STATES]
    if not maintained:
        return False, None, None
    if len(maintained) != len(states):
        return None, None, (
            f"{len(maintained)} of {len(states)} adapters report maintenance ({', '.join(states)}), "
            "so the resource is only partly in maintenance."
        )
    modes = sorted(set(maintained))
    if len(modes) > 1:
        return True, None, f"Adapters report different maintenance modes: {', '.join(modes)}."
    return True, modes[0], None


def read_maintenance_state(client: AriaClient, resource_id: str) -> dict:
    """Read one resource's maintenance state from ``GET /resources/{id}``.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Resource UUID.

    Returns:
        ``resource_id``, ``name``, ``kind``, ``adapter_states`` (each adapter's
        state and collection status), ``in_maintenance`` (True / False /
        ``None`` = unknown), ``maintenance_mode`` (``MAINTAINED`` or
        ``MAINTAINED_MANUAL`` when in maintenance), ``note`` (why unknown) and
        ``read_error`` (``None`` here; set by the write paths when the read
        itself failed).
    """
    rid = _require_resource_id(resource_id)
    data = client.get(f"/resources/{rid}")
    body = data if isinstance(data, dict) else {}
    key = body.get("resourceKey") if isinstance(body.get("resourceKey"), dict) else {}
    raw = body.get("resourceStatusStates")
    adapter_states = [
        {
            "adapter_instance_id": sanitize(str(s.get("adapterInstanceId") or "")),
            "state": sanitize(str(s.get("resourceState") or "")),
            "status": sanitize(str(s.get("resourceStatus") or "")),
        }
        for s in (raw if isinstance(raw, list) else [])
        if isinstance(s, dict)
    ]
    in_maintenance, mode, note = _verdict(raw, adapter_states)
    return {
        "resource_id": sanitize(rid),
        "name": sanitize(str(key.get("name") or ""), max_len=300),
        "kind": sanitize(str(key.get("resourceKindKey") or "")),
        "adapter_states": adapter_states,
        "in_maintenance": in_maintenance,
        "maintenance_mode": mode,
        "note": note,
        "read_error": None,
    }


def _read_or_unknown(client: AriaClient, rid: str) -> dict:
    """The state, or an explicit "unknown" when the read fails — never a guess."""
    try:
        return read_maintenance_state(client, rid)
    except Exception as exc:  # noqa: BLE001 — an unreadable state is reported, not raised
        _log.warning("Could not read maintenance state of resource %s: %s", rid, exc)
        return {
            "resource_id": sanitize(rid),
            "name": None,
            "kind": None,
            "adapter_states": None,
            "in_maintenance": None,
            "maintenance_mode": None,
            "note": "The state could not be read, so it is unknown.",
            "read_error": describe_read_failure(exc),
        }


def _audit(
    audit_logger: AuditLogger | None,
    target_name: str,
    operation: str,
    rid: str,
    parameters: dict,
    before: dict,
    after: dict,
) -> None:
    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation=operation,
            resource=f"resource/{rid}",
            skill="aria",
            parameters=parameters,
            before_state=before,
            after_state=after,
            result="ok",
        )


# ---------------------------------------------------------------------------
# start / end
# ---------------------------------------------------------------------------


def _start_note(mode: str, confirmed: bool | None) -> str | None:
    parts = []
    if confirmed is None:
        parts.append(
            "Aria accepted the request, but the state could not be read afterwards, so the change is "
            "not confirmed — check get_resource before relying on it."
        )
    elif confirmed is False:
        parts.append(
            "Aria accepted the request, but the resource does not report maintenance yet — re-check "
            "with get_resource."
        )
    if mode == "manual":
        parts.append(
            "Manual maintenance (MAINTAINED_MANUAL) has no end: alerting and collection stay stopped "
            "until end_resource_maintenance is run."
        )
    return " ".join(parts) or None


def start_resource_maintenance(
    client: AriaClient,
    resource_id: str,
    duration_minutes: int | None = None,
    end_time_ms: int | None = None,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Put a resource in maintenance (``PUT /resources/{id}/maintained``).

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Resource UUID.
        duration_minutes: Window length in minutes (-> ``MAINTAINED``).
        end_time_ms: Window end, epoch ms (-> ``MAINTAINED``). With neither,
            the resource enters ``MAINTAINED_MANUAL`` and stays there.
        audit_logger: Optional audit logger; the operation is logged if given.
        target_name: Target name for the audit record.

    Returns:
        ``requested`` window, ``before`` / ``after`` state, ``confirmed``
        (True / False / ``None`` when the after-state is unknown) and ``note``.
    """
    rid = _require_resource_id(resource_id)
    params = maintenance_window_params(duration_minutes, end_time_ms)
    before = _read_or_unknown(client, rid)

    client.put(f"/resources/{rid}/maintained", params=params)

    after = _read_or_unknown(client, rid)
    requested = {
        "mode": "manual" if params is None else "timed",
        "duration_minutes": duration_minutes,
        "end_time_ms": end_time_ms,
    }
    confirmed = after["in_maintenance"]
    _audit(audit_logger, target_name, "start_resource_maintenance", rid, {"resource_id": rid, **requested}, before, after)
    return {
        "resource_id": sanitize(rid),
        "action": "start_maintenance",
        "requested": requested,
        "before": before,
        "after": after,
        "confirmed": confirmed,
        "note": _start_note(requested["mode"], confirmed),
    }


def end_resource_maintenance(
    client: AriaClient,
    resource_id: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Take a resource out of maintenance (``DELETE /resources/{id}/maintained``).

    Refuses a resource whose state was read and is not a maintenance state:
    what the DELETE does to, say, a STOPPED resource is undocumented, and this
    tool does not find out. When the before-state is unknown the call proceeds
    (the operator asked for it) and the result says the before-state is unknown.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Resource UUID.
        audit_logger: Optional audit logger; the operation is logged if given.
        target_name: Target name for the audit record.

    Returns:
        ``before`` / ``after`` state, ``confirmed`` (True / False / ``None``)
        and ``note``.

    Raises:
        ValueError: The resource is confirmed not in maintenance.
    """
    rid = _require_resource_id(resource_id)
    before = _read_or_unknown(client, rid)
    if before["in_maintenance"] is False:
        states = ", ".join(s["state"] for s in before["adapter_states"] or []) or "unknown"
        raise ValueError(
            f"Resource {sanitize(rid)} is not in maintenance (state: {states}), so there is nothing to end. "
            "The suite-api does not document what ending maintenance does to a resource that is not in it. "
            "Check get_resource; use start_resource_maintenance to begin a window."
        )

    client.delete(f"/resources/{rid}/maintained")

    after = _read_or_unknown(client, rid)
    confirmed = None if after["in_maintenance"] is None else after["in_maintenance"] is False
    if confirmed is None:
        note = (
            "Aria accepted the request, but the state could not be read afterwards, so the change is not "
            "confirmed — check get_resource."
        )
    elif confirmed is False:
        note = "Aria accepted the request, but the resource still reports maintenance — re-check with get_resource."
    else:
        note = None
    _audit(audit_logger, target_name, "end_resource_maintenance", rid, {"resource_id": rid}, before, after)
    return {
        "resource_id": sanitize(rid),
        "action": "end_maintenance",
        "before": before,
        "after": after,
        "confirmed": confirmed,
        "note": note,
    }


# ---------------------------------------------------------------------------
# list_maintenance_schedules
# ---------------------------------------------------------------------------


def _schedule_row(raw: dict) -> dict:
    schedule = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    return {
        "id": text_or_none(raw.get("id")),
        "name": text_or_none(raw.get("key"), max_len=200),
        "schedule_type": text_or_none(schedule.get("scheduleType")),
        "recurrence": int_or_none(schedule.get("recurrence")),
        "start_hour": int_or_none(schedule.get("hour")),
        "start_minute": int_or_none(schedule.get("minuteOfTheHour")),
        "duration_minutes": int_or_none(schedule.get("duration")),
        "time_zone": text_or_none(schedule.get("timeZone")),
        "start_date": text_or_none(schedule.get("startDate")),
        "expiration_date": text_or_none(schedule.get("expirationDate")),
        "expire_runs": int_or_none(schedule.get("expireRuns")),
        "days_of_week": list_or_none(schedule.get("daysOfTheWeek")),
        "days_of_month": list_or_none(schedule.get("daysOfTheMonth")),
        "weeks_of_month": list_or_none(schedule.get("weeksOfTheMonth")),
        "months": list_or_none(schedule.get("months")),
    }


def list_maintenance_schedules(
    client: AriaClient,
    resource_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List maintenance schedules (``GET /maintenanceschedules``).

    The MaintenanceSchedule model is ``id`` + ``key`` + ``schedule``; it does
    not list the resources a schedule applies to. ``resource_id`` is the
    suite-api's own filter for that question.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Only schedules that apply to this resource UUID.
        limit: Page size, 1-500; out of range is rejected.
        offset: Rows to skip; pass the previous ``next_offset``.

    Returns:
        Paginated envelope of schedule rows plus ``schedules_note``: ``None``
        when the answer was read, otherwise why ``items`` is unknown rather
        than empty.
    """
    validate_page_args(limit, offset)
    rid = resource_id.strip() if isinstance(resource_id, str) and resource_id.strip() else None
    window = read_window(
        client,
        "/maintenanceschedules",
        "schedules",
        limit=limit,
        offset=offset,
        extra_params={"resourceId": [rid]} if rid else None,
    )
    rows = [_schedule_row(r) for r in window.rows]
    if not window.recognised:
        return paginated(
            rows,
            limit=limit,
            total=None,
            next_offset=None,
            schedules_note=(
                "Aria answered without a readable 'schedules' list, so whether any maintenance schedules "
                "exist is unknown — this is not 'no schedules'. Retry, or run 'vmware-aria doctor'."
            ),
        )
    return paginated(
        rows,
        limit=limit,
        total=window.total,
        next_offset=next_offset(len(rows), limit, offset, window.total),
        schedules_note=None,
    )

"""Blast-radius measurements for the gated MCP writes (HLD §7).

Each function reads what a write would change, with endpoints this skill
already calls (``GET /alerts/{id}``, ``GET /alertdefinitions/{id}``,
``GET /reports/{id}``, ``GET /resources/{id}``) — no new API surface, and every
path is in the suite-api indexes under ``tests/eval/spec``.

A 404 is not a measurement problem: the id names nothing, so the connection
layer's teaching error ("list the collection and copy an exact id") is raised
as-is. Any other failed read becomes ``unmeasured``, which previews normally
and refuses ``confirm=True``.

Context that does not change what the write does — the name of the resource an
alert is on, the title of the definition a report came from — is shown when it
can be read and reported as ``None`` with a note when it cannot. It is not
``unmeasured``: an alert on a resource that has since been deleted is exactly
the alert an operator cancels, and refusing it would make cleanup impossible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize

from vmware_aria.connection import AriaApiError
from vmware_aria.ops._collection import iso_utc_or_none
from vmware_aria.ops._ids import require_uuid
from vmware_aria.ops.alerts import _ALERT_ID_HINT, _max_state_severity
from vmware_aria.ops.maintenance import (
    _read_or_unknown,
    maintenance_window_params,
    require_resource_id,
)
from vmware_aria.ops.write_gate import MAX_LISTED

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.gate_measures")

#: Alert status Aria reports for a cancelled alert (8.18.7 capture: one L).
CANCELED_STATUSES = frozenset({"CANCELED", "CANCELLED"})


def _read(fetch: Callable[[], Any], what: str) -> dict | None:
    """The JSON body ``fetch`` returns; ``None`` when unreadable; a 404 is raised.

    Callers pass ``lambda: client.get(f"/literal/{id}")`` rather than a path so
    the literal stays at the call site, where the spec-conformance scan
    (tests/eval/regression/test_aria_spec_conformance.py) can see it.
    """
    try:
        data = fetch()
    except AriaApiError as exc:
        if exc.status_code == 404:
            raise
        _log.warning("Could not read %s for a blast radius: %s", what, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — unreadable is unmeasured, which refuses
        _log.warning("Could not read %s for a blast radius: %s", what, exc)
        return None
    return data if isinstance(data, dict) else None


def _text(body: dict | None, key: str, max_len: int = 300) -> str | None:
    value = (body or {}).get(key)
    return sanitize(str(value), max_len=max_len) if value not in (None, "") else None


def _missing(fields: dict[str, Any]) -> list[str]:
    return [name for name, value in fields.items() if value is None]


def _resource_name(client: AriaClient, resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    try:
        body = client.get(f"/resources/{resource_id}")
    except Exception:  # noqa: BLE001 — context only, never a reason to act or refuse
        return None
    key = body.get("resourceKey") if isinstance(body, dict) else None
    return _text(key, "name") if isinstance(key, dict) else None


# ─── alerts ──────────────────────────────────────────────────────────────────


def measure_alert(client: AriaClient, alert_id: str, effect: str) -> dict:
    """The alert a take-ownership or cancel would change."""
    aid = require_uuid(alert_id, "alert_id", "alert", _ALERT_ID_HINT)
    body = _read(lambda: client.get(f"/alerts/{aid}"), "alert")
    status = _text(body, "status")
    resource_id = _text(body, "resourceId")
    resource_name = _resource_name(client, resource_id)
    identity = {"status": status, "control_state": _text(body, "controlState"),
                "resource_id": resource_id}
    return {
        "alert_id": aid,
        "definition_id": _text(body, "alertDefinitionId"),
        "definition_name": _text(body, "alertDefinitionName"),
        "criticality": _text(body, "alertLevel"),
        **identity,
        "resource_name": resource_name,
        "resource_note": None if resource_name or not resource_id else (
            "The resource's name could not be read (it may have been removed); "
            "resource_id identifies it."
        ),
        "start_time_utc": iso_utc_or_none((body or {}).get("startTimeUTC")),
        "effect": effect,
        "blockers": [],
        "unmeasured": ["alert"] if body is None else _missing(identity),
    }


def is_canceled(radius: dict) -> bool:
    return str(radius.get("status") or "").upper() in CANCELED_STATUSES


# ─── alert definitions ───────────────────────────────────────────────────────


def measure_alert_definition(client: AriaClient, definition_id: str) -> dict:
    """The alert definition a delete would remove."""
    if not isinstance(definition_id, str) or not definition_id.strip():
        raise ValueError(
            "definition_id must be a non-empty alert definition id. Run "
            "list_alert_definitions and copy an exact 'id' value."
        )
    did = definition_id.strip()
    body = _read(lambda: client.get(f"/alertdefinitions/{did}"), "alert definition")
    states = (body or {}).get("states")
    identity = {"name": _text(body, "name")}
    return {
        "definition_id": sanitize(did, max_len=200),
        **identity,
        "description": _text(body, "description", 500),
        "adapter_kind": _text(body, "adapterKindKey"),
        "resource_kind": _text(body, "resourceKindKey"),
        "criticality": (_max_state_severity(states) or None) if isinstance(states, list) else None,
        "state_count": len(states) if isinstance(states, list) else None,
        "effect": (
            "Permanently removes this definition: it stops generating new alerts. Alerts it "
            "already raised are not deleted. Irreversible — set_alert_definition_state("
            "enabled=False) silences it instead."
        ),
        "blockers": [],
        "unmeasured": ["definition"] if body is None else _missing(identity),
    }


# ─── reports ─────────────────────────────────────────────────────────────────


def measure_report(client: AriaClient, report_id: str) -> dict:
    """The generated report a delete would remove."""
    if not isinstance(report_id, str) or not report_id.strip():
        raise ValueError(
            "report_id must be a non-empty generated-report UUID. Run list_reports and copy "
            "an exact 'id' — the id of a report run, not the report definition id."
        )
    rid = report_id.strip()
    body = _read(lambda: client.get(f"/reports/{rid}"), "report")
    definition_id = _text(body, "reportDefinitionId")
    title = None
    if definition_id:
        from vmware_aria.ops.reports import _definition_title

        title = _definition_title(client, definition_id)
    identity = {"status": _text(body, "status"), "definition_id": definition_id}
    return {
        "report_id": sanitize(rid, max_len=200),
        "name": title,
        **identity,
        "completion_time": _text(body, "completionTime", 100),
        "owner": _text(body, "owner", 200),
        "effect": (
            "Permanently removes this generated report and its output. The report "
            "definition and any schedules remain; generate_report recreates it."
        ),
        "blockers": [],
        "unmeasured": ["report"] if body is None else _missing(identity),
    }


# ─── maintenance ─────────────────────────────────────────────────────────────


def _describe_window(window: dict[str, int] | None) -> str:
    if window is None:
        return "with NO end (MAINTAINED_MANUAL — stays until end_resource_maintenance)"
    if "duration" in window:
        return f"for {window['duration']} minutes"
    return f"until {window['end']} (epoch ms)"


def _state_radius(state: dict) -> dict:
    adapters = state.get("adapter_states")
    return {
        "resource_id": state["resource_id"],
        "name": state.get("name") or None,
        "kind": state.get("kind") or None,
        "adapter_count": len(adapters) if isinstance(adapters, list) else None,
        "adapter_states": (adapters or [])[:MAX_LISTED],
        "in_maintenance": state.get("in_maintenance"),
        "maintenance_mode": state.get("maintenance_mode"),
        "state_note": state.get("note"),
    }


def measure_maintenance_start(
    client: AriaClient,
    resource_id: str,
    duration_minutes: int | None,
    end_time_ms: int | None,
) -> dict:
    """What starting maintenance on ``resource_id`` would change."""
    rid = require_resource_id(resource_id)
    window = maintenance_window_params(duration_minutes, end_time_ms)
    state = _state_radius(_read_or_unknown(client, rid))
    blockers = []
    if state["in_maintenance"] is True:
        blockers.append(
            f"The resource is already in maintenance ({state['maintenance_mode'] or 'mode unknown'}). "
            "Starting again would replace that window, possibly someone else's. End it first "
            "with end_resource_maintenance, or leave it as it is."
        )
    return {
        **state,
        "requested": {
            "mode": "manual" if window is None else "timed",
            "duration_minutes": duration_minutes,
            "end_time_ms": end_time_ms,
        },
        "effect": f"Alerting and data collection stop for this resource {_describe_window(window)}.",
        "blockers": blockers,
        "unmeasured": ["maintenance_state"] if state["in_maintenance"] is None else [],
    }


def measure_maintenance_end(client: AriaClient, resource_id: str) -> dict:
    """What ending maintenance on ``resource_id`` would change."""
    rid = require_resource_id(resource_id)
    state = _state_radius(_read_or_unknown(client, rid))
    blockers = []
    if state["in_maintenance"] is False:
        states = ", ".join(s["state"] for s in state["adapter_states"]) or "unknown"
        blockers.append(
            f"Resource {rid} is not in maintenance (state: {states}), so there is nothing to "
            "end. The suite-api does not document what ending maintenance does to a resource "
            "that is not in it. Check get_resource; use start_resource_maintenance to begin a "
            "window."
        )
    return {
        **state,
        "effect": "Alerting and data collection resume for this resource.",
        "blockers": blockers,
        "unmeasured": ["maintenance_state"] if state["in_maintenance"] is None else [],
    }


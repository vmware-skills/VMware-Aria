"""Alert recommendations (READ): alert -> alert definition -> recommendation text.

The Alert model carries no recommendations. The definition does, per state,
as ``recommendationPriorityMap`` — {recommendation id: priority}, lower first —
and the text is a separate Recommendation object, fetched here in one batched
``GET /recommendations?id=..&id=..`` (confirmed on 8.18.7: seven ids, seven rows).

Which state applies is not an exact severity match. On the 8.18.7 appliance
three of the four definitions behind its active alerts have one state with
severity ``AUTO`` while the alerts are IMMEDIATE or CRITICAL. So: the state whose
severity equals the alert's level; otherwise the only state; otherwise every
state merged, and the result says so.

``status`` keeps "none defined" and "could not read" apart:

* ``found`` — every recommendation's text resolved.
* ``partial`` — ids and priorities are known, some text is not (``note`` says why).
* ``none_defined`` — the definition was read and defines none (``[]``).
* ``unknown`` — the definition could not be read or understood (``None``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize

from vmware_aria.ops._collection import describe_read_failure, text_or_none
from vmware_aria.ops.alert_notes import require_alert_id
from vmware_aria.ops.alerts import _LOOKUP_FAILED, _LOOKUP_NOT_FOUND, _fetch_by_ids, _lookup_outcome, _unresolved_note

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

#: Ids per ``/recommendations`` request. Ids look like
#: ``Recommendation-df-vCenter Operations Adapter-ResourceIsDownRecommendation``
#: (~70 characters, spaces URL-encoded), so the same bound as definition ids.
_RECOMMENDATION_ID_CHUNK = 50

DESCRIPTION_MAX_LEN = 1000


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _state_maps(definition: Any) -> list[tuple[str, dict[str, int]]] | None:
    """``[(severity, {id: priority})]`` per state, or ``None`` if unreadable."""
    states = definition.get("states") if isinstance(definition, dict) else None
    if not isinstance(states, list) or not states or not all(isinstance(s, dict) for s in states):
        return None
    out = []
    for state in states:
        raw = state.get("recommendationPriorityMap")
        if raw is None:
            priorities: dict[str, int] = {}
        elif isinstance(raw, dict) and all(isinstance(k, str) and _is_int(v) for k, v in raw.items()):
            priorities = dict(raw)
        else:
            return None
        out.append((str(state.get("severity") or ""), priorities))
    return out


def _choose_state(
    maps: list[tuple[str, dict[str, int]]], criticality: str
) -> tuple[str | None, dict[str, int], str | None]:
    """``(state severity or None when merged, priorities, note)``."""
    matching = [m for m in maps if criticality and m[0].upper() == criticality.upper()]
    if len(matching) == 1:
        return sanitize(matching[0][0]), matching[0][1], None
    if len(maps) == 1:
        return sanitize(maps[0][0]), maps[0][1], None
    merged: dict[str, int] = {}
    for _, priorities in maps:
        for rec_id, priority in priorities.items():
            merged[rec_id] = min(priority, merged.get(rec_id, priority))
    note = (
        f"No single state of the definition matches the alert's criticality {criticality or '(none)'}; "
        f"recommendations from all {len(maps)} states are merged, each at its highest priority."
    )
    return None, merged, note


def _action(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "method": text_or_none(raw.get("targetMethod")),
        "action_adapter_kind": text_or_none(raw.get("actionAdapterKindId")),
        "target_adapter_kind": text_or_none(raw.get("targetAdapterKindId")),
        "target_resource_kind": text_or_none(raw.get("targetResourceKindId")),
    }


def get_alert_recommendations(client: AriaClient, alert_id: str) -> dict:
    """Prioritized recommendations for one alert.

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: Alert UUID.

    Returns:
        ``alert_id``, ``alert_name``, ``criticality``, ``alert_definition_id``,
        ``alert_definition_name``, ``state_severity`` (the state used; ``None``
        when states were merged or none was read), ``status``,
        ``recommendations`` (rows of id, priority, description, action, lookup
        — ``[]`` when none are defined, ``None`` when unknown) and ``note``.

    Raises:
        AriaApiError: The alert itself could not be read.
    """
    aid = require_alert_id(alert_id)
    data = client.get(f"/alerts/{aid}")
    alert = data if isinstance(data, dict) else {}
    definition_id = alert.get("alertDefinitionId")
    criticality = sanitize(str(alert.get("alertLevel") or ""))
    result: dict[str, Any] = {
        "alert_id": sanitize(aid),
        "alert_name": text_or_none(alert.get("alertDefinitionName"), max_len=300),
        "criticality": criticality,
        "alert_definition_id": text_or_none(definition_id, max_len=300),
        "alert_definition_name": text_or_none(alert.get("alertDefinitionName"), max_len=300),
        "state_severity": None,
        "status": "unknown",
        "recommendations": None,
        "note": None,
    }
    if not isinstance(definition_id, str) or not definition_id:
        return {**result, "note": "The alert carries no alertDefinitionId, so its recommendations are unknown — not none."}

    try:
        definition = client.get(f"/alertdefinitions/{definition_id}")
    except Exception as exc:  # noqa: BLE001 — an unreadable definition is an unknown answer, not "none"
        return {
            **result,
            "note": (
                f"The alert definition could not be read ({describe_read_failure(exc)}), so the "
                "recommendations are unknown — not none. Retry, or check list_alert_definitions."
            ),
        }
    maps = _state_maps(definition)
    if maps is None:
        return {
            **result,
            "note": (
                "The alert definition answered without readable states / recommendationPriorityMap, so the "
                "recommendations are unknown — not none."
            ),
        }

    severity, priorities, state_note = _choose_state(maps, criticality)
    if not priorities:
        return {**result, "state_severity": severity, "status": "none_defined", "recommendations": [], "note": state_note}

    ids = sorted(priorities, key=lambda k: (priorities[k], k))
    found, failed = _fetch_by_ids(
        lambda batch: client.get("/recommendations", params={"id": batch}),
        ids,
        container="recommendations",
        id_key="id",
        chunk=_RECOMMENDATION_ID_CHUNK,
    )
    rows = []
    for rec_id in ids:
        row = found.get(rec_id)
        rows.append({
            "id": sanitize(rec_id, max_len=300),
            "priority": priorities[rec_id],
            "description": text_or_none(row.get("description"), max_len=DESCRIPTION_MAX_LEN) if row else None,
            "action": _action(row.get("action")) if row else None,
            "lookup": _lookup_outcome(rec_id, found, failed),
        })
    n_failed = sum(1 for r in rows if r["lookup"] == _LOOKUP_FAILED)
    n_missing = sum(1 for r in rows if r["lookup"] == _LOOKUP_NOT_FOUND)
    note = _unresolved_note(
        "recommendation texts",
        n_failed,
        n_missing,
        "Their ids and priorities are listed; their text is unknown, not blank.",
        extra=(state_note or "",),
    )
    return {
        **result,
        "state_severity": severity,
        "status": "partial" if n_failed or n_missing else "found",
        "recommendations": rows,
        "note": note,
    }

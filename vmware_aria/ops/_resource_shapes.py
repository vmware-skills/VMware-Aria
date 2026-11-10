"""Project ResourceDto badges and collection state without extra remote reads."""

from __future__ import annotations


from typing import TYPE_CHECKING

from vmware_policy import sanitize


if TYPE_CHECKING:
    pass

from vmware_aria.ops._metric_shapes import (
    VALID_INTERVAL_TYPES as VALID_INTERVAL_TYPES,
    VALID_ROLLUP_TYPES as VALID_ROLLUP_TYPES,
    _echoed_resolution as _echoed_resolution,
    _interval_report as _interval_report,
    _parse_stat_series as _parse_stat_series,
    normalize_interval as normalize_interval,
)


def _badges_by_type(dto: dict) -> dict[str, dict]:
    """Index the ResourceDto badges[] array ({type, color, score}) by type.

    The wire field is ``badges`` (array, type enum HEALTH/RISK/EFFICIENCY) —
    there is no singular ``badge`` object (2026-06-08 spec audit).
    """
    return {b.get("type", ""): b for b in dto.get("badges") or []}


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

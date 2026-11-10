"""Alert severity and nested symptom response interpretation."""

from __future__ import annotations


from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize


if TYPE_CHECKING:
    pass


_SEVERITY_RANK = {
    "NONE": 0,
    "AUTO": 1,
    "INFORMATION": 2,
    "WARNING": 3,
    "IMMEDIATE": 4,
    "CRITICAL": 5,
}


def _max_state_severity(states: list[dict]) -> str:
    """Return the highest severity across AlertDefinition states[].

    Falls back to the first state's severity when values are unranked,
    and "" when there are no states.
    """
    severities = [str(s.get("severity", "")) for s in states if s.get("severity")]
    if not severities:
        return ""
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s.upper(), -1))


_LOOKUP_NOT_NEEDED = "not_needed"


_SYMPTOM_CONTAINER_KEYS = ("contributingSymptoms", "symptoms", "symptom", "result")


_SYMPTOM_LEAF_KEYS = (
    "symptomId",
    "symptomSetId",
    "symptomDefinitionsIds",
    "symptomDefinitionId",
    "alertConditions",
    "id",
    "name",
    "message",
    "severity",
    "symptomCriticality",
)


_MAX_SYMPTOM_DEPTH = 8


def _walk_symptoms(node: Any, depth: int = 0) -> tuple[list[dict], bool]:
    """Return ``(leaf symptom dicts, recognized)`` for one node of the response.

    ``recognized`` is what separates "walked to the bottom and there were none"
    from "could not follow this body at all". A response we understood but that
    held nothing is a confirmed none; anything else must not be reported as one.
    """
    if depth > _MAX_SYMPTOM_DEPTH:
        return [], False
    if isinstance(node, list):
        rows: list[dict] = []
        recognized = True
        for entry in node:
            sub, ok = _walk_symptoms(entry, depth + 1)
            rows.extend(sub)
            recognized = recognized and ok
        return rows, recognized
    if not isinstance(node, dict):
        return [], False

    containers = [k for k in _SYMPTOM_CONTAINER_KEYS if isinstance(node.get(k), (list, dict))]
    if containers:
        rows = []
        recognized = True
        for key in containers:
            sub, ok = _walk_symptoms(node[key], depth + 1)
            rows.extend(sub)
            recognized = recognized and ok
        return rows, recognized

    # No nested level below here. Inside a container, a dict carrying any
    # symptom field is the symptom itself; a per-alert entry with no symptom
    # container simply had nothing triggered. The top-level body is never a
    # leaf — a bare dict with no container key there is a shape we do not know.
    if depth and any(k in node for k in _SYMPTOM_LEAF_KEYS):
        return [node], True
    return [], not node or (depth > 0 and "alertId" in node)


def _own_name(s: dict) -> str:
    """The name a symptom instance carries itself, or ``""``."""
    return str(s.get("name") or s.get("message") or "")


def _own_severity(s: dict) -> str:
    """The severity a symptom instance carries itself, or ``""``."""
    conditions = [c for c in (s.get("alertConditions") or []) if isinstance(c, dict)]
    return str(s.get("severity") or s.get("symptomCriticality") or _max_state_severity(conditions))


def _definition_id(s: dict) -> str:
    definition_ids = s.get("symptomDefinitionsIds") or []
    return str(
        s.get("symptomDefinitionId")
        or (definition_ids[0] if isinstance(definition_ids, list) and definition_ids else "")
    )


def _definition_severity(definition: dict) -> str:
    """Severity of a SymptomDefinition: ``state.severity``, or the max of ``states[]``.

    8.18.7 answers with a single ``state`` object (``{"severity": "IMMEDIATE",
    "condition": {...}}``) on all eleven definitions its ten alerts reference;
    ``states[]`` is kept for the plural form other versions use on definitions.
    """
    state = definition.get("state")
    if isinstance(state, dict) and state.get("severity"):
        return str(state["severity"])
    states = [x for x in (definition.get("states") or []) if isinstance(x, dict)]
    return _max_state_severity(states)


def _summarize_symptom(s: dict, definition: dict | None = None, lookup: str = _LOOKUP_NOT_NEEDED) -> dict:
    """Project one triggered symptom onto summary fields.

    Reads both wire vocabularies. The 9.1 leaf carries none of severity /
    message / symptomDefinitionId: the severity sits on ``alertConditions[]``
    and the definition ids are the plural ``symptomDefinitionsIds``, so mapping
    only the older names left every field blank even once the nesting was
    followed. ``condition`` is the actual reason the symptom fired, which is
    the whole point of asking for symptoms.

    On 8.18.7 even that is not enough: ``alertConditions`` is empty, so the
    instance carries ids and nothing else, and all eight symptoms on four live
    alerts came back with blank name and severity. Those two fields then come
    from the symptom ``definition`` (fetched in one batch by the caller), and
    ``definition_lookup`` records whether that happened — a blank name next to
    ``"failed"`` is unknown, not nameless.
    """
    conditions = [c for c in (s.get("alertConditions") or []) if isinstance(c, dict)]
    first_condition = conditions[0].get("condition") if conditions else None
    if not isinstance(first_condition, dict):
        first_condition = {}
    definition = definition or {}

    severity = _own_severity(s) or _definition_severity(definition)
    name = _own_name(s) or definition.get("name") or first_condition.get("key") or ""
    definition_id = _definition_id(s)
    condition = " ".join(str(first_condition.get(k) or "") for k in ("key", "operator", "settingValue")).strip()

    return {
        "id": sanitize(str(s.get("id") or s.get("symptomId") or "")),
        "name": sanitize(str(name), max_len=300),
        "severity": sanitize(str(severity)),
        "symptom_definition_id": sanitize(str(definition_id)),
        "resource_id": sanitize(str(s.get("resourceId") or "")),
        "condition": sanitize(condition, max_len=300),
        "definition_lookup": lookup,
    }

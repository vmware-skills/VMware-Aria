"""Aria Operations alert management: list, get, acknowledge, cancel, list definitions.

Write operations (acknowledge, cancel) are audit-logged.
All API responses pass through sanitize() to strip control characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

from vmware_aria.ops._paging import (
    MAX_LIMIT,
    CollectionTotal,
    _MAX_TOTAL,
    iter_collection,
    next_offset,
    paginate,
    validate_page_args,
)

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient
    from vmware_aria.notify.audit import AuditLogger

_log = logging.getLogger("vmware-aria.ops.alerts")

_VALID_CRITICALITIES = {"INFORMATION", "WARNING", "IMMEDIATE", "CRITICAL"}

# Severity ranking for picking the max across AlertDefinition states[]
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


# ---------------------------------------------------------------------------
# list_alerts
# ---------------------------------------------------------------------------

#: Server-side page requested from POST /alerts/query. The appliance may return
#: fewer; nothing here depends on it returning exactly this many.
_ALERT_PAGE_SIZE = MAX_LIMIT


def _walk_alert_pages(
    client: AriaClient, query: dict[str, Any], wanted: int
) -> tuple[list[dict], int | None]:
    """Collect up to ``wanted`` alerts from POST /alerts/query, and the total.

    Walks 0-based ``page``/``pageSize`` from the beginning, which is the same
    pair every other suite-api collection in this skill pages by, and the pair
    the alerts endpoints document.

    It stops on any of: enough rows, a short page, an exhausted
    ``pageInfo.totalCount``, the module's safety cap — or **a page that adds no
    alert this walk has not already seen**. That last one is not defensive
    padding. This endpoint has a recorded habit of accepting query parameters
    and ignoring them: ``status`` and ``criticality`` were silently dropped
    here until the 2026-06-08 report moved them into the request body. If
    ``page`` is dropped the same way, every request returns page zero, and a
    walk that trusted the page number would collect the same alerts for ever.
    Tracking ids makes that case terminate with a short answer instead — short
    being visible in ``returned``, where a duplicate-filled one would not be.

    Returns:
        The alerts collected, and ``pageInfo.totalCount`` if the appliance
        reported one. ``None`` when it did not: this endpoint is not documented
        here as carrying ``pageInfo``, and a total inferred from what we
        happened to fetch would read as fact (踩坑 #36).
    """
    collected: list[dict] = []
    seen: set[str] = set()
    total_count: int | None = None
    page = 0
    while len(collected) < wanted:
        # Pure query endpoint — idempotent, safe to retry transient gateways.
        data = client.post(
            "/alerts/query",
            json_data=query,
            params={"page": page, "pageSize": _ALERT_PAGE_SIZE},
            retries=1,
        )
        items = data.get("alerts", []) or []
        reported = (data.get("pageInfo") or {}).get("totalCount")
        if isinstance(reported, int):
            total_count = reported

        fresh: list[dict] = []
        for alert in items:
            key = alert.get("alertId")
            if key is not None:
                if key in seen:
                    continue
                seen.add(str(key))
            fresh.append(alert)
        if not fresh:
            break
        collected.extend(fresh)

        if len(items) < _ALERT_PAGE_SIZE:
            break
        if total_count is not None and len(collected) >= total_count:
            break
        if len(collected) >= _MAX_TOTAL:
            _log.warning(
                "list_alerts hit the %d-alert safety cap; narrow with "
                "criticality or resource_id.",
                _MAX_TOTAL,
            )
            break
        page += 1
    return collected, total_count


def list_alerts(
    client: AriaClient,
    active_only: bool = True,
    criticality: str | None = None,
    resource_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List alerts from Aria Operations.

    Args:
        client: Authenticated Aria Operations API client.
        active_only: Return only active (non-cancelled) alerts.
        criticality: Filter by criticality: INFORMATION, WARNING, IMMEDIATE, CRITICAL.
        resource_id: Scope alerts to a specific resource UUID.
        limit: Maximum number of alerts to return (1–500). A page size, not a
            ceiling: out-of-range values are rejected, not clamped.
        offset: Alerts to skip before collecting this page. 0 or more; pass the
            previous response's ``next_offset`` to walk the whole set.

    Returns:
        Result envelope with alert summary dicts under ``items``. ``total``
        carries ``pageInfo.totalCount`` where the appliance reports one and is
        ``None`` where it does not — an invented total reads as fact.

        The envelope carries ``next_offset``: pass it back as ``offset`` for
        the next page and stop when it is ``None``. Do not loop on
        ``truncated`` — that says this page is not the whole set, which stays
        true on the last page of a walk.

    Until 2026-08-30 this fetched a single page and clamped ``limit`` to 500
    with ``max(1, min(limit, 500))``, so on an estate with 2,783 alerts the
    other 2,283 could not be reached under any combination of parameters —
    while the envelope's hint advised raising the limit, which the clamp
    silently undid.

    Paging walks server pages from page 0 and skips ``offset`` rows, rather
    than computing ``page = offset // pageSize``. The appliance chooses its own
    page size and need not honour the one we ask for, and if it returns 100
    where we asked 500 then page arithmetic lands the window somewhere else
    entirely — silently, because every row in it is a real alert. Walking costs
    one request per 500 rows skipped and cannot be wrong about which rows those
    are.
    """
    if criticality and criticality.upper() not in _VALID_CRITICALITIES:
        raise ValueError(
            f"Invalid criticality '{criticality}'. "
            f"Must be one of: {', '.join(sorted(_VALID_CRITICALITIES))}. "
            f"Pass one of those values to --criticality (case-insensitive), or "
            f"omit it to list alerts at every criticality."
        )

    validate_page_args(limit, offset)

    # GET /alerts only supports id/resourceId/page/pageSize — status and
    # criticality params were silently ignored (2026-06-08 user report).
    # Server-side filtering goes through POST /alerts/query (AlertQuery).
    query: dict[str, Any] = {"compositeOperator": "AND"}
    if active_only:
        query["activeOnly"] = True
    if criticality:
        query["alertCriticality"] = [criticality.upper()]
    if resource_id:
        query["resource-query"] = {"resourceId": [resource_id]}

    fetched, total_count = _walk_alert_pages(client, query, offset + limit)
    items = paginate(fetched, limit, offset)

    # Alert model fields (2026-06-08 spec audit): criticality is `alertLevel`,
    # the display name is `alertDefinitionName`. There is no alertName,
    # criticality, resourceName, or info field — resolve the resource name
    # via get_resource(resourceId) when needed.
    rows = [
        {
            "id": sanitize(a.get("alertId", "")),
            "name": sanitize(a.get("alertDefinitionName", ""), max_len=300),
            "criticality": sanitize(a.get("alertLevel", "")),
            "status": sanitize(a.get("status", "")),
            "alert_impact": sanitize(a.get("alertImpact", "")),
            "resource_id": sanitize(a.get("resourceId", "")),
            "start_time_ms": a.get("startTimeUTC", None),
            "update_time_ms": a.get("updateTimeUTC", None),
            "alert_definition_id": sanitize(a.get("alertDefinitionId", "")),
            "alert_definition_name": sanitize(a.get("alertDefinitionName", ""), max_len=300),
            "control_state": sanitize(a.get("controlState", "")),
        }
        for a in items
    ]
    return paginated(
        rows,
        limit=limit,
        total=total_count,
        next_offset=next_offset(len(rows), limit, offset, total_count),
    )


# ---------------------------------------------------------------------------
# get_alert
# ---------------------------------------------------------------------------


# Keys that hold a *nested level* of a contributingsymptoms body rather than a
# symptom itself, and the keys that identify a leaf symptom object. The 9.1
# body is `{"contributingSymptoms": [ {"alertId": ..., "contributingSymptoms":
# {"contributingSymptoms": [ <symptom>, ... ]}} ]}` — three levels, and the
# same key name repeated at each. Unwrapping one level (what this did before)
# yields the per-alert wrappers, whose every symptom field is absent, so all
# five CRITICAL alerts on a real 9.1 appliance came back with symptoms that
# said nothing. Recursing instead of hard-coding three hops keeps the flat
# `{"symptoms": [...]}` body older appliances send working too.
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
# Guards against a self-referential body walking forever. The real shape is
# three deep; anything past this is not a shape we claim to understand.
_MAX_SYMPTOM_DEPTH = 8

#: Attached to get_alert when the symptoms body could not be walked. Without it
#: an unparsed body and a genuinely quiet alert are the same answer — `[]` —
#: and an agent reads "nothing is wrong" off a tool that simply failed to read
#: the response (形态 #1).
_UNPARSED_SYMPTOMS_NOTE = (
    "contributing-symptoms response shape unrecognized — the empty symptom "
    "list is unconfirmed, not a confirmed 'this alert has no symptoms'. The "
    "appliance answered in a form this tool did not recognise; treat it as "
    "unknown and inspect the alert in the Operations UI."
)

#: Attached when the symptoms call itself failed. Same hazard, different cause:
#: a 500 or a timeout also lands as an empty list.
_SYMPTOMS_UNAVAILABLE_NOTE = (
    "contributing symptoms could not be retrieved — the empty symptom list "
    "reflects a failed lookup, not an alert without symptoms. Retry, or "
    "inspect the alert in the Operations UI."
)


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


def _summarize_symptom(s: dict) -> dict:
    """Project one triggered symptom onto summary fields.

    Reads both wire vocabularies. The 9.1 leaf carries none of severity /
    message / symptomDefinitionId: the severity sits on ``alertConditions[]``
    and the definition ids are the plural ``symptomDefinitionsIds``, so mapping
    only the older names left every field blank even once the nesting was
    followed. ``condition`` is the actual reason the symptom fired, which is
    the whole point of asking for symptoms.
    """
    conditions = [c for c in (s.get("alertConditions") or []) if isinstance(c, dict)]
    definition_ids = s.get("symptomDefinitionsIds") or []
    first_condition = conditions[0].get("condition") if conditions else None
    if not isinstance(first_condition, dict):
        first_condition = {}

    severity = s.get("severity") or s.get("symptomCriticality") or _max_state_severity(conditions)
    name = s.get("name") or s.get("message") or first_condition.get("key") or ""
    definition_id = s.get("symptomDefinitionId") or (
        definition_ids[0] if isinstance(definition_ids, list) and definition_ids else ""
    )
    condition = " ".join(
        str(first_condition.get(k) or "")
        for k in ("key", "operator", "settingValue")
    ).strip()

    return {
        "id": sanitize(str(s.get("id") or s.get("symptomId") or "")),
        "name": sanitize(str(name), max_len=300),
        "severity": sanitize(str(severity)),
        "symptom_definition_id": sanitize(str(definition_id)),
        "resource_id": sanitize(str(s.get("resourceId") or "")),
        "condition": sanitize(condition, max_len=300),
    }


def _get_contributing_symptoms(client: AriaClient, alert_id: str) -> tuple[list[dict], str]:
    """Fetch triggered symptoms via GET /alerts/contributingsymptoms?id=<alertId>.

    The Alert model has no alertSymptomList — triggered symptoms come from this
    separate endpoint (2026-06-08 spec audit). Returns ``(symptoms, note)``,
    where a non-empty note says the empty list is unconfirmed. Failures still
    degrade to an empty list (logged) so a symptoms hiccup never breaks
    get_alert, but they no longer pass silently as "no symptoms".
    """
    try:
        data = client.get("/alerts/contributingsymptoms", params={"id": alert_id})
    except Exception as exc:
        _log.warning("Could not fetch contributing symptoms for alert %s: %s", alert_id, exc)
        return [], _SYMPTOMS_UNAVAILABLE_NOTE

    rows, recognized = _walk_symptoms(data)
    if not recognized:
        _log.warning("Unrecognized contributing-symptoms shape for alert %s", alert_id)
    return [_summarize_symptom(s) for s in rows], "" if recognized else _UNPARSED_SYMPTOMS_NOTE


def get_alert(client: AriaClient, alert_id: str) -> dict:
    """Get full details for a specific alert.

    Triggered symptoms are fetched from GET /alerts/contributingsymptoms.
    Recommendations are not included — they hang off the alert definition,
    not the alert. The Alert model has no resourceName field; resolve the
    name via get_resource(resource_id) when needed.

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: The alert UUID.

    Returns:
        Dict with alert details and contributing symptom list. A
        ``symptoms_note`` key is present only when the symptom list is empty
        for a reason other than the alert having no symptoms — an unrecognised
        response shape, or a lookup that failed.
    """
    if not alert_id:
        raise ValueError(
            "alert_id must be a non-empty Aria alert UUID. Run list_alerts to see "
            "open alerts and copy an exact 'id' — note that the alert UUID is not "
            "the affected resource UUID."
        )

    data = client.get(f"/alerts/{alert_id}")
    symptoms, symptoms_note = _get_contributing_symptoms(client, alert_id)
    result = {
        "id": sanitize(data.get("alertId", "")),
        "name": sanitize(data.get("alertDefinitionName", ""), max_len=300),
        "criticality": sanitize(data.get("alertLevel", "")),
        "status": sanitize(data.get("status", "")),
        "alert_impact": sanitize(data.get("alertImpact", "")),
        "resource_id": sanitize(data.get("resourceId", "")),
        "start_time_ms": data.get("startTimeUTC", None),
        "update_time_ms": data.get("updateTimeUTC", None),
        "cancel_time_ms": data.get("cancelTimeUTC", None),
        "control_state": sanitize(data.get("controlState", "")),
        "alert_definition_id": sanitize(data.get("alertDefinitionId", "")),
        "alert_definition_name": sanitize(data.get("alertDefinitionName", ""), max_len=300),
        "symptoms": symptoms,
    }
    if symptoms_note:
        result["symptoms_note"] = symptoms_note
    return result


# ---------------------------------------------------------------------------
# acknowledge_alert
# ---------------------------------------------------------------------------


def acknowledge_alert(
    client: AriaClient,
    alert_id: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Acknowledge an active alert by taking ownership of it.

    The suite-api has no dedicated "acknowledge" operation (2026-06-08 user
    report — POST /alerts/{id}/acknowledge does not exist). The closest
    semantic equivalent is POST /alerts?action=takeownership, which assigns
    the alert to the calling user (control state ASSIGNED).

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: The alert UUID to acknowledge (take ownership of).
        audit_logger: Optional audit logger; operation is logged if provided.
        target_name: Target name for audit log record.

    Returns:
        Dict confirming the operation with alert id and new control_state.
    """
    if not alert_id:
        raise ValueError(
            "alert_id must be a non-empty Aria alert UUID. Run list_alerts to see "
            "open alerts and copy an exact 'id' — note that the alert UUID is not "
            "the affected resource UUID."
        )

    # Capture before state
    before = {}
    try:
        before = get_alert(client, alert_id)
    except Exception as exc:
        _log.warning("Could not retrieve before-state for alert %s: %s", alert_id, exc)

    client.post("/alerts", json_data={"uuids": [alert_id]}, params={"action": "takeownership"})

    result = {
        "alert_id": alert_id,
        "action": "takeownership",
        "control_state": "ASSIGNED",
    }

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="acknowledge",
            resource=f"alert/{alert_id}",
            skill="aria",
            parameters={"alert_id": alert_id},
            before_state=before,
            after_state=result,
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# cancel_alert
# ---------------------------------------------------------------------------


def cancel_alert(
    client: AriaClient,
    alert_id: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Cancel (dismiss) an active alert.

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: The alert UUID to cancel.
        audit_logger: Optional audit logger; operation is logged if provided.
        target_name: Target name for audit log record.

    Returns:
        Dict confirming the cancellation.
    """
    if not alert_id:
        raise ValueError(
            "alert_id must be a non-empty Aria alert UUID. Run list_alerts to see "
            "open alerts and copy an exact 'id' — note that the alert UUID is not "
            "the affected resource UUID."
        )

    before = {}
    try:
        before = get_alert(client, alert_id)
    except Exception as exc:
        _log.warning("Could not retrieve before-state for alert %s: %s", alert_id, exc)

    # DELETE /alerts/{id} does not exist (2026-06-08 user report). Cancelling
    # goes through POST /alerts?action=cancel with a uuids body.
    client.post("/alerts", json_data={"uuids": [alert_id]}, params={"action": "cancel"})

    result = {
        "alert_id": alert_id,
        "action": "cancelled",
        "status": "CANCELLED",
    }

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="cancel",
            resource=f"alert/{alert_id}",
            skill="aria",
            parameters={"alert_id": alert_id},
            before_state=before,
            after_state=result,
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# list_alert_definitions
# ---------------------------------------------------------------------------


def list_alert_definitions(
    client: AriaClient,
    name_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List alert definitions (templates that generate alerts).

    Args:
        client: Authenticated Aria Operations API client.
        name_filter: Optional substring filter on definition name (case-insensitive).
        limit: Maximum number of definitions to return (1–500). Page size, not a
            ceiling: out-of-range values are rejected, not clamped.
        offset: Rows to skip before collecting this page. 0 or more; pass
            the previous response's ``next_offset`` to walk the collection.

    Returns:
        Result envelope with alert definition summary dicts under ``items``.
        ``total`` carries the collection's ``pageInfo.totalCount``, except under
        a name_filter — that filter is applied client-side, so the server's
        count describes the unfiltered collection, not this result.

        The envelope carries ``next_offset``: pass it back as ``offset`` for
        the next page and stop when it is ``None``. Do not loop on
        ``truncated`` — that says this page is not the whole collection, which
        stays true on the last page of a walk.
    """
    validate_page_args(limit, offset)

    # Walk every page so a name_filter match beyond the first page is not
    # invisible; stop once `limit` results have been collected.
    collection_total = CollectionTotal()
    results = []
    skipped = 0
    for d in iter_collection(
        client, "/alertdefinitions", "alertDefinitions", total_sink=collection_total
    ):
        name = sanitize(d.get("name", ""), max_len=300)
        if name_filter and name_filter.lower() not in name.lower():
            continue
        if skipped < offset:
            skipped += 1
            continue
        states = d.get("states") or []
        # AlertDefinition has no top-level criticality or active fields
        # (2026-06-08 spec audit): criticality is per-state — report the
        # max severity across states[].severity.
        criticality = _max_state_severity(states)
        # impact location is version-ambiguous: read top-level
        # impact.impactType first, fall back to states[0].impact.impactType.
        impact = (d.get("impact") or {}).get("impactType", "")
        if not impact and states:
            impact = ((states[0].get("impact") or {}).get("impactType", ""))
        results.append(
            {
                "id": sanitize(d.get("id", "")),
                "name": name,
                "description": sanitize(d.get("description", ""), max_len=500),
                "adapter_kind": sanitize(d.get("adapterKindKey", "")),
                "resource_kind": sanitize(d.get("resourceKindKey", "")),
                "criticality": sanitize(criticality),
                "impact": sanitize(impact),
                "type": sanitize(d.get("type", "")),
                "sub_type": sanitize(d.get("subType", "")),
            }
        )
        if len(results) >= limit:
            break
    total = None if name_filter else collection_total.value
    return paginated(
        results,
        limit=limit,
        total=total,
        next_offset=next_offset(len(results), limit, offset, total),
    )


# ---------------------------------------------------------------------------
# create_alert_definition
# ---------------------------------------------------------------------------

_VALID_CRITICALITIES_DEF = {"INFORMATION", "WARNING", "IMMEDIATE", "CRITICAL"}


def create_alert_definition(
    client: AriaClient,
    name: str,
    description: str,
    resource_kind: str,
    symptom_definition_ids: list[str],
    criticality: str = "WARNING",
    adapter_kind: str = "VMWARE",
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Create a new alert definition referencing existing symptom definitions.

    To find symptom_definition_ids, use list_symptom_definitions().

    Args:
        client: Authenticated Aria Operations API client.
        name: Alert definition name (must be unique).
        description: Human-readable description.
        resource_kind: Resource kind this alert applies to, e.g. VirtualMachine,
            HostSystem, ClusterComputeResource.
        symptom_definition_ids: List of symptom definition UUIDs that trigger this
            alert. Any one symptom firing triggers (OR across symptom ids).
        criticality: Alert severity: INFORMATION, WARNING, IMMEDIATE, CRITICAL.
        adapter_kind: Adapter kind key. Default VMWARE (vSphere adapter).
        audit_logger: Optional audit logger.
        target_name: Target name for audit log.

    Returns:
        Dict with new alert definition id and name.
    """
    if not name:
        raise ValueError(
            "name must be a non-empty alert definition name (e.g. 'High CPU on "
            "prod cluster'). Specify one; run list_alert_definitions first to see "
            "existing names and avoid creating a duplicate."
        )
    if not symptom_definition_ids:
        raise ValueError(
            "symptom_definition_ids must be a non-empty list of symptom "
            "definition UUIDs — an alert definition fires on symptoms, so at "
            "least one is required. Run list_symptom_definitions and copy the "
            "'id' of each symptom to attach."
        )
    criticality = criticality.upper()
    if criticality not in _VALID_CRITICALITIES_DEF:
        raise ValueError(
            f"criticality must be one of: "
            f"{', '.join(sorted(_VALID_CRITICALITIES_DEF))}. "
            f"Pass one of those values to --criticality (case-insensitive)."
        )

    payload = {
        "name": name,
        "description": description,
        "adapterKindKey": adapter_kind,
        "resourceKindKey": resource_kind,
        # "base-symptom-set" is the correct wire key (the Broadcom portal's
        # model page calls the property "symptoms", but the live server JSON
        # uses base-symptom-set — verified against VMware's own client code).
        # relation must be SELF. aggregation=ALL + symptomSetOperator=OR is
        # the doc-sample-verified combination: any one symptom firing
        # triggers (OR across symptom ids).
        "states": [
            {
                "severity": criticality,
                "base-symptom-set": {
                    "type": "SYMPTOM_SET",
                    "relation": "SELF",
                    "aggregation": "ALL",
                    "symptomSetOperator": "OR",
                    "symptomDefinitionIds": symptom_definition_ids,
                },
            }
        ],
    }

    data = client.post("/alertdefinitions", json_data=payload)
    # AlertDefinition has no top-level active field — no enabled in response.
    result = {
        "id": sanitize(data.get("id", "")),
        "name": sanitize(data.get("name", ""), max_len=300),
        "action": "created",
    }

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="create_alert_definition",
            resource=f"alertdefinition/{result['id']}",
            skill="aria",
            parameters={"name": name, "criticality": criticality, "resource_kind": resource_kind},
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# set_alert_definition_state  (enable / disable)
# ---------------------------------------------------------------------------


def set_alert_definition_state(
    client: AriaClient,
    definition_id: str,
    enabled: bool,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Enable or disable an alert definition.

    Args:
        client: Authenticated Aria Operations API client.
        definition_id: Alert definition UUID.
        enabled: True to enable, False to disable.
        audit_logger: Optional audit logger.
        target_name: Target name for audit log.

    Returns:
        Dict with definition_id, enabled, action.
    """
    if not definition_id:
        raise ValueError(
            "definition_id must be a non-empty alert definition UUID. Run "
            "list_alert_definitions and copy an exact 'id' value."
        )

    # PUT, not POST (2026-06-08 user report; endpoints exist in 8.6+).
    if enabled:
        client.put(f"/alertdefinitions/{definition_id}/enable")
    else:
        client.put(f"/alertdefinitions/{definition_id}/disable")
    action_path = "enable" if enabled else "disable"

    result = {
        "definition_id": definition_id,
        "enabled": enabled,
        "action": action_path,
    }

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="set_alert_definition_state",
            resource=f"alertdefinition/{definition_id}",
            skill="aria",
            parameters={"definition_id": definition_id, "enabled": enabled},
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# delete_alert_definition
# ---------------------------------------------------------------------------


def delete_alert_definition(
    client: AriaClient,
    definition_id: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Delete an alert definition permanently.

    Args:
        client: Authenticated Aria Operations API client.
        definition_id: Alert definition UUID to delete.
        audit_logger: Optional audit logger.
        target_name: Target name for audit log.

    Returns:
        Dict confirming deletion.
    """
    if not definition_id:
        raise ValueError(
            "definition_id must be a non-empty alert definition UUID. Run "
            "list_alert_definitions and copy an exact 'id' value."
        )

    client.delete(f"/alertdefinitions/{definition_id}")

    result = {"definition_id": definition_id, "action": "deleted"}

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="delete_alert_definition",
            resource=f"alertdefinition/{definition_id}",
            skill="aria",
            parameters={"definition_id": definition_id},
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# list_symptom_definitions  (helper for create_alert_definition)
# ---------------------------------------------------------------------------


def list_symptom_definitions(
    client: AriaClient,
    name_filter: str | None = None,
    resource_kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List symptom definitions — use these IDs when creating alert definitions.

    Args:
        client: Authenticated Aria Operations API client.
        name_filter: Optional substring filter on symptom name (case-insensitive).
        resource_kind: Optional resource kind to filter (e.g. VirtualMachine).
        limit: Maximum number of symptom definitions to return (1–500). Page size, not a
            ceiling: out-of-range values are rejected, not clamped.
        offset: Rows to skip before collecting this page. 0 or more; pass
            the previous response's ``next_offset`` to walk the collection.

    Returns:
        Result envelope with symptom definition dicts under ``items``, each with
        id, name, resource_kind, metric_key, threshold_type, and criticality.
        ``total`` carries ``pageInfo.totalCount`` (which already reflects the
        server-side resource_kind filter), except under a client-side
        name_filter.

        The envelope carries ``next_offset``: pass it back as ``offset`` for
        the next page and stop when it is ``None``. Do not loop on
        ``truncated`` — that says this page is not the whole collection, which
        stays true on the last page of a walk.
    """
    validate_page_args(limit, offset)
    extra_params: dict = {}
    if resource_kind:
        # Query param is `resourceKind`, NOT `resourceKindKey` (spec audit).
        extra_params["resourceKind"] = resource_kind

    # Walk every page so a name_filter match beyond the first page is not
    # invisible; stop once `limit` results have been collected.
    collection_total = CollectionTotal()
    results = []
    skipped = 0
    for s in iter_collection(
        client,
        "/symptomdefinitions",
        "symptomDefinitions",
        extra_params=extra_params,
        total_sink=collection_total,
    ):
        name = sanitize(s.get("name", ""), max_len=300)
        if name_filter and name_filter.lower() not in name.lower():
            continue
        if skipped < offset:
            skipped += 1
            continue
        condition = s.get("state", {}).get("condition", {})
        results.append({
            "id": sanitize(s.get("id", "")),
            "name": name,
            "resource_kind": sanitize(s.get("resourceKindKey", "")),
            "adapter_kind": sanitize(s.get("adapterKindKey", "")),
            "metric_key": sanitize(condition.get("key", ""), max_len=200),
            "threshold_type": sanitize(condition.get("thresholdType", ""), max_len=100),
            "criticality": sanitize(s.get("state", {}).get("severity", ""), max_len=50),
        })
        if len(results) >= limit:
            break
    total = None if name_filter else collection_total.value
    return paginated(
        results,
        limit=limit,
        total=total,
        next_offset=next_offset(len(results), limit, offset, total),
    )

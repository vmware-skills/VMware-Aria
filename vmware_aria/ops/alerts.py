"""Aria Operations alert management: list, get, acknowledge, cancel, list definitions.

Write operations (acknowledge, cancel) are audit-logged.
All API responses pass through sanitize() to strip control characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize

from vmware_aria.ops._paging import iter_collection

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


def list_alerts(
    client: AriaClient,
    active_only: bool = True,
    criticality: str | None = None,
    resource_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List alerts from Aria Operations.

    Args:
        client: Authenticated Aria Operations API client.
        active_only: Return only active (non-cancelled) alerts.
        criticality: Filter by criticality: INFORMATION, WARNING, IMMEDIATE, CRITICAL.
        resource_id: Scope alerts to a specific resource UUID.
        limit: Maximum number of alerts to return (1–500).

    Returns:
        List of alert summary dicts.
    """
    if criticality and criticality.upper() not in _VALID_CRITICALITIES:
        raise ValueError(
            f"Invalid criticality '{criticality}'. "
            f"Must be one of: {', '.join(sorted(_VALID_CRITICALITIES))}"
        )

    limit = max(1, min(limit, 500))

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

    # Pure query endpoint — idempotent, safe to retry transient gateway errors.
    data = client.post("/alerts/query", json_data=query, params={"pageSize": limit}, retries=1)
    items = data.get("alerts", [])

    # Alert model fields (2026-06-08 spec audit): criticality is `alertLevel`,
    # the display name is `alertDefinitionName`. There is no alertName,
    # criticality, resourceName, or info field — resolve the resource name
    # via get_resource(resourceId) when needed.
    return [
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


# ---------------------------------------------------------------------------
# get_alert
# ---------------------------------------------------------------------------


def _get_contributing_symptoms(client: AriaClient, alert_id: str) -> list[dict]:
    """Fetch triggered symptoms via GET /alerts/contributingsymptoms?id=<alertId>.

    The Alert model has no alertSymptomList — triggered symptoms come from
    this separate endpoint (2026-06-08 spec audit). The response container
    key varies across versions, so parse whichever value is a list and read
    Symptom fields defensively. Failures degrade to an empty list (logged)
    so a symptoms hiccup never breaks get_alert.
    """
    try:
        data = client.get("/alerts/contributingsymptoms", params={"id": alert_id})
    except Exception as exc:
        _log.warning("Could not fetch contributing symptoms for alert %s: %s", alert_id, exc)
        return []

    container: list = []
    for key in ("symptoms", "symptom", "contributingSymptoms", "result"):
        value = data.get(key)
        if isinstance(value, list):
            container = value
            break
    else:
        container = next((v for v in data.values() if isinstance(v, list)), [])

    symptoms = []
    for s in container:
        if not isinstance(s, dict):
            continue
        symptoms.append(
            {
                "id": sanitize(str(s.get("id") or s.get("symptomId") or "")),
                "name": sanitize(
                    str(s.get("name") or s.get("message") or ""), max_len=300
                ),
                "severity": sanitize(
                    str(s.get("severity") or s.get("symptomCriticality") or "")
                ),
                "symptom_definition_id": sanitize(str(s.get("symptomDefinitionId") or "")),
                "resource_id": sanitize(str(s.get("resourceId") or "")),
            }
        )
    return symptoms


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
        Dict with alert details and contributing symptom list.
    """
    if not alert_id:
        raise ValueError("alert_id must not be empty")

    data = client.get(f"/alerts/{alert_id}")
    return {
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
        "symptoms": _get_contributing_symptoms(client, alert_id),
    }


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
        raise ValueError("alert_id must not be empty")

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
        raise ValueError("alert_id must not be empty")

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
) -> list[dict]:
    """List alert definitions (templates that generate alerts).

    Args:
        client: Authenticated Aria Operations API client.
        name_filter: Optional substring filter on definition name (case-insensitive).
        limit: Maximum number of definitions to return (1–500).

    Returns:
        List of alert definition summary dicts.
    """
    limit = max(1, min(limit, 500))

    # Walk every page so a name_filter match beyond the first page is not
    # invisible; stop once `limit` results have been collected.
    results = []
    for d in iter_collection(client, "/alertdefinitions", "alertDefinitions"):
        name = sanitize(d.get("name", ""), max_len=300)
        if name_filter and name_filter.lower() not in name.lower():
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
    return results


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
        raise ValueError("name must not be empty")
    if not symptom_definition_ids:
        raise ValueError("symptom_definition_ids must not be empty")
    criticality = criticality.upper()
    if criticality not in _VALID_CRITICALITIES_DEF:
        raise ValueError(f"criticality must be one of: {', '.join(sorted(_VALID_CRITICALITIES_DEF))}")

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
        raise ValueError("definition_id must not be empty")

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
        raise ValueError("definition_id must not be empty")

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
) -> list[dict]:
    """List symptom definitions — use these IDs when creating alert definitions.

    Args:
        client: Authenticated Aria Operations API client.
        name_filter: Optional substring filter on symptom name (case-insensitive).
        resource_kind: Optional resource kind to filter (e.g. VirtualMachine).
        limit: Maximum number of symptom definitions to return (1–500).

    Returns:
        List of symptom definition dicts with id, name, resource_kind, metric_key,
        threshold_type, and criticality.
    """
    limit = max(1, min(limit, 500))
    extra_params: dict = {}
    if resource_kind:
        # Query param is `resourceKind`, NOT `resourceKindKey` (spec audit).
        extra_params["resourceKind"] = resource_kind

    # Walk every page so a name_filter match beyond the first page is not
    # invisible; stop once `limit` results have been collected.
    results = []
    for s in iter_collection(
        client, "/symptomdefinitions", "symptomDefinitions", extra_params=extra_params
    ):
        name = sanitize(s.get("name", ""), max_len=300)
        if name_filter and name_filter.lower() not in name.lower():
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
    return results

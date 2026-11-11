"""Aria alert and symptom definition operations."""

from __future__ import annotations


from typing import TYPE_CHECKING

from vmware_policy import paginated, sanitize


from vmware_aria.ops._paging import (
    CollectionTotal,
    iter_collection,
    next_offset,
    validate_page_args,
)

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient
    from vmware_aria.notify.audit import AuditLogger

import vmware_aria.ops._alert_shapes as _compat_exports_0
_LOOKUP_NOT_NEEDED = _compat_exports_0._LOOKUP_NOT_NEEDED
_MAX_SYMPTOM_DEPTH = _compat_exports_0._MAX_SYMPTOM_DEPTH
_SEVERITY_RANK = _compat_exports_0._SEVERITY_RANK
_SYMPTOM_CONTAINER_KEYS = _compat_exports_0._SYMPTOM_CONTAINER_KEYS
_SYMPTOM_LEAF_KEYS = _compat_exports_0._SYMPTOM_LEAF_KEYS
_definition_id = _compat_exports_0._definition_id
_definition_severity = _compat_exports_0._definition_severity
_max_state_severity = _compat_exports_0._max_state_severity
_own_name = _compat_exports_0._own_name
_own_severity = _compat_exports_0._own_severity
_summarize_symptom = _compat_exports_0._summarize_symptom
_walk_symptoms = _compat_exports_0._walk_symptoms


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
    for d in iter_collection(client, "/alertdefinitions", "alertDefinitions", total_sink=collection_total):
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
            impact = (states[0].get("impact") or {}).get("impactType", "")
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
        results.append(
            {
                "id": sanitize(s.get("id", "")),
                "name": name,
                "resource_kind": sanitize(s.get("resourceKindKey", "")),
                "adapter_kind": sanitize(s.get("adapterKindKey", "")),
                "metric_key": sanitize(condition.get("key", ""), max_len=200),
                "threshold_type": sanitize(condition.get("thresholdType", ""), max_len=100),
                "criticality": sanitize(s.get("state", {}).get("severity", ""), max_len=50),
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

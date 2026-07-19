"""ALERT DEFINITION write tools (2, write).

create_alert_definition, set_alert_definition_state.

delete_alert_definition keeps its definition in ``vmware_aria/mcp_server/server.py``
because its confirmed-gate preview contract is asserted there by AST
inspection in ``tests/test_no_destructive_ops.py``. The read-only
list_alert_definitions / list_symptom_definitions live in
``vmware_aria/mcp_server/tools/alerts.py``.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def create_alert_definition(
    name: str,
    description: str,
    resource_kind: str,
    symptom_definition_ids: list[str],
    criticality: str = "WARNING",
    adapter_kind: str = "VMWARE",
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Create a new alert definition referencing existing symptom definitions.

    Use list_symptom_definitions() to find symptom_definition_ids.

    Args:
        name: Alert definition name (must be unique in Aria Operations).
        description: Human-readable description of when/why this alert fires.
        resource_kind: Resource kind this alert applies to: VirtualMachine,
            HostSystem, ClusterComputeResource, Datastore.
        symptom_definition_ids: List of symptom definition UUIDs. Any one
            symptom firing triggers (OR across symptom ids).
        criticality: Alert severity: INFORMATION, WARNING, IMMEDIATE, CRITICAL.
        adapter_kind: Adapter kind key. Default VMWARE (vSphere adapter).
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alerts import create_alert_definition as _create

        return _create(
            server._get_connection(target),
            name=name,
            description=description,
            resource_kind=resource_kind,
            symptom_definition_ids=symptom_definition_ids,
            criticality=criticality,
            adapter_kind=adapter_kind,
            audit_logger=server._audit,
            target_name=server._target_name(target),
        )
    except Exception as e:
        return {"error": server._safe_error(e, "create_alert_definition"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def set_alert_definition_state(
    definition_id: str,
    enabled: bool,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Enable or disable an existing alert definition.

    Args:
        definition_id: Alert definition UUID (from list_alert_definitions).
        enabled: True to enable the definition, False to disable it.
        target: Optional Aria Operations target name from config. Uses default if omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.alerts import set_alert_definition_state as _set_state

        return _set_state(
            server._get_connection(target),
            definition_id=definition_id,
            enabled=enabled,
            audit_logger=server._audit,
            target_name=server._target_name(target),
        )
    except Exception as e:
        return {"error": server._safe_error(e, "set_alert_definition_state"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}

"""MCP server wrapping VMware Aria Operations monitoring and capacity planning.

This module exposes VMware Aria Operations management tools via the Model
Context Protocol (MCP) using stdio transport.  The 44 tools are split by
domain across ``vmware_aria/mcp_server/tools/*.py``; each module registers its tools onto
the shared ``mcp`` instance defined in ``vmware_aria/mcp_server/_shared.py``.  Importing
those modules below is what performs the registration.

This file stays the thin entrypoint: it imports the tool modules (so the
``@mcp.tool`` decorators run), re-exports the shared plumbing and every tool
function so the historical paths ``from vmware_aria.mcp_server.server import _safe_error,
mcp, <tool fns>`` keep resolving, and exposes ``main()`` (踩坑 #17).  The four
confirmed-gate destructive tools (acknowledge_alert, cancel_alert,
delete_alert_definition, delete_report) are defined here because their
preview-until-confirmed contract is asserted by AST inspection of *this file*
in ``tests/test_no_destructive_ops.py``.

Tool categories
---------------
* **Resource** (5 tools, read-only): list_resources, get_resource,
  get_resource_metrics, get_resource_health, get_top_consumers
  — ``vmware_aria/mcp_server/tools/resources.py``

* **Resource catalog** (3 tools, read-only): list_metric_keys,
  get_resource_properties, get_resource_relationships — ``tools/catalog.py``

* **Alerts** (5 tools, 3 read + 2 write): list_alerts, get_alert,
  list_alert_definitions (read, ``tools/alerts.py``); acknowledge_alert,
  cancel_alert (write, this file)

* **Alert Definitions** (4 tools, write): list_symptom_definitions
  (read, ``tools/alerts.py``); create_alert_definition,
  set_alert_definition_state (write, ``tools/alert_definitions.py``);
  delete_alert_definition (write, this file)

* **Capacity** (4 tools, read-only): get_capacity_overview,
  get_remaining_capacity, get_time_remaining,
  list_rightsizing_recommendations — ``tools/capacity.py``

* **Anomaly** (2 tools, read-only): list_anomalies, get_resource_riskbadge
  — ``tools/anomaly.py``

* **Health** (4 tools, read-only): get_aria_health, list_collector_groups,
  get_aria_node_resources, list_adapters — ``tools/health.py``

* **Fleet / PromQL** (5 tools, read-only, VCF Operations 9.1):
  fleet_certificate_list, fleet_password_account_list, fleet_domain_list,
  findings_list, promql_query — ``tools/fleet.py``

* **Reports** (5 tools, 4 read + 1 write... ): list_report_definitions,
  generate_report, list_reports, get_report (``tools/reports.py``);
  delete_report (write, this file)

* **Maintenance** (3 tools, 1 read + 2 write): list_maintenance_schedules
  (read); start_resource_maintenance, end_resource_maintenance (write,
  confirmed gate, each other's undo) — ``tools/maintenance.py``

* **Alert workflow** (3 tools, 2 read + 1 write): list_alert_notes,
  get_alert_recommendations (read); add_alert_note (write, low risk)
  — ``tools/alert_workflow.py``

Security considerations
-----------------------
* **Credential handling**: Credentials are loaded from environment
  variables / ``.env`` file — never passed via MCP messages.
* **Transport**: Uses stdio transport (local only); no network listener.
* **Write operations**: acknowledge_alert, cancel_alert modify alert state;
  confirmation is recommended before execution.
* **Sanitization**: All API text responses pass through _sanitize() to strip
  control characters and truncate to prevent prompt injection.

For VM lifecycle operations use vmware-aiops.
For NSX networking use vmware-nsx.
"""


import logging
from typing import Optional

from vmware_policy import describe_tool_parameters, vmware_tool

# Shared plumbing — re-exported so `from vmware_aria.mcp_server.server import _safe_error,
# mcp, _get_connection, ...` (and monkeypatch targets) keep resolving.
from vmware_aria.mcp_server._shared import (  # noqa: F401  (logger re-exported for the historical vmware_aria.mcp_server.server.logger path)
    _audit,
    _gated,
    _get_connection,
    _safe_error,
    _target_name,
    logger,
    mcp,
)

# Importing the tool modules runs their @mcp.tool decorators, registering the
# read/non-confirmed-write tools onto the shared `mcp` instance.
from vmware_aria.mcp_server.tools import (  # noqa: F401  (imported for registration side-effect)
    alert_definitions,
    alert_workflow,
    alerts,
    anomaly,
    capacity,
    catalog,
    fleet,
    health,
    maintenance,
    reports,
    resources,
)

# Re-export every tool function so `vmware_aria.mcp_server.server.<tool>` resolves (tests
# call e.g. `server.get_resource(...)` and patch `server._get_connection`).
from vmware_aria.mcp_server.tools.alert_definitions import (  # noqa: F401
    create_alert_definition,
    set_alert_definition_state,
)
from vmware_aria.mcp_server.tools.alert_workflow import (  # noqa: F401
    add_alert_note,
    get_alert_recommendations,
    list_alert_notes,
)
from vmware_aria.mcp_server.tools.maintenance import (  # noqa: F401
    end_resource_maintenance,
    list_maintenance_schedules,
    start_resource_maintenance,
)
from vmware_aria.mcp_server.tools.alerts import (  # noqa: F401
    get_alert,
    investigate_alert,
    list_alert_definitions,
    list_alerts,
    list_symptom_definitions,
)
from vmware_aria.mcp_server.tools.anomaly import (  # noqa: F401
    get_resource_riskbadge,
    list_anomalies,
)
from vmware_aria.mcp_server.tools.capacity import (  # noqa: F401
    get_capacity_overview,
    get_remaining_capacity,
    get_time_remaining,
    list_rightsizing_recommendations,
)
from vmware_aria.mcp_server.tools.catalog import (  # noqa: F401
    get_resource_properties,
    get_resource_relationships,
    list_metric_keys,
)
from vmware_aria.mcp_server.tools.fleet import (  # noqa: F401
    findings_list,
    fleet_certificate_list,
    fleet_domain_list,
    fleet_password_account_list,
    promql_query,
)
from vmware_aria.mcp_server.tools.health import (  # noqa: F401
    get_aria_health,
    get_aria_node_resources,
    list_adapters,
    list_collector_groups,
)
from vmware_aria.mcp_server.tools.reports import (  # noqa: F401
    generate_report,
    get_report,
    list_report_definitions,
    list_reports,
)
from vmware_aria.mcp_server.tools.resources import (  # noqa: F401
    get_resource,
    get_resource_health,
    get_resource_metrics,
    get_top_consumers,
    list_resources,
)

__all__ = [
    "mcp",
    "main",
    "_safe_error",
    "_get_connection",
    "_target_name",
    "_audit",
    # tool functions
    "list_resources",
    "get_resource",
    "get_resource_metrics",
    "get_resource_health",
    "get_top_consumers",
    "list_metric_keys",
    "get_resource_properties",
    "get_resource_relationships",
    "list_alerts",
    "get_alert",
    "list_alert_definitions",
    "list_symptom_definitions",
    "investigate_alert",
    "acknowledge_alert",
    "cancel_alert",
    "create_alert_definition",
    "set_alert_definition_state",
    "delete_alert_definition",
    "get_capacity_overview",
    "get_remaining_capacity",
    "get_time_remaining",
    "list_rightsizing_recommendations",
    "list_anomalies",
    "get_resource_riskbadge",
    "get_aria_health",
    "list_collector_groups",
    "get_aria_node_resources",
    "list_adapters",
    "fleet_certificate_list",
    "fleet_password_account_list",
    "fleet_domain_list",
    "findings_list",
    "promql_query",
    "list_report_definitions",
    "generate_report",
    "list_reports",
    "get_report",
    "delete_report",
    "start_resource_maintenance",
    "end_resource_maintenance",
    "list_maintenance_schedules",
    "list_alert_notes",
    "add_alert_note",
    "get_alert_recommendations",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Confirmed-gate destructive tools (defined here; AST-asserted by
# tests/test_no_destructive_ops.py against this file)
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def acknowledge_alert(
    alert_id: str,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Acknowledge an active alert by taking ownership (does not cancel it).

    The suite-api has no dedicated "acknowledge" action; this maps to
    POST /alerts?action=takeownership, assigning the alert to the API user
    (control state ASSIGNED). The alert remains active until cancelled.
    Use this when you want to own the alert without closing it; cancel_alert
    closes it for good.

    Without confirm=True this only previews: it returns blast_radius (alert id,
    definition, criticality, status, control state, and the resource it is on)
    and changes nothing. Show that to the user and get their explicit decision.
    Do not set confirm=True on your own because the user asked earlier: they
    have not seen what it changes yet. Refused with confirm=True: a cancelled
    alert, and an alert whose status or resource cannot be read.

    Args:
        alert_id: The alert UUID to acknowledge.
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.ops import gate_measures
    from vmware_aria.ops.alerts import acknowledge_alert as _ack
    from vmware_aria.ops.write_gate import gate, resolve_confirm

    decision = resolve_confirm(confirm, confirmed)

    def run() -> dict:
        client = _get_connection(target)
        radius = gate_measures.measure_alert(
            client, alert_id, "Assigns the alert to the API user (control state ASSIGNED); it stays active."
        )
        if gate_measures.is_canceled(radius):
            radius = {**radius, "blockers": [
                "The alert is cancelled; there is nothing to take ownership of. List active "
                "alerts with list_alerts."
            ]}
        return gate(
            "acknowledge_alert", f"alert {radius['alert_id']}", radius, act=decision.act,
            next_step="Check it with get_alert and retry.",
            apply=lambda: _ack(client, alert_id, audit_logger=_audit, target_name=_target_name(target)),
        )

    return _gated("acknowledge_alert", decision.deprecated, run)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def cancel_alert(
    alert_id: str,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Cancel (dismiss) an active alert. This WRITE operation permanently closes the alert.

    Use acknowledge_alert instead if you only want to mark it as seen.
    Cancelled alerts will not re-trigger unless the underlying condition recurs.

    Without confirm=True this only previews: it returns blast_radius (alert id,
    definition, criticality, status, and the resource it is on) and changes
    nothing. Show that to the user and get their explicit decision. Do not set
    confirm=True on your own because the user asked earlier: they have not seen
    what it changes yet. An alert already cancelled returns action "noop".
    Refused with confirm=True: an alert whose status or resource cannot be read.

    Args:
        alert_id: The alert UUID to cancel.
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.ops import gate_measures
    from vmware_aria.ops.alerts import cancel_alert as _cancel
    from vmware_aria.ops.write_gate import gate, resolve_confirm

    decision = resolve_confirm(confirm, confirmed)

    def run() -> dict:
        client = _get_connection(target)
        radius = gate_measures.measure_alert(
            client, alert_id, "Closes the alert permanently; it re-triggers only if the condition recurs."
        )
        return gate(
            "cancel_alert", f"alert {radius['alert_id']}", radius, act=decision.act,
            next_step="Check it with get_alert and retry.",
            noop="The alert is already cancelled; nothing to do." if gate_measures.is_canceled(radius) else None,
            apply=lambda: _cancel(client, alert_id, audit_logger=_audit, target_name=_target_name(target)),
        )

    return _gated("cancel_alert", decision.deprecated, run)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def delete_alert_definition(
    definition_id: str,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Permanently delete an alert definition. Irreversible.

    This WRITE operation removes the alert definition from Aria Operations.
    Active alerts generated by this definition will not be affected.
    Use set_alert_definition_state(enabled=False) instead to silence a
    definition you may want back.

    Without confirm=True this only previews: it returns blast_radius (the
    definition's id, name, adapter and resource kind, criticality and state
    count) and deletes nothing. Show that to the user and get their explicit
    decision. Do not set confirm=True on your own because the user asked
    earlier: they have not seen what it deletes yet. Refused with confirm=True:
    a definition that cannot be read.

    Args:
        definition_id: Alert definition id to delete (from list_alert_definitions).
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.ops import gate_measures
    from vmware_aria.ops.alerts import delete_alert_definition as _delete
    from vmware_aria.ops.write_gate import gate, resolve_confirm

    decision = resolve_confirm(confirm, confirmed)

    def run() -> dict:
        client = _get_connection(target)
        radius = gate_measures.measure_alert_definition(client, definition_id)
        return gate(
            "delete_alert_definition", f"alert definition {radius['definition_id']}", radius,
            act=decision.act, next_step="Check it with list_alert_definitions and retry.",
            apply=lambda: _delete(
                client, definition_id=definition_id.strip(), audit_logger=_audit,
                target_name=_target_name(target),
            ),
        )

    return _gated("delete_alert_definition", decision.deprecated, run)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def delete_report(
    report_id: str,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Permanently delete a generated report artifact from Aria Operations. Removes only the generated report instance and its output — the report definition and any schedules remain intact; re-run generate_report to recreate it. Deletion is irreversible and is recorded in the audit log. Returns an error if the report_id does not exist; use list_reports to find valid UUIDs first.

    Without confirm=True this only previews: it returns blast_radius (report
    id, title, status, definition id, completion time, owner) and deletes
    nothing. Show that to the user and get their explicit decision. Do not set
    confirm=True on your own because the user asked earlier: they have not
    seen what it deletes yet. Refused with confirm=True: a report whose status
    or definition cannot be read.

    Args:
        report_id: The report UUID to delete (from generate_report or list_reports).
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.ops import gate_measures
    from vmware_aria.ops.reports import delete_report as _delete
    from vmware_aria.ops.write_gate import gate, resolve_confirm

    decision = resolve_confirm(confirm, confirmed)

    def run() -> dict:
        client = _get_connection(target)
        radius = gate_measures.measure_report(client, report_id)
        return gate(
            "delete_report", f"report {radius['report_id']}", radius, act=decision.act,
            next_step="Check it with get_report and retry.",
            apply=lambda: _delete(
                client, report_id=report_id.strip(), audit_logger=_audit,
                target_name=_target_name(target),
            ),
        )

    return _gated("delete_report", decision.deprecated, run)


# ---------------------------------------------------------------------------
# Environment declaration
# ---------------------------------------------------------------------------

# The environment resolver lives in policy_environment so the CLI registers
# it too (its @guarded writes go through the same guard()); importing it here
# registers it for the MCP surface.
from vmware_aria.policy_environment import _cached_config, _environment_for  # noqa: F401 — imported to register the resolver, and re-exported

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the MCP server using stdio transport."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()

# The docstrings above are the schema. `describe_tool_parameters` copies each
# `Args:` entry into the JSON schema an agent actually reads, and closes the
# object. Without it every parameter reaches the model as a bare name and a
# type, which is how a wrong guess becomes an unfiltered result or a silent
# zero-row answer instead of an error (real-hardware round, 2026-08-30).
_DESCRIBED_PARAMS = describe_tool_parameters(mcp._tool_manager._tools)

"""MCP server wrapping VMware Aria Operations monitoring and capacity planning.

This module exposes VMware Aria Operations management tools via the Model
Context Protocol (MCP) using stdio transport.  The 27 tools are split by
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

* **Health** (2 tools, read-only): get_aria_health, list_collector_groups
  — ``tools/health.py``

* **Reports** (5 tools, 4 read + 1 write... ): list_report_definitions,
  generate_report, list_reports, get_report (``tools/reports.py``);
  delete_report (write, this file)

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

from vmware_policy import apply_read_only_gate, mtime_cached_loader, set_environment_resolver, vmware_tool

from vmware_aria.config import CONFIG_FILE, load_config

# Shared plumbing — re-exported so `from vmware_aria.mcp_server.server import _safe_error,
# mcp, _get_connection, ...` (and monkeypatch targets) keep resolving.
from vmware_aria.mcp_server._shared import (  # noqa: F401  (logger re-exported for the historical vmware_aria.mcp_server.server.logger path)
    _audit,
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
    alerts,
    anomaly,
    capacity,
    health,
    reports,
    resources,
)

# Re-export every tool function so `vmware_aria.mcp_server.server.<tool>` resolves (tests
# call e.g. `server.get_resource(...)` and patch `server._get_connection`).
from vmware_aria.mcp_server.tools.alert_definitions import (  # noqa: F401
    create_alert_definition,
    set_alert_definition_state,
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
from vmware_aria.mcp_server.tools.health import (  # noqa: F401
    get_aria_health,
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
    "list_report_definitions",
    "generate_report",
    "list_reports",
    "get_report",
    "delete_report",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Confirmed-gate destructive tools (defined here; AST-asserted by
# tests/test_no_destructive_ops.py against this file)
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def acknowledge_alert(alert_id: str, confirmed: bool = False, target: Optional[str] = None) -> dict:
    """[WRITE] Acknowledge an active alert by taking ownership (does not cancel it).

    The suite-api has no dedicated "acknowledge" action; this maps to
    POST /alerts?action=takeownership, assigning the alert to the API user
    (control state ASSIGNED). The alert remains active until cancelled.
    Use this when you want to own the alert without closing it; cancel_alert
    closes it for good. Default confirmed=False returns a preview without
    making any change.

    Args:
        alert_id: The alert UUID to acknowledge.
        confirmed: Must be True to actually acknowledge. Default False = preview only.
        target: Aria target name from config; default when omitted.
    """
    if not confirmed:
        return {
            "preview": True,
            "action": "acknowledge",
            "alert_id": alert_id,
            "message": (
                f"[preview] Would acknowledge alert {alert_id}. "
                "Re-invoke with confirmed=True to execute."
            ),
        }
    try:
        from vmware_aria.ops.alerts import acknowledge_alert as _ack

        return _ack(_get_connection(target), alert_id, audit_logger=_audit, target_name=_target_name(target))
    except Exception as e:
        return {"error": _safe_error(e, "acknowledge_alert"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def cancel_alert(alert_id: str, confirmed: bool = False, target: Optional[str] = None) -> dict:
    """[WRITE] Cancel (dismiss) an active alert. This WRITE operation permanently closes the alert.

    Use acknowledge_alert instead if you only want to mark it as seen.
    Cancelled alerts will not re-trigger unless the underlying condition recurs.
    Default confirmed=False returns a preview without making any change.

    Args:
        alert_id: The alert UUID to cancel.
        confirmed: Must be True to actually cancel. Default False = preview only.
        target: Aria target name from config; default when omitted.
    """
    if not confirmed:
        return {
            "preview": True,
            "action": "cancel",
            "alert_id": alert_id,
            "message": (
                f"[preview] Would cancel alert {alert_id}. "
                "Re-invoke with confirmed=True to execute."
            ),
        }
    try:
        from vmware_aria.ops.alerts import cancel_alert as _cancel

        return _cancel(_get_connection(target), alert_id, audit_logger=_audit, target_name=_target_name(target))
    except Exception as e:
        return {"error": _safe_error(e, "cancel_alert"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def delete_alert_definition(
    definition_id: str,
    confirmed: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Permanently delete an alert definition. Irreversible.

    This WRITE operation removes the alert definition from Aria Operations.
    Active alerts generated by this definition will not be affected.
    Use set_alert_definition_state(enabled=False) instead to silence a
    definition you may want back.
    Default confirmed=False returns a preview without making any change.

    Args:
        definition_id: Alert definition UUID to delete.
        confirmed: Must be True to actually delete. Default False = preview only.
        target: Aria target name from config; default when omitted.
    """
    if not confirmed:
        return {
            "preview": True,
            "action": "delete_alert_definition",
            "definition_id": definition_id,
            "message": (
                f"[preview] Would permanently delete alert definition {definition_id}. "
                "Re-invoke with confirmed=True to execute."
            ),
        }
    try:
        from vmware_aria.ops.alerts import delete_alert_definition as _delete

        return _delete(
            _get_connection(target),
            definition_id=definition_id,
            audit_logger=_audit,
            target_name=_target_name(target),
        )
    except Exception as e:
        return {"error": _safe_error(e, "delete_alert_definition"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def delete_report(
    report_id: str,
    confirmed: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE] Permanently delete a generated report artifact from Aria Operations. Removes only the generated report instance and its output — the report definition and any schedules remain intact; re-run generate_report to recreate it. Deletion is irreversible and is recorded in the audit log. Returns an error if the report_id does not exist; use list_reports to find valid UUIDs first. Default confirmed=False returns a preview without deleting.

    Args:
        report_id: The report UUID to delete (from generate_report or list_reports).
        confirmed: Must be True to actually delete. Default False = preview only.
        target: Aria target name from config; default when omitted.
    """
    if not confirmed:
        return {
            "preview": True,
            "action": "delete_report",
            "report_id": report_id,
            "message": (
                f"[preview] Would permanently delete report {report_id}. "
                "Re-invoke with confirmed=True to execute."
            ),
        }
    try:
        from vmware_aria.ops.reports import delete_report as _delete

        return _delete(
            _get_connection(target),
            report_id=report_id,
            audit_logger=_audit,
            target_name=_target_name(target),
        )
    except Exception as e:
        return {"error": _safe_error(e, "delete_report"), "hint": "Run 'vmware-aria doctor' to verify connectivity."}


# ---------------------------------------------------------------------------
# Read-only gate
# ---------------------------------------------------------------------------


def _config_read_only() -> Optional[bool]:
    """Best-effort read of ``read_only`` from the config file.

    Runs at import time, when no config file need exist yet (tests, ``--help``,
    smoke checks), so every failure degrades to "not configured" and lets the
    env vars decide. None and False are equivalent here — config is the last
    link in the precedence chain — but None keeps 'not configured'
    distinguishable from 'configured off' in logs and debugging.
    """
    try:
        return load_config().read_only
    except Exception:  # noqa: BLE001 — absent/unreadable config is not an error here
        return None


# Applied once, after every tool module above has registered. In read-only mode
# the write tools are removed from the registry, so list_tools() never offers
# them — the guarantee is structural rather than a prompt instruction the model
# may ignore (VMware-AIops issue #31).
WITHHELD_WRITE_TOOLS: list[str] = apply_read_only_gate(
    mcp, "vmware-aria", config_flag=_config_read_only()
)


# ---------------------------------------------------------------------------
# Environment declaration
# ---------------------------------------------------------------------------


_cached_config = mtime_cached_loader("VMWARE_ARIA_CONFIG", CONFIG_FILE, load_config)


def _environment_for(target: Optional[str]) -> str:
    """Report the environment a target declares, for policy scoping.

    Policy rules scope by environment ("irreversible work in production needs a
    second person"), and vmware-policy cannot read this skill's config itself.
    Registering this lookup is what lets those rules fire at all. Reloaded on
    config.yaml mtime change so an edit takes effect without restarting the
    server. The config is cached via :func:`vmware_policy.mtime_cached_loader`,
    so repeated tool calls pay one ``os.stat`` instead of a full YAML parse.
    """
    try:
        return _cached_config().environment_for(target)
    except Exception:  # noqa: BLE001 — an unreadable config means "undeclared"
        return ""


set_environment_resolver(_environment_for)


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

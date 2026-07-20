"""Alert investigation: one call from an alert UUID to its confirmed resource.

Correlating an Aria alert with the affected object takes three steps — fetch
the alert, read ``resourceId`` off it, fetch that resource — and the alert and
the resource each carry a UUID. Agents driving this by hand routinely pass the
alert UUID where the resource UUID belongs, or start querying vCenter before
the resource kind is known and ask the wrong tool.

VMware-AIops issue #31 is the evidence: an operator running a local Llama 3.3
70B had to write three prompt guardrails ("first resolve the resource_id",
"do not confuse the alert ID with the resource ID", "only correlate after the
name and type have been confirmed") to make the sequence reliable. That is
bookkeeping, and bookkeeping belongs in the tool.

:func:`investigate_alert` does the whole sequence server-side and returns both
IDs explicitly labelled, the confirmed resource name and kind, and a
ready-to-use handoff naming the exact vmware-monitor tool and argument for
that kind of object. Nothing is left for the caller to infer.

Scope: this resolves the Aria half only. It deliberately does not reach into
vCenter — vmware-aria holds no vCenter credentials, and coupling the two would
force every Aria deployment to configure one. The ``next_step`` block hands off
to vmware-monitor instead.
"""

from __future__ import annotations

from typing import Any, Optional

from vmware_aria.ops.alerts import get_alert
from vmware_aria.ops.resources import get_resource

#: Aria ``resourceKindKey`` → the vmware-monitor tool that investigates it.
#: Values are ``(tool_name, argument_name)``; the argument takes the confirmed
#: resource name. Kinds absent from this map get an explanatory note instead of
#: a guessed tool.
_MONITOR_HANDOFF: dict[str, tuple[str, str]] = {
    "VirtualMachine": ("vm_investigation_bundle", "vm_name"),
    "HostSystem": ("host_investigation_bundle", "host_name"),
    "Datastore": ("datastore_investigation_bundle", "datastore_name"),
    "ClusterComputeResource": ("cluster_health_summary", "cluster_filter"),
}


def _handoff(kind: str, name: str) -> dict[str, Any]:
    """Build the next_step block for a confirmed resource."""
    mapping = _MONITOR_HANDOFF.get(kind)
    if mapping is None:
        return {
            "skill": "vmware-monitor",
            "tool": None,
            "args": {},
            "note": (
                f"No vmware-monitor investigation tool covers resource kind "
                f"'{kind}'. This object is monitored by Aria only — use "
                f"get_resource_metrics or get_resource_health on resource_id "
                f"instead of querying vCenter."
            ),
        }
    tool, arg = mapping
    return {
        "skill": "vmware-monitor",
        "tool": tool,
        "args": {arg: name},
        "note": (
            f"Resource name and kind are confirmed. Call {tool} on the "
            f"vmware-monitor skill with {arg}='{name}' to pull the vCenter-side "
            f"picture for this alert."
        ),
    }


def investigate_alert(client: Any, alert_id: str) -> dict[str, Any]:
    """Resolve one alert to its affected resource, confirmed and ready to hand off.

    Replaces the alert → resource_id → resource → vCenter sequence that agents
    otherwise stitch together by hand.

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: The alert UUID (from ``list_alerts``). This is *not* the
            resource UUID — the two are different objects and the returned
            ``correlation`` block labels each explicitly.

    Returns:
        Dict with five always-present keys:

        * ``alert`` — the full alert, with Aria's own criticality / status /
          impact / control-state values passed through verbatim.
        * ``resource`` — the affected resource, or ``None`` if it could not be
          resolved.
        * ``correlation`` — both UUIDs, the confirmed name and kind, and a
          ``confirmed`` flag. Every key is always present; unresolved values are
          explicit ``None`` rather than omitted.
        * ``next_step`` — which vmware-monitor tool to call next and with what
          argument, or ``None`` when there is no resource to hand off.
        * ``warnings`` — why anything above is incomplete; empty on success.

    A resource that cannot be fetched degrades to a warning and explicit nulls,
    keeping the alert that was already retrieved, rather than raising and losing
    it.
    """
    if not alert_id:
        raise ValueError(
            "alert_id must be a non-empty Aria alert UUID. Run list_alerts to see "
            "open alerts and copy an exact 'id' — note that the alert UUID is not "
            "the affected resource UUID."
        )

    alert = get_alert(client, alert_id)
    warnings: list[str] = []

    resource_id: Optional[str] = alert.get("resource_id") or None
    resource: Optional[dict] = None

    if resource_id is None:
        warnings.append(
            "This alert names no affected resource (resourceId is empty), so it "
            "cannot be correlated with vCenter inventory. Alert-definition-level "
            "alerts and some platform alerts have no resource."
        )
    else:
        try:
            resource = get_resource(client, resource_id)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            warnings.append(
                f"Affected resource {resource_id} could not be resolved: {exc} "
                f"The alert details above are still accurate."
            )

    name: Optional[str] = (resource or {}).get("name") or None
    kind: Optional[str] = (resource or {}).get("kind") or None
    confirmed = bool(name and kind)

    if resource is not None and not confirmed:
        warnings.append(
            f"Resource {resource_id} resolved but is missing its name or kind, "
            f"so correlation is unconfirmed. Do not match it against vCenter "
            f"inventory by guesswork."
        )

    return {
        "alert": alert,
        "resource": resource,
        "correlation": {
            "alert_id": alert.get("id") or alert_id,
            "resource_id": resource_id,
            "resource_name": name,
            "resource_kind": kind,
            "confirmed": confirmed,
        },
        "next_step": _handoff(kind, name) if confirmed else None,
        "warnings": warnings,
    }

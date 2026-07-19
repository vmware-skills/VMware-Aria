"""alert_investigate: resolve an alert to its affected resource in one call.

Source: VMware-AIops issue #31 (juanpf-ha). Driving the family with a local
Llama 3.3 70B, the operator had to hand-write three separate prompt guardrails
to make alert→resource correlation work:

    * "When processing an Aria alert, first resolve the affected resource_id
       through VMware-Aria before querying VMware-Monitor."
    * "Do not confuse the alert ID with the affected resource ID."
    * "Only correlate Aria and vCenter data after the resource name and type
       have been confirmed."

All three describe bookkeeping the tool should do, not the model. This suite
pins that ``investigate_alert`` does it: both IDs come back explicitly
labelled, the resource name and kind are confirmed before any handoff is
suggested, and a failure to resolve degrades to a warning plus explicit nulls
rather than a crash or a silently missing field.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from vmware_aria.connection import AriaApiError
from vmware_aria.ops.investigate import investigate_alert

ALERT_ID = "11111111-1111-1111-1111-111111111111"
RESOURCE_ID = "22222222-2222-2222-2222-222222222222"


def _alert_payload(resource_id: str = RESOURCE_ID) -> dict:
    return {
        "alertId": ALERT_ID,
        "alertDefinitionName": "Virtual machine has memory contention",
        "alertLevel": "CRITICAL",
        "status": "ACTIVE",
        "alertImpact": "RISK",
        "resourceId": resource_id,
        "controlState": "OPEN",
        "startTimeUTC": 1_700_000_000_000,
        "alertDefinitionId": "AlertDefinition-abc",
    }


def _resource_payload(kind: str = "VirtualMachine", name: str = "web-01") -> dict:
    return {
        "identifier": RESOURCE_ID,
        "resourceKey": {
            "name": name,
            "resourceKindKey": kind,
            "adapterKindKey": "VMWARE",
            "resourceIdentifiers": [],
        },
        "badges": [{"type": "HEALTH", "color": "RED", "score": 25}],
        "resourceStatusStates": [
            {"resourceState": "STARTED", "resourceStatus": "DATA_RECEIVING"}
        ],
    }


def _client(alert: dict | None = None, resource=None) -> MagicMock:
    """Route /alerts/<id> and /resources/<id> to their payloads."""
    client = MagicMock(name="AriaClient")

    def _get(path: str, *args, **kwargs):
        if path.startswith("/alerts/") and "contributingsymptoms" not in path:
            return _alert_payload() if alert is None else alert
        if path.startswith("/resources/"):
            if isinstance(resource, Exception):
                raise resource
            return _resource_payload() if resource is None else resource
        return {}

    client.get.side_effect = _get
    return client


# ---------------------------------------------------------------------------
# Correlation bookkeeping — the three hand-written guardrails
# ---------------------------------------------------------------------------


def test_resolves_resource_from_alert_in_one_call():
    result = investigate_alert(_client(), ALERT_ID)
    assert result["resource"]["name"] == "web-01"
    assert result["resource"]["kind"] == "VirtualMachine"


def test_both_ids_are_present_and_distinctly_labelled():
    """The model must never have to infer which UUID is which."""
    correlation = investigate_alert(_client(), ALERT_ID)["correlation"]
    assert correlation["alert_id"] == ALERT_ID
    assert correlation["resource_id"] == RESOURCE_ID
    assert correlation["alert_id"] != correlation["resource_id"]


def test_correlation_confirmed_only_with_name_and_kind():
    correlation = investigate_alert(_client(), ALERT_ID)["correlation"]
    assert correlation["confirmed"] is True
    assert correlation["resource_name"] == "web-01"
    assert correlation["resource_kind"] == "VirtualMachine"


def test_correlation_keys_always_present_even_when_unresolved():
    """Explicit nulls, never missing keys — a missing key invites invention."""
    client = _client(alert=_alert_payload(resource_id=""))
    correlation = investigate_alert(client, ALERT_ID)["correlation"]
    for key in ("alert_id", "resource_id", "resource_name", "resource_kind", "confirmed"):
        assert key in correlation
    assert correlation["resource_id"] is None
    assert correlation["resource_name"] is None
    assert correlation["confirmed"] is False


# ---------------------------------------------------------------------------
# Handoff — tell the model exactly what to call next
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "tool", "arg"),
    [
        ("VirtualMachine", "vm_investigation_bundle", "vm_name"),
        ("HostSystem", "host_investigation_bundle", "host_name"),
        ("Datastore", "datastore_investigation_bundle", "datastore_name"),
    ],
)
def test_next_step_targets_the_matching_monitor_bundle(kind, tool, arg):
    client = _client(resource=_resource_payload(kind=kind, name="thing-01"))
    next_step = investigate_alert(client, ALERT_ID)["next_step"]
    assert next_step["skill"] == "vmware-monitor"
    assert next_step["tool"] == tool
    assert next_step["args"] == {arg: "thing-01"}


def test_next_step_for_cluster_uses_health_summary():
    client = _client(resource=_resource_payload(kind="ClusterComputeResource", name="prod"))
    next_step = investigate_alert(client, ALERT_ID)["next_step"]
    assert next_step["tool"] == "cluster_health_summary"
    assert next_step["args"] == {"cluster_filter": "prod"}


def test_unmappable_kind_gives_no_tool_but_explains_why():
    client = _client(resource=_resource_payload(kind="NSXTAdapterInstance", name="nsx-1"))
    next_step = investigate_alert(client, ALERT_ID)["next_step"]
    assert next_step["tool"] is None
    assert "NSXTAdapterInstance" in next_step["note"]


def test_next_step_is_null_when_resource_unresolved():
    client = _client(alert=_alert_payload(resource_id=""))
    assert investigate_alert(client, ALERT_ID)["next_step"] is None


# ---------------------------------------------------------------------------
# Degradation — partial results beat crashes and beat silence
# ---------------------------------------------------------------------------


def test_alert_without_resource_warns_and_returns_alert_anyway():
    client = _client(alert=_alert_payload(resource_id=""))
    result = investigate_alert(client, ALERT_ID)
    assert result["alert"]["id"] == ALERT_ID
    assert result["resource"] is None
    assert any("no affected resource" in w.lower() for w in result["warnings"])


def test_unresolvable_resource_degrades_to_warning_not_exception():
    """A 404 on the resource must not lose the alert we already fetched."""
    err = AriaApiError("404: resource not found. List the parent collection first.")
    client = _client(resource=err)
    result = investigate_alert(client, ALERT_ID)
    assert result["alert"]["criticality"] == "CRITICAL"
    assert result["resource"] is None
    assert result["correlation"]["confirmed"] is False
    assert any("404" in w for w in result["warnings"])


def test_warnings_key_always_present_and_empty_on_success():
    assert investigate_alert(_client(), ALERT_ID)["warnings"] == []


def test_empty_alert_id_rejected():
    with pytest.raises(ValueError):
        investigate_alert(_client(), "")


# ---------------------------------------------------------------------------
# Fidelity — Aria's own values must survive the aggregation untouched
# ---------------------------------------------------------------------------


def test_aria_enum_values_are_passed_through_verbatim():
    """The operator's guardrail: "Keep the exact criticality, status, impact
    and control-state values returned by Aria Operations"."""
    result = investigate_alert(_client(), ALERT_ID)
    assert result["alert"]["criticality"] == "CRITICAL"
    assert result["alert"]["status"] == "ACTIVE"
    assert result["alert"]["alert_impact"] == "RISK"
    assert result["alert"]["control_state"] == "OPEN"

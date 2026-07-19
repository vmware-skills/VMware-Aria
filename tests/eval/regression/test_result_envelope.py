"""Every list tool must state its truncation instead of leaving it inferable.

Source: VMware-AIops issue #31 (juanpf-ha). Running the family against a local
Llama 3.3 70B, the operator reported that "with long tool responses, it may
omit existing information or incorrectly state that no data was returned."

A bare ``list[dict]`` gives a model no way to tell a complete answer from a
page-one answer, so it guesses — and the guess that reads "no data returned"
looks like a finding. Every read tool listed in SKILL.md now returns the
family envelope (``vmware_policy.paginated``), so ``returned``/``limit``/
``total``/``truncated`` are stated rather than inferred.

These tests pin, per tool:

* the six envelope keys are always present (a missing key invites invention);
* a page filled to the limit is flagged ``truncated`` — conservatively when no
  total is known, because a full page cannot be told from a capped one;
* a short page is not flagged, and carries no hint;
* where the suite-api genuinely reports a collection size, a full page that
  matches it is NOT flagged — a known total removes the ambiguity, so agents
  are not sent back for a redundant second query.

The totals used here are real wire fields, not invented ones (踩坑 #36):
``pageInfo.totalCount`` on GET /resources and on the paged definition
collections. Endpoints that report no count keep ``total=None``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

ENVELOPE_KEYS = ("items", "returned", "limit", "total", "truncated", "hint")


def _client() -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.get.return_value = {}
    client.post.return_value = {}
    return client


def _resource_rows(n: int, start: int = 0) -> list[dict]:
    return [
        {
            "identifier": f"vm-{i}",
            "resourceKey": {"name": f"vm-{i}", "resourceKindKey": "VirtualMachine"},
        }
        for i in range(start, start + n)
    ]


# ---------------------------------------------------------------------------
# Every listed read tool returns the envelope
# ---------------------------------------------------------------------------


def _all_list_results() -> dict[str, dict]:
    """Call each enveloped ops function once against a canned client."""
    from vmware_aria.ops.alerts import (
        list_alert_definitions,
        list_alerts,
        list_symptom_definitions,
    )
    from vmware_aria.ops.anomaly import list_anomalies
    from vmware_aria.ops.capacity import list_rightsizing_recommendations
    from vmware_aria.ops.health import list_collector_groups
    from vmware_aria.ops.reports import list_report_definitions, list_reports
    from vmware_aria.ops.resources import get_top_consumers, list_resources

    results: dict[str, dict] = {}

    c = _client()
    c.get.return_value = {"resourceList": _resource_rows(3)}
    results["list_resources"] = list_resources(c)

    c = _client()
    c.get.side_effect = [
        {"resourceList": _resource_rows(3)},
        {"resourceStatGroups": []},
    ]
    results["get_top_consumers"] = get_top_consumers(c)

    c = _client()
    c.post.return_value = {"alerts": [{"alertId": "a-1"}]}
    results["list_alerts"] = list_alerts(c)

    c = _client()
    c.get.return_value = {"alertDefinitions": [{"id": "ad-1", "name": "def"}]}
    results["list_alert_definitions"] = list_alert_definitions(c)

    c = _client()
    c.get.return_value = {"symptomDefinitions": [{"id": "sd-1", "name": "sym"}]}
    results["list_symptom_definitions"] = list_symptom_definitions(c)

    c = _client()
    c.get.return_value = {"resourceList": _resource_rows(3)}
    c.post.return_value = {"values": []}
    results["list_anomalies"] = list_anomalies(c)

    c = _client()
    c.get.return_value = {"resourceList": _resource_rows(3)}
    c.post.return_value = {"values": []}
    results["list_rightsizing_recommendations"] = list_rightsizing_recommendations(c)

    c = _client()
    c.get.return_value = {"collectorGroups": [{"id": "cg-1", "name": "g"}]}
    results["list_collector_groups"] = list_collector_groups(c)

    c = _client()
    c.get.return_value = {"reportDefinitions": [{"id": "rd-1", "name": "r"}]}
    results["list_report_definitions"] = list_report_definitions(c)

    c = _client()
    c.get.return_value = {"reports": [{"id": "r-1", "name": "r"}]}
    results["list_reports"] = list_reports(c)

    return results


ALL_RESULTS = _all_list_results()
TOOL_NAMES = sorted(ALL_RESULTS)


def test_every_declared_list_tool_is_covered() -> None:
    """SKILL.md declares these ten read tools as list-returning."""
    assert TOOL_NAMES == [
        "get_top_consumers",
        "list_alert_definitions",
        "list_alerts",
        "list_anomalies",
        "list_collector_groups",
        "list_report_definitions",
        "list_reports",
        "list_resources",
        "list_rightsizing_recommendations",
        "list_symptom_definitions",
    ]


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_result_is_an_envelope_not_a_bare_list(tool: str) -> None:
    result = ALL_RESULTS[tool]
    assert isinstance(result, dict), f"{tool} still returns a bare list"
    assert isinstance(result["items"], list)


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_all_six_envelope_keys_are_always_present(tool: str) -> None:
    """Explicit nulls, never missing keys — a missing key invites invention."""
    result = ALL_RESULTS[tool]
    for key in ENVELOPE_KEYS:
        assert key in result, f"{tool} envelope is missing '{key}'"


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_returned_matches_the_row_count(tool: str) -> None:
    result = ALL_RESULTS[tool]
    assert result["returned"] == len(result["items"])


# ---------------------------------------------------------------------------
# Truncation: full page flagged, short page not
# ---------------------------------------------------------------------------


def test_full_page_of_alerts_is_flagged_truncated() -> None:
    """POST /alerts/query reports no count — a full page may hide more."""
    from vmware_aria.ops.alerts import list_alerts

    client = _client()
    client.post.return_value = {"alerts": [{"alertId": f"a-{i}"} for i in range(10)]}
    result = list_alerts(client, limit=10)

    assert result["returned"] == 10
    assert result["total"] is None, "no collection size on the wire — do not invent one"
    assert result["truncated"] is True
    assert "limit" in result["hint"].lower()


def test_short_page_of_alerts_is_complete_and_carries_no_hint() -> None:
    from vmware_aria.ops.alerts import list_alerts

    client = _client()
    client.post.return_value = {"alerts": [{"alertId": f"a-{i}"} for i in range(3)]}
    result = list_alerts(client, limit=10)

    assert result["truncated"] is False
    assert result["hint"] is None


def test_empty_result_is_complete_not_truncated() -> None:
    """"No alerts" must read as an answer, not as a suppressed page."""
    from vmware_aria.ops.alerts import list_alerts

    client = _client()
    client.post.return_value = {"alerts": []}
    result = list_alerts(client, limit=10)

    assert result["items"] == []
    assert result["returned"] == 0
    assert result["truncated"] is False
    assert result["hint"] is None


def test_unpaged_collection_states_that_it_is_complete() -> None:
    """list_collector_groups takes no limit — truncated=False is the answer."""
    from vmware_aria.ops.health import list_collector_groups

    client = _client()
    client.get.side_effect = lambda path, params=None: (
        {"collectorGroups": [{"id": f"cg-{i}", "name": f"g{i}"} for i in range(4)]}
        if path == "/collectorgroups"
        else {"collector": []}
    )
    result = list_collector_groups(client)

    assert result["returned"] == 4
    assert result["limit"] is None
    assert result["truncated"] is False
    assert result["hint"] is None


# ---------------------------------------------------------------------------
# Real totals: pageInfo.totalCount (GET /resources, paged definitions)
# ---------------------------------------------------------------------------


def test_full_page_matching_a_known_total_is_not_truncated() -> None:
    """totalCount removes the ambiguity a full page would otherwise create."""
    from vmware_aria.ops.resources import list_resources

    client = _client()
    client.get.return_value = {
        "resourceList": _resource_rows(50),
        "pageInfo": {"totalCount": 50, "page": 0, "pageSize": 1000},
    }
    result = list_resources(client, limit=50)

    assert result["returned"] == 50
    assert result["total"] == 50, "totalCount is a real suite-api wire field"
    assert result["truncated"] is False, (
        "a full page that matches the server's totalCount is complete — "
        "flagging it sends the agent back for a redundant query"
    )
    assert result["hint"] is None


def test_full_page_short_of_a_known_total_is_truncated_with_exact_numbers() -> None:
    from vmware_aria.ops.resources import list_resources

    client = _client()
    client.get.return_value = {
        "resourceList": _resource_rows(1000),
        "pageInfo": {"totalCount": 4000, "page": 0, "pageSize": 1000},
    }
    result = list_resources(client, limit=50)

    assert result["returned"] == 50
    assert result["total"] == 4000
    assert result["truncated"] is True
    assert "50" in result["hint"] and "4000" in result["hint"]


def test_client_side_name_filter_suppresses_the_unfiltered_total() -> None:
    """totalCount counts the whole kind, not the filtered matches."""
    from vmware_aria.ops.resources import list_resources

    client = _client()
    client.get.return_value = {
        "resourceList": _resource_rows(3),
        "pageInfo": {"totalCount": 3, "page": 0, "pageSize": 1000},
    }
    result = list_resources(client, name_filter="vm-1")

    assert result["returned"] == 1
    assert result["total"] is None, (
        "reporting the unfiltered totalCount alongside filtered rows would "
        "describe a collection the caller did not ask for"
    )


def test_definition_collection_total_comes_from_page_info() -> None:
    """iter_collection abandons the generator early; the count must survive."""
    from vmware_aria.ops.alerts import list_alert_definitions

    client = _client()
    client.get.return_value = {
        "alertDefinitions": [{"id": f"ad-{i}", "name": f"def-{i}"} for i in range(5)],
        "pageInfo": {"totalCount": 5, "page": 0, "pageSize": 500},
    }
    result = list_alert_definitions(client, limit=5)

    assert result["returned"] == 5
    assert result["total"] == 5
    assert result["truncated"] is False


def test_report_total_counts_every_match_because_the_endpoint_is_unpaged() -> None:
    """GET /reports returns everything, so the matched count is exact."""
    from vmware_aria.ops.reports import list_reports

    client = _client()
    client.get.return_value = {
        "reports": [{"id": f"r-{i}", "reportDefinitionId": "want"} for i in range(9)]
    }
    result = list_reports(client, definition_id="want", limit=4)

    assert result["returned"] == 4
    assert result["total"] == 9, "the whole matching set was in hand"
    assert result["truncated"] is True
    assert "9" in result["hint"]


# ---------------------------------------------------------------------------
# Scan-style tools: a short list is not proof the environment is clean
# ---------------------------------------------------------------------------


def test_capped_anomaly_scan_reports_the_unscanned_remainder() -> None:
    """Only flagged VMs come back, so a short list cannot mean "complete"."""
    from vmware_aria.ops.anomaly import list_anomalies

    client = _client()
    client.get.return_value = {
        "resourceList": _resource_rows(10),
        "pageInfo": {"totalCount": 4000, "page": 0, "pageSize": 10},
    }
    client.post.return_value = {"values": []}
    result = list_anomalies(client, limit=10)

    assert result["scanned"] == 10
    assert result["total"] == 4000
    assert result["truncated"] is True, (
        "3990 VMs went unscanned — any of them could be anomalous"
    )


def test_complete_anomaly_scan_is_not_flagged_truncated() -> None:
    from vmware_aria.ops.anomaly import list_anomalies

    client = _client()
    client.get.return_value = {
        "resourceList": _resource_rows(6),
        "pageInfo": {"totalCount": 6, "page": 0, "pageSize": 50},
    }
    client.post.return_value = {"values": []}
    result = list_anomalies(client, limit=50)

    assert result["scanned"] == 6
    assert result["total"] is None, "every VM was scanned — nothing is behind this"
    assert result["truncated"] is False


def test_rightsizing_evaluation_reports_the_vm_total() -> None:
    """One row per VM evaluated, so totalCount describes the same collection."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _client()
    client.get.return_value = {
        "resourceList": _resource_rows(20),
        "pageInfo": {"totalCount": 350, "page": 0, "pageSize": 20},
    }
    client.post.return_value = {"values": []}
    result = list_rightsizing_recommendations(client, limit=20)

    assert result["returned"] == 20
    assert result["total"] == 350
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


def test_mcp_list_tools_declare_object_results_not_arrays() -> None:
    """The annotation drives the schema an agent is handed."""
    import inspect

    from mcp_server.tools import alerts, anomaly, capacity, health, reports, resources

    modules = (alerts, anomaly, capacity, health, reports, resources)
    checked = []
    for module in modules:
        for name in TOOL_NAMES:
            fn = getattr(module, name, None)
            if fn is None:
                continue
            # Unwrap the @mcp.tool / @vmware_tool decorators.
            target = inspect.unwrap(fn)
            annotation = inspect.signature(target).return_annotation
            assert annotation is dict, (
                f"{module.__name__}.{name} must return the envelope dict, "
                f"got {annotation!r}"
            )
            checked.append(name)

    assert sorted(checked) == TOOL_NAMES, (
        "every list tool must be reachable on its module — a renamed or moved "
        "tool would otherwise pass this test vacuously"
    )

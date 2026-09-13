"""Spec-conformance shape regressions — 2026-06-08 second verification pass.

Every test pins a response/request SHAPE verified against the official
VMware/Broadcom suite-api documentation (vROps 8.6 spec index). The first
pass (test_aria_specific.py) fixed invented endpoints; this pass fixes
invented FIELD shapes: badges[] vs badge{}, alertLevel vs criticality,
subject string-array vs object, vRealizeOpsToken header, group-level
capacity percentage, total_alarms metric key, collectorId int array, and
completionTime.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock


def _client() -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.get.return_value = {}
    client.post.return_value = {}
    client.put.return_value = {}
    return client


# ── C1: empty resourceStatusStates must not IndexError ─────────────────


def test_list_resources_handles_empty_status_states() -> None:
    from vmware_aria.ops.resources import list_resources

    client = _client()
    client.get.return_value = {
        "resourceList": [
            {
                "identifier": "vm-1",
                "resourceKey": {"name": "web-01", "resourceKindKey": "VirtualMachine"},
                "resourceStatusStates": [],  # key present, list empty
            }
        ]
    }
    results = list_resources(client)["items"]
    assert results[0]["status"] == ""


# ── H1: badges is an ARRAY of {type, color, score}, not badge{} ────────


def test_list_resources_parses_badges_array() -> None:
    from vmware_aria.ops.resources import list_resources

    client = _client()
    client.get.return_value = {
        "resourceList": [
            {
                "identifier": "vm-1",
                "resourceKey": {"name": "web-01", "resourceKindKey": "VirtualMachine"},
                "badges": [
                    {"type": "HEALTH", "color": "GREEN", "score": 100.0},
                    {"type": "RISK", "color": "RED", "score": 75.0},
                ],
                "resourceStatusStates": [{"resourceState": "STARTED"}],
            }
        ]
    }
    results = list_resources(client)["items"]
    assert results[0]["health_color"] == "GREEN"
    assert results[0]["health_score"] == 100.0


def test_get_resource_parses_badges_array() -> None:
    from vmware_aria.ops.resources import get_resource

    client = _client()
    client.get.return_value = {
        "identifier": "vm-1",
        "resourceKey": {"name": "web-01", "resourceKindKey": "VirtualMachine"},
        "badges": [
            {"type": "HEALTH", "color": "GREEN", "score": 100.0},
            {"type": "RISK", "color": "YELLOW", "score": 50.0},
            {"type": "EFFICIENCY", "color": "RED", "score": 25.0},
        ],
    }
    result = get_resource(client, "vm-1")
    assert result["health_color"] == "GREEN" and result["health_score"] == 100.0
    assert result["risk_color"] == "YELLOW" and result["risk_score"] == 50.0
    assert result["efficiency_color"] == "RED" and result["efficiency_score"] == 25.0


# ── C2: ReportDefinition subject is an array of strings ────────────────


def test_report_definition_subject_is_string_array() -> None:
    from vmware_aria.ops.reports import list_report_definitions

    client = _client()
    client.get.return_value = {
        "reportDefinitions": [
            {
                "id": "rd-1",
                "name": "Capacity Report",
                "subject": ["VirtualMachine", "HostSystem"],
            }
        ]
    }
    results = list_report_definitions(client)["items"]
    assert results[0]["subject_type"] == "VirtualMachine, HostSystem"

    # subject absent / null must not blow up either
    client.get.return_value = {"reportDefinitions": [{"id": "rd-2", "name": "X", "subject": None}]}
    assert list_report_definitions(client)["items"][0]["subject_type"] == ""


# ── C3: Authorization header literal is vRealizeOpsToken ───────────────


def _fake_aria_client(monkeypatch, post_recorder=None):
    from vmware_aria.config import TargetConfig
    from vmware_aria.connection import AriaClient

    expiry_epoch_ms = int((time.time() + 6 * 3600) * 1000)

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "tok-123", "validity": expiry_epoch_ms}

    def fake_post(self, *args, **kwargs):
        if post_recorder is not None:
            post_recorder.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr("httpx.Client.post", fake_post)
    return AriaClient(TargetConfig(host="h", username="u"), "pw")


def test_auth_header_uses_vrealizeopstoken(monkeypatch) -> None:
    client = _fake_aria_client(monkeypatch)
    headers = client._headers()
    assert headers["Authorization"] == "vRealizeOpsToken tok-123", (
        "Authorization header must be 'vRealizeOpsToken <token>' — the bare "
        "'OpsToken' literal 401s on 8.6"
    )


# ── M5: token release takes no body ─────────────────────────────────────


def test_token_release_sends_no_body(monkeypatch) -> None:
    calls: list = []
    client = _fake_aria_client(monkeypatch, post_recorder=calls)
    monkeypatch.setattr("httpx.Client.close", lambda self: None)
    calls.clear()
    client.close()

    release_calls = [
        (a, k) for a, k in calls if any("token/release" in str(x) for x in a)
    ]
    assert release_calls, "close() must POST /auth/token/release"
    _, kwargs = release_calls[0]
    assert kwargs.get("json") is None, "POST /auth/token/release takes no body"


# ── H2: topn resourceStats data nests under stat ───────────────────────


def test_top_consumers_data_nests_under_stat() -> None:
    from vmware_aria.ops.resources import get_top_consumers

    client = _client()
    client.get.side_effect = [
        {"resourceList": [{"identifier": "vm-1", "resourceKey": {"name": "web-01"}}]},
        {
            "resourceStatGroups": [
                {
                    "groupKey": "vm-1",
                    "resourceStats": [
                        {
                            "resourceId": "vm-1",
                            "stat": {
                                "statKey": {"key": "cpu|usage_average"},
                                "timestamps": [1000],
                                "data": [42.0],
                            },
                        }
                    ],
                }
            ]
        },
    ]
    results = get_top_consumers(client, metric_key="cpu|usage_average", top_n=5)["items"]
    assert results[0]["value"] == 42.0, (
        "resourceStats[] elements are {resourceId, stat: {statKey, timestamps, "
        "data}} — data nests under stat"
    )


# ── M3: topn caps candidate resourceId list at 100 ─────────────────────


def test_top_consumers_caps_resource_ids_at_100() -> None:
    from vmware_aria.ops.resources import get_top_consumers

    client = _client()
    many = [
        {"identifier": f"vm-{i}", "resourceKey": {"name": f"vm-{i}"}}
        for i in range(150)
    ]
    client.get.side_effect = [{"resourceList": many}, {"resourceStatGroups": []}]
    get_top_consumers(client, top_n=5)

    topn_call = client.get.call_args_list[1]
    assert len(topn_call.kwargs["params"]["resourceId"]) <= 100, (
        "resourceId list must be capped at 100 to avoid HTTP 414"
    )


# ── H3: Alert fields — alertLevel + alertDefinitionName ────────────────

_ALERT_WIRE = {
    "alertId": "alert-1",
    "alertLevel": "CRITICAL",
    "alertDefinitionName": "VM CPU contention",
    "alertDefinitionId": "ad-1",
    "status": "ACTIVE",
    "alertImpact": "RISK",
    "resourceId": "res-1",
    "startTimeUTC": 1000,
    "updateTimeUTC": 2000,
    "cancelTimeUTC": 0,
    "controlState": "OPEN",
}


def test_list_alerts_uses_alert_level_and_definition_name() -> None:
    from vmware_aria.ops.alerts import list_alerts

    client = _client()
    client.post.return_value = {"alerts": [dict(_ALERT_WIRE)]}
    results = list_alerts(client)["items"]
    a = results[0]
    assert a["criticality"] == "CRITICAL", "criticality comes from alertLevel"
    assert a["name"] == "VM CPU contention", "name comes from alertDefinitionName"
    assert "resource_name" not in a, "Alert model has no resourceName field"
    assert "info" not in a, "Alert model has no info field"
    assert a["resource_id"] == "res-1"


def test_get_alert_fields_and_contributing_symptoms() -> None:
    from vmware_aria.ops.alerts import get_alert

    client = _client()

    def get_side(path, params=None):
        if path == "/alerts/alert-1":
            return dict(_ALERT_WIRE)
        if path == "/alerts/contributingsymptoms":
            assert params == {"id": "alert-1"}
            return {
                "symptoms": [
                    {
                        "id": "sym-1",
                        "message": "CPU usage above 90%",
                        "symptomCriticality": "CRITICAL",
                        "symptomDefinitionId": "sd-1",
                        "resourceId": "res-1",
                    }
                ]
            }
        raise AssertionError(f"unexpected GET {path}")

    client.get.side_effect = get_side
    result = get_alert(client, "alert-1")

    assert result["criticality"] == "CRITICAL"
    assert result["name"] == "VM CPU contention"
    assert "resource_name" not in result and "info" not in result
    assert "recommendations" not in result, (
        "alertRecommendationList does not exist — recommendations hang off "
        "the alert definition"
    )
    assert result["symptoms"], "symptoms must come from GET /alerts/contributingsymptoms"
    sym = result["symptoms"][0]
    assert sym["id"] == "sym-1"
    assert sym["severity"] == "CRITICAL"
    assert "CPU usage" in sym["name"]


def test_get_alert_survives_contributing_symptoms_failure() -> None:
    from vmware_aria.ops.alerts import get_alert

    client = _client()

    def get_side(path, params=None):
        if path == "/alerts/alert-1":
            return dict(_ALERT_WIRE)
        raise ConnectionError("boom")

    client.get.side_effect = get_side
    result = get_alert(client, "alert-1")
    assert result["symptoms"] == []


# ── H5: AlertDefinition has no top-level criticality/active ────────────


def test_alert_definitions_criticality_from_states() -> None:
    from vmware_aria.ops.alerts import list_alert_definitions

    client = _client()
    client.get.return_value = {
        "alertDefinitions": [
            {
                "id": "ad-1",
                "name": "Multi-state def",
                "adapterKindKey": "VMWARE",
                "resourceKindKey": "VirtualMachine",
                "type": 16,
                "subType": 19,
                "states": [
                    {"severity": "WARNING", "impact": {"impactType": "BADGE", "detail": "risk"}},
                    {"severity": "CRITICAL"},
                ],
            }
        ]
    }
    results = list_alert_definitions(client)["items"]
    d = results[0]
    assert d["criticality"] == "CRITICAL", "criticality = max severity across states[]"
    assert "enabled" not in d, "AlertDefinition has no top-level active field"
    assert d["impact"] == "BADGE", "impact read from states[0].impact.impactType"


def test_alert_definitions_top_level_impact_also_read() -> None:
    from vmware_aria.ops.alerts import list_alert_definitions

    client = _client()
    client.get.return_value = {
        "alertDefinitions": [
            {
                "id": "ad-2",
                "name": "Top-level impact",
                "impact": {"impactType": "HEALTH"},
                "states": [{"severity": "WARNING"}],
            }
        ]
    }
    assert list_alert_definitions(client)["items"][0]["impact"] == "HEALTH"


# ── M1 + H5: create_alert_definition body and response shape ───────────


def test_create_alert_definition_aggregation_all_or() -> None:
    from vmware_aria.ops.alerts import create_alert_definition

    client = _client()
    client.post.return_value = {"id": "new-def", "name": "n"}
    result = create_alert_definition(
        client, "n", "d", "VirtualMachine", symptom_definition_ids=["s1"]
    )
    sset = client.post.call_args.kwargs["json_data"]["states"][0]["base-symptom-set"]
    assert sset["aggregation"] == "ALL", "doc-sample-verified combination"
    assert sset["symptomSetOperator"] == "OR"
    assert "enabled" not in result, "response must not invent an active/enabled field"


# ── H6: symptomdefinitions query param is resourceKind ─────────────────


def test_symptom_definitions_param_is_resource_kind() -> None:
    from vmware_aria.ops.alerts import list_symptom_definitions

    client = _client()
    client.get.return_value = {"symptomDefinitions": []}
    list_symptom_definitions(client, resource_kind="VirtualMachine")
    params = client.get.call_args.kwargs["params"]
    assert params.get("resourceKind") == "VirtualMachine"
    assert "resourceKindKey" not in params


# ── H7: capacity percentage exists only at group level ─────────────────


def test_capacity_overview_uses_group_level_percentage() -> None:
    from vmware_aria.ops.capacity import get_capacity_overview

    client = _client()
    client.get.return_value = {"values": []}
    result = get_capacity_overview(client, "cl-1")

    keys = client.get.call_args.kwargs["params"]["statKey"]
    assert "OnlineCapacityAnalytics|capacityRemainingPercentage" in keys
    for key in keys:
        assert "demand|capacityRemainingPercentage" not in key, (
            f"per-dimension percentage key {key} does not exist"
        )
    assert "capacity_remaining_pct" in result
    dims = {d["dimension"] for d in result["dimensions"]}
    assert dims == {"cpu", "mem", "diskspace"}


def test_remaining_capacity_uses_group_level_percentage() -> None:
    from vmware_aria.ops.capacity import get_remaining_capacity

    client = _client()
    client.get.return_value = {"values": []}
    result = get_remaining_capacity(client, "cl-1")

    keys = client.get.call_args.kwargs["params"]["statKey"]
    assert "OnlineCapacityAnalytics|capacityRemainingPercentage" in keys
    for key in keys:
        assert "demand|capacityRemainingPercentage" not in key
    assert "capacity_remaining_pct" in result
    for entry in result["remaining_capacity"]:
        assert set(entry) == {"dimension", "remaining_value"}


# ── H8: rightsizing keys have no demand segment ────────────────────────


def test_rightsizing_keys_have_no_demand_segment() -> None:
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _client()
    # Rightsizing now uses the bulk POST /resources/stats/query (was a per-VM
    # GET /resources/{id}/stats/latest N+1); the statKey array is unchanged.
    client.post.return_value = {"values": []}
    list_rightsizing_recommendations(client, resource_id="vm-1")

    body = next(
        c.kwargs["json_data"] for c in client.post.call_args_list
        if c.args and c.args[0] == "/resources/stats/query"
    )  # the page also issues one bulk properties query; select the stats one
    assert [k for k in body["statKey"] if k.endswith("|recommendedSize")] == [
        "OnlineCapacityAnalytics|cpu|recommendedSize",
        "OnlineCapacityAnalytics|mem|recommendedSize",
        # Broadcom's Capacity Analytics list names three keys on the VM; the
        # third was missing here until 2026-09-07.
        "OnlineCapacityAnalytics|diskspace|recommendedSize",
    ]
    assert not [k for k in body["statKey"] if "|demand|" in k]
    assert body["resourceId"] == ["vm-1"]


# ── H9: anomaly metric wire key is total_alarms ────────────────────────


def test_anomaly_stat_key_is_total_alarms() -> None:
    from vmware_aria.ops.anomaly import list_anomalies

    client = _client()
    # list_anomalies now uses the bulk POST /resources/stats/query (was a
    # per-VM GET /resources/{id}/stats/latest N+1); the statKey is unchanged.
    client.post.return_value = {"values": []}
    results = list_anomalies(client, resource_id="res-1")["items"]

    assert client.post.call_args.kwargs["json_data"]["statKey"] == [
        "System Attributes|total_alarms"
    ], "'System Attributes|anomaly' does not exist — wire key is total_alarms"
    assert results[0]["metric_key"] == "System Attributes|total_alarms"


# ── H10: CollectorGroup has collectorId int array, enrich via /collectors


def test_collector_groups_parse_collector_id_array() -> None:
    from vmware_aria.ops.health import list_collector_groups

    client = _client()

    def get_side(path, params=None):
        if path == "/collectorgroups":
            return {
                "collectorGroups": [
                    {
                        "id": "cg-1",
                        "name": "Default group",
                        "description": "d",
                        "collectorId": [1, 2],
                        "systemDefined": True,
                    }
                ]
            }
        if path == "/collectors":
            return {
                "collector": [
                    {"id": 1, "name": "vrops-node-1", "state": "UP", "local": True},
                    {"id": 2, "name": "remote-col", "state": "DOWN", "local": False},
                ]
            }
        raise AssertionError(f"unexpected GET {path}")

    client.get.side_effect = get_side
    groups = list_collector_groups(client)["items"]

    g = groups[0]
    assert g["collector_count"] == 2, "collector_count = len(collectorId)"
    assert g["system_defined"] is True
    members = {c["id"]: c for c in g["collectors"]}
    assert members["1"]["name"] == "vrops-node-1"
    assert members["1"]["state"] == "UP"
    assert members["1"]["local"] is True
    assert members["2"]["state"] == "DOWN"
    for c in g["collectors"]:
        assert "type" not in c and "host" not in c, (
            "Collector model has no collectorType/hostname"
        )


def test_collector_groups_survive_collectors_failure() -> None:
    from vmware_aria.ops.health import list_collector_groups

    client = _client()

    def get_side(path, params=None):
        if path == "/collectorgroups":
            return {"collectorGroups": [{"id": "cg-1", "name": "g", "collectorId": [7]}]}
        raise ConnectionError("boom")

    client.get.side_effect = get_side
    groups = list_collector_groups(client)["items"]
    assert groups[0]["collector_count"] == 1
    assert groups[0]["collectors"][0]["id"] == "7"


# ── H11 + M4: reports completionTime + no pageSize, client-side limit ──


def test_reports_expose_completion_time() -> None:
    from vmware_aria.ops.reports import get_report, list_reports

    client = _client()
    client.get.return_value = {
        "reports": [{"id": "r1", "name": "n", "status": "COMPLETED", "completionTime": 1234}]
    }
    r = list_reports(client)["items"][0]
    assert r["completion_time_ms"] == 1234
    assert "generation_time_ms" not in r and "finish_time_ms" not in r

    client._base_url = "https://h:443/suite-api/api"
    client.get.return_value = {"id": "r1", "status": "COMPLETED", "completionTime": 5678}
    detail = get_report(client, "r1")
    assert detail["completion_time_ms"] == 5678
    assert "generation_time_ms" not in detail and "finish_time_ms" not in detail


def test_list_reports_no_page_size_param_and_client_side_limit() -> None:
    from vmware_aria.ops.reports import list_reports

    client = _client()
    client.get.return_value = {
        "reports": [{"id": f"r{i}", "reportDefinitionId": "want"} for i in range(5)]
    }
    results = list_reports(client, definition_id="want", limit=2)["items"]

    params = client.get.call_args.kwargs.get("params") or {}
    assert "pageSize" not in params, "GET /reports has no pageSize param"
    assert len(results) == 2, "limit must be applied client-side after the filter"


# ── H12: /deployment/node/status 503 is a health signal, not a crash ──
#
# 2026-06-09 user report (#6): `vmware-aria health status` aborted with a
# traceback when the node returned 503. The endpoint returns 503 while
# services are not ONLINE, so a health check must surface that as OFFLINE
# instead of propagating the error. The client now raises AriaApiError.


def _aria_api_error(code: int) -> Exception:
    from vmware_aria.connection import AriaApiError

    return AriaApiError(
        f"HTTP {code}", status_code=code, method="GET", path="/deployment/node/status"
    )


def test_get_aria_health_treats_503_as_offline() -> None:
    from vmware_aria.ops.health import get_aria_health

    client = _client()
    client.get.side_effect = _aria_api_error(503)

    result = get_aria_health(client)
    assert result["healthy"] is False
    assert result["overall_status"] == "OFFLINE"
    assert result["system_time_ms"] is None
    assert "503" in result["details"]


def test_get_aria_health_reraises_non_503_errors() -> None:
    import pytest

    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.health import get_aria_health

    client = _client()
    client.get.side_effect = _aria_api_error(500)

    with pytest.raises(AriaApiError):
        get_aria_health(client)


# ── #7: list_resources must follow pagination, not stop at one 500-row page ──


def _resource_rows(prefix: str, n: int) -> list[dict]:
    """n minimal ResourceDto rows with unique identifiers/names."""
    return [
        {
            "identifier": f"{prefix}-{i}",
            "resourceKey": {"name": f"{prefix}-{i}", "resourceKindKey": "VirtualMachine"},
        }
        for i in range(n)
    ]


def test_list_resources_follows_pagination_across_pages() -> None:
    """A 2-page response (500 + 300) must return all 800, not just 500.

    Root cause of the 2026-06-09 "maximum of 500 resources returned" report:
    the old code fetched a single page (pageSize=limit) and ignored pageInfo,
    so large environments were silently truncated.
    """
    from vmware_aria.ops.resources import list_resources

    client = _client()
    page0 = _resource_rows("vm", 1000)  # full page → there is a next page
    page1 = _resource_rows("vmx", 300)  # short page → last page
    client.get.side_effect = [
        {"resourceList": page0, "pageInfo": {"totalCount": 1300, "page": 0, "pageSize": 1000}},
        {"resourceList": page1, "pageInfo": {"totalCount": 1300, "page": 1, "pageSize": 1000}},
    ]

    results = list_resources(client)["items"]

    assert len(results) == 1300, "all pages must be accumulated, not just the first"
    # Successive pages requested with an incrementing 0-based `page` param.
    assert client.get.call_args_list[0].kwargs["params"]["page"] == 0
    assert client.get.call_args_list[1].kwargs["params"]["page"] == 1


def test_list_resources_terminates_on_totalcount_even_with_full_last_page() -> None:
    """If the last page is exactly pageSize but totalCount is reached, stop."""
    from vmware_aria.ops.resources import list_resources

    client = _client()
    page0 = _resource_rows("vm", 1000)
    page1 = _resource_rows("vmx", 1000)
    client.get.side_effect = [
        {"resourceList": page0, "pageInfo": {"totalCount": 2000, "page": 0, "pageSize": 1000}},
        {"resourceList": page1, "pageInfo": {"totalCount": 2000, "page": 1, "pageSize": 1000}},
    ]

    results = list_resources(client)["items"]

    assert len(results) == 2000
    assert client.get.call_count == 2, "must not request a 3rd page past totalCount"


def test_list_resources_explicit_limit_stops_early() -> None:
    """An explicit limit caps results and avoids extra page fetches."""
    from vmware_aria.ops.resources import list_resources

    client = _client()
    client.get.return_value = {
        "resourceList": _resource_rows("vm", 1000),
        "pageInfo": {"totalCount": 5000, "page": 0, "pageSize": 1000},
    }

    results = list_resources(client, limit=50)["items"]

    assert len(results) == 50
    assert client.get.call_count == 1, "limit satisfied on page 0 — no second fetch"


# ── Rightsizing: a published zero is not a recommended size ────────────


def _rightsizing_client(values: dict):
    """A client whose bulk stats query answers with `values` for one VM."""
    client = _client()
    client.get.return_value = {
        "resourceList": [{"identifier": "vm-1", "resourceKey": {"name": "web-01"}}],
        "pageInfo": {"totalCount": 1},
    }
    client.post.return_value = {
        "values": [
            {
                "resourceId": "vm-1",
                "stat-list": {
                    "stat": [
                        {"statKey": {"key": k}, "data": [v]} for k, v in values.items()
                    ]
                },
            }
        ]
    }
    return client


def test_a_published_zero_is_reclaimable_not_a_recommendation() -> None:
    """KB 379521: after 8.17 the engine publishes 0 continuously while it holds
    a VM to be reclaimable. Passing that through in a field called
    ``recommended_cpu`` tells the caller to size the VM down to nothing."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _rightsizing_client(
        {
            "OnlineCapacityAnalytics|cpu|recommendedSize": 0.0,
            "OnlineCapacityAnalytics|mem|recommendedSize": 0.0,
        }
    )
    row = list_rightsizing_recommendations(client)["items"][0]

    assert row["sizing_status"] == "reclaimable"
    assert row["recommended_cpu"] is None, "a zero must not surface as a size"
    assert row["recommended_memory"] is None


def test_nothing_published_says_so_rather_than_claiming_no_data() -> None:
    """A VM that needs no resizing publishes no metric, and so does one the
    analytics never scored. The appliance does not separate them, so the reply
    names the ambiguity instead of picking one."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    row = list_rightsizing_recommendations(_rightsizing_client({}))["items"][0]

    assert row["sizing_status"] == "none_published"
    assert row["recommended_cpu"] is None


def test_a_real_recommendation_still_comes_through() -> None:
    """The positive control. Without it the two tests above would keep passing
    with the whole query stubbed out."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _rightsizing_client(
        {
            "OnlineCapacityAnalytics|cpu|recommendedSize": 2.0,
            "OnlineCapacityAnalytics|diskspace|recommendedSize": 40960.0,
        }
    )
    row = list_rightsizing_recommendations(client)["items"][0]

    assert row["sizing_status"] == "recommendation"
    assert row["recommended_cpu"] == 2.0
    assert row["recommended_diskspace"] == 40960.0, "the third key must be read"


def test_a_size_beside_a_zero_is_reported_as_a_recommendation() -> None:
    """Declared precedence, not an accident. Reclaimable is a VM-level state per
    KB 379521, so a row with one dimension sized and another at zero should not
    occur — but the code cannot know that, so what it does is pinned."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _rightsizing_client(
        {
            "OnlineCapacityAnalytics|cpu|recommendedSize": 2.0,
            "OnlineCapacityAnalytics|mem|recommendedSize": 0.0,
        }
    )
    row = list_rightsizing_recommendations(client)["items"][0]

    assert row["sizing_status"] == "recommendation"
    assert row["recommended_cpu"] == 2.0
    assert row["recommended_memory"] is None, "the zero must not surface as a size"


def test_the_cli_renders_the_three_sizing_states_distinctly(monkeypatch) -> None:
    """The Monitor lesson from this same week, applied here before release: the
    payload learned three states and the CLI kept printing raw values, so a
    reclaimable VM and one with nothing published would both have rendered as an
    identical empty pair — "no data", the exact misreading sizing_status exists
    to prevent. And str(None) is the string 'None'.
    """
    from typer.testing import CliRunner

    from vmware_aria.cli import app

    c = _client()
    c.get.return_value = {
        "resourceList": [
            {"identifier": "vm-1", "resourceKey": {"name": "sized-01"}},
            {"identifier": "vm-2", "resourceKey": {"name": "idle-01"}},
            {"identifier": "vm-3", "resourceKey": {"name": "quiet-01"}},
        ],
        "pageInfo": {"totalCount": 3},
    }
    def st(k, v):
        return {"statKey": {"key": k}, "data": [v]}
    c.post.return_value = {"values": [
        {"resourceId": "vm-1", "stat-list": {"stat": [
            st("OnlineCapacityAnalytics|cpu|recommendedSize", 2.0)]}},
        {"resourceId": "vm-2", "stat-list": {"stat": [
            st("OnlineCapacityAnalytics|cpu|recommendedSize", 0.0)]}},
        {"resourceId": "vm-3", "stat-list": {"stat": []}},
    ]}
    monkeypatch.setattr(
        "vmware_aria.cli._get_connection", lambda target, config: (c, __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock())
    )

    result = CliRunner().invoke(app, ["capacity", "rightsizing"])
    assert result.exit_code == 0, result.output

    assert "reclaimable" in result.output, "the reclaimable state must be visible"
    assert "none published" in result.output, "so must the none-published state"
    assert "None" not in result.output, "str(None) leaking into the table"


def test_rightsizing_asks_for_a_day_wide_window() -> None:
    """The capacity engine publishes on its own cadence, which no document
    commits to. Behind the helper's 1-hour default, any cadence longer than an
    hour reads every VM as none_published on an estate that has
    recommendations — an empty table that looks like an answer. 25 hours covers
    a daily cycle; LATEST still returns one newest point.
    """
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _rightsizing_client({"OnlineCapacityAnalytics|cpu|recommendedSize": 2.0})
    list_rightsizing_recommendations(client, resource_id="vm-1")

    body = next(
        c.kwargs["json_data"] for c in client.post.call_args_list
        if c.args and c.args[0] == "/resources/stats/query"
    )  # the page also issues one bulk properties query; select the stats one
    assert body["end"] - body["begin"] == 25 * 3_600_000, (
        "the rightsizing stats window must span 25 hours"
    )

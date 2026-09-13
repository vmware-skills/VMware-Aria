"""An empty metric answer must say which of three different things happened.

2026-09-13, real Aria Operations 8.18.7. A powered-on VM had 55 stat keys and
no ``cpu|usage_average``. ``get_resource_metrics`` for that key returned a bare
``{}`` — the same answer as a key with no points in the window, and nothing
distinguished either from a wrong resource. The captured ``stats/query``
responses prove the API itself does not tell them apart: a nonexistent key
and an existing key over an empty window both come back as ``{"values": []}``.
Only ``GET /resources/{id}/statkeys`` can.

``get_top_consumers`` had the same shape: ``resourceStatGroups: []`` became
``items: []`` with ``hint: None``, which reads as "nothing is consuming".

A wrong resource id is already a 404 from the API and stays an error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.eval.regression._live_8187 import api_error, body

VM = "11111111-2222-4333-8444-555555555555"


def _client(query_answer, statkeys_answer=None) -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.post.return_value = query_answer
    if isinstance(statkeys_answer, BaseException):
        client.get.side_effect = statkeys_answer
    else:
        client.get.return_value = statkeys_answer
    return client


def _missing(result) -> dict:
    return {m["metric_key"]: m for m in result["missing"]}


@pytest.mark.unit
def test_keys_the_resource_never_reports_are_named_as_such():
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client(body("stats_query_mixed.json"), body("vm_statkeys.json"))
    result = get_resource_metrics(
        client, VM, ["cpu|usage_average", "mem|guest_demand", "nonexistent|bogus_key"]
    )

    assert list(result["metrics"]) == ["mem|guest_demand"]
    assert len(result["metrics"]["mem|guest_demand"]) == 12
    missing = _missing(result)
    assert missing["cpu|usage_average"]["reason"] == "not_collected_for_resource"
    assert missing["nonexistent|bogus_key"]["reason"] == "not_collected_for_resource"
    assert missing["cpu|usage_average"]["similar_keys"] == ["cpu|demandmhz", "cpu|effective_limit"], (
        "the keys this resource does have in the same group are the next thing to try"
    )
    assert result["stat_keys_on_resource"] == 55
    client.get.assert_called_once_with(f"/resources/{VM}/statkeys")


@pytest.mark.unit
def test_a_key_the_resource_has_but_with_no_points_is_a_window_problem():
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client(body("stats_query_no_values.json"), body("vm_statkeys.json"))
    result = get_resource_metrics(client, VM, ["mem|guest_demand"])

    assert result["metrics"] == {}
    assert _missing(result)["mem|guest_demand"]["reason"] == "no_data_in_window"


@pytest.mark.unit
def test_a_stat_entry_with_zero_points_is_missing_not_present():
    from vmware_aria.ops.resources import get_resource_metrics

    empty_stat = {"values": [{"resourceId": VM, "stat-list": {"stat": [
        {"statKey": {"key": "mem|guest_demand"}, "timestamps": [], "data": []}
    ]}}]}
    client = _client(empty_stat, body("vm_statkeys.json"))
    result = get_resource_metrics(client, VM, ["mem|guest_demand"])

    assert result["metrics"] == {}
    assert _missing(result)["mem|guest_demand"]["reason"] == "no_data_in_window"


@pytest.mark.unit
def test_a_resource_with_no_stat_keys_at_all_is_reported_as_that():
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client({"values": []}, {"stat-key": []})
    result = get_resource_metrics(client, VM, ["cpu|usage_average"])

    assert _missing(result)["cpu|usage_average"]["reason"] == "resource_reports_no_stat_keys"
    assert result["stat_keys_on_resource"] == 0


@pytest.mark.unit
def test_unreadable_stat_keys_leave_the_reason_undetermined_without_crashing():
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client({"values": []}, api_error(500, f"/resources/{VM}/statkeys"))
    result = get_resource_metrics(client, VM, ["cpu|usage_average"])

    entry = _missing(result)["cpu|usage_average"]
    assert entry["reason"] == "undetermined"
    assert "500" in entry["detail"]
    assert result["stat_keys_on_resource"] is None


@pytest.mark.unit
def test_a_complete_answer_costs_no_extra_call():
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client(body("stats_query_mixed.json"))
    result = get_resource_metrics(client, VM, ["mem|guest_demand"])

    assert result["missing"] == []
    client.get.assert_not_called()


# ── get_top_consumers ──────────────────────────────────────────────────────


def _candidates(n: int) -> dict:
    return {"resourceList": [
        {"identifier": f"vm-{i}", "resourceKey": {"name": f"vm-{i}"}} for i in range(n)
    ]}


@pytest.mark.unit
def test_an_empty_ranking_explains_itself():
    from vmware_aria.ops.resources import get_top_consumers

    client = MagicMock(name="AriaClient")
    client.get.side_effect = [_candidates(9), body("topn_no_groups.json")]
    result = get_top_consumers(client, metric_key="nonexistent|bogus_key", top_n=10)

    assert result["items"] == []
    assert result["hint"], "an empty ranking with no hint reads as 'nothing is consuming'"
    assert "nonexistent|bogus_key" in result["hint"]
    assert "9" in result["hint"]
    assert "get_resource_metrics" in result["hint"]


@pytest.mark.unit
def test_a_short_ranking_says_how_many_candidates_reported():
    from vmware_aria.ops.resources import get_top_consumers

    groups = {"resourceStatGroups": [
        {"groupKey": f"vm-{i}", "resourceStats": [{"resourceId": f"vm-{i}", "stat": {"data": [float(i)]}}]}
        for i in range(3)
    ]}
    client = MagicMock(name="AriaClient")
    client.get.side_effect = [_candidates(9), groups]
    result = get_top_consumers(client, metric_key="cpu|usage_average", top_n=10)

    assert result["returned"] == 3
    assert "3 of 9" in result["hint"]


@pytest.mark.unit
def test_no_candidates_says_the_kind_matched_nothing():
    from vmware_aria.ops.resources import get_top_consumers

    client = MagicMock(name="AriaClient")
    client.get.return_value = {"resourceList": []}
    result = get_top_consumers(client, resource_kind="HostSystem")

    assert result["items"] == []
    assert "HostSystem" in result["hint"]

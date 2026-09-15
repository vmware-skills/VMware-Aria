"""A "service is down" symptom must say which service.

Found 2026-09-15 on Aria Operations 8.18.7. ``alert get`` on the active
"vCenter app health is affected" alert listed two symptoms named "vCenter
appliance health service is down" with an empty ``resource_id`` and
``condition``. Which services were down took listing the VC_APP children and
reading ``SERVICE|AVAILABILITY`` on all eight.

The captured payloads settle where the id is. The contributing-symptom leaf
carries only ``symptomId`` / ``symptomSetId`` / ``symptomDefinitionsIds`` and an
empty ``alertConditions``. The symptom *instance* in ``GET /symptoms`` carries
``resourceId``, ``statKey`` and ``message`` — but the appliance ignores the
``id`` filter there (and in ``POST /symptoms/query``) and answers every
symptom, so the instances are found by walking the collection and keying rows
by their own id.

A symptom whose instance could not be read keeps an empty ``resource_id`` and
says so; it is never presented as a symptom with no resource.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.eval.regression._live_8187 import api_error, body

ALERT_ID = "ba793832-533a-4c87-bee4-217ed3747c85"
VC_APP = "31aa1b41-2ae3-479b-8e25-176016ad874e"
MEM = "ba13f0b7-19b5-4f30-b321-bc3a791d3497"
SYSTEM = "f2c18bac-d83f-4ccf-a7cb-ba5eda184486"
DEFINITION_ID = "SymptomDefinition-vCenterApplianceHealthServiceDownSymptom"
# Name and severity as `alert get` resolved them on the appliance (2026-09-15).
DEFINITION = {"id": DEFINITION_ID, "name": "vCenter appliance health service is down", "state": {"severity": "CRITICAL"}}


def _alert() -> dict:
    alert = copy.deepcopy(body("alert_vcapp_health.json"))
    alert.update({"alertId": ALERT_ID, "status": "ACTIVE", "cancelTimeUTC": 0,
                  "startTimeUTC": 1789287487717, "updateTimeUTC": 1789287487717})
    return alert


def _children_by_id() -> dict[str, dict]:
    return {r["identifier"]: r for r in body("vcapp_children.json")["resourceList"]}


def _resources_answer(params: Any) -> dict:
    wanted = params["resourceId"]
    children = _children_by_id()
    return {"resourceList": [children[i] for i in wanted if i in children]}


class _Client:
    def __init__(self, **overrides: Any) -> None:
        self.routes: dict[str, Any] = {
            f"/alerts/{ALERT_ID}": _alert(),
            "/alerts/contributingsymptoms": body("contributingsymptoms_vcapp_health.json"),
            "/symptomdefinitions": {"symptomDefinitions": [DEFINITION]},
            "/symptoms": body("symptoms_ignores_id_filter.json"),
            "/resources": _resources_answer,
        }
        self.routes.update(overrides)
        self.calls: list[tuple[str, Any]] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append((path, params))
        if path not in self.routes:
            raise AssertionError(f"unexpected GET {path}")
        answer = self.routes[path]
        if callable(answer):
            answer = answer(params)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def count(self, path: str) -> int:
        return sum(1 for p, _ in self.calls if p == path)


def _paged(rows: list[dict]) -> Any:
    """A /symptoms route that ignores the id filter and honours page/pageSize."""

    def answer(params: Any) -> dict:
        size, page = params["pageSize"], params["page"]
        return {"pageInfo": {"totalCount": len(rows), "page": page, "pageSize": size},
                "symptom": rows[page * size : (page + 1) * size]}

    return answer


def _captured_symptom_rows() -> list[dict]:
    return copy.deepcopy(body("symptoms_ignores_id_filter.json")["symptom"])


@pytest.mark.unit
def test_service_down_symptoms_name_the_services_that_are_down():
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_Client(), ALERT_ID)

    by_resource = {s["resource_id"]: s for s in result["symptoms"]}
    assert set(by_resource) == {MEM, SYSTEM}
    assert {s["resource_name"] for s in result["symptoms"]} == {"mem", "system"}
    for symptom in result["symptoms"]:
        assert symptom["resource_kind"] == "VCENTER_APPLIANCE_HEALTH_SERVICES"
        assert symptom["stat_key"] == "SERVICE|AVAILABILITY"
        assert symptom["condition"] == "HT not equal 0 != 1"
        assert symptom["resource_lookup"] == "resolved"
        assert symptom["name"] == "vCenter appliance health service is down"
    assert "symptom_resources_note" not in result


@pytest.mark.unit
def test_one_walk_and_one_name_lookup_per_alert_not_per_symptom():
    from vmware_aria.ops.alerts import get_alert

    client = _Client()
    get_alert(client, ALERT_ID)

    assert client.count("/symptoms") == 1
    assert client.count("/resources") == 1


@pytest.mark.unit
def test_a_failed_symptom_read_is_unknown_not_resourceless():
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_Client(**{"/symptoms": api_error(503, "/symptoms")}), ALERT_ID)

    for symptom in result["symptoms"]:
        assert symptom["resource_id"] == ""
        assert symptom["resource_lookup"] == "failed"
    note = result["symptom_resources_note"]
    assert "2" in note and "unknown" in note.lower()


@pytest.mark.unit
def test_instances_absent_from_a_complete_walk_are_not_found():
    from vmware_aria.ops.alerts import get_alert

    others = [r for r in _captured_symptom_rows() if r["resourceId"] not in (MEM, SYSTEM)]
    result = get_alert(_Client(**{"/symptoms": _paged(others)}), ALERT_ID)

    assert {s["resource_lookup"] for s in result["symptoms"]} == {"not_found"}
    assert "symptom_resources_note" in result


@pytest.mark.unit
def test_the_walk_pages_past_rows_nobody_asked_for(monkeypatch):
    from vmware_aria.ops import symptom_resources
    from vmware_aria.ops.alerts import get_alert

    monkeypatch.setattr(symptom_resources, "_SYMPTOM_PAGE_SIZE", 2)
    client = _Client(**{"/symptoms": _paged(_captured_symptom_rows())})  # wanted rows are on page 1
    result = get_alert(client, ALERT_ID)

    assert {s["resource_lookup"] for s in result["symptoms"]} == {"resolved"}
    assert client.count("/symptoms") == 2


@pytest.mark.unit
def test_the_walk_stops_once_every_instance_is_found(monkeypatch):
    from vmware_aria.ops import symptom_resources
    from vmware_aria.ops.alerts import get_alert

    monkeypatch.setattr(symptom_resources, "_SYMPTOM_PAGE_SIZE", 2)
    rows = list(reversed(_captured_symptom_rows()))  # wanted rows on page 0
    client = _Client(**{"/symptoms": _paged(rows)})
    get_alert(client, ALERT_ID)

    assert client.count("/symptoms") == 1


@pytest.mark.unit
def test_a_walk_cut_short_by_its_cap_is_failed_not_not_found(monkeypatch):
    from vmware_aria.ops import symptom_resources
    from vmware_aria.ops.alerts import get_alert

    monkeypatch.setattr(symptom_resources, "_SYMPTOM_PAGE_SIZE", 2)
    monkeypatch.setattr(symptom_resources, "_SYMPTOM_MAX_TOTAL", 2)
    result = get_alert(_Client(**{"/symptoms": _paged(_captured_symptom_rows())}), ALERT_ID)

    assert {s["resource_lookup"] for s in result["symptoms"]} == {"failed"}


@pytest.mark.unit
def test_a_failed_name_lookup_keeps_the_resource_id_and_says_the_name_is_unknown():
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_Client(**{"/resources": api_error(503, "/resources")}), ALERT_ID)

    assert {s["resource_id"] for s in result["symptoms"]} == {MEM, SYSTEM}
    assert {s["resource_name"] for s in result["symptoms"]} == {None}
    assert "symptom_resources_note" in result


@pytest.mark.unit
def test_symptoms_that_carry_their_resource_need_no_walk():
    from vmware_aria.ops.alerts import get_alert

    leaves = copy.deepcopy(body("contributingsymptoms_vcapp_health.json"))
    for leaf in leaves["contributingSymptoms"][0]["contributingSymptoms"]["contributingSymptoms"]:
        leaf["resourceId"] = MEM
    client = _Client(**{"/alerts/contributingsymptoms": leaves})
    result = get_alert(client, ALERT_ID)

    assert client.count("/symptoms") == 0
    assert {s["resource_lookup"] for s in result["symptoms"]} == {"not_needed"}
    assert {s["resource_name"] for s in result["symptoms"]} == {"mem"}


def _rows_by_resource() -> dict[str, dict]:
    return {r["resourceId"]: r for r in _captured_symptom_rows()}


def _capped(rows: list[dict], cap: int, total: bool = True) -> Any:
    """A /symptoms route whose server caps every page at ``cap`` rows, whatever pageSize asks."""

    def answer(params: Any) -> dict:
        page = params["page"]
        body_ = {"symptom": rows[page * cap : (page + 1) * cap]}
        if total:
            body_["pageInfo"] = {"totalCount": len(rows), "page": page, "pageSize": cap}
        return body_

    return answer


@pytest.mark.unit
def test_a_server_that_caps_page_size_is_walked_to_its_total_count():
    from vmware_aria.ops.alerts import get_alert

    by_resource = _rows_by_resource()
    others = [r for rid, r in by_resource.items() if rid not in (MEM, SYSTEM)]
    rows = others + [by_resource[MEM], by_resource[SYSTEM]]  # wanted rows on the second capped page
    client = _Client(**{"/symptoms": _capped(rows, cap=2)})
    result = get_alert(client, ALERT_ID)

    assert {s["resource_lookup"] for s in result["symptoms"]} == {"resolved"}
    assert client.count("/symptoms") == 2


@pytest.mark.unit
def test_without_a_total_count_a_symptom_not_seen_is_unknown_not_not_found():
    from vmware_aria.ops.alerts import get_alert

    others = [r for rid, r in _rows_by_resource().items() if rid not in (MEM, SYSTEM)]
    result = get_alert(_Client(**{"/symptoms": _capped(others, cap=2, total=False)}), ALERT_ID)

    assert {s["resource_lookup"] for s in result["symptoms"]} == {"failed"}


@pytest.mark.unit
def test_a_page_that_fails_midway_keeps_what_earlier_pages_found():
    from vmware_aria.ops.alerts import get_alert

    by_resource = _rows_by_resource()
    others = [r for rid, r in by_resource.items() if rid not in (MEM, SYSTEM)]
    page_zero = [by_resource[MEM], others[0]]

    def answer(params: Any) -> dict:
        if params["page"] == 0:
            return {"pageInfo": {"totalCount": 4}, "symptom": page_zero}
        raise api_error(503, "/symptoms")

    result = get_alert(_Client(**{"/symptoms": answer}), ALERT_ID)
    by_lookup = {s["resource_lookup"]: s for s in result["symptoms"]}

    assert set(by_lookup) == {"resolved", "failed"}
    assert by_lookup["resolved"]["resource_name"] == "mem"
    assert by_lookup["failed"]["resource_id"] == ""
    assert "symptom_resources_note" in result


class _WritingClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.posts: list[tuple[str, Any]] = []

    def post(self, path: str, json_data: Any = None, params: Any = None, **_kw: Any) -> Any:
        self.posts.append((path, params))
        return {}


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["acknowledge_alert", "cancel_alert"])
def test_writes_capture_before_state_without_walking_symptoms(operation):
    from vmware_aria.ops import alerts

    client = _WritingClient()
    getattr(alerts, operation)(client, ALERT_ID)

    assert client.posts, "the write itself must still be sent"
    assert client.count("/symptoms") == 0
    assert client.count("/resources") == 0


@pytest.mark.unit
def test_investigate_alert_carries_the_down_services():
    from vmware_aria.ops.investigate import investigate_alert

    client = _Client(**{f"/resources/{VC_APP}": {
        "identifier": VC_APP,
        "resourceKey": {"name": "vCenter-192.0.2.16", "resourceKindKey": "VC_APP", "adapterKindKey": "VMWARE_INFRA_HEALTH"},
    }})
    result = investigate_alert(client, ALERT_ID)

    assert {s["resource_name"] for s in result["alert"]["symptoms"]} == {"mem", "system"}

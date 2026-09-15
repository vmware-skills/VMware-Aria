"""A service object's green badge is not its service state.

Found 2026-09-15 on Aria Operations 8.18.7 monitoring vCenter 8.0.3. The
vCenter app object ("vCenter app health is affected", CRITICAL) has eight
``VCENTER_APPLIANCE_HEALTH_SERVICES`` children. For two of them, ``mem`` and
``system``, ``resource health`` answered HEALTH GREEN 100 while their
``SERVICE|STATUS`` property was ``orange`` and ``SERVICE|AVAILABILITY`` had been
0 for 72 hours. The badge scores the alerts attached to that object, and the
alert is raised on the parent, so the badge on the failing service stays green.

For a service-kind object the health answer now also carries the service's own
state. A state that could not be read is unknown, never "available".
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.eval.regression._live_8187 import api_error, body

MEM = "ba13f0b7-19b5-4f30-b321-bc3a791d3497"  # SERVICE|STATUS orange on the appliance
APPLMGMT = "878b9d70-e13b-4c62-b5c3-2f67a3256d1f"  # SERVICE|STATUS green
STATS = "/resources/stats/query"


def _properties(resource_id: str) -> dict:
    for row in body("properties_bulk_vcapp_services.json")["resourcePropertiesList"]:
        if row["resourceId"] == resource_id:
            return {"resourceId": resource_id, "property": row["property"]}
    raise AssertionError(f"no captured properties for {resource_id}")


def _resource(resource_id: str, name: str, kind: str = "VCENTER_APPLIANCE_HEALTH_SERVICES") -> dict:
    captured = copy.deepcopy(body("service_resource_mem.json"))
    captured["identifier"] = resource_id
    captured["resourceKey"]["name"] = name
    captured["resourceKey"]["resourceKindKey"] = kind
    return captured


def _availability(resource_id: str, *values: float) -> dict:
    """``POST /resources/stats/query`` body in the shape latest_stats_bulk parses."""
    if not values:
        return {"values": []}
    return {
        "values": [
            {
                "resourceId": resource_id,
                "stat-list": {
                    "stat": [
                        {
                            "statKey": {"key": "SERVICE|AVAILABILITY"},
                            "timestamps": [1789437000000 + i for i in range(len(values))],
                            "data": list(values),
                        }
                    ]
                },
            }
        ]
    }


class _Client:
    def __init__(self, gets: dict[str, Any], stats: Any = None) -> None:
        self.gets = gets
        self.stats = stats
        self.calls: list[tuple[str, str]] = []

    def _answer(self, answer: Any) -> Any:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(("GET", path))
        if path not in self.gets:
            raise AssertionError(f"unexpected GET {path}")
        return self._answer(self.gets[path])

    def post(self, path: str, json_data: Any = None, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(("POST", path))
        if path != STATS or self.stats is None:
            raise AssertionError(f"unexpected POST {path}")
        return self._answer(self.stats)


def _service_client(resource_id: str, name: str, *availability: float, **overrides: Any) -> _Client:
    gets = {
        f"/resources/{resource_id}": _resource(resource_id, name),
        f"/resources/{resource_id}/properties": _properties(resource_id),
    }
    gets.update(overrides.pop("gets", {}))
    stats = overrides.pop("stats", _availability(resource_id, *availability))
    return _Client(gets, stats)


@pytest.mark.unit
def test_a_down_service_is_not_reported_healthy_by_its_green_badge():
    from vmware_aria.ops.resources import get_resource_health

    result = get_resource_health(_service_client(MEM, "mem", 0.0, 0.0, 0.0), MEM)

    assert result["health_color"] == "GREEN" and result["health_score"] == 100.0
    service = result["service"]
    assert service["status"] == "orange"
    assert service["availability"] == 0.0
    assert service["available"] is False
    assert service["read_errors"] == []
    assert "badge" in service["note"].lower()
    assert result["kind"] == "VCENTER_APPLIANCE_HEALTH_SERVICES"
    assert result["name"] == "mem"


@pytest.mark.unit
def test_an_uppercase_id_reads_the_same_availability_as_the_lowercase_one():
    """Aria's ids are lowercase and latest_stats_bulk keys by them (2026-09-15 review:
    ``BA13F0B7-…`` answered available null with a false "no point" error)."""
    from vmware_aria.ops.resources import get_resource_health

    result = get_resource_health(_service_client(MEM, "mem", 0.0), MEM.upper())

    assert result["resource_id"] == MEM
    assert result["service"]["available"] is False
    assert result["service"]["read_errors"] == []


@pytest.mark.unit
def test_a_running_service_reads_available():
    from vmware_aria.ops.resources import get_resource_health

    result = get_resource_health(_service_client(APPLMGMT, "applmgmt", 1.0), APPLMGMT)

    assert result["service"]["status"] == "green"
    assert result["service"]["available"] is True


@pytest.mark.unit
def test_unreadable_properties_leave_status_unknown_not_green():
    from vmware_aria.ops.resources import get_resource_health

    failed = api_error(503, f"/resources/{MEM}/properties")
    client = _service_client(MEM, "mem", 0.0, gets={f"/resources/{MEM}/properties": failed})
    service = get_resource_health(client, MEM)["service"]

    assert service["status"] is None
    assert service["available"] is False  # availability was still read
    assert any("/properties" in e for e in service["read_errors"])


@pytest.mark.unit
def test_unreadable_availability_is_unknown_not_available():
    from vmware_aria.ops.resources import get_resource_health

    client = _service_client(MEM, "mem", stats=api_error(503, STATS))
    service = get_resource_health(client, MEM)["service"]

    assert service["availability"] is None
    assert service["available"] is None
    assert service["status"] == "orange"
    assert any("SERVICE|AVAILABILITY" in e for e in service["read_errors"])


@pytest.mark.unit
def test_no_availability_points_is_unknown_not_available():
    from vmware_aria.ops.resources import get_resource_health

    service = get_resource_health(_service_client(MEM, "mem"), MEM)["service"]

    assert service["availability"] is None
    assert service["available"] is None
    assert service["read_errors"]


@pytest.mark.unit
@pytest.mark.parametrize("value", [0.5, 2.0, -1.0])
def test_an_unrecognised_availability_value_is_not_a_verdict(value):
    from vmware_aria.ops.resources import get_resource_health

    service = get_resource_health(_service_client(MEM, "mem", value), MEM)["service"]

    assert service["availability"] == value
    assert service["available"] is None


@pytest.mark.unit
def test_a_non_service_object_gets_no_service_block_and_no_extra_reads():
    from vmware_aria.ops.resources import get_resource_health

    vm = "f654322a-50c3-4a9a-937c-2d4bae8a2b36"
    client = _Client({f"/resources/{vm}": _resource(vm, "web-01", kind="VirtualMachine")})
    result = get_resource_health(client, vm)

    assert result["service"] is None
    assert client.calls == [("GET", f"/resources/{vm}")]


@pytest.mark.unit
def test_mcp_get_resource_health_carries_the_service_state(monkeypatch):
    from vmware_aria.mcp_server import server
    from vmware_aria.mcp_server.tools.resources import get_resource_health

    client = _service_client(MEM, "mem", 0.0)
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    result = get_resource_health(MEM)

    assert "error" not in result, result
    assert result["service"]["available"] is False
    assert result["service"]["status"] == "orange"

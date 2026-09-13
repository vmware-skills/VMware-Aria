"""A 503 from node status is not "the platform is down".

Real appliance, 2026-09-13 (Aria Operations 8.18.7): ``GET /deployment/node/status``
answered 503 ``{"status": "OFFLINE"}`` for more than three hours, and
``get_aria_health`` reported OFFLINE / unhealthy. In the same minutes
``/deployment/node/services/info`` showed CASA, ADMINUI, API, COLLECTOR, UI and
ANALYTICS all OK — only LOCATOR was ERROR — and metrics, rightsizing and alerts
were all being produced.

The node's own flag is "every service is running", so one stuck service turns
it OFFLINE. Reporting that bit alone told an agent the platform was down while
it was answering. The health result now carries the per-service breakdown and
an ``assessment`` that tells DOWN from DEGRADED, while ``overall_status`` keeps
the node's own word unchanged.

踩坑 #37 still holds: a 503 is a status signal, never a traceback.
"""

from __future__ import annotations

import copy

import pytest

from tests.eval.regression._live_8187 import RoutedClient, api_error, body, startup_routes
from vmware_aria.connection import AriaApiError


def _health(routes):
    from vmware_aria.ops.health import get_aria_health

    return get_aria_health(RoutedClient(routes))


@pytest.mark.unit
def test_one_failed_service_under_a_503_is_degraded_not_down():
    result = _health(startup_routes())

    assert result["overall_status"] == "OFFLINE", "the node's own word is kept verbatim"
    assert result["healthy"] is False
    assert result["assessment"] == "DEGRADED", (
        "six of seven services report OK; calling that DOWN is what sent the agent "
        "away from a platform that was answering"
    )
    assert result["services_not_ok"] == ["LOCATOR"]
    assert [s["name"] for s in result["services"]] == [
        "CASA", "ADMINUI", "LOCATOR", "API", "COLLECTOR", "UI", "ANALYTICS",
    ]
    locator = next(s for s in result["services"] if s["name"] == "LOCATOR")
    assert locator["health"] == "ERROR"
    assert "not responding" in locator["details"]


@pytest.mark.unit
def test_the_503_body_system_time_is_not_thrown_away():
    """The 503 response body carries systemTime; the old path returned None."""
    result = _health(startup_routes())

    assert result["system_time_ms"] == body("node_status_503.json")["systemTime"]


@pytest.mark.unit
def test_version_is_part_of_the_health_answer():
    result = _health(startup_routes())

    assert result["release_name"] == "VMware Aria Operations 8.18.7"
    assert result["product_version"] == "8.18.7"
    assert result["product_line"] == "8.x"
    assert result["build_number"] == 25423534
    assert result["version_error"] is None


@pytest.mark.unit
def test_api_version_fields_are_not_mistaken_for_the_product_version():
    """/versions/current also has major=1, minor=77 — the API version. Reading
    those would announce a "1.x" product, confidently and wrongly (踩坑 #36)."""
    routes = startup_routes()
    no_name = dict(body("versions_current.json"), releaseName=None)
    routes["/versions/current"] = no_name

    result = _health(routes)

    assert result["product_version"] is None
    assert result["product_line"] is None
    assert result["version_error"], "an unreadable version must say so, not stay silent"


@pytest.mark.unit
def test_unreadable_version_does_not_break_the_health_check():
    routes = startup_routes()
    routes["/versions/current"] = api_error(500, "/versions/current")

    result = _health(routes)

    assert result["assessment"] == "DEGRADED"
    assert result["product_version"] is None
    assert "500" in result["version_error"]


@pytest.mark.unit
def test_every_service_failing_is_down():
    routes = startup_routes()
    info = copy.deepcopy(body("services_info.json"))
    for svc in info["service"]:
        svc["health"] = "ERROR"
    routes["/deployment/node/services/info"] = info

    assert _health(routes)["assessment"] == "DOWN"


@pytest.mark.unit
def test_offline_with_an_unreadable_breakdown_is_unknown_not_down():
    """形态 #1's mirror: not being able to read the services is not evidence
    that they are all down."""
    routes = startup_routes()
    routes["/deployment/node/services/info"] = api_error(503, "/deployment/node/services/info")

    result = _health(routes)

    assert result["assessment"] == "UNKNOWN"
    assert result["services"] is None, "unread must not render as an empty list"
    assert result["services_not_ok"] is None
    assert "503" in result["services_error"]


@pytest.mark.unit
def test_online_with_every_service_ok_is_healthy():
    routes = startup_routes()
    routes["/deployment/node/status"] = {"status": "ONLINE", "systemTime": 1789280781529}
    info = copy.deepcopy(body("services_info.json"))
    for svc in info["service"]:
        svc["health"] = "OK"
    routes["/deployment/node/services/info"] = info

    result = _health(routes)

    assert result["overall_status"] == "ONLINE"
    assert result["assessment"] == "HEALTHY"
    assert result["healthy"] is True
    assert result["services_not_ok"] == []


@pytest.mark.unit
def test_an_unrecognised_service_state_is_not_counted_as_ok_or_failed():
    """Only OK and ERROR have been observed. A third value must not be folded
    into either verdict by exclusion (unknown_is_not_a_verdict)."""
    routes = startup_routes()
    routes["/deployment/node/status"] = {"status": "ONLINE", "systemTime": 1}
    info = copy.deepcopy(body("services_info.json"))
    for svc in info["service"]:
        svc["health"] = "OK"
    info["service"][0]["health"] = "WARNING"
    routes["/deployment/node/services/info"] = info

    result = _health(routes)

    assert result["assessment"] == "UNKNOWN"
    assert result["services_not_ok"] == []
    assert result["services_unrecognized"] == ["CASA"]
    assert result["healthy"] is False


@pytest.mark.unit
def test_a_non_503_node_status_error_still_raises():
    routes = startup_routes()
    routes["/deployment/node/status"] = api_error(500, "/deployment/node/status")

    with pytest.raises(AriaApiError):
        _health(routes)

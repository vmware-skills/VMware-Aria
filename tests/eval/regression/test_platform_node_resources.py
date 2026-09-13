"""get_aria_node_resources: the Aria node's own memory, swap, heap and watchdog.

Source: 2026-09-13, live 8.18.7. LOCATOR stopped responding and the only
evidence for why was in Aria's self-monitoring objects (adapter kind
``vCenter Operations Adapter``): 7.61 of 7.75 GB used, ~0.47 GB actually free,
swap in use, 3.9 GB of heap committed, zero watchdog restarts. No tool could
read those objects, so nobody could see it without the UI.

The fixtures are the appliance after the memory upgrade to 16 GB, captured
read-only. The pressure indicator is tested on both sides of each threshold,
including the incident's own numbers.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

from vmware_aria.connection import AriaApiError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aria_8187"
AK = "vCenter Operations Adapter"
NODE_ID = "8369187c-bd97-4be1-8ba3-4ec74633090e"
WD_ID = "df7bd3fa-973e-4dfc-87e8-da668e7af260"
NODE_DEFS = f"/adapterkinds/{AK}/resourcekinds/vC-Ops-Node/statkeys"
WD_DEFS = f"/adapterkinds/{AK}/resourcekinds/vC-Ops-Watchdog/statkeys"


def body(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["body"]


def api_error(status: int | None, path: str) -> AriaApiError:
    return AriaApiError(f"Aria Operations returned HTTP {status}.", status_code=status, method="GET", path=path)


class FakeClient:
    """Answers per path; a callable answer receives params/json, an exception is raised."""

    def __init__(self, get_routes: dict[str, Any], post_routes: dict[str, Any]) -> None:
        self.get_routes = get_routes
        self.post_routes = post_routes
        self.calls: list[tuple[str, str, Any]] = []

    def _answer(self, routes: dict[str, Any], method: str, path: str, arg: Any) -> Any:
        self.calls.append((method, path, copy.deepcopy(arg)))
        if path not in routes:
            raise AssertionError(f"unexpected {method} {path}")
        answer = routes[path]
        if callable(answer) and not isinstance(answer, BaseException):
            answer = answer(arg)
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        return self._answer(self.get_routes, "GET", path, params)

    def post(self, path: str, json_data: Any = None, params: Any = None, **_kw: Any) -> Any:
        return self._answer(self.post_routes, "POST", path, json_data)


def live_routes() -> tuple[dict[str, Any], dict[str, Any]]:
    node = body("platform_resources_node.json")
    watchdog = body("platform_resources_watchdog.json")

    def resources(params: dict) -> dict:
        return {"vC-Ops-Node": node, "vC-Ops-Watchdog": watchdog}[params["resourceKind"]]

    gets = {
        "/resources": resources,
        NODE_DEFS: body("platform_node_statkey_definitions.json"),
        WD_DEFS: body("platform_watchdog_statkey_definitions.json"),
        f"/resources/{NODE_ID}/statkeys": body("platform_node_statkeys.json"),
        f"/resources/{WD_ID}/statkeys": body("platform_watchdog_statkeys.json"),
        "/resources/stats/latest": body("platform_stats_latest.json"),
    }
    posts = {"/resources/stats/query": body("platform_stats_query.json")}
    return gets, posts


def run(gets: dict[str, Any] | None = None, posts: dict[str, Any] | None = None, **kw: Any) -> dict:
    from vmware_aria.ops.platform import get_aria_node_resources

    g, p = live_routes()
    g.update(gets or {})
    p.update(posts or {})
    return get_aria_node_resources(FakeClient(g, p), **kw)


def drop_key(stats_body: dict, key: str) -> dict:
    out = copy.deepcopy(stats_body)
    for v in out["values"]:
        v["stat-list"]["stat"] = [s for s in v["stat-list"]["stat"] if s["statKey"]["key"] != key]
    return out


def only_node(result: dict) -> dict:
    assert result["nodes"] is not None, result
    assert len(result["nodes"]) == 1
    return result["nodes"][0]


# ---------------------------------------------------------------------------
# The live shape
# ---------------------------------------------------------------------------


def test_live_shape_reads_the_upgraded_node() -> None:
    result = run()
    node = only_node(result)
    assert node["name"] == "vRealize Operations Node-aria-ops-01"
    assert node["resource_id"] == NODE_ID
    assert node["collection_status"] == "DATA_RECEIVING"
    total = node["memory"]["mem|total"]
    assert total["latest"] == pytest.approx(15.614, abs=0.001)
    assert total["unit"] == "GB"
    assert node["memory"]["mem|actualFree"]["latest"] == pytest.approx(6.713, abs=0.001)
    assert node["swap"]["swap|used"]["unit"] == "GB"
    assert node["heap"]["heap|CommittedMemory"]["unit"] == "MB"
    assert node["heap"]["heap|NodeHeapMemoryRemaining"]["unit"] == "%"
    assert set(node["heap_components"]) == {"Analytics", "SuiteAPI", "Collector", "AdminUI", "CaSA", "ProductUI"}
    assert node["heap_components"]["Analytics"]["latest"] == 5040.0
    assert node["missing"] == []
    for key in ("nodes_error", "units_error", "latest_error", "window_error", "watchdog_error"):
        assert result[key] is None, (key, result[key])


def test_watchdog_restarts_are_paired_by_node_id_and_zero_is_a_reading() -> None:
    node = only_node(run())
    restarts = node["watchdog_restarts"]
    assert set(restarts) == {
        "analytics",
        "api",
        "collector",
        "gemfire",
        "ntp",
        "vmware-casa",
        "vmware-vcops-web",
        "vpostgres",
        "vpostgres-repl",
    }
    assert restarts["analytics"]["latest"] == 0.0
    assert restarts["analytics"]["window"]["max"] == 0.0
    assert node["watchdog_resource_id"] == WD_ID


def test_one_latest_read_and_one_window_query_cover_both_resources() -> None:
    from vmware_aria.ops.platform import get_aria_node_resources

    g, p = live_routes()
    client = FakeClient(g, p)
    result = get_aria_node_resources(client, window_hours=6)
    queries = [c for c in client.calls if c[1] == "/resources/stats/query"]
    latest = [c for c in client.calls if c[1] == "/resources/stats/latest"]
    assert len(queries) == 1 and len(latest) == 1
    payload = queries[0][2]
    assert set(payload["resourceId"]) == {NODE_ID, WD_ID}
    assert "mem|actualFree" in payload["statKey"] and "Service:analytics|Restarts" in payload["statKey"]
    assert payload["rollUpType"] == "AVG"
    assert payload["intervalType"] == "MINUTES" and payload["intervalQuantifier"] == 5
    assert payload["end"] - payload["begin"] == 6 * 3_600_000
    assert result["window_end_ms"] - result["window_begin_ms"] == 6 * 3_600_000


# ---------------------------------------------------------------------------
# Window statistics
# ---------------------------------------------------------------------------


def _single_stat_query(key: str, data: list) -> dict:
    return {
        "values": [
            {
                "resourceId": NODE_ID,
                "stat-list": {"stat": [{"statKey": {"key": key}, "timestamps": list(range(len(data))), "data": data}]},
            }
        ]
    }


def test_window_min_avg_max_over_numeric_points_only() -> None:
    result = run(posts={"/resources/stats/query": _single_stat_query("mem|total", [4.0, None, 8.0, "x", 6.0])})
    window = only_node(result)["memory"]["mem|total"]["window"]
    assert window == {"min": 4.0, "avg": 6.0, "max": 8.0, "points": 3}


def test_a_key_with_a_latest_value_but_no_window_points_has_window_none() -> None:
    result = run(posts={"/resources/stats/query": {"values": []}})
    metric = only_node(result)["memory"]["mem|total"]
    assert metric["latest"] is not None
    assert metric["window"] is None


@pytest.mark.parametrize("hours", [0, -1, 721, True, 2.5])
def test_window_hours_out_of_range_is_rejected(hours: Any) -> None:
    with pytest.raises(ValueError, match="window_hours"):
        run(window_hours=hours)


def test_window_hours_upper_bound_is_accepted() -> None:
    assert run(window_hours=720)["window_hours"] == 720


# ---------------------------------------------------------------------------
# Units come from the definitions
# ---------------------------------------------------------------------------


def test_units_are_read_from_definitions_not_assumed() -> None:
    defs = body("platform_node_statkey_definitions.json")
    for attr in defs["resourceTypeAttributes"]:
        if attr["key"] == "heap|MaxHeapSize":
            attr["unit"] = "GB"
    node = only_node(run(gets={NODE_DEFS: defs}))
    assert node["heap"]["heap|MaxHeapSize"]["unit"] == "GB"


def test_instanced_watchdog_key_resolves_to_its_uninstanced_definition() -> None:
    defs = body("platform_watchdog_statkey_definitions.json")
    assert [a["key"] for a in defs["resourceTypeAttributes"]] == ["Service|Restarts"]
    assert defs["resourceTypeAttributes"][0].get("unit") is None
    node = only_node(run())
    assert node["watchdog_restarts"]["api"]["unit"] is None
    defs["resourceTypeAttributes"][0]["unit"] = "count"
    node = only_node(run(gets={WD_DEFS: defs}))
    assert node["watchdog_restarts"]["api"]["unit"] == "count"


def test_unreadable_definitions_leave_units_unknown_and_pressure_unknown() -> None:
    result = run(gets={NODE_DEFS: api_error(500, NODE_DEFS)})
    node = only_node(result)
    assert "HTTP 500" in result["units_error"]
    assert node["memory"]["mem|total"]["unit"] is None
    assert node["memory"]["mem|total"]["latest"] == pytest.approx(15.614, abs=0.001)
    assert node["memory_pressure"]["level"] == "UNKNOWN"


def test_mismatched_units_make_pressure_unknown() -> None:
    defs = body("platform_node_statkey_definitions.json")
    for attr in defs["resourceTypeAttributes"]:
        if attr["key"] == "mem|total":
            attr["unit"] = "MB"
    pressure = only_node(run(gets={NODE_DEFS: defs}))["memory_pressure"]
    assert pressure["level"] == "UNKNOWN"
    assert "unit" in pressure["basis"]


# ---------------------------------------------------------------------------
# Missing is never zero
# ---------------------------------------------------------------------------


def test_a_key_the_node_does_not_report_is_missing_not_zero() -> None:
    statkeys = body("platform_node_statkeys.json")
    statkeys["stat-key"] = [k for k in statkeys["stat-key"] if k["key"] != "mem|actualFree"]
    result = run(
        gets={
            f"/resources/{NODE_ID}/statkeys": statkeys,
            "/resources/stats/latest": drop_key(body("platform_stats_latest.json"), "mem|actualFree"),
        },
        posts={"/resources/stats/query": drop_key(body("platform_stats_query.json"), "mem|actualFree")},
    )
    node = only_node(result)
    assert "mem|actualFree" not in node["memory"]
    assert [(m["key"], m["reason"]) for m in node["missing"]] == [("mem|actualFree", "not_reported")]
    assert node["memory_pressure"]["level"] == "UNKNOWN"


def test_a_reported_key_without_any_points_is_no_data() -> None:
    result = run(
        gets={"/resources/stats/latest": drop_key(body("platform_stats_latest.json"), "swap|used")},
        posts={"/resources/stats/query": drop_key(body("platform_stats_query.json"), "swap|used")},
    )
    node = only_node(result)
    assert "swap|used" not in node["swap"]
    assert [(m["key"], m["reason"]) for m in node["missing"]] == [("swap|used", "no_data")]


def test_unreadable_node_statkeys_make_an_absent_key_undetermined() -> None:
    path = f"/resources/{NODE_ID}/statkeys"
    result = run(
        gets={
            path: api_error(503, path),
            "/resources/stats/latest": drop_key(body("platform_stats_latest.json"), "swap|used"),
        },
        posts={"/resources/stats/query": drop_key(body("platform_stats_query.json"), "swap|used")},
    )
    node = only_node(result)
    assert [(m["key"], m["reason"]) for m in node["missing"]] == [("swap|used", "undetermined")]


def test_failed_stat_reads_never_report_no_data() -> None:
    result = run(
        gets={"/resources/stats/latest": api_error(500, "/resources/stats/latest")},
        posts={"/resources/stats/query": api_error(None, "/resources/stats/query")},
    )
    node = only_node(result)
    assert "HTTP 500" in result["latest_error"]
    assert "no HTTP response" in result["window_error"]
    assert node["memory"] == {} and node["heap_components"] == {}
    reasons = {m["reason"] for m in node["missing"]}
    assert reasons == {"undetermined"}, reasons
    assert "Service:analytics|Restarts" in {m["key"] for m in node["missing"]}
    assert node["watchdog_restarts"] is None, "unread restarts must not read as an empty (zero) set"
    assert node["watchdog_note"]
    assert node["memory_pressure"]["level"] == "UNKNOWN"


def test_failed_latest_read_keeps_the_window() -> None:
    result = run(gets={"/resources/stats/latest": api_error(500, "/resources/stats/latest")})
    node = only_node(result)
    metric = node["memory"]["mem|total"]
    assert metric["latest"] is None and metric["window"]["points"] > 0
    assert node["memory_pressure"]["level"] == "UNKNOWN"


@pytest.mark.parametrize("shape", [{"values": "nope"}, {"other": []}, ["not", "a", "dict"]])
def test_unrecognised_latest_shape_is_an_error_not_zeros(shape: Any) -> None:
    result = run(gets={"/resources/stats/latest": shape})
    assert "unrecognised" in result["latest_error"]
    assert only_node(result)["memory_pressure"]["level"] == "UNKNOWN"


@pytest.mark.parametrize("listing", [{"resourceList": "x"}, {"nothing": 1}, {"resourceList": []}])
def test_unrecognised_or_empty_node_listing_is_not_an_empty_cluster(listing: Any) -> None:
    node = body("platform_resources_node.json")
    watchdog = body("platform_resources_watchdog.json")
    result = run(gets={"/resources": lambda p: listing if p["resourceKind"] == "vC-Ops-Node" else watchdog})
    assert result["nodes"] is None
    assert result["nodes_error"]
    assert node  # the unmodified fixture still has a node: the empty answer is the stub's


def test_node_listing_http_failure_raises() -> None:
    with pytest.raises(AriaApiError):
        run(gets={"/resources": api_error(401, "/resources")})


def test_watchdog_without_a_matching_node_id_is_not_paired() -> None:
    watchdog = body("platform_resources_watchdog.json")
    for ident in watchdog["resourceList"][0]["resourceKey"]["resourceIdentifiers"]:
        if ident["identifierType"]["name"] == "NODEID":
            ident["value"] = "some-other-node"
    node_body = body("platform_resources_node.json")
    node = only_node(run(gets={"/resources": lambda p: node_body if p["resourceKind"] == "vC-Ops-Node" else watchdog}))
    assert node["watchdog_restarts"] is None
    assert "NODEID" in node["watchdog_note"]


def test_watchdog_listing_failure_is_reported() -> None:
    node_body = body("platform_resources_node.json")
    failure = api_error(500, "/resources")
    result = run(gets={"/resources": lambda p: node_body if p["resourceKind"] == "vC-Ops-Node" else failure})
    node = only_node(result)
    assert "HTTP 500" in result["watchdog_error"]
    assert node["watchdog_restarts"] is None


def test_watchdog_reporting_no_restart_keys_is_not_zero_restarts() -> None:
    path = f"/resources/{WD_ID}/statkeys"
    node = only_node(run(gets={path: {"stat-key": []}}))
    assert node["watchdog_restarts"] is None
    assert node["watchdog_note"]


def test_api_strings_are_sanitized() -> None:
    node_body = body("platform_resources_node.json")
    node_body["resourceList"][0]["resourceKey"]["name"] = "node\x1b[31m-evil\x07"
    watchdog = body("platform_resources_watchdog.json")
    node = only_node(run(gets={"/resources": lambda p: node_body if p["resourceKind"] == "vC-Ops-Node" else watchdog}))
    assert "\x1b" not in node["name"] and "\x07" not in node["name"]


# ---------------------------------------------------------------------------
# The pressure indicator: both sides of every threshold
# ---------------------------------------------------------------------------


def _metric(value: Any, unit: str | None = "GB") -> dict:
    return {"latest": value, "latest_time_ms": 1, "unit": unit, "window": None}


def test_threshold_constants_are_the_documented_values() -> None:
    from vmware_aria.ops import platform

    assert platform.MEMORY_PRESSURE_HIGH_FREE_PCT == 10.0
    assert platform.MEMORY_PRESSURE_ELEVATED_FREE_PCT == 20.0
    doc = platform.get_aria_node_resources.__doc__
    assert "10%" in doc and "20%" in doc


@pytest.mark.parametrize(
    ("total", "free", "level"),
    [
        (7.75, 0.47, "HIGH"),  # the 2026-09-13 incident, before the upgrade
        (100.0, 9.99, "HIGH"),
        (100.0, 10.0, "ELEVATED"),
        (100.0, 19.99, "ELEVATED"),
        (100.0, 20.0, "NORMAL"),
        (15.614, 6.713, "NORMAL"),  # after the upgrade
        (100.0, 100.0, "NORMAL"),
    ],
)
def test_pressure_level_on_each_side_of_the_thresholds(total: float, free: float, level: str) -> None:
    from vmware_aria.ops.platform import assess_memory_pressure

    result = assess_memory_pressure(_metric(total), _metric(free))
    assert result["level"] == level
    assert result["actual_free_pct"] == pytest.approx(free / total * 100)


@pytest.mark.parametrize(
    ("total", "free"),
    [
        (None, _metric(1.0)),
        (_metric(8.0), None),
        (_metric(None), _metric(1.0)),
        (_metric(0.0), _metric(0.0)),
        (_metric(8.0), _metric(9.0)),
        (_metric(math.nan), _metric(1.0)),
        (_metric(8.0), _metric(-1.0)),
        (_metric(8.0, unit=None), _metric(1.0, unit=None)),
        (_metric(8.0, unit="GB"), _metric(1.0, unit="MB")),
        (_metric(True), _metric(1.0)),
    ],
)
def test_pressure_is_unknown_when_the_numbers_do_not_settle_it(total: Any, free: Any) -> None:
    from vmware_aria.ops.platform import assess_memory_pressure

    result = assess_memory_pressure(total, free)
    assert result["level"] == "UNKNOWN"
    assert result["actual_free_pct"] is None
    assert result["basis"]

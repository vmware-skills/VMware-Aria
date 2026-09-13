"""list_adapters: which adapter instance stopped collecting, and since when.

Source: 2026-09-13, live 8.18.7. Aria raised "Objects are not receiving data"
on the resource "new VC" and no tool could show the adapter instances behind
it. ``GET /adapters`` carries ``lastCollected`` / ``lastHeartbeat`` and the
``monitoringInterval``, which is what a staleness flag can honestly be based on.

The fixture is the live list (six adapters, unpaged — no pageInfo).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from vmware_aria.connection import AriaApiError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aria_8187"
MINUTE_MS = 60_000


def adapters_body() -> dict:
    return json.loads((FIXTURES / "platform_adapters.json").read_text(encoding="utf-8"))["body"]


def new_vc(b: dict) -> dict:
    return next(a for a in b["adapterInstancesInfoDto"] if a["resourceKey"]["name"] == "new VC")


class FakeClient:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(path)
        answer = self.routes[path]
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)


def run(b: Any = None, node_status: Any = None, **kw: Any) -> dict:
    from vmware_aria.ops.platform import list_adapters

    b = adapters_body() if b is None else b
    if node_status is None:
        newest = max(a["lastCollected"] for a in adapters_body()["adapterInstancesInfoDto"])
        node_status = {"status": "ONLINE", "systemTime": newest + MINUTE_MS}
    return list_adapters(FakeClient({"/adapters": b, "/deployment/node/status": node_status}), **kw)


def row(result: dict, name: str) -> dict:
    return next(r for r in result["items"] if r["name"] == name)


# ---------------------------------------------------------------------------
# Live shape
# ---------------------------------------------------------------------------


def test_live_shape_lists_every_adapter_with_collection_fields() -> None:
    result = run()
    assert result["returned"] == 6 and result["total"] == 6
    assert result["truncated"] is False and result["next_offset"] is None
    vc = row(result, "new VC")
    assert vc["adapter_kind"] == "VMWARE"
    assert vc["resource_kind"] == "VMwareAdapter Instance"
    assert vc["id"] == "431af8a8-18b5-4811-aff5-ac903aaf03f7"
    assert vc["collector_id"] == 1
    assert vc["collector_group_id"] == "207ab254-981a-4bac-80f9-f9ad5330afac"
    assert vc["monitoring_interval_min"] == 5
    assert vc["resources_collected"] == 21 and vc["metrics_collected"] == 1382
    assert vc["last_collected_ms"] == 1789287184216
    assert vc["message"] == ""
    assert vc["stale"] is False
    assert result["reference_clock"] == "appliance"
    assert result["stale_adapters"] == [] and result["staleness_unknown"] == []
    assert "VMWARE" in result["adapter_kinds_present"]


def test_group_id_absent_on_the_wire_is_null_not_empty_string() -> None:
    infra = row(run(), "vRealize Operations Adapter - aria-ops-01")
    assert infra["collector_group_id"] is None


# ---------------------------------------------------------------------------
# Staleness: both sides of the threshold, and unknown
# ---------------------------------------------------------------------------


def _at_age(interval: Any, age_ms: int, **overrides: Any) -> dict:
    b = adapters_body()
    vc = new_vc(b)
    vc["monitoringInterval"] = interval
    vc.update(overrides)
    ref = vc["lastCollected"] + age_ms if isinstance(vc.get("lastCollected"), int) else 1789287184216
    return row(run(b, node_status={"status": "ONLINE", "systemTime": ref}), "new VC")


def test_threshold_constants() -> None:
    from vmware_aria.ops import platform

    assert platform.ADAPTER_STALE_MISSED_INTERVALS == 3
    assert platform.ADAPTER_STALE_MIN_AGE_MINUTES == 15


def test_stale_at_exactly_the_floor_is_not_stale_and_one_ms_later_is() -> None:
    assert _at_age(5, 15 * MINUTE_MS)["stale"] is False
    assert _at_age(5, 15 * MINUTE_MS + 1)["stale"] is True


def test_a_long_interval_raises_the_threshold_above_the_floor() -> None:
    assert _at_age(10, 30 * MINUTE_MS)["stale"] is False
    assert _at_age(10, 30 * MINUTE_MS + 1)["stale"] is True
    assert _at_age(10, 20 * MINUTE_MS)["stale"] is False


def test_stale_adapters_are_named_in_the_envelope() -> None:
    b = adapters_body()
    vc = new_vc(b)
    ref = vc["lastCollected"] + 2 * 3_600_000
    result = run(b, node_status={"status": "ONLINE", "systemTime": ref})
    assert "new VC" in result["stale_adapters"]
    assert row(result, "new VC")["last_collected_age_s"] == 7200


@pytest.mark.parametrize(
    "change",
    [
        {"lastCollected": None},
        {"lastCollected": "yesterday"},
        {"monitoringInterval": None},
        {"monitoringInterval": 0},
        {"monitoringInterval": True},
    ],
)
def test_staleness_is_unknown_when_the_fields_cannot_support_it(change: dict) -> None:
    b = adapters_body()
    vc = new_vc(b)
    vc.update(change)
    result = run(b)
    got = row(result, "new VC")
    assert got["stale"] is None
    assert got["stale_basis"]
    assert "new VC" in result["staleness_unknown"]
    assert "new VC" not in result["stale_adapters"]


def test_a_collection_time_after_the_reference_is_unknown_not_fresh() -> None:
    got = _at_age(5, -10 * MINUTE_MS)
    assert got["stale"] is None


def test_the_503_node_status_body_still_supplies_appliance_time() -> None:
    b = adapters_body()
    ref = new_vc(b)["lastCollected"] + 60 * MINUTE_MS
    err = AriaApiError(
        "503",
        status_code=503,
        method="GET",
        path="/deployment/node/status",
        body={"status": "OFFLINE", "systemTime": ref},
    )
    result = run(b, node_status=err)
    assert result["reference_clock"] == "appliance"
    assert result["reference_time_ms"] == ref
    assert row(result, "new VC")["stale"] is True


def test_unreadable_appliance_time_falls_back_to_the_local_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_aria.ops import platform

    b = adapters_body()
    local = new_vc(b)["lastCollected"] + MINUTE_MS
    monkeypatch.setattr(platform, "_local_now_ms", lambda: local)
    err = AriaApiError("boom", status_code=500, method="GET", path="/deployment/node/status")
    result = run(b, node_status=err)
    assert result["reference_clock"] == "local"
    assert result["reference_time_ms"] == local


# ---------------------------------------------------------------------------
# Filter and paging
# ---------------------------------------------------------------------------


def test_filter_by_adapter_kind_is_case_insensitive() -> None:
    result = run(adapter_kind="vmware")
    assert [r["name"] for r in result["items"]] == ["new VC"]
    assert result["total"] == 1


def test_a_kind_with_no_instances_is_an_answer_with_the_kinds_present() -> None:
    result = run(adapter_kind="NSXTAdapter")
    assert result["items"] == [] and result["total"] == 0
    assert "VMWARE" in result["adapter_kinds_present"]


def test_paging_walks_every_row_once() -> None:
    seen: list[str] = []
    offset: int | None = 0
    calls = 0
    while offset is not None:
        calls += 1
        assert calls < 10
        page = run(limit=4, offset=offset)
        seen.extend(r["id"] for r in page["items"])
        offset = page["next_offset"]
    assert len(seen) == 6 and len(set(seen)) == 6
    assert calls == 2


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (501, 0), (10, -1)])
def test_invalid_page_args_are_rejected(limit: int, offset: int) -> None:
    with pytest.raises(ValueError):
        run(limit=limit, offset=offset)


def test_blank_adapter_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="adapter_kind"):
        run(adapter_kind="  ")


# ---------------------------------------------------------------------------
# Unknown is not "no adapters"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", [{"somethingElse": []}, {"adapterInstancesInfoDto": "x"}, ["a"]])
def test_unrecognised_shape_raises_instead_of_listing_nothing(shape: Any) -> None:
    with pytest.raises(AriaApiError, match="adapterInstancesInfoDto"):
        run(shape)


def test_an_empty_adapter_list_is_not_believed() -> None:
    with pytest.raises(AriaApiError, match="self-monitoring"):
        run({"adapterInstancesInfoDto": []})


def test_http_failure_propagates() -> None:
    with pytest.raises(AriaApiError):
        run(AriaApiError("403", status_code=403, method="GET", path="/adapters"))


def test_rows_in_an_unrecognised_form_are_counted() -> None:
    b = adapters_body()
    b["adapterInstancesInfoDto"].append("not-an-object")
    result = run(b)
    assert result["unrecognized_rows"] == 1
    assert result["total"] == 6


def test_api_strings_are_sanitized() -> None:
    b = adapters_body()
    vc = new_vc(b)
    vc["messageFromAdapterInstance"] = "Collection failed\x1b[2J\x00 " + "x" * 2000
    vc["resourceKey"]["name"] = "new\x07 VC"
    got = run(b)["items"]
    msg = next(r["message"] for r in got if r["id"] == vc["id"])
    name = next(r["name"] for r in got if r["id"] == vc["id"])
    assert "\x1b" not in msg and "\x00" not in msg and len(msg) <= 300
    assert "\x07" not in name

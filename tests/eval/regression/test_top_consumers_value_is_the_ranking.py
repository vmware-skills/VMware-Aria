"""get_top_consumers must report the number Aria ranked by.

Live on Aria Operations 8.18.7, GET /resources/stats/topn (rollUpType AVG,
5-minute intervals over the last hour) orders groups by the AVERAGE of the
window's points: cpu|usage_average came back test-llm (avg 10.22, last 0.40),
vcsa (avg 7.41, last 8.44), ... — descending by average, not by the last point
and not by the max (mem|usage_average put a VM with max 61 below one with max
26.6). The tool reported the last point as ``value``, so a correct ranking
read as unsorted and its top entry looked idle.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _candidates(names: list[str]) -> dict:
    return {"resourceList": [{"identifier": n, "resourceKey": {"name": n}} for n in names]}


def _group(rid: str, points: list[float]) -> dict:
    return {"groupKey": rid, "resourceStats": [{"resourceId": rid, "stat": {"data": points}}]}


@pytest.mark.unit
def test_value_is_the_window_average_and_latest_value_is_the_last_point() -> None:
    from vmware_aria.ops.resources import get_top_consumers

    client = MagicMock(name="AriaClient")
    client.get.side_effect = [
        _candidates(["busy-then-idle", "steady"]),
        # API order: descending by window average (51.26 > 8.44).
        {"resourceStatGroups": [_group("busy-then-idle", [102.12, 0.40]), _group("steady", [8.44, 8.44])]},
    ]
    result = get_top_consumers(client, metric_key="cpu|usage_average", top_n=10)

    first, second = result["items"]
    assert first["name"] == "busy-then-idle"
    assert first["value"] == pytest.approx(51.26)
    assert first["latest_value"] == pytest.approx(0.40)
    assert second["value"] == pytest.approx(8.44)
    assert second["latest_value"] == pytest.approx(8.44)
    values = [item["value"] for item in result["items"]]
    assert values == sorted(values, reverse=True), "items must read as ranked by value"


@pytest.mark.unit
def test_rightsizing_cli_prints_a_failed_property_read() -> None:
    from typer.testing import CliRunner

    import vmware_aria.cli as cli
    import vmware_aria.ops.capacity as capacity

    note = "POST /resources/properties/latest/query failed (HTTP 403); power state is unknown"
    answer = {"items": [], "returned": 0, "limit": 20, "total": 0, "truncated": False,
              "hint": None, "properties_note": note}
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(cli, "_get_connection", lambda target, config: (MagicMock(), None))
        mp.setattr(capacity, "list_rightsizing_recommendations", lambda client, **kw: answer)
        out = CliRunner().invoke(cli.app, ["capacity", "rightsizing"], env={"COLUMNS": "200"})
    finally:
        mp.undo()
    assert out.exit_code == 0, out.output
    assert "HTTP 403" in " ".join(out.output.split())

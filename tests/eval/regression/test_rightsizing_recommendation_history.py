"""A rightsizing recommendation says whether it has settled, and over how long.

2026-09-14, live Aria Operations 8.18.7: the memory recommendation for
vRealize-Operations was 9.0 GiB one day and 12.3 GiB the next, and the tool
showed only the latest value. Read back by day with MIN and MAX rollups
(verified on the appliance 2026-09-15), the key had ranged from 8389138 KB to
33554432 KB (8 GiB to 32 GiB) on 09-13, then 12683900-12960065 KB on 09-14 —
and the appliance held only three days of it, not seven.

A caller acting on the latest point of a recommendation that moved that far has
no way to know it did. The daily series and threshold below come from that
reading.
"""

from __future__ import annotations

import pytest

STATS_PATH = "/resources/stats/query"
PROPS_PATH = "/resources/properties/latest/query"
MEM = "OnlineCapacityAnalytics|mem|recommendedSize"
CPU = "OnlineCapacityAnalytics|cpu|recommendedSize"
DISK = "OnlineCapacityAnalytics|diskspace|recommendedSize"
DAY_MS = 86_400_000

#: Daily MIN / MAX series per VM, as the appliance returned them.
HISTORY = {
    "swing": {
        "MIN": {MEM: [8389138.0, 12683900.0, 12960162.0], CPU: [2611.2001953125] * 3},
        "MAX": {MEM: [33554432.0, 12960065.0, 12961757.0], CPU: [2611.2001953125] * 3},
    },
    "settled": {
        "MIN": {MEM: [12683900.0, 12960162.0], CPU: [2611.2001953125] * 2},
        "MAX": {MEM: [12960065.0, 12961757.0], CPU: [2611.2001953125] * 2},
    },
}
LATEST = {MEM: 12961757.0, CPU: 2611.2001953125}
PROPS = {
    "config|hardware|numCpu": 2.0,
    "cpu|speed": 2 * 2611.2001953125 * 1_000_000,
    "config|hardware|memoryKB": 16777216.0,
    "summary|runtime|powerState": "Powered On",
    "summary|config|isTemplate": "false",
}


def _series(rid: str, values_by_key: dict[str, float | list[float]]) -> dict:
    stats = []
    for key, values in values_by_key.items():
        data = values if isinstance(values, list) else [values]
        stats.append({"statKey": {"key": key}, "timestamps": [DAY_MS * (i + 1) for i in range(len(data))], "data": data})
    return {"resourceId": rid, "stat-list": {"stat": stats}}


class _Client:
    def __init__(self, history: dict | None = None, fail_history: int | None = 0) -> None:
        self.history = HISTORY if history is None else history
        self.fail_history = fail_history
        self.stats_bodies: list[dict] = []

    def get(self, path: str, params: dict | None = None, **_kw) -> dict:
        assert path == "/resources", path
        return {
            "resourceList": [
                {"identifier": rid, "resourceKey": {"name": rid}} for rid in ("swing", "settled")
            ],
            "pageInfo": {"totalCount": 2},
        }

    def post(self, path: str, json_data: dict | None = None, **_kw) -> dict:
        body = json_data or {}
        if path == PROPS_PATH:
            content = [
                {"statKey": k, "data": [v]} if isinstance(v, float) else {"statKey": k, "values": [v]}
                for k, v in PROPS.items()
            ]
            return {
                "values": [
                    {"resourceId": rid, "property-contents": {"property-content": content}}
                    for rid in body["resourceIds"]
                ]
            }
        assert path == STATS_PATH, path
        self.stats_bodies.append(body)
        rollup = body["rollUpType"]
        if rollup == "LATEST":
            return {"values": [_series(rid, LATEST) for rid in body["resourceId"]]}
        if self.fail_history != 0:
            from vmware_aria.connection import AriaApiError

            raise AriaApiError("boom", status_code=self.fail_history, method="POST", path=STATS_PATH)
        return {
            "values": [
                _series(rid, self.history[rid][rollup]) for rid in body["resourceId"] if rid in self.history
            ]
        }


def _run(client: _Client) -> tuple[dict, dict]:
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    result = list_rightsizing_recommendations(client, limit=10)
    return result, {r["id"]: r for r in result["items"]}


def test_history_is_two_bulk_queries_by_day_whatever_the_vm_count():
    client = _Client()
    _run(client)
    history = [b for b in client.stats_bodies if b["rollUpType"] != "LATEST"]
    assert sorted(b["rollUpType"] for b in history) == ["MAX", "MIN"]
    for body in history:
        assert body["intervalType"] == "DAYS" and body["intervalQuantifier"] == 1
        assert set(body["statKey"]) == {MEM, CPU, DISK}
        assert body["end"] - body["begin"] == 7 * DAY_MS
        assert set(body["resourceId"]) == {"swing", "settled"}


def test_a_recommendation_that_swung_is_not_settled_and_not_actionable():
    _result, rows = _run(_Client())
    swing = rows["swing"]
    assert swing["memory_direction"] == "oversized"  # the latest point alone says act
    assert swing["recommendation_stable"] is False
    assert swing["actionable"] is False
    caveat = next(c for c in swing["caveats"] if "not settled" in c)
    assert "8.0" in caveat and "32.0" in caveat and "3 day" in caveat


def test_drift_inside_the_threshold_is_settled_and_stays_actionable():
    _result, rows = _run(_Client())
    settled = rows["settled"]
    assert settled["recommendation_stable"] is True
    assert settled["actionable"] is True
    assert not any("not settled" in c for c in settled["caveats"])


def test_the_row_says_what_the_range_covers():
    _result, rows = _run(_Client())
    rng = rows["swing"]["recommendation_range"]
    assert rng["window_days"] == 7
    assert rng["days_with_data"] == 3
    assert rng["memory_kb"] == [8389138.0, 33554432.0]
    assert rng["cpu_mhz"] == [2611.2001953125, 2611.2001953125]


def test_the_threshold_sits_between_live_drift_and_a_real_resize():
    from vmware_aria.ops.capacity import RECOMMENDATION_SPREAD_UNSTABLE

    # 12683900 -> 12961757 KB in a day is 2.1% of drift; vcsa's real change was 7.4%.
    assert 0.021 < RECOMMENDATION_SPREAD_UNSTABLE < 0.074


@pytest.mark.parametrize("status", [500, None])
def test_a_failed_history_read_leaves_stability_unknown_and_says_why(status):
    result, rows = _run(_Client(fail_history=status))
    assert set(rows) == {"swing", "settled"}
    for row in rows.values():
        assert row["recommendation_stable"] is None
        assert row["recommendation_range"] is None
    # Unknown history does not block: actionable is what it was without it.
    assert rows["swing"]["actionable"] is True
    assert result["history_note"] and "could not be read" in result["history_note"]


def test_no_history_published_is_unknown_not_stable():
    result, rows = _run(_Client(history={}))
    assert rows["swing"]["recommendation_stable"] is None
    assert result["history_note"] is None


def test_history_note_is_null_when_the_read_succeeds():
    result, _rows = _run(_Client())
    assert result["history_note"] is None


def test_a_moving_disk_recommendation_does_not_unsettle_the_row():
    """Disk carries no direction, so its movement is reported but not judged."""
    history = {
        "settled": {
            "MIN": {**HISTORY["settled"]["MIN"], DISK: [10.0, 10.0]},
            "MAX": {**HISTORY["settled"]["MAX"], DISK: [40.0, 40.0]},
        },
        "swing": HISTORY["swing"],
    }
    _result, rows = _run(_Client(history=history))
    assert rows["settled"]["recommendation_range"]["diskspace_gb"] == [10.0, 40.0]
    assert rows["settled"]["recommendation_stable"] is True
    assert rows["settled"]["actionable"] is True


def test_cli_prints_the_history_note_when_the_read_fails(monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from vmware_aria import cli

    monkeypatch.setattr(cli, "_get_connection", lambda *_a, **_k: (_Client(fail_history=500), None))
    monkeypatch.setenv("COLUMNS", "400")
    result = CliRunner().invoke(cli.app, ["capacity", "rightsizing", "--limit", "10"])
    assert result.exit_code == 0, result.output
    assert "recommendation history failed (HTTP 500)" in " ".join(result.output.split())


def test_cli_prints_the_not_settled_caveat(monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from vmware_aria import cli

    monkeypatch.setattr(cli, "_get_connection", lambda *_a, **_k: (_Client(), None))
    monkeypatch.setenv("COLUMNS", "250")
    result = CliRunner().invoke(cli.app, ["capacity", "rightsizing", "--limit", "10"])
    assert result.exit_code == 0, result.output
    assert "not settled" in result.output

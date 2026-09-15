"""``resource metrics`` can answer with a summary instead of every point.

2026-09-14 and again 2026-09-15 on Aria Operations 8.18.7: 4 metrics over 72
hours came back as ~625 raw points each. Finding when ``badge|health`` fell
from 100 to 25 took a local script. The summary gives, per metric, n / min /
max / avg / latest and the change points — the timestamps where the value
changed. The raw series stays the default and is still one flag away.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

RID = "ba13f0b7-19b5-4f30-b321-bc3a791d3497"
T0 = 1789200000000
STEP = 300_000


def _series(values: list[float]) -> list[dict]:
    return [{"timestamp_ms": T0 + i * STEP, "value": v} for i, v in enumerate(values)]


def _stats_body(series: dict[str, list[float]]) -> dict:
    return {
        "values": [
            {
                "resourceId": RID,
                "stat-list": {
                    "stat": [
                        {
                            "statKey": {"key": key},
                            "timestamps": [T0 + i * STEP for i in range(len(values))],
                            "data": values,
                        }
                        for key, values in series.items()
                    ]
                },
            }
        ]
    }


@pytest.mark.unit
def test_summary_gives_extremes_average_latest_and_where_the_value_changed():
    from vmware_aria.ops.metric_summary import summarize_series

    summary = summarize_series(_series([100.0, 100.0, 25.0, 25.0, 100.0]))

    assert summary["n"] == 5
    assert summary["min"] == 25.0 and summary["max"] == 100.0
    assert summary["avg"] == pytest.approx(70.0)
    assert summary["latest"] == 100.0
    assert summary["first_timestamp_ms"] == T0
    assert summary["latest_timestamp_ms"] == T0 + 4 * STEP
    assert summary["change_count"] == 2
    assert summary["change_points"] == [
        {"timestamp_ms": T0 + 2 * STEP, "from": 100.0, "to": 25.0},
        {"timestamp_ms": T0 + 4 * STEP, "from": 25.0, "to": 100.0},
    ]
    assert summary["change_points_truncated"] is False


@pytest.mark.unit
def test_a_constant_series_has_no_change_points():
    from vmware_aria.ops.metric_summary import summarize_series

    summary = summarize_series(_series([0.0] * 634))

    assert summary["n"] == 634 and summary["change_count"] == 0
    assert summary["change_points"] == []


@pytest.mark.unit
def test_change_points_are_bounded_and_keep_the_most_recent():
    from vmware_aria.ops import metric_summary

    values = [float(i % 2) for i in range(300)]
    summary = metric_summary.summarize_series(_series(values))

    cap = metric_summary.MAX_CHANGE_POINTS
    assert summary["change_count"] == 299
    assert len(summary["change_points"]) == cap
    assert summary["change_points_truncated"] is True
    assert summary["change_points"][-1]["timestamp_ms"] == T0 + 299 * STEP


@pytest.mark.unit
def test_points_that_are_not_numbers_are_counted_not_averaged():
    from vmware_aria.ops.metric_summary import summarize_series

    points = _series([1.0, 3.0]) + [{"timestamp_ms": T0 + 9 * STEP, "value": None}]
    summary = summarize_series(points)

    assert summary["n"] == 2
    assert summary["non_numeric_points"] == 1
    assert summary["avg"] == 2.0
    assert summary["latest"] == 3.0


@pytest.mark.unit
def test_get_resource_metrics_summary_mode_replaces_points_and_keeps_missing():
    from vmware_aria.ops.resources import get_resource_metrics

    client = MagicMock()
    client.post.return_value = _stats_body({"badge|health": [100.0, 25.0, 25.0]})
    client.get.return_value = {"stat-key": [{"key": "badge|health"}, {"key": "SERVICE|AVAILABILITY"}]}

    result = get_resource_metrics(client, RID, ["badge|health", "SERVICE|AVAILABILITY"], summary=True)

    assert result["mode"] == "summary"
    assert "metrics" not in result
    assert result["summary"]["badge|health"]["change_points"] == [
        {"timestamp_ms": T0 + STEP, "from": 100.0, "to": 25.0}
    ]
    assert [m["metric_key"] for m in result["missing"]] == ["SERVICE|AVAILABILITY"]


@pytest.mark.unit
def test_raw_points_stay_the_default():
    from vmware_aria.ops.resources import get_resource_metrics

    client = MagicMock()
    client.post.return_value = _stats_body({"badge|health": [100.0, 25.0]})
    result = get_resource_metrics(client, RID, ["badge|health"])

    assert result["mode"] == "raw"
    assert len(result["metrics"]["badge|health"]) == 2
    assert "summary" not in result


@pytest.mark.unit
def test_cli_summary_flag(monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_aria import cli

    client = MagicMock()
    client.post.return_value = _stats_body({"badge|health": [100.0, 25.0]})
    monkeypatch.setattr(cli, "console", Console(width=400))
    with patch.object(cli, "_get_connection", lambda target=None, config=None: (client, None)):
        outcome = CliRunner().invoke(cli.app, ["resource", "metrics", RID, "-m", "badge|health", "--summary"])

    assert outcome.exit_code == 0, outcome.output
    data = json.loads(outcome.output)
    assert data["mode"] == "summary"
    assert data["summary"]["badge|health"]["change_count"] == 1


@pytest.mark.unit
def test_mcp_summary_option(monkeypatch):
    from vmware_aria.mcp_server import server
    from vmware_aria.mcp_server.tools.resources import get_resource_metrics

    client = MagicMock()
    client.post.return_value = _stats_body({"badge|health": [100.0, 25.0]})
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    result = get_resource_metrics(RID, ["badge|health"], hours=72, summary=True)

    assert "error" not in result, result
    assert result["summary"]["badge|health"]["latest"] == 25.0

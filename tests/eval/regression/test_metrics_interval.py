"""``get_resource_metrics`` can ask Aria for coarser samples than 5 minutes.

2026-10-13, AIops#45: CPU utilisation for a month came back as raw 5-minute
samples (~8,600 points per key) and the model had to read all of them. The ops
layer always accepted ``interval_type``/``interval_quantity``, but neither the
MCP tool nor the CLI passed them on, so every query asked Aria for 5-minute
samples. With ``interval_type="DAYS"`` a month is about 30 points per key.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

RID = "ba13f0b7-19b5-4f30-b321-bc3a791d3497"
T0 = 1789200000000
DAY = 86_400_000


def _stats_body(key: str, values: list[float], step: int = DAY) -> dict:
    return {
        "values": [
            {
                "resourceId": RID,
                "stat-list": {
                    "stat": [
                        {
                            "statKey": {"key": key},
                            "timestamps": [T0 + i * step for i in range(len(values))],
                            "data": values,
                        }
                    ]
                },
            }
        ]
    }


def _client(values: list[float] | None = None) -> MagicMock:
    client = MagicMock()
    client.post.return_value = _stats_body("cpu|usage_average", values or [10.0, 20.0])
    return client


def _sent_payload(client: MagicMock) -> dict:
    return client.post.call_args.kwargs["json_data"]


@pytest.mark.unit
def test_daily_interval_reaches_the_stats_query():
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    get_resource_metrics(client, RID, ["cpu|usage_average"], interval_type="days", interval_quantity=1)

    payload = _sent_payload(client)
    assert payload["intervalType"] == "DAYS"
    assert payload["intervalQuantifier"] == 1


@pytest.mark.unit
def test_five_minutes_stays_the_default():
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    get_resource_metrics(client, RID, ["cpu|usage_average"])

    payload = _sent_payload(client)
    assert (payload["intervalType"], payload["intervalQuantifier"]) == ("MINUTES", 5)


@pytest.mark.unit
def test_the_result_states_the_resolution_it_asked_for():
    from vmware_aria.ops.resources import get_resource_metrics

    result = get_resource_metrics(
        _client(), RID, ["cpu|usage_average"], rollup_type="max", interval_type="HOURS", interval_quantity=6
    )

    assert {k: result["interval"][k] for k in ("type", "quantity", "rollup")} == {
        "type": "HOURS", "quantity": 6, "rollup": "MAX"
    }


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["DAY", "fortnight", "", "SECONDS"])
def test_an_unknown_interval_type_is_refused_before_any_request(bad):
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    with pytest.raises(ValueError) as excinfo:
        get_resource_metrics(client, RID, ["cpu|usage_average"], interval_type=bad)

    message = str(excinfo.value)
    for valid in ("MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS"):
        assert valid in message
    client.post.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1])
def test_an_interval_quantity_below_one_is_refused(bad):
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    with pytest.raises(ValueError, match="interval_quantity"):
        get_resource_metrics(client, RID, ["cpu|usage_average"], interval_quantity=bad)
    client.post.assert_not_called()


@pytest.mark.unit
def test_mcp_tool_passes_the_interval_on(monkeypatch):
    from vmware_aria.mcp_server import server
    from vmware_aria.mcp_server.tools.resources import get_resource_metrics

    client = _client([10.0, 20.0, 30.0])
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    result = get_resource_metrics(
        RID, ["cpu|usage_average"], hours=720, interval_type="DAYS", interval_quantity=1, summary=True
    )

    assert "error" not in result, result
    payload = _sent_payload(client)
    assert (payload["intervalType"], payload["intervalQuantifier"]) == ("DAYS", 1)
    assert payload["end"] - payload["begin"] == 720 * 3_600_000
    assert result["summary"]["cpu|usage_average"]["n"] == 3


@pytest.mark.unit
def test_mcp_tool_turns_a_bad_interval_into_an_error_payload(monkeypatch):
    from vmware_aria.mcp_server import server
    from vmware_aria.mcp_server.tools.resources import get_resource_metrics

    client = _client()
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    result = get_resource_metrics(RID, ["cpu|usage_average"], interval_type="DAY")

    assert "DAYS" in result["error"]
    client.post.assert_not_called()


@pytest.mark.unit
def test_cli_interval_options(monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_aria import cli

    client = _client()
    monkeypatch.setattr(cli, "console", Console(width=400))
    with patch.object(cli, "_get_connection", lambda target=None, config=None: (client, None)):
        outcome = CliRunner().invoke(
            cli.app,
            ["resource", "metrics", RID, "-m", "cpu|usage_average", "--hours", "720",
             "--interval-type", "DAYS", "--interval-quantity", "1"],
        )

    assert outcome.exit_code == 0, outcome.output
    payload = _sent_payload(client)
    assert (payload["intervalType"], payload["intervalQuantifier"]) == ("DAYS", 1)
    assert json.loads(outcome.output)["interval"]["type"] == "DAYS"


# ── Review follow-ups (2026-10-14) ──────────────────────────────────────────


def _echoing_body(interval_type: str, quantity: int, rollup: str, n: int = 3) -> dict:
    """A stats/query body that echoes its resolution the way live 8.18.7 does."""
    body = _stats_body("cpu|usage_average", [float(i) for i in range(n)])
    stat = body["values"][0]["stat-list"]["stat"][0]
    stat["rollUpType"] = rollup
    stat["intervalUnit"] = {"quantifier": quantity, "intervalType": interval_type}
    return body


@pytest.mark.unit
def test_the_result_reports_the_resolution_aria_applied():
    from vmware_aria.ops.resources import get_resource_metrics

    client = MagicMock()
    client.post.return_value = _echoing_body("DAYS", 1, "AVG")
    result = get_resource_metrics(client, RID, ["cpu|usage_average"], interval_type="DAYS", interval_quantity=1)

    assert result["interval"]["applied"] == [{"type": "DAYS", "quantity": 1, "rollup": "AVG"}]
    assert "note" not in result["interval"]


@pytest.mark.unit
def test_a_resolution_aria_changed_is_called_out():
    from vmware_aria.ops.resources import get_resource_metrics

    client = MagicMock()
    client.post.return_value = _echoing_body("MINUTES", 5, "AVG")
    result = get_resource_metrics(client, RID, ["cpu|usage_average"], interval_type="MINUTES", interval_quantity=1)

    assert result["interval"]["applied"] == [{"type": "MINUTES", "quantity": 5, "rollup": "AVG"}]
    assert "differs" in result["interval"]["note"]


@pytest.mark.unit
def test_an_unreported_resolution_is_none_not_assumed():
    from vmware_aria.ops.resources import get_resource_metrics

    result = get_resource_metrics(_client(), RID, ["cpu|usage_average"], interval_type="DAYS", interval_quantity=1)

    assert result["interval"]["applied"] is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["AVERAGE", "p95", "NONE"])
def test_an_unknown_rollup_is_refused_before_any_request(bad):
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    with pytest.raises(ValueError) as excinfo:
        get_resource_metrics(client, RID, ["cpu|usage_average"], rollup_type=bad)
    for valid in ("AVG", "MAX", "MIN", "SUM", "COUNT", "LATEST"):
        assert valid in str(excinfo.value)
    client.post.assert_not_called()


@pytest.mark.unit
def test_mcp_bad_interval_is_refused_before_connecting(monkeypatch):
    from vmware_aria.mcp_server import server
    from vmware_aria.mcp_server.tools.resources import get_resource_metrics

    def _no_connection(target=None):
        raise AssertionError("connected before validating the interval")

    monkeypatch.setattr(server, "_get_connection", _no_connection)
    result = get_resource_metrics(RID, ["cpu|usage_average"], interval_type="DAY")

    assert "DAYS" in result["error"]
    assert "doctor" not in result.get("hint", "")


@pytest.mark.unit
def test_mcp_defaults_and_rollup_reach_the_query(monkeypatch):
    from vmware_aria.mcp_server import server
    from vmware_aria.mcp_server.tools.resources import get_resource_metrics

    client = _client()
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    get_resource_metrics(RID, ["cpu|usage_average"], rollup_type="max")

    payload = _sent_payload(client)
    assert (payload["intervalType"], payload["intervalQuantifier"], payload["rollUpType"]) == ("MINUTES", 5, "MAX")


def _run_cli(args: list[str], client: MagicMock, monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_aria import cli

    monkeypatch.setattr(cli, "console", Console(width=400))
    with patch.object(cli, "_get_connection", lambda target=None, config=None: (client, None)):
        return CliRunner().invoke(cli.app, ["resource", "metrics", RID, "-m", "cpu|usage_average", *args])


@pytest.mark.unit
def test_cli_defaults_and_rollup_reach_the_query(monkeypatch):
    client = _client()
    outcome = _run_cli(["--rollup", "MAX"], client, monkeypatch)

    assert outcome.exit_code == 0, outcome.output
    payload = _sent_payload(client)
    assert (payload["intervalType"], payload["intervalQuantifier"], payload["rollUpType"]) == ("MINUTES", 5, "MAX")


@pytest.mark.unit
def test_cli_bad_interval_is_a_teaching_error_before_connecting(monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_aria import cli

    def _no_connection(target=None, config=None):
        raise AssertionError("connected before validating the interval")

    monkeypatch.setattr(cli, "console", Console(width=400))
    monkeypatch.setattr(cli, "_get_connection", _no_connection)
    outcome = CliRunner().invoke(cli.app, ["resource", "metrics", RID, "--interval-type", "DAY"])

    assert outcome.exit_code == 2, outcome.output
    assert "DAYS" in outcome.output
    assert "Traceback" not in outcome.output


# ── Re-review follow-ups: an echo Aria did not fully give is not a difference ──


def _body_with_unit(unit, rollup="AVG") -> dict:
    body = _stats_body("cpu|usage_average", [1.0, 2.0])
    stat = body["values"][0]["stat-list"]["stat"][0]
    if unit is not None:
        stat["intervalUnit"] = unit
    if rollup is not None:
        stat["rollUpType"] = rollup
    return body


def _report(body: dict, *, interval_type="MINUTES", interval_quantity=5, rollup_type="AVG") -> dict:
    from vmware_aria.ops.resources import get_resource_metrics

    client = MagicMock()
    client.post.return_value = body
    return get_resource_metrics(
        client, RID, ["cpu|usage_average"], interval_type=interval_type,
        interval_quantity=interval_quantity, rollup_type=rollup_type,
    )["interval"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "unit, rollup",
    [
        ({"quantifier": "5", "intervalType": "MINUTES"}, "AVG"),
        ({"quantifier": 5, "intervalType": "minutes"}, "Avg"),
    ],
)
def test_a_differently_spelled_echo_is_the_same_resolution(unit, rollup):
    report = _report(_body_with_unit(unit, rollup))

    assert report["applied"] == [{"type": "MINUTES", "quantity": 5, "rollup": "AVG"}]
    assert "note" not in report


@pytest.mark.unit
@pytest.mark.parametrize(
    "unit, rollup",
    [
        ({"quantifier": 5, "intervalType": "MINUTES"}, None),
        ({}, "AVG"),
        ({"quantifier": "five", "intervalType": "MINUTES"}, "AVG"),
    ],
)
def test_an_incomplete_echo_is_not_said_rather_than_different(unit, rollup):
    report = _report(_body_with_unit(unit, rollup))

    assert report["applied"] is None
    assert report["series_without_echo"] == 1
    assert "note" not in report


@pytest.mark.unit
def test_live_8187_hours_capture_echoes_what_was_asked():
    """Positive control: real 8.18.7 body captured with HOURS/1/AVG."""
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "aria_8187" / "platform_stats_query.json"
    body = json.loads(fixture.read_text(encoding="utf-8"))["body"]
    report = _report(body, interval_type="HOURS", interval_quantity=1)

    assert report["applied"] == [{"type": "HOURS", "quantity": 1, "rollup": "AVG"}]
    assert "note" not in report

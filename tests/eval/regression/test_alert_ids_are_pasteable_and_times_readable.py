"""Alert output a person can act on: whole IDs and readable times.

2026-09-15 on Aria Operations 8.18.7:

* ``alert list`` at the default terminal width cut IDs to ``ba793832-5…``,
  which cannot be pasted into ``alert get``.
* ``alert get`` gave its times only as epoch milliseconds.

IDs now never shrink, the table has a start-time column, ``--json`` prints
the envelope, and both list rows and ``alert get`` carry ISO-8601 UTC times
beside the millisecond fields.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

AID = "ba793832-533a-4c87-bee4-217ed3747c85"
RID = "31aa1b41-2ae3-479b-8e25-176016ad874e"
EPOCH_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z
EPOCH_ISO = "2023-11-14T22:13:20.000Z"


def _raw_alert(**overrides: Any) -> dict:
    alert = {
        "alertId": AID,
        "resourceId": RID,
        "alertLevel": "CRITICAL",
        "status": "ACTIVE",
        "startTimeUTC": EPOCH_MS,
        "updateTimeUTC": EPOCH_MS,
        "cancelTimeUTC": 0,
        "controlState": "OPEN",
        "alertDefinitionId": "AlertDefinition-VCAPPHealthStatus",
        "alertDefinitionName": "vCenter app health is affected",
        "alertImpact": "HEALTH",
    }
    alert.update(overrides)
    return alert


class _Client:
    def __init__(self, alert: dict) -> None:
        self.alert = alert

    def post(self, path: str, json_data: Any = None, params: Any = None, **_kw: Any) -> Any:
        assert path == "/alerts/query", path
        return {"alerts": [self.alert], "pageInfo": {"totalCount": 1}}

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        if path == "/resources":
            return {"resourceList": [{"identifier": RID, "resourceKey": {
                "name": "vCenter-192.0.2.16", "resourceKindKey": "VC_APP"}}]}
        if path == f"/alerts/{AID}":
            return self.alert
        if path == "/alerts/contributingsymptoms":
            return {"contributingSymptoms": []}
        raise AssertionError(f"unexpected GET {path}")


@pytest.mark.unit
def test_list_rows_carry_iso_start_and_update_times():
    from vmware_aria.ops.alerts import list_alerts

    row = list_alerts(_Client(_raw_alert()))["items"][0]

    assert row["start_time_ms"] == EPOCH_MS
    assert row["start_time_utc"] == EPOCH_ISO
    assert row["update_time_utc"] == EPOCH_ISO


@pytest.mark.unit
def test_get_alert_carries_iso_times_and_an_uncancelled_alert_has_none():
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_Client(_raw_alert()), AID)

    assert result["start_time_ms"] == EPOCH_MS
    assert result["start_time_utc"] == EPOCH_ISO
    assert result["update_time_utc"] == EPOCH_ISO
    assert result["cancel_time_ms"] == 0
    assert result["cancel_time_utc"] is None


@pytest.mark.unit
def test_get_alert_cancel_time_keeps_its_milliseconds():
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_Client(_raw_alert(status="CANCELED", cancelTimeUTC=EPOCH_MS + 500)), AID)

    assert result["cancel_time_utc"] == "2023-11-14T22:13:20.500Z"


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, 0, -5, "1700000000000", True, 1.5])
def test_times_that_are_not_positive_integer_ms_have_no_iso_form(value):
    from vmware_aria.ops._collection import iso_utc_or_none

    assert iso_utc_or_none(value) is None


def _invoke(monkeypatch, width: int, *args: str):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_aria import cli

    monkeypatch.setattr(cli, "console", Console(width=width))
    with patch.object(cli, "_get_connection", lambda target=None, config=None: (_Client(_raw_alert()), None)):
        return CliRunner().invoke(cli.app, ["alert", "list", *args])


@pytest.mark.unit
def test_alert_list_prints_whole_ids_at_a_narrow_terminal(monkeypatch):
    outcome = _invoke(monkeypatch, 80)

    assert outcome.exit_code == 0, outcome.output
    assert AID in outcome.output
    assert RID in outcome.output
    assert "Started" in outcome.output


@pytest.mark.unit
def test_alert_list_table_at_a_wide_terminal_has_whole_ids_and_a_start_column(monkeypatch):
    outcome = _invoke(monkeypatch, 200)

    assert outcome.exit_code == 0, outcome.output
    assert AID in outcome.output and RID in outcome.output
    assert "Started (UTC)" in outcome.output
    assert "2023-11-14 22:13" in outcome.output
    assert "…" not in outcome.output


@pytest.mark.unit
def test_alert_list_json_prints_the_envelope(monkeypatch):
    outcome = _invoke(monkeypatch, 400, "--json")

    assert outcome.exit_code == 0, outcome.output
    data = json.loads(outcome.output)
    assert data["items"][0]["id"] == AID
    assert data["items"][0]["start_time_utc"] == EPOCH_ISO

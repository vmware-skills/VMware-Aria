"""list_maintenance_schedules (READ): recurring maintenance windows.

The live 8.18.7 appliance has no schedules, so its answer is pinned as the
*recognised* empty case. The populated shape is the 9.1 OpenAPI example body
for ``GET /api/maintenanceschedules`` (three schedules: DAILY / MONTHLY /
WEEKLY), transcribed verbatim.

The MaintenanceSchedule model is ``id`` + ``key`` + ``schedule`` — it carries no
resource list. Which resources a schedule applies to is only answerable through
the ``resourceId`` filter, so that is what the tool offers instead of inventing
a field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aria_8187"

#: GET /api/maintenanceschedules 200 example, VCF Operations 9.1.0.0 OpenAPI.
SPEC_EXAMPLE = {
    "schedules": [
        {
            "id": "f5534f68-0139-4c57-aac0-fef0005d13d3",
            "key": "daily-ms1",
            "schedule": {
                "hour": 3, "minuteOfTheHour": 0, "duration": 180, "scheduleType": "DAILY",
                "recurrence": 1, "expirationDate": "01/20/2050", "timeZone": "Asia/Yerevan",
            },
        },
        {
            "id": "0f5d8313-079c-4c00-ae9e-cb14d5e38443",
            "key": "monthly-ms1",
            "schedule": {
                "hour": 3, "minuteOfTheHour": 0, "duration": 3, "scheduleType": "MONTHLY",
                "daysOfTheMonth": ["1", "2"], "months": [1], "timeZone": "Asia/Yerevan", "expireRuns": 3,
            },
        },
        {
            "id": "d07b52b5-6839-4ced-8809-7e8afc527059",
            "key": "weekly-ms1",
            "schedule": {
                "hour": 15, "minuteOfTheHour": 3, "duration": 1, "scheduleType": "WEEKLY", "recurrence": 5,
                "daysOfTheWeek": ["SATURDAY", "SUNDAY"], "timeZone": "Asia/Yerevan", "expireRuns": 7,
            },
        },
    ]
}


class _Client:
    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.calls: list[tuple[str, Any]] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append((path, params))
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer


def _live_empty() -> dict:
    return json.loads((FIXTURES / "maintenanceschedules_empty.json").read_text(encoding="utf-8"))["body"]


def test_live_empty_answer_is_empty_and_says_nothing_is_unknown() -> None:
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    client = _Client(_live_empty())
    result = list_maintenance_schedules(client)
    assert result["items"] == []
    assert result["total"] == 0
    assert result["schedules_note"] is None
    assert client.calls[0][0] == "/maintenanceschedules"


def test_spec_example_rows_are_mapped() -> None:
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    rows = list_maintenance_schedules(_Client(SPEC_EXAMPLE))["items"]
    assert [r["name"] for r in rows] == ["daily-ms1", "monthly-ms1", "weekly-ms1"]
    daily, monthly, weekly = rows
    assert daily["id"] == "f5534f68-0139-4c57-aac0-fef0005d13d3"
    assert daily["schedule_type"] == "DAILY"
    assert daily["start_hour"] == 3 and daily["start_minute"] == 0
    assert daily["duration_minutes"] == 180
    assert daily["recurrence"] == 1
    assert daily["expiration_date"] == "01/20/2050"
    assert daily["time_zone"] == "Asia/Yerevan"
    assert monthly["days_of_month"] == ["1", "2"] and monthly["months"] == [1]
    assert monthly["expire_runs"] == 3
    assert weekly["days_of_week"] == ["SATURDAY", "SUNDAY"]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"pageInfo": {"totalCount": 3}}, id="container-missing"),
        pytest.param({"schedules": "not-a-list"}, id="container-wrong-type"),
        pytest.param([], id="body-not-an-object"),
    ],
)
def test_an_unrecognised_answer_is_unknown_not_no_schedules(answer: Any) -> None:
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    result = list_maintenance_schedules(_Client(answer))
    assert result["items"] == []
    assert result["schedules_note"], "an answer this could not read must not look like 'no schedules'"
    assert result["total"] is None


def test_a_failed_read_raises_rather_than_answering_empty() -> None:
    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    with pytest.raises(AriaApiError):
        list_maintenance_schedules(_Client(AriaApiError("HTTP 503", status_code=503)))


def test_resource_filter_is_sent_as_resource_id() -> None:
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    client = _Client(_live_empty())
    list_maintenance_schedules(client, resource_id="r-1")
    assert client.calls[0][1]["resourceId"] == ["r-1"]


def test_paging_window_and_next_offset() -> None:
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    result = list_maintenance_schedules(_Client({**SPEC_EXAMPLE, "pageInfo": {"totalCount": 3}}), limit=1, offset=1)
    assert [r["name"] for r in result["items"]] == ["monthly-ms1"]
    assert result["next_offset"] == 2
    last = list_maintenance_schedules(_Client({**SPEC_EXAMPLE, "pageInfo": {"totalCount": 3}}), limit=2, offset=1)
    assert last["next_offset"] is None


def test_page_args_out_of_range_are_rejected() -> None:
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    with pytest.raises(ValueError):
        list_maintenance_schedules(_Client(_live_empty()), limit=0)


def test_schedule_text_is_sanitized() -> None:
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    body = {"schedules": [{"id": "s-1", "key": "evil\x1b]0;x\x07key", "schedule": {"timeZone": "UTC\x00"}}]}
    (row,) = list_maintenance_schedules(_Client(body))["items"]
    assert "\x1b" not in row["name"] and "\x07" not in row["name"]
    assert "\x00" not in row["time_zone"]


def test_mcp_tool_is_read_and_returns_the_envelope(monkeypatch) -> None:
    import asyncio

    import vmware_aria.mcp_server.server as server

    monkeypatch.setattr(server, "_get_connection", lambda target=None: _Client(SPEC_EXAMPLE))
    result = server.list_maintenance_schedules()
    assert len(result["items"]) == 3
    tool = {t.name: t for t in asyncio.run(server.mcp.list_tools())}["list_maintenance_schedules"]
    assert tool.annotations.readOnlyHint is True
    assert tool.description.startswith("[READ]")

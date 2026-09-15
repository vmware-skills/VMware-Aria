"""Alert notes: list_alert_notes (READ) and add_alert_note (WRITE).

Why: an alert has no field for "who is handling this and what was done". The
suite-api keeps that as notes on the alert.

Shapes: the live 8.18.7 appliance has no notes on any of its 10 active alerts,
so its answer pins the recognised empty case and its 404 body pins the unknown
alert. The populated rows and the POST answer are the 9.1 OpenAPI examples. The
write is exercised only against a recording client — no note was created on a
real appliance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from vmware_aria.connection import AriaApiError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aria_8187"
AID = "8bbb1892-9819-4982-8bed-fbfe30fce1a4"

#: GET /api/alerts/{id}/notes 200 example, VCF Operations 9.1.0.0 OpenAPI.
SPEC_NOTES = {
    "alertNotes": [
        {
            "id": "ea3f7bcc-12f0-432b-9ba4-a808b14b8891", "alertId": "0d9ff4f7-1603-43c9-b51d-db2b5b47c65e",
            "creationTimeUTC": 0, "type": "USER", "userId": "2d8b511a-676a-4b9b-a032-aae9278c4f1f",
            "userName": "testUser", "note": "sample note",
        },
        {
            "id": "24c93393-eba2-450c-85fb-105c6d97c7ab", "alertId": "0d9ff4f7-1603-43c9-b51d-db2b5b47c65e",
            "creationTimeUTC": 0, "type": "SYSTEM", "userId": "2d8b511a-676a-4b9b-a032-aae9278c4f1f",
            "userName": "testUser", "note": "sample note",
        },
    ]
}

#: POST /api/alerts/{id}/notes 201 example, VCF Operations 9.1.0.0 OpenAPI.
SPEC_CREATED = SPEC_NOTES["alertNotes"][0]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["body"]


class _Client:
    def __init__(self, get_answer: Any = None, post_answer: Any = None) -> None:
        self._get = get_answer
        self._post = post_answer
        self.calls: list[tuple[str, str, Any, Any]] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(("GET", path, params, None))
        if isinstance(self._get, BaseException):
            raise self._get
        return self._get

    def post(self, path: str, json_data: Any = None, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(("POST", path, params, json_data))
        return self._post


# ---------------------------------------------------------------------------
# list_alert_notes
# ---------------------------------------------------------------------------


def test_live_alert_without_notes_is_empty_and_known() -> None:
    from vmware_aria.ops.alert_notes import list_alert_notes

    client = _Client(_fixture("alert_notes_empty.json"))
    result = list_alert_notes(client, AID)
    assert result["items"] == []
    assert result["total"] == 0
    assert result["notes_note"] is None
    assert client.calls[0][1] == f"/alerts/{AID}/notes"


def test_spec_rows_are_mapped() -> None:
    from vmware_aria.ops.alert_notes import list_alert_notes

    rows = list_alert_notes(_Client(SPEC_NOTES), AID)["items"]
    assert rows[0] == {
        "id": "ea3f7bcc-12f0-432b-9ba4-a808b14b8891",
        "note": "sample note",
        "type": "USER",
        "user_name": "testUser",
        "user_id": "2d8b511a-676a-4b9b-a032-aae9278c4f1f",
        "created_time_ms": 0,
    }
    assert rows[1]["type"] == "SYSTEM"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"pageInfo": {"totalCount": 2}}, id="container-missing"),
        pytest.param({"alertNotes": {"note": "x"}}, id="container-wrong-type"),
        pytest.param("<html>", id="body-not-an-object"),
    ],
)
def test_an_unrecognised_answer_is_unknown_not_no_notes(answer: Any) -> None:
    from vmware_aria.ops.alert_notes import list_alert_notes

    result = list_alert_notes(_Client(answer), AID)
    assert result["items"] == []
    assert result["notes_note"]
    assert result["total"] is None


def test_unknown_alert_404_is_an_error_not_an_empty_list(monkeypatch) -> None:
    import vmware_aria.mcp_server.server as server

    live = _fixture("alert_notes_404.json")
    err = AriaApiError(live["error"], status_code=404, method="GET", path=f"/alerts/{AID}/notes", body=live["body"])
    monkeypatch.setattr(server, "_get_connection", lambda target=None: _Client(err))
    result = server.list_alert_notes(AID)
    assert "error" in result and "items" not in result
    assert "404" in result["error"]


def test_note_text_is_sanitized() -> None:
    from vmware_aria.ops.alert_notes import list_alert_notes

    body = {"alertNotes": [{**SPEC_CREATED, "note": "restart\x1b[2J done\x07", "userName": "ops\x00"}]}
    (row,) = list_alert_notes(_Client(body), AID)["items"]
    assert "\x1b" not in row["note"] and "\x07" not in row["note"]
    assert "\x00" not in row["user_name"]


def test_page_args_are_validated_and_sent() -> None:
    from vmware_aria.ops.alert_notes import list_alert_notes

    with pytest.raises(ValueError):
        list_alert_notes(_Client(SPEC_NOTES), AID, limit=501)
    client = _Client(SPEC_NOTES)
    assert [r["type"] for r in list_alert_notes(client, AID, limit=1, offset=1)["items"]] == ["SYSTEM"]


def test_empty_alert_id_is_refused_without_a_call() -> None:
    from vmware_aria.ops.alert_notes import add_alert_note, list_alert_notes

    client = _Client(SPEC_NOTES, SPEC_CREATED)
    with pytest.raises(ValueError, match="list_alerts"):
        list_alert_notes(client, "")
    with pytest.raises(ValueError, match="list_alerts"):
        add_alert_note(client, "", "text")
    assert client.calls == []


# ---------------------------------------------------------------------------
# add_alert_note
# ---------------------------------------------------------------------------


def test_add_posts_content_and_returns_the_created_note() -> None:
    from vmware_aria.ops.alert_notes import add_alert_note

    client = _Client(post_answer=SPEC_CREATED)
    audit = MagicMock()
    result = add_alert_note(client, AID, "  Taking this — rebooting host  ", audit_logger=audit, target_name="home-aria")
    assert client.calls == [("POST", f"/alerts/{AID}/notes", None, {"content": "Taking this — rebooting host"})]
    assert result["created"]["id"] == SPEC_CREATED["id"]
    assert result["confirmation_note"] is None
    row = audit.log.call_args.kwargs
    assert row["operation"] == "add_alert_note"
    assert row["resource"] == f"alert/{AID}"
    assert row["target"] == "home-aria"
    assert row["parameters"]["note"] == "Taking this — rebooting host"


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_an_empty_note_is_refused_without_a_call(text: str) -> None:
    from vmware_aria.ops.alert_notes import add_alert_note

    client = _Client(post_answer=SPEC_CREATED)
    with pytest.raises(ValueError):
        add_alert_note(client, AID, text)
    assert client.calls == []


def test_an_answer_without_a_note_id_is_unconfirmed() -> None:
    from vmware_aria.ops.alert_notes import add_alert_note

    result = add_alert_note(_Client(post_answer={}), AID, "handled")
    assert result["created"] is None
    assert "list_alert_notes" in result["confirmation_note"]


def test_mcp_add_is_a_low_risk_write() -> None:
    import asyncio

    import vmware_aria.mcp_server.server as server

    tool = {t.name: t for t in asyncio.run(server.mcp.list_tools())}["add_alert_note"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.description.startswith("[WRITE]")
    assert server.add_alert_note._risk_level == "low"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(args: list[str], answers: str = ""):
    from vmware_aria import cli

    connect = MagicMock(return_value=(MagicMock(), MagicMock(default_target="home-aria")))
    with patch.object(cli, "_get_connection", connect), \
         patch.object(cli, "_audit", MagicMock()), \
         patch("vmware_aria.ops.alert_notes.add_alert_note") as add, \
         patch("vmware_aria.ops.alert_notes.list_alert_notes") as notes:
        add.return_value = {"alert_id": "a0000000-0000-4000-8000-00000000000a", "created": {"id": "n-1"}, "confirmation_note": None}
        notes.return_value = {"items": [], "notes_note": None, "next_offset": None}
        result = CliRunner().invoke(cli.app, args, input=answers)
    return result, connect, add, notes


def test_cli_note_add_dry_run_prints_the_call_and_makes_none() -> None:
    result, connect, add, _ = _cli(["alert", "note-add", "a0000000-0000-4000-8000-00000000000a", "rebooting host", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "POST" in result.output and "/alerts/a0000000-0000-4000-8000-00000000000a/notes" in result.output
    assert "rebooting host" in result.output
    connect.assert_not_called()
    add.assert_not_called()


def test_cli_note_add_stops_when_declined_and_runs_when_confirmed() -> None:
    declined, _, add, _ = _cli(["alert", "note-add", "a0000000-0000-4000-8000-00000000000a", "rebooting host"], "n\n")
    assert declined.exit_code != 0
    add.assert_not_called()

    accepted, _, add, _ = _cli(["alert", "note-add", "a0000000-0000-4000-8000-00000000000a", "rebooting host"], "y\n")
    assert accepted.exit_code == 0, accepted.output
    add.assert_called_once()

    flagged, _, add, _ = _cli(["alert", "note-add", "a0000000-0000-4000-8000-00000000000a", "rebooting host", "--yes"])
    assert flagged.exit_code == 0, flagged.output
    add.assert_called_once()


def test_cli_notes_lists() -> None:
    result, _, _, notes = _cli(["alert", "notes", "a0000000-0000-4000-8000-00000000000a"])
    assert result.exit_code == 0, result.output
    notes.assert_called_once()


def test_cli_guarded_name_equals_the_mcp_tool_name() -> None:
    import vmware_aria.mcp_server.server as server
    from vmware_aria import cli_workflow

    assert cli_workflow.alert_note_add._guarded_tool == "add_alert_note"
    assert cli_workflow.alert_note_add._risk_level == server.add_alert_note._risk_level


@pytest.mark.parametrize(
    "args",
    [["alert", "note-add", " ", "rebooting host", "--dry-run"], ["alert", "note-add", "a0000000-0000-4000-8000-00000000000a", "   ", "--dry-run"]],
    ids=["blank-alert-id", "blank-text"],
)
def test_cli_note_add_dry_run_refuses_blank_arguments(args: list[str]) -> None:
    result, connect, add, _ = _cli(args)
    assert result.exit_code == 2, result.output
    assert "DRY-RUN" not in result.output
    connect.assert_not_called()
    add.assert_not_called()


def test_cli_note_add_dry_run_prints_the_id_and_text_the_real_call_would_send() -> None:
    result, _, _, _ = _cli(["alert", "note-add", " a0000000-0000-4000-8000-00000000000a ", "  rebooting host ", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "/alerts/a0000000-0000-4000-8000-00000000000a/notes" in result.output
    assert '{"content": "rebooting host"}' in result.output

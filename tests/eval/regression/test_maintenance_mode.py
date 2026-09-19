"""Resource maintenance mode: start / end (WRITE) with before and after state.

Why this exists: powering the Aria appliance off for a memory upgrade
(2026-09-13) made its own resources alert on themselves. Maintenance mode is
the suite-api's answer — ``PUT /resources/{id}/maintained`` stops alerting and
collection for the resource, ``DELETE`` brings it back.

What is pinned here, and why each matters:

* **Request shape.** ``duration`` (minutes) and ``end`` (epoch ms) are the only
  query parameters the 9.1 OpenAPI documents; with neither the resource enters
  ``MAINTAINED_MANUAL`` and stays there until someone ends it. Sending both is
  refused rather than letting the appliance pick one.
* **State is read, not assumed.** The result carries the state before and after
  the write. A failed read is *unknown* (``in_maintenance: None``), never
  ``False`` — reporting "not in maintenance" because a GET timed out would tell
  the operator the write did nothing when it may well have worked.
* **End refuses a resource that is confirmed not in maintenance.** What the
  DELETE does to a STOPPED resource is undocumented; the tool does not find out.
* **Dry-run makes no call, and nothing runs without confirmation.**

The GET body is the live 8.18.7 capture for VM test-llm; the writes are only
ever exercised against a recording client. No write was sent to a real
appliance to produce these tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from vmware_aria.connection import AriaApiError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aria_8187"
RID = "f654322a-50c3-4a9a-937c-2d4bae8a2b36"


def _resource(*states: str, name: str | None = None) -> dict:
    body = json.loads((FIXTURES / "resource_test_llm.json").read_text(encoding="utf-8"))["body"]
    template = body["resourceStatusStates"][0]
    out = {**body, "resourceStatusStates": [{**template, "resourceState": s} for s in states]}
    if name is not None:
        out = {**out, "resourceKey": {**body["resourceKey"], "name": name}}
    return out


def _unavailable() -> AriaApiError:
    return AriaApiError("Aria Operations returned HTTP 503.", status_code=503, method="GET", path=f"/resources/{RID}")


class _Client:
    """Records every call. GETs answer from a queue; an exception is raised."""

    def __init__(self, *gets: Any) -> None:
        self._gets = list(gets)
        self.calls: list[tuple[str, str, Any, Any]] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(("GET", path, params, None))
        answer = self._gets.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def put(self, path: str, json_data: Any = None, params: Any = None) -> dict:
        self.calls.append(("PUT", path, params, json_data))
        return {}

    def delete(self, path: str) -> None:
        self.calls.append(("DELETE", path, None, None))

    def post(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover - must never happen
        raise AssertionError("maintenance mode never POSTs")

    @property
    def writes(self) -> list[tuple[str, str, Any, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------


def test_live_started_resource_reads_as_not_in_maintenance() -> None:
    from vmware_aria.ops.maintenance import read_maintenance_state

    state = read_maintenance_state(_Client(_resource("STARTED")), RID)
    assert state["in_maintenance"] is False
    assert state["maintenance_mode"] is None
    assert state["name"] == "test-llm"
    assert state["kind"] == "VirtualMachine"
    assert state["adapter_states"][0]["state"] == "STARTED"
    assert state["note"] is None


@pytest.mark.parametrize("mode", ["MAINTAINED", "MAINTAINED_MANUAL"])
def test_both_maintenance_states_read_as_in_maintenance(mode: str) -> None:
    from vmware_aria.ops.maintenance import read_maintenance_state

    state = read_maintenance_state(_Client(_resource(mode)), RID)
    assert state["in_maintenance"] is True
    assert state["maintenance_mode"] == mode


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_resource(), id="no-adapter-states"),
        pytest.param({k: v for k, v in _resource("STARTED").items() if k != "resourceStatusStates"}, id="key-missing"),
        pytest.param(_resource("SOMETHING_NEW"), id="unrecognised-state"),
        pytest.param(_resource("MAINTAINED", "STARTED"), id="mixed-across-adapters"),
    ],
)
def test_a_state_this_cannot_read_is_unknown_not_false(body: dict) -> None:
    from vmware_aria.ops.maintenance import read_maintenance_state

    state = read_maintenance_state(_Client(body), RID)
    assert state["in_maintenance"] is None, "unknown must not be reported as 'not in maintenance'"
    assert state["note"], "an unknown state must say why it is unknown"


def test_resource_name_is_sanitized() -> None:
    from vmware_aria.ops.maintenance import read_maintenance_state

    state = read_maintenance_state(_Client(_resource("STARTED", name="test\x1b[31m\x07-llm")), RID)
    assert "\x1b" not in state["name"] and "\x07" not in state["name"]


# ---------------------------------------------------------------------------
# start_resource_maintenance
# ---------------------------------------------------------------------------


def test_timed_start_sends_duration_and_reports_before_and_after() -> None:
    from vmware_aria.ops.maintenance import start_resource_maintenance

    client = _Client(_resource("STARTED"), _resource("MAINTAINED"))
    audit = MagicMock()
    result = start_resource_maintenance(client, RID, duration_minutes=90, audit_logger=audit, target_name="home-aria")

    assert client.writes == [("PUT", f"/resources/{RID}/maintained", {"duration": 90}, None)]
    assert result["requested"] == {"mode": "timed", "duration_minutes": 90, "end_time_ms": None}
    assert result["before"]["in_maintenance"] is False
    assert result["after"]["in_maintenance"] is True
    assert result["after"]["maintenance_mode"] == "MAINTAINED"
    assert result["confirmed"] is True
    audit.log.assert_called_once()
    row = audit.log.call_args.kwargs
    assert row["operation"] == "start_resource_maintenance"
    assert row["target"] == "home-aria"
    assert row["resource"] == f"resource/{RID}"
    assert row["before_state"]["in_maintenance"] is False
    assert row["after_state"]["in_maintenance"] is True


def test_start_until_an_end_time_sends_end() -> None:
    from vmware_aria.ops.maintenance import start_resource_maintenance

    end = int(time.time() * 1000) + 3_600_000
    client = _Client(_resource("STARTED"), _resource("MAINTAINED"))
    result = start_resource_maintenance(client, RID, end_time_ms=end)
    assert client.writes == [("PUT", f"/resources/{RID}/maintained", {"end": end}, None)]
    assert result["requested"]["mode"] == "timed"


def test_start_without_a_window_is_manual_and_says_it_stays() -> None:
    from vmware_aria.ops.maintenance import start_resource_maintenance

    client = _Client(_resource("STARTED"), _resource("MAINTAINED_MANUAL"))
    result = start_resource_maintenance(client, RID)
    assert client.writes == [("PUT", f"/resources/{RID}/maintained", None, None)]
    assert result["requested"]["mode"] == "manual"
    assert "end_resource_maintenance" in result["note"]


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"duration_minutes": 30, "end_time_ms": int(time.time() * 1000) + 60_000}, id="both"),
        pytest.param({"duration_minutes": 0}, id="zero-duration"),
        pytest.param({"duration_minutes": -5}, id="negative-duration"),
        pytest.param({"duration_minutes": True}, id="bool-duration"),
        pytest.param({"end_time_ms": int(time.time() * 1000) - 60_000}, id="end-in-the-past"),
        pytest.param({"end_time_ms": int(time.time())}, id="end-in-seconds-not-ms"),
    ],
)
def test_a_window_that_cannot_mean_what_it_says_is_refused_before_any_call(kwargs: dict) -> None:
    from vmware_aria.ops.maintenance import start_resource_maintenance

    client = _Client()
    with pytest.raises(ValueError):
        start_resource_maintenance(client, RID, **kwargs)
    assert client.calls == []


def test_empty_resource_id_is_refused() -> None:
    from vmware_aria.ops.maintenance import end_resource_maintenance, start_resource_maintenance

    for fn in (start_resource_maintenance, end_resource_maintenance):
        client = _Client()
        with pytest.raises(ValueError, match="list_resources"):
            fn(client, "")
        assert client.calls == []


def test_a_failed_after_read_leaves_the_change_unconfirmed_not_failed() -> None:
    from vmware_aria.ops.maintenance import start_resource_maintenance

    client = _Client(_resource("STARTED"), _unavailable())
    result = start_resource_maintenance(client, RID, duration_minutes=10)
    assert len(client.writes) == 1
    assert result["after"]["in_maintenance"] is None
    assert result["after"]["read_error"]
    assert result["confirmed"] is None
    assert "not confirmed" in result["note"]


def test_after_read_that_disagrees_is_reported_not_hidden() -> None:
    from vmware_aria.ops.maintenance import start_resource_maintenance

    client = _Client(_resource("STARTED"), _resource("STARTED"))
    result = start_resource_maintenance(client, RID, duration_minutes=10)
    assert result["confirmed"] is False
    assert result["note"]


def test_a_failed_before_read_does_not_block_the_write() -> None:
    from vmware_aria.ops.maintenance import start_resource_maintenance

    client = _Client(_unavailable(), _resource("MAINTAINED"))
    result = start_resource_maintenance(client, RID, duration_minutes=10)
    assert len(client.writes) == 1
    assert result["before"]["in_maintenance"] is None
    assert result["confirmed"] is True


# ---------------------------------------------------------------------------
# end_resource_maintenance
# ---------------------------------------------------------------------------


def test_end_sends_delete_and_reports_before_and_after() -> None:
    from vmware_aria.ops.maintenance import end_resource_maintenance

    client = _Client(_resource("MAINTAINED_MANUAL"), _resource("STARTED"))
    audit = MagicMock()
    result = end_resource_maintenance(client, RID, audit_logger=audit, target_name="home-aria")
    assert client.writes == [("DELETE", f"/resources/{RID}/maintained", None, None)]
    assert result["before"]["maintenance_mode"] == "MAINTAINED_MANUAL"
    assert result["after"]["in_maintenance"] is False
    assert result["confirmed"] is True
    assert audit.log.call_args.kwargs["operation"] == "end_resource_maintenance"


def test_end_refuses_a_resource_confirmed_not_in_maintenance() -> None:
    from vmware_aria.ops.maintenance import end_resource_maintenance

    client = _Client(_resource("STOPPED"))
    with pytest.raises(ValueError, match="not in maintenance"):
        end_resource_maintenance(client, RID)
    assert client.writes == []


def test_end_proceeds_when_the_before_state_is_unknown() -> None:
    from vmware_aria.ops.maintenance import end_resource_maintenance

    client = _Client(_unavailable(), _resource("STARTED"))
    result = end_resource_maintenance(client, RID)
    assert client.writes == [("DELETE", f"/resources/{RID}/maintained", None, None)]
    assert result["before"]["in_maintenance"] is None


# ---------------------------------------------------------------------------
# MCP tools: preview gate and undo
# ---------------------------------------------------------------------------


class _UndoStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, **kw: Any) -> str:
        self.rows.append(kw)
        return "undo-1"


@pytest.fixture
def undo_store(monkeypatch: pytest.MonkeyPatch) -> _UndoStore:
    store = _UndoStore()
    monkeypatch.setattr("vmware_policy.undo.get_undo_store", lambda: store)
    return store


# Updated 2026-09-19 (HLD §7): the MCP preview now *measures* — it reads the
# resource to state the blast radius — so it connects; it still writes nothing.
# A confirmed call reads the state three times: the gate's measurement, then
# the ops function's own before and after. Every _Client below queues that
# extra first answer.


def test_mcp_tools_preview_without_writing_or_recording_undo(monkeypatch, undo_store) -> None:
    import vmware_aria.mcp_server.server as server

    client = _Client(_resource("STARTED"), _resource("MAINTAINED"))
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    start = server.start_resource_maintenance(RID, duration_minutes=30)
    end = server.end_resource_maintenance(RID)
    for result in (start, end):
        assert result["action"] == "preview"
        assert "confirm=True" in result["hint"]
    assert client.writes == []
    assert undo_store.rows == [], "a preview changed nothing, so it has nothing to undo"


def test_confirmed_start_records_end_as_its_undo(monkeypatch, undo_store) -> None:
    import vmware_aria.mcp_server.server as server

    client = _Client(_resource("STARTED"), _resource("STARTED"), _resource("MAINTAINED"))
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    result = server.start_resource_maintenance(RID, duration_minutes=30, confirm=True, target="home-aria")
    assert result["confirmed"] is True
    (row,) = undo_store.rows
    descriptor = row["undo_descriptor"]
    assert descriptor["tool"] == "end_resource_maintenance"
    assert descriptor["params"]["resource_id"] == RID
    assert descriptor["params"]["target"] == "home-aria"


def test_confirmed_end_records_start_as_its_undo(monkeypatch, undo_store) -> None:
    import vmware_aria.mcp_server.server as server

    client = _Client(_resource("MAINTAINED"), _resource("MAINTAINED"), _resource("STARTED"))
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    server.end_resource_maintenance(RID, confirm=True)
    (row,) = undo_store.rows
    assert row["undo_descriptor"]["tool"] == "start_resource_maintenance"
    assert row["undo_descriptor"]["params"]["resource_id"] == RID


def test_mcp_refusal_is_a_teaching_error_and_records_no_undo(monkeypatch, undo_store) -> None:
    import vmware_aria.mcp_server.server as server

    monkeypatch.setattr(server, "_get_connection", lambda target=None: _Client(_resource("STARTED")))
    result = server.end_resource_maintenance(RID, confirm=True)
    assert "not in maintenance" in result["error"]
    assert undo_store.rows == []


def test_mcp_maintenance_tools_are_writes_with_medium_risk() -> None:
    import asyncio

    import vmware_aria.mcp_server.server as server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("start_resource_maintenance", "end_resource_maintenance"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].description.startswith("[WRITE]")
        assert getattr(server, name)._risk_level == "medium"


# ---------------------------------------------------------------------------
# CLI: dry-run, confirmation, guarded name
# ---------------------------------------------------------------------------


def _cli(args: list[str], answers: str = ""):
    from vmware_aria import cli

    connect = MagicMock(return_value=(MagicMock(), MagicMock(default_target="home-aria")))
    with patch.object(cli, "_get_connection", connect), \
         patch.object(cli, "_audit", MagicMock()), \
         patch("vmware_aria.ops.maintenance.start_resource_maintenance") as start, \
         patch("vmware_aria.ops.maintenance.end_resource_maintenance") as end:
        start.return_value = {"resource_id": "d0000000-0000-4000-8000-000000000001", "confirmed": True}
        end.return_value = {"resource_id": "d0000000-0000-4000-8000-000000000001", "confirmed": True}
        result = CliRunner().invoke(cli.app, args, input=answers)
    return result, connect, start, end


def test_cli_start_dry_run_prints_the_call_and_makes_none() -> None:
    result, connect, start, _ = _cli(["maintenance", "start", "d0000000-0000-4000-8000-000000000001", "--duration", "60", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "PUT" in result.output
    assert "/resources/d0000000-0000-4000-8000-000000000001/maintained" in result.output
    assert "duration=60" in result.output
    connect.assert_not_called()
    start.assert_not_called()


def test_cli_end_dry_run_prints_the_call_and_makes_none() -> None:
    result, connect, _, end = _cli(["maintenance", "end", "d0000000-0000-4000-8000-000000000001", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DELETE" in result.output and "/resources/d0000000-0000-4000-8000-000000000001/maintained" in result.output
    connect.assert_not_called()
    end.assert_not_called()


def test_cli_dry_run_still_refuses_an_impossible_window() -> None:
    result, connect, start, _ = _cli(["maintenance", "start", "d0000000-0000-4000-8000-000000000001", "--duration", "5", "--end", "1", "--dry-run"])
    assert result.exit_code != 0
    connect.assert_not_called()
    start.assert_not_called()


@pytest.mark.parametrize("command", [["maintenance", "start", "d0000000-0000-4000-8000-000000000001", "--duration", "60"], ["maintenance", "end", "d0000000-0000-4000-8000-000000000001"]])
def test_cli_writes_stop_when_the_prompt_is_declined(command: list[str]) -> None:
    result, connect, start, end = _cli(command, "n\n")
    assert result.exit_code != 0
    start.assert_not_called()
    end.assert_not_called()


def test_cli_start_runs_after_yes_at_the_prompt() -> None:
    result, _, start, _ = _cli(["maintenance", "start", "d0000000-0000-4000-8000-000000000001", "--duration", "60"], "y\n")
    assert result.exit_code == 0, result.output
    start.assert_called_once()
    assert start.call_args.kwargs["duration_minutes"] == 60


def test_cli_yes_flag_skips_the_prompt() -> None:
    result, _, _, end = _cli(["maintenance", "end", "d0000000-0000-4000-8000-000000000001", "--yes"])
    assert result.exit_code == 0, result.output
    end.assert_called_once()


def test_cli_guarded_names_equal_the_mcp_tool_names() -> None:
    import vmware_aria.mcp_server.server as server
    from vmware_aria import cli_workflow

    for command, tool in (
        (cli_workflow.maintenance_start, "start_resource_maintenance"),
        (cli_workflow.maintenance_end, "end_resource_maintenance"),
    ):
        assert command._guarded_tool == tool
        assert command._risk_level == getattr(server, tool)._risk_level


# ---------------------------------------------------------------------------
# Review 2026-09-13: UNKNOWN / NONE, undo from the before-state, dry-run input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "states",
    [("UNKNOWN",), ("NONE",), ("UNKNOWN", "STARTED"), ("NONE", "MAINTAINED")],
    ids=["unknown", "none", "unknown-and-started", "none-and-maintained"],
)
def test_an_adapter_that_does_not_know_the_state_makes_it_unknown(states: tuple[str, ...]) -> None:
    from vmware_aria.ops.maintenance import read_maintenance_state

    state = read_maintenance_state(_Client(_resource(*states)), RID)
    assert state["in_maintenance"] is None, f"{states} must not read as 'not in maintenance'"
    assert state["note"], "an unknown state must say why it is unknown"


@pytest.mark.parametrize(
    "state_name", ["STARTED", "STOPPED", "STARTING", "STOPPING", "UPDATING", "FAILED", "REMOVING", "NOT_EXISTING"]
)
def test_a_reported_non_maintenance_state_reads_as_not_in_maintenance(state_name: str) -> None:
    from vmware_aria.ops.maintenance import read_maintenance_state

    state = read_maintenance_state(_Client(_resource(state_name)), RID)
    assert state["in_maintenance"] is False
    assert state["note"] is None


def test_end_is_not_refused_when_the_adapter_reports_unknown() -> None:
    from vmware_aria.ops.maintenance import end_resource_maintenance

    client = _Client(_resource("UNKNOWN"), _resource("STARTED"))
    result = end_resource_maintenance(client, RID)
    assert client.writes == [("DELETE", f"/resources/{RID}/maintained", None, None)]
    assert result["before"]["in_maintenance"] is None


# The gate reads the state first and refuses "already in maintenance" and
# "unknown" outright (tests/test_gated_writes.py). The undo guard still matters
# for the window between that read and the ops function's own before-read, so
# these cases pass the gate (its read says STARTED / MAINTAINED) and vary what
# the ops before-read then finds.
@pytest.mark.parametrize(
    ("before", "expect_undo"),
    [
        pytest.param(_resource("STARTED"), True, id="before-false"),
        pytest.param(_resource("MAINTAINED_MANUAL"), False, id="before-true-already-in-maintenance"),
        pytest.param(_unavailable(), False, id="before-unknown"),
    ],
)
def test_start_records_its_undo_only_when_it_began_the_maintenance(monkeypatch, undo_store, before, expect_undo) -> None:
    import vmware_aria.mcp_server.server as server

    client = _Client(_resource("STARTED"), before, _resource("MAINTAINED"))
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    server.start_resource_maintenance(RID, duration_minutes=30, confirm=True)
    assert client.writes, "the write itself must still run"
    if expect_undo:
        (row,) = undo_store.rows
        assert row["undo_descriptor"]["tool"] == "end_resource_maintenance"
    else:
        assert undo_store.rows == [], "replaying this undo would end a window this call did not open"


@pytest.mark.parametrize(
    ("before", "expect_undo"),
    [
        pytest.param(_resource("MAINTAINED"), True, id="before-true"),
        pytest.param(_unavailable(), False, id="before-unknown"),
        pytest.param(_resource("UNKNOWN"), False, id="before-adapter-unknown"),
    ],
)
def test_end_records_its_undo_only_when_the_resource_was_in_maintenance(monkeypatch, undo_store, before, expect_undo) -> None:
    import vmware_aria.mcp_server.server as server

    client = _Client(_resource("MAINTAINED"), before, _resource("STARTED"))
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    server.end_resource_maintenance(RID, confirm=True)
    assert client.writes, "the write itself must still run"
    if expect_undo:
        (row,) = undo_store.rows
        descriptor = row["undo_descriptor"]
        assert descriptor["tool"] == "start_resource_maintenance"
        assert "not restored" in descriptor["note"]
    else:
        assert undo_store.rows == [], "replaying this undo could put a resource into indefinite maintenance"


@pytest.mark.parametrize("in_maintenance", [True, False, None])
def test_undo_functions_follow_the_before_state(in_maintenance: bool | None) -> None:
    from vmware_aria.mcp_server.tools.maintenance import _undo_end, _undo_start

    result = {"before": {"in_maintenance": in_maintenance}, "after": {"in_maintenance": None}}
    params = {"resource_id": RID, "target": "home-aria"}
    assert (_undo_start(params, result) is not None) is (in_maintenance is False)
    assert (_undo_end(params, result) is not None) is (in_maintenance is True)


@pytest.mark.parametrize(
    "args",
    [
        ["maintenance", "start", " ", "--duration", "60", "--dry-run"],
        ["maintenance", "start", "", "--dry-run"],
        ["maintenance", "end", " ", "--dry-run"],
    ],
    ids=["start-blank", "start-empty", "end-blank"],
)
def test_cli_dry_run_refuses_a_blank_resource_id(args: list[str]) -> None:
    result, connect, start, end = _cli(args)
    assert result.exit_code == 2, result.output
    assert "DRY-RUN" not in result.output
    assert "resource_id" in result.output
    connect.assert_not_called()
    start.assert_not_called()
    end.assert_not_called()


def test_cli_dry_run_prints_the_id_the_real_call_would_use() -> None:
    result, _, _, _ = _cli(["maintenance", "start", "  d0000000-0000-4000-8000-000000000001 ", "--duration", "60", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "/resources/d0000000-0000-4000-8000-000000000001/maintained?duration=60" in result.output

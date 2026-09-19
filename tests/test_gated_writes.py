"""The six confirm-gated Aria writes behind HLD §7 (revised 2026-09-16).

* L2 — a bare call previews: no POST / PUT / DELETE reaches the appliance.
* L1 — preview and acting response carry ``blast_radius``: the alert (id,
  definition, status, resource), the alert definition, the report, or the
  resource and its maintenance state — measured with GETs this skill already
  makes.
* L3 — ``confirm=True`` is refused when a blocker is present (a cancelled
  alert, a resource already in / not in maintenance) or a field the radius
  depends on could not be read.

``confirmed`` is a deprecated alias; the conservative reading wins.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from vmware_aria.connection import AriaApiError
from vmware_aria.mcp_server import server

FIXTURES = Path(__file__).resolve().parent / "eval" / "fixtures" / "aria_8187"
AID = "8bbb1892-9819-4982-8bed-fbfe30fce1a4"
RES = "31aa1b41-2ae3-479b-8e25-176016ad874e"
RID = "f654322a-50c3-4a9a-937c-2d4bae8a2b36"
REPORT = "0e3c6a7d-1111-4222-8333-944455556666"
DEF = "AlertDefinition-VCAPPHealthStatus"
REPORT_DEF = "b1a2c3d4-0000-4000-8000-000000000abc"

GATED = [
    "acknowledge_alert",
    "cancel_alert",
    "delete_alert_definition",
    "delete_report",
    "start_resource_maintenance",
    "end_resource_maintenance",
]


def _alert(**changes: Any) -> dict:
    body = json.loads((FIXTURES / "alert_vcapp_health.json").read_text(encoding="utf-8"))["body"]
    return {**body, "status": "ACTIVE", **changes}


def _resource(*states: str) -> dict:
    body = json.loads((FIXTURES / "resource_test_llm.json").read_text(encoding="utf-8"))["body"]
    template = body["resourceStatusStates"][0]
    return {**body, "resourceStatusStates": [{**template, "resourceState": s} for s in states]}


def _definition() -> dict:
    return json.loads(
        (FIXTURES / "alertdefinition_vcapp_health.json").read_text(encoding="utf-8")
    )["body"]


def _unavailable(path: str = "/x") -> AriaApiError:
    return AriaApiError("Aria Operations returned HTTP 503.", status_code=503, method="GET", path=path)


def _missing(path: str = "/x") -> AriaApiError:
    return AriaApiError(
        f"Not found: {path}. List the collection and copy an exact id.",
        status_code=404, method="GET", path=path,
    )


class _Client:
    """Answers GETs by path (a list is a queue); records every write."""

    _base_url = "https://aria.example/suite-api/api"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = {k: (list(v) if isinstance(v, list) else v) for k, v in answers.items()}
        self.calls: list[tuple[str, str, Any, Any]] = []

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        self.calls.append(("GET", path, params, None))
        answer = self.answers.get(path, _unavailable(path))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def post(self, path: str, json_data: Any = None, params: Any = None, **_kw: Any) -> dict:
        self.calls.append(("POST", path, params, json_data))
        return {}

    def put(self, path: str, json_data: Any = None, params: Any = None, **_kw: Any) -> dict:
        self.calls.append(("PUT", path, params, json_data))
        return {}

    def delete(self, path: str, **_kw: Any) -> None:
        self.calls.append(("DELETE", path, None, None))

    @property
    def writes(self) -> list[tuple[str, str, Any, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def aria(monkeypatch):
    def install(**answers: Any) -> _Client:
        defaults = {
            f"/alerts/{AID}": _alert(),
            f"/resources/{RES}": {"resourceKey": {"name": "vcsa-01", "resourceKindKey": "VCAPP"}},
            f"/alertdefinitions/{DEF}": _definition(),
            f"/reports/{REPORT}": {"id": REPORT, "status": "COMPLETED", "reportDefinitionId": REPORT_DEF,
                                   "completionTime": "Sun Aug 30 04:40:08 UTC 2026", "owner": "admin"},
            f"/reportdefinitions/{REPORT_DEF}": {"name": "Utilization Report - vSphere Clusters"},
            f"/resources/{RID}": _resource("STARTED"),
        }
        client = _Client({**defaults, **answers})
        monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
        return client
    return install


@pytest.fixture
def audit_rows(monkeypatch):
    rows: list[dict] = []

    class _Recorder:
        def log(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: _Recorder())
    return rows


def _call(tool: str, **kw: Any) -> dict:
    first = {
        "acknowledge_alert": AID, "cancel_alert": AID, "delete_alert_definition": DEF,
        "delete_report": REPORT, "start_resource_maintenance": RID,
        "end_resource_maintenance": RID,
    }[tool]
    if tool == "start_resource_maintenance":
        kw = {"duration_minutes": 30, **kw}
    return getattr(server, tool)(first, **kw)


def _ready(aria, tool: str) -> _Client:
    """A client on which ``tool`` has nothing in its way."""
    if tool == "end_resource_maintenance":
        return aria(**{f"/resources/{RID}": [_resource("MAINTAINED"), _resource("MAINTAINED"),
                                             _resource("STARTED")]})
    if tool == "start_resource_maintenance":
        return aria(**{f"/resources/{RID}": [_resource("STARTED"), _resource("STARTED"),
                                             _resource("MAINTAINED")]})
    return aria()


# ─── every gated tool: L2, act, alias, schema, audit ─────────────────────────


@pytest.mark.parametrize("tool", GATED)
def test_bare_call_previews_and_writes_nothing(aria, tool):
    client = _ready(aria, tool)
    out = _call(tool)
    assert out["action"] == "preview", out
    assert client.writes == []
    assert out["blast_radius"]["blockers"] == [] and out["blast_radius"]["unmeasured"] == []


@pytest.mark.parametrize("tool", GATED)
def test_confirm_writes_once_and_returns_the_blast_radius(aria, tool):
    client = _ready(aria, tool)
    out = _call(tool, confirm=True)
    assert "error" not in out, out
    assert len(client.writes) == 1
    assert out["blast_radius"]["unmeasured"] == []


@pytest.mark.parametrize("tool", GATED)
def test_legacy_confirmed_true_still_acts_and_says_it_is_deprecated(aria, tool):
    client = _ready(aria, tool)
    out = _call(tool, confirmed=True)
    assert len(client.writes) == 1
    assert "confirmed is deprecated; use confirm" in out["deprecated"]


@pytest.mark.parametrize("tool", GATED)
def test_explicit_confirmed_false_holds_against_confirm(aria, tool):
    client = _ready(aria, tool)
    out = _call(tool, confirm=True, confirmed=False)
    assert out["action"] == "preview"
    assert client.writes == []
    assert "deprecated" in out


@pytest.mark.parametrize("tool", GATED)
def test_no_alias_no_deprecation_note(aria, tool):
    _ready(aria, tool)
    assert "deprecated" not in _call(tool)


@pytest.mark.parametrize("tool", GATED)
def test_schema_defaults_confirm_false_and_alias_null(tool):
    t = next(x for x in asyncio.run(server.mcp.list_tools()) if x.name == tool)
    props = t.inputSchema["properties"]
    assert props["confirm"]["default"] is False
    assert props["confirmed"]["default"] is None
    assert "Deprecated alias for confirm" in props["confirmed"]["description"]
    assert "confirm" not in t.inputSchema.get("required", [])


@pytest.mark.parametrize("tool", GATED)
def test_docstring_tells_the_model_not_to_confirm_on_its_own(tool):
    doc = " ".join(getattr(server, tool).__doc__.split())
    assert "False (default) returns the blast radius and changes nothing. True applies it." in doc
    assert "Do not set confirm=True on your own" in doc


# ─── alerts ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["acknowledge_alert", "cancel_alert"])
def test_alert_preview_states_the_alert_and_its_resource(aria, tool):
    aria()
    br = _call(tool)["blast_radius"]
    assert br["alert_id"] == AID
    assert br["definition_id"] == DEF
    assert br["definition_name"] == "vCenter app health is affected"
    assert br["criticality"] == "CRITICAL"
    assert br["status"] == "ACTIVE" and br["control_state"] == "OPEN"
    assert br["resource_id"] == RES and br["resource_name"] == "vcsa-01"


def test_acknowledge_posts_takeownership(aria):
    client = aria()
    out = server.acknowledge_alert(AID, confirm=True)
    assert client.writes == [("POST", "/alerts", {"action": "takeownership"}, {"uuids": [AID]})]
    assert out["control_state"] == "ASSIGNED"


def test_cancel_posts_cancel(aria):
    client = aria()
    server.cancel_alert(AID, confirm=True)
    assert client.writes == [("POST", "/alerts", {"action": "cancel"}, {"uuids": [AID]})]


def test_acknowledging_a_cancelled_alert_is_refused(aria):
    client = aria(**{f"/alerts/{AID}": _alert(status="CANCELED")})
    assert server.acknowledge_alert(AID)["blast_radius"]["blockers"]
    out = server.acknowledge_alert(AID, confirm=True)
    assert client.writes == []
    assert "cancelled" in out["error"]


def test_cancelling_a_cancelled_alert_is_a_noop(aria):
    client = aria(**{f"/alerts/{AID}": _alert(status="CANCELED")})
    out = server.cancel_alert(AID, confirm=True)
    assert out["action"] == "noop"
    assert client.writes == []


@pytest.mark.parametrize("tool", ["acknowledge_alert", "cancel_alert"])
def test_an_unreadable_alert_is_refused(aria, tool):
    client = aria(**{f"/alerts/{AID}": _unavailable()})
    assert _call(tool)["blast_radius"]["unmeasured"] == ["alert"]
    out = _call(tool, confirm=True)
    assert client.writes == []
    assert "could not read alert" in out["error"]


@pytest.mark.parametrize("tool", ["acknowledge_alert", "cancel_alert"])
def test_an_alert_without_status_is_refused(aria, tool):
    body = {k: v for k, v in _alert().items() if k != "status"}
    client = aria(**{f"/alerts/{AID}": body})
    out = _call(tool, confirm=True)
    assert client.writes == []
    assert "could not read status" in out["error"]


@pytest.mark.parametrize("tool", ["acknowledge_alert", "cancel_alert"])
def test_an_alert_that_does_not_exist_teaches(aria, tool):
    client = aria(**{f"/alerts/{AID}": _missing(f"/alerts/{AID}")})
    out = _call(tool, confirm=True)
    assert client.writes == []
    assert "copy an exact id" in out["error"]


def test_an_alert_on_a_removed_resource_can_still_be_cancelled(aria):
    """The resource name is context, not a condition: cleanup must stay possible."""
    client = aria(**{f"/resources/{RES}": _missing()})
    out = server.cancel_alert(AID, confirm=True)
    assert len(client.writes) == 1
    assert out["blast_radius"]["resource_name"] is None
    assert out["blast_radius"]["resource_note"]


def test_a_refusal_is_audited_as_a_failure(aria, audit_rows):
    aria(**{f"/alerts/{AID}": _unavailable()})
    server.cancel_alert(AID, confirm=True)
    assert audit_rows and audit_rows[0]["status"].startswith("error")


# ─── alert definitions ───────────────────────────────────────────────────────


def test_delete_definition_preview_states_the_definition(aria):
    aria()
    br = server.delete_alert_definition(DEF)["blast_radius"]
    assert br["definition_id"] == DEF
    assert br["name"] == _definition()["name"]
    assert br["state_count"] == len(_definition()["states"])
    assert br["resource_kind"] == _definition().get("resourceKindKey")


def test_delete_definition_sends_the_delete(aria):
    client = aria()
    server.delete_alert_definition(DEF, confirm=True)
    assert client.writes == [("DELETE", f"/alertdefinitions/{DEF}", None, None)]


def test_delete_definition_unreadable_is_refused(aria):
    client = aria(**{f"/alertdefinitions/{DEF}": _unavailable()})
    out = server.delete_alert_definition(DEF, confirm=True)
    assert client.writes == []
    assert "could not read definition" in out["error"]


def test_delete_definition_without_a_name_is_refused(aria):
    client = aria(**{f"/alertdefinitions/{DEF}": {k: v for k, v in _definition().items() if k != "name"}})
    out = server.delete_alert_definition(DEF, confirm=True)
    assert client.writes == []
    assert "could not read name" in out["error"]


# ─── reports ─────────────────────────────────────────────────────────────────


def test_delete_report_preview_states_the_report(aria):
    aria()
    br = server.delete_report(REPORT)["blast_radius"]
    assert br["report_id"] == REPORT
    assert br["name"] == "Utilization Report - vSphere Clusters"
    assert br["status"] == "COMPLETED" and br["definition_id"] == REPORT_DEF


def test_delete_report_sends_the_delete(aria):
    client = aria()
    server.delete_report(REPORT, confirm=True)
    assert client.writes == [("DELETE", f"/reports/{REPORT}", None, None)]


def test_delete_report_unreadable_is_refused(aria):
    client = aria(**{f"/reports/{REPORT}": _unavailable()})
    out = server.delete_report(REPORT, confirm=True)
    assert client.writes == []
    assert "could not read report" in out["error"]


def test_delete_report_without_status_is_refused(aria):
    client = aria(**{f"/reports/{REPORT}": {"id": REPORT, "reportDefinitionId": REPORT_DEF}})
    out = server.delete_report(REPORT, confirm=True)
    assert client.writes == []
    assert "could not read status" in out["error"]


# ─── maintenance ─────────────────────────────────────────────────────────────


def test_start_preview_states_resource_state_and_window(aria):
    aria()
    br = server.start_resource_maintenance(RID, duration_minutes=45)["blast_radius"]
    assert br["resource_id"] == RID and br["name"] == "test-llm" and br["kind"] == "VirtualMachine"
    assert br["in_maintenance"] is False
    assert br["adapter_count"] == 1
    assert br["requested"] == {"mode": "timed", "duration_minutes": 45, "end_time_ms": None}
    assert "45 minutes" in br["effect"]


def test_start_sends_the_window(aria):
    client = _ready(aria, "start_resource_maintenance")
    server.start_resource_maintenance(RID, duration_minutes=45, confirm=True)
    assert client.writes == [("PUT", f"/resources/{RID}/maintained", {"duration": 45}, None)]


@pytest.mark.parametrize("mode", ["MAINTAINED", "MAINTAINED_MANUAL"])
def test_start_on_a_resource_already_in_maintenance_is_refused(aria, mode):
    client = aria(**{f"/resources/{RID}": _resource(mode)})
    out = server.start_resource_maintenance(RID, duration_minutes=30, confirm=True)
    assert client.writes == []
    assert "already in maintenance" in out["error"]


@pytest.mark.parametrize("tool", ["start_resource_maintenance", "end_resource_maintenance"])
@pytest.mark.parametrize("state", [_resource("UNKNOWN"), _unavailable()], ids=["adapter-unknown", "unreadable"])
def test_an_unknown_maintenance_state_is_refused(aria, tool, state):
    client = aria(**{f"/resources/{RID}": state})
    assert _call(tool)["blast_radius"]["unmeasured"] == ["maintenance_state"]
    out = _call(tool, confirm=True)
    assert client.writes == []
    assert "could not read maintenance_state" in out["error"]


def test_end_on_a_resource_not_in_maintenance_keeps_its_refusal(aria):
    client = aria()
    out = server.end_resource_maintenance(RID, confirm=True)
    assert client.writes == []
    assert "not in maintenance" in out["error"]


def test_end_sends_the_delete(aria):
    client = _ready(aria, "end_resource_maintenance")
    out = server.end_resource_maintenance(RID, confirm=True)
    assert client.writes == [("DELETE", f"/resources/{RID}/maintained", None, None)]
    assert out["blast_radius"]["in_maintenance"] is True


def test_an_impossible_window_is_refused_before_connecting(monkeypatch):
    def no_connection(target=None):
        raise AssertionError("an invalid window must be refused before connecting")

    monkeypatch.setattr(server, "_get_connection", no_connection)
    out = server.start_resource_maintenance(RID, duration_minutes=5, end_time_ms=1)
    assert "not both" in out["error"]


@pytest.mark.parametrize("action", ["preview", "noop"])
def test_a_response_that_changed_nothing_never_records_an_undo(action):
    """Guarded twice on the real path (a preview has no ``before``); pinned here on its own."""
    from vmware_aria.mcp_server.tools.maintenance import _changed, _undo_start

    result = {"action": action, "before": {"in_maintenance": False}}
    assert _changed(result) is False
    assert _undo_start({"resource_id": RID}, result) is None

"""Alerts on Aria Operations 8.18.7 came back unnamed — symptoms and resources.

Found 2026-09-13 against a real 8.18.7 appliance (four ACTIVE alerts):

* ``get_alert`` / ``investigate_alert`` returned eight contributing symptoms
  whose ``name`` and ``severity`` were all ``""``. On 8.18.7 a triggered
  symptom instance carries only ids — ``symptomId``, ``symptomSetId``,
  ``symptomDefinitionsIds`` and an **empty** ``alertConditions`` — so every
  field the summarizer tried was absent. The name and severity live on the
  symptom *definition* (``name``, ``state.severity``), which the tool never
  fetched. An agent was handed eight reasons an alert fired, none of which
  said anything.
* ``list_alerts`` rows carry only ``resource_id``. The Alert model has no
  resource name, so a list of alerts named no object at all.

Both are fixed by batched lookups — one ``GET /symptomdefinitions?id=..&id=..``
per alert and one ``GET /resources?resourceId=..&resourceId=..`` per page —
confirmed on the appliance to accept a repeated id parameter and return every
requested row. The fixtures below are the real 8.18.7 bodies (links stripped,
lab IPs replaced with TEST-NET, resource identifiers emptied), not hand-written
shapes: the hand-written 9.1 fixture put severity on ``alertConditions[]``,
which is exactly the field 8.18.7 leaves empty.

The N+1 tests use a counting client, because "one lookup per symptom" or "one
GET /resources/{id} per alert" passes every correctness test while making a
500-row page cost 501 round trips.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from vmware_aria.ops import alerts as alerts_mod
from vmware_aria.ops.alerts import get_alert, list_alerts

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "aria_8187_alerts.json").read_text(encoding="utf-8")
)
ACTIVE = FIXTURE["alerts_query"]["alerts"]
DEFINITIONS = {d["id"]: d for d in FIXTURE["symptom_definitions"]["symptomDefinitions"]}
RESOURCES = {r["identifier"]: r for r in FIXTURE["resources"]["resourceList"]}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]


class FakeAria:
    """Answers the way the 8.18.7 appliance did, and counts every request.

    The id-filtered collections honour the repeated parameter — the appliance
    returns only the requested rows and silently omits unknown ids (both
    observed live) — so a caller that forgets an id sees it missing, not a
    free pass.
    """

    def __init__(
        self,
        alerts: list[dict] | None = None,
        contrib: dict[str, dict] | None = None,
        definitions: dict[str, dict] | None = None,
        resources: dict[str, dict] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.alerts = ACTIVE if alerts is None else alerts
        self.contrib = FIXTURE["contributing_symptoms_by_alert"] if contrib is None else contrib
        self.definitions = DEFINITIONS if definitions is None else definitions
        self.resources = RESOURCES if resources is None else resources
        self.fail = fail or set()
        self.calls: list[tuple[str, str, dict]] = []

    def count(self, path: str) -> int:
        return sum(1 for _, p, _ in self.calls if p == path)

    def get(self, path: str, params: dict | None = None, **_: Any) -> dict:
        params = dict(params or {})
        self.calls.append(("GET", path, params))
        if path in self.fail:
            raise RuntimeError(f"simulated failure on {path}")
        if path == "/symptomdefinitions":
            wanted = _as_list(params.get("id"))
            rows = [self.definitions[i] for i in wanted if i in self.definitions]
            return {"pageInfo": {"totalCount": len(rows), "page": 0, "pageSize": 1000}, "symptomDefinitions": rows}
        if path == "/resources":
            wanted = _as_list(params.get("resourceId"))
            rows = [self.resources[i] for i in wanted if i in self.resources]
            return {"pageInfo": {"totalCount": len(rows), "page": 0, "pageSize": 1000}, "resourceList": rows}
        if path == "/alerts/contributingsymptoms":
            return self.contrib[str(params["id"])]
        if path.startswith("/alerts/"):
            alert_id = path.rsplit("/", 1)[-1]
            return next(a for a in self.alerts if a["alertId"] == alert_id)
        raise AssertionError(f"unexpected GET {path} — per-item lookups are the N+1 this suite forbids")

    def post(self, path: str, json_data: dict | None = None, params: dict | None = None, **_: Any) -> dict:
        self.calls.append(("POST", path, dict(params or {})))
        assert path == "/alerts/query", path
        page = int((params or {}).get("page", 0))
        return {"alerts": list(self.alerts) if page == 0 else []}


# ---------------------------------------------------------------------------
# get_alert: symptom names and severities come from the definitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alert", ACTIVE, ids=lambda a: a["alertDefinitionName"])
def test_every_real_symptom_gets_its_definition_name_and_severity(alert: dict) -> None:
    client = FakeAria()
    result = get_alert(client, alert["alertId"])

    assert result["symptoms"], "each of the four live alerts has contributing symptoms"
    for sym in result["symptoms"]:
        definition = DEFINITIONS[sym["symptom_definition_id"]]
        assert sym["name"] == definition["name"], "8.18.7 symptom instances carry no name — the definition does"
        assert sym["severity"] == definition["state"]["severity"], "severity is the definition's state.severity"
        assert sym["definition_lookup"] == "resolved"
    assert "symptom_definitions_note" not in result


def test_the_real_bodies_really_are_nameless() -> None:
    """CONTROL: the fixture must still carry the defect, or the test above proves nothing."""
    leaves = [
        s
        for body in FIXTURE["contributing_symptoms_by_alert"].values()
        for entry in body["contributingSymptoms"]
        for s in entry["contributingSymptoms"]["contributingSymptoms"]
    ]
    assert len(leaves) == 8
    for s in leaves:
        assert not any(s.get(k) for k in ("name", "message", "severity", "symptomCriticality"))
        assert s["alertConditions"] == []


def test_definition_lookup_is_one_batched_request_per_alert() -> None:
    alert = max(ACTIVE, key=lambda a: len(
        FIXTURE["contributing_symptoms_by_alert"][a["alertId"]]["contributingSymptoms"][0]
        ["contributingSymptoms"]["contributingSymptoms"]
    ))
    client = FakeAria()
    get_alert(client, alert["alertId"])

    assert client.count("/symptomdefinitions") == 1
    assert not any(p.startswith("/symptomdefinitions/") for _, p, _ in client.calls)


def test_definition_lookup_does_not_grow_with_symptom_count() -> None:
    """120 symptoms over 60 definitions: dedupe, then ceil(60 / chunk) requests."""
    chunk = alerts_mod._DEFINITION_ID_CHUNK
    template = next(iter(DEFINITIONS.values()))
    definitions = {f"SymptomDefinition-synthetic-{i}": {**template, "id": f"SymptomDefinition-synthetic-{i}", "name": f"synthetic {i}"} for i in range(60)}
    leaves = [
        {"symptomId": f"sym-{n}", "symptomSetId": "set", "symptomDefinitionsIds": [f"SymptomDefinition-synthetic-{n % 60}"], "alertConditions": []}
        for n in range(120)
    ]
    alert = {**ACTIVE[0], "alertId": "big"}
    contrib = {"big": {"contributingSymptoms": [{"alertId": "big", "contributingSymptoms": {"contributingSymptoms": leaves}}]}}
    client = FakeAria(alerts=[alert], contrib=contrib, definitions=definitions)

    result = get_alert(client, "big")

    assert client.count("/symptomdefinitions") == math.ceil(60 / chunk)
    requested = [i for m, p, prm in client.calls if p == "/symptomdefinitions" for i in _as_list(prm.get("id"))]
    assert sorted(requested) == sorted(definitions), "every definition asked for exactly once"
    assert all(s["name"].startswith("synthetic ") for s in result["symptoms"])


def test_symptoms_that_already_say_something_trigger_no_lookup() -> None:
    """CONTROL: the older flat body names its symptoms — do not spend a request on it."""
    body = {"symptoms": [{"id": "s", "message": "CPU above 90%", "symptomCriticality": "CRITICAL", "symptomDefinitionId": "sd-9"}]}
    client = FakeAria(alerts=[{**ACTIVE[0], "alertId": "flat"}], contrib={"flat": body})

    sym = get_alert(client, "flat")["symptoms"][0]

    assert client.count("/symptomdefinitions") == 0
    assert sym["name"] == "CPU above 90%"
    assert sym["definition_lookup"] == "not_needed"


def test_failed_definition_lookup_is_marked_not_left_blank() -> None:
    client = FakeAria(fail={"/symptomdefinitions"})
    result = get_alert(client, ACTIVE[0]["alertId"])

    assert result["symptoms"], "a naming failure must not cost the symptoms themselves"
    assert all(s["definition_lookup"] == "failed" for s in result["symptoms"])
    assert all(s["name"] == "" for s in result["symptoms"])
    assert "could not be retrieved" in result["symptom_definitions_note"]


def test_definition_the_appliance_does_not_return_is_marked_not_found() -> None:
    """The appliance omits an unknown id silently (observed live) — that must not read as resolved."""
    client = FakeAria(definitions={})
    result = get_alert(client, ACTIVE[0]["alertId"])

    assert all(s["definition_lookup"] == "not_found" for s in result["symptoms"])
    assert "not returned" in result["symptom_definitions_note"]


# ---------------------------------------------------------------------------
# list_alerts: rows name the affected resource
# ---------------------------------------------------------------------------


def test_list_alerts_rows_name_their_resource() -> None:
    client = FakeAria()
    result = list_alerts(client)

    assert result["returned"] == 4
    for row in result["items"]:
        key = RESOURCES[row["resource_id"]]["resourceKey"]
        assert row["resource_name"] == key["name"]
        assert row["resource_kind"] == key["resourceKindKey"]
    assert result["resource_names_note"] is None


def test_list_alerts_resolves_resources_in_one_request_per_page() -> None:
    client = FakeAria()
    list_alerts(client)

    assert client.count("/resources") == 1
    assert not any(p.startswith("/resources/") for _, p, _ in client.calls)


def test_list_alerts_resource_lookup_does_not_grow_with_row_count() -> None:
    """500 alerts: on 5 shared resources one request; on 500 distinct ones ceil(500 / chunk)."""
    chunk = alerts_mod._RESOURCE_ID_CHUNK
    template_alert, template_res = ACTIVE[0], next(iter(RESOURCES.values()))

    def run(distinct: int) -> FakeAria:
        res = {f"res-{i}": {**template_res, "identifier": f"res-{i}", "resourceKey": {**template_res["resourceKey"], "name": f"obj-{i}"}} for i in range(distinct)}
        rows = [{**template_alert, "alertId": f"a-{n}", "resourceId": f"res-{n % distinct}"} for n in range(500)]
        client = FakeAria(alerts=rows, resources=res)
        result = list_alerts(client, limit=500)
        assert all(r["resource_name"] == f"obj-{int(r['resource_id'][4:])}" for r in result["items"])
        return client

    assert run(5).count("/resources") == 1
    assert run(500).count("/resources") == math.ceil(500 / chunk)


def test_list_alerts_resource_lookup_failure_is_marked() -> None:
    client = FakeAria(fail={"/resources"})
    result = list_alerts(client)

    assert result["returned"] == 4, "a naming failure must not cost the alerts"
    assert all(r["resource_name"] is None and r["resource_kind"] is None for r in result["items"])
    assert "could not be retrieved" in result["resource_names_note"]


def test_list_alerts_resource_not_returned_is_marked() -> None:
    """A deleted resource is omitted by the appliance, not reported — say so."""
    missing = ACTIVE[0]["resourceId"]
    client = FakeAria(resources={k: v for k, v in RESOURCES.items() if k != missing})
    result = list_alerts(client)

    row = next(r for r in result["items"] if r["resource_id"] == missing)
    assert row["resource_name"] is None
    assert "not returned" in result["resource_names_note"]


def test_a_resolved_resource_with_no_name_is_explained() -> None:
    """2026-09-13 review: resource_name null with resource_names_note also null."""
    nameless = ACTIVE[0]["resourceId"]
    row = RESOURCES[nameless]
    resources = {**RESOURCES, nameless: {**row, "resourceKey": {**row["resourceKey"], "name": ""}}}
    result = list_alerts(FakeAria(resources=resources))

    item = next(r for r in result["items"] if r["resource_id"] == nameless)
    assert item["resource_name"] is None
    assert result["resource_names_note"], "every null resource_name has an explanation"
    assert "no name" in result["resource_names_note"]
    assert "not returned" not in result["resource_names_note"], "it was returned — just unnamed"


class FilterIgnoringAria(FakeAria):
    """/resources ignores resourceId and answers with a page of other resources."""

    def get(self, path: str, params: dict | None = None, **kw: Any) -> dict:
        if path == "/resources":
            self.calls.append(("GET", path, dict(params or {})))
            rows = [self.resources[i] for i in self.resources if i not in _as_list((params or {}).get("resourceId"))]
            return {"pageInfo": {"totalCount": 999, "page": 0, "pageSize": 100}, "resourceList": rows}
        return super().get(path, params, **kw)


def test_an_ignored_id_filter_is_not_reported_as_deleted_resources() -> None:
    """Rows nobody asked for mean the filter was ignored — absence then proves nothing."""
    others = {f"other-{i}": {**next(iter(RESOURCES.values())), "identifier": f"other-{i}"} for i in range(3)}
    result = list_alerts(FilterIgnoringAria(resources=others))

    assert all(r["resource_name"] is None for r in result["items"])
    note = result["resource_names_note"]
    assert note
    assert "deleted or stale" not in note, "the appliance did not say these resources are gone"
    assert "could not be retrieved" in note


def test_an_ignored_filter_that_still_returns_the_requested_rows_resolves() -> None:
    """CONTROL: extra rows alongside every requested one — the names are still exact."""

    class ExtraRows(FakeAria):
        def get(self, path: str, params: dict | None = None, **kw: Any) -> dict:
            answer = super().get(path, params, **kw)
            if path == "/resources":
                extra = {**next(iter(RESOURCES.values())), "identifier": "unrequested"}
                return {**answer, "resourceList": [*answer["resourceList"], extra]}
            return answer

    result = list_alerts(ExtraRows())
    assert result["resource_names_note"] is None
    assert all(r["resource_name"] for r in result["items"])


def test_a_symptom_with_no_definition_id_and_no_name_is_noted() -> None:
    """2026-09-13 review: definition_lookup no_definition_id but symptom_definitions_note absent."""
    body = {"contributingSymptoms": [{"alertId": "bare", "contributingSymptoms": {
        "contributingSymptoms": [{"symptomId": "s-1", "symptomSetId": "set", "alertConditions": []}]
    }}]}
    client = FakeAria(alerts=[{**ACTIVE[0], "alertId": "bare"}], contrib={"bare": body})
    result = get_alert(client, "bare")

    assert [s["definition_lookup"] for s in result["symptoms"]] == ["no_definition_id"]
    assert result["symptoms"][0]["name"] == ""
    assert "no definition id" in result["symptom_definitions_note"]


def test_list_alerts_with_no_alerts_makes_no_lookup() -> None:
    client = FakeAria(alerts=[])
    result = list_alerts(client)

    assert result["items"] == []
    assert client.count("/resources") == 0
    assert result["resource_names_note"] is None


# ---------------------------------------------------------------------------
# The agent reads the tool description, not this module
# ---------------------------------------------------------------------------


def test_mcp_descriptions_name_the_new_fields() -> None:
    from vmware_aria.mcp_server.tools import alerts as tools

    assert "resource_name" in (tools.list_alerts.__doc__ or "")
    assert "resource_names_note" in (tools.list_alerts.__doc__ or "")
    assert "definition_lookup" in (tools.get_alert.__doc__ or "")
    assert "symptom_definitions_note" in (tools.get_alert.__doc__ or "")
    assert "symptom" in (tools.investigate_alert.__doc__ or "")

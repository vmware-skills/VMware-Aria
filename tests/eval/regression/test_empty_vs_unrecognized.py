""""No rows" must be distinguishable from "no rows I recognised" (形态 #1).

Two real-appliance failures from the 2026-08 VCF Operations 9.1 test run, both
the same shape: the tool answered with an empty list and nothing else, and the
agent reading it concluded the environment was clean.

* ``fleet_domain_list`` reported 0 domains against a fleet that had one. The
  9.1 body is the ``VCFDomainSummaries`` model — three sibling arrays
  (configuredDomains / notConfiguredDomains / removedDomains) — and the parser
  was looking for a single ``domains`` container that the appliance never
  sends.
* ``get_alert`` reported no symptoms for all five CRITICAL alerts. The
  contributingsymptoms body nests the symptoms three levels deep and the parser
  unwrapped one, so the tool whose job is to explain *why* an alert fired
  explained nothing while looking like it had answered.

Each fix is pinned from both ends: the real shape must yield rows, AND a
genuinely empty result must still report empty *without* the unconfirmed-shape
note. A fix that answers "unknown" for everything would satisfy a one-sided
test and make the tool useless.
"""
from __future__ import annotations

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Wire bodies, transcribed from the vendor API references
# ---------------------------------------------------------------------------

#: GET /api/integrations/vcf/{integrationId}/domains — VCF Operations 9.1 API
#: reference, Integrations -> "Get Domain Summary". Response model
#: ``VCFDomainSummaries``; the documented example body is exactly these three
#: keys, each an array.
_DOMAIN_SUMMARIES_WIRE = {
    "configuredDomains": [
        {"id": "dom-1", "name": "wld-01", "type": "WORKLOAD", "status": "ACTIVE"}
    ],
    "notConfiguredDomains": [],
    "removedDomains": [],
}

#: GET /api/alerts/contributingsymptoms — VCF Operations / vROps suite-api
#: reference, "Get Alert Contributing Symptoms". Three levels: a per-alert
#: array, each entry holding a contributingSymptoms *object*, which holds the
#: contributingSymptoms *array* of actual symptoms.
_CONTRIBUTING_SYMPTOMS_WIRE = {
    "contributingSymptoms": [
        {
            "alertId": "alert-1",
            "contributingSymptoms": {
                "contributingSymptoms": [
                    {
                        "symptomId": "sym-1",
                        "symptomSetId": "set-1",
                        "symptomDefinitionsIds": ["sd-1"],
                        "alertConditions": [
                            {
                                "id": "cond-1",
                                "severity": "CRITICAL",
                                "waitCycles": 1,
                                "cancelCycles": 1,
                                "condition": {
                                    "key": "cpu|demandPct",
                                    "operator": "GT",
                                    "settingValue": "90.0",
                                    "thresholdType": "STATIC",
                                    "instanced": False,
                                },
                            }
                        ],
                    }
                ]
            },
        }
    ]
}

_ALERT_WIRE = {
    "alertId": "alert-1",
    "alertDefinitionName": "VM CPU contention",
    "alertLevel": "CRITICAL",
    "status": "ACTIVE",
    "resourceId": "res-1",
}


def _client() -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.get.return_value = {}
    client.post.return_value = {}
    return client


def _alert_client(symptoms_body: object) -> MagicMock:
    """A client whose alert GET returns _ALERT_WIRE and whose symptoms GET returns `symptoms_body`."""
    client = _client()

    def get_side(path, params=None):
        if path == "/alerts/alert-1":
            return dict(_ALERT_WIRE)
        if path == "/alerts/contributingsymptoms":
            if isinstance(symptoms_body, Exception):
                raise symptoms_body
            return symptoms_body
        raise AssertionError(f"unexpected GET {path}")

    client.get.side_effect = get_side
    return client


# ---------------------------------------------------------------------------
# fleet_domain_list — the real VCFDomainSummaries body
# ---------------------------------------------------------------------------


def test_domain_list_reads_the_vcf_domain_summaries_buckets() -> None:
    """One configured domain must come back as one row, not as zero."""
    from vmware_aria.ops.fleet import list_fleet_domains

    c = _client()
    c.get.return_value = _DOMAIN_SUMMARIES_WIRE
    result = list_fleet_domains(c, integration_id="int-123")

    assert result["returned"] == 1, "a configured domain must not read as 'no domains'"
    assert result["items"][0]["name"] == "wld-01"
    assert result["items"][0]["id"] == "dom-1"
    assert result.get("note") is None, "a shape we now understand carries no note"


def test_domain_list_labels_which_bucket_each_domain_came_from() -> None:
    """The three arrays do not mean the same thing — a removed domain is not a live one."""
    from vmware_aria.ops.fleet import list_fleet_domains

    c = _client()
    c.get.return_value = {
        "configuredDomains": [{"id": "d1", "name": "wld-01"}],
        "notConfiguredDomains": [{"id": "d2", "name": "wld-02"}],
        "removedDomains": [{"id": "d3", "name": "wld-03"}],
    }
    states = {r["name"]: r["configuration_state"] for r in list_fleet_domains(c, "int-1")["items"]}
    assert states == {
        "wld-01": "configured",
        "wld-02": "not_configured",
        "wld-03": "removed",
    }


def test_domain_list_empty_buckets_are_a_genuine_none() -> None:
    """CONTROL: an appliance with no domains must report none, not 'unknown'.

    Without this the fix could return the unconfirmed note unconditionally and
    still pass the positive test above, leaving the tool unable to ever say
    "there are no domains".
    """
    from vmware_aria.ops.fleet import list_fleet_domains

    c = _client()
    c.get.return_value = {"configuredDomains": [], "notConfiguredDomains": [], "removedDomains": []}
    result = list_fleet_domains(c, integration_id="int-123")

    assert result["items"] == []
    assert result.get("note") is None, "recognised-and-empty is a confirmed 'none'"


def test_domain_list_unknown_shape_is_not_reported_as_none() -> None:
    """CONTROL: a body we cannot parse must still refuse to claim 'no domains'.

    The body here carries content but no list of records. It used to be
    ``{"someFutureContainer": [{"name": "wld-01"}]}``, which the 2026-08-30
    fleet fix made *parseable* — one list of records in a body has only one
    thing it can be, and refusing to read it is what made a 32-certificate
    fleet report zero. That case is now a positive test
    (test_fleet_rows_survive_the_container_key); the lesson asserted below is
    unchanged and needs a body that genuinely says nothing readable.
    """
    from vmware_aria.ops.fleet import list_fleet_domains

    c = _client()
    c.get.return_value = {"pageInfo": {"totalCount": 3}, "status": "OK"}
    result = list_fleet_domains(c, integration_id="int-123")

    assert result["items"] == []
    assert "note" in result, "unrecognised shape must carry an unconfirmed-empty note"
    assert "unconfirmed" in result["note"]


def test_domain_list_cli_prints_the_unconfirmed_shape_note(monkeypatch) -> None:
    """The note has to reach the operator — a defence that dies one layer up is not a defence.

    The CLI read only ``["items"]``, so the note existed in the envelope and
    the person running the command never saw it.
    """
    from typer.testing import CliRunner

    from vmware_aria.cli import app

    c = _client()
    c.get.return_value = {"pageInfo": {"totalCount": 3}, "status": "OK"}
    monkeypatch.setattr("vmware_aria.cli._get_connection", lambda target, config: (c, MagicMock()))

    result = CliRunner().invoke(app, ["fleet", "domains", "int-123"])
    assert result.exit_code == 0
    assert "unconfirmed" in result.output, "the shape note must be printed, not dropped"


# ---------------------------------------------------------------------------
# get_alert — contributing symptoms nested three levels deep
# ---------------------------------------------------------------------------


def test_get_alert_unwraps_three_level_contributing_symptoms() -> None:
    """The real body must yield a symptom that actually says something."""
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_alert_client(_CONTRIBUTING_SYMPTOMS_WIRE), "alert-1")

    assert len(result["symptoms"]) == 1, "the symptoms are three levels down, not one"
    sym = result["symptoms"][0]
    assert sym["id"] == "sym-1"
    assert sym["severity"] == "CRITICAL", "severity lives on alertConditions[], not on the symptom"
    assert sym["symptom_definition_id"] == "sd-1", "the wire key is the plural symptomDefinitionsIds"
    assert "cpu|demandPct" in sym["condition"], "the triggering condition is the 'why' this tool owes"
    assert result.get("symptoms_note") is None


def test_get_alert_keeps_reading_the_flat_symptom_shape() -> None:
    """CONTROL: the older flat container must keep working — this is a widening, not a swap."""
    from vmware_aria.ops.alerts import get_alert

    body = {
        "symptoms": [
            {
                "id": "sym-9",
                "message": "CPU usage above 90%",
                "symptomCriticality": "CRITICAL",
                "symptomDefinitionId": "sd-9",
                "resourceId": "res-1",
            }
        ]
    }
    sym = get_alert(_alert_client(body), "alert-1")["symptoms"][0]
    assert sym["id"] == "sym-9"
    assert sym["severity"] == "CRITICAL"
    assert "CPU usage" in sym["name"]


def test_get_alert_reports_a_genuinely_symptomless_alert_as_empty() -> None:
    """CONTROL: an alert with nothing triggered must report empty, and say so plainly.

    This is the other end of the fix. Answering "shape unknown" here would make
    the note meaningless — every alert would carry it.

    All three ways an appliance says "nothing triggered" are covered, because
    they leave the walk at different points: an empty body never enters a
    container, a per-alert entry with no symptom container falls out at the
    leaf test, and an empty innermost array bottoms out in the list branch.
    """
    from vmware_aria.ops.alerts import get_alert

    bodies = {
        "empty body": {},
        "alert entry with nothing triggered": {"contributingSymptoms": [{"alertId": "alert-1"}]},
        "empty innermost array": {
            "contributingSymptoms": [
                {"alertId": "alert-1", "contributingSymptoms": {"contributingSymptoms": []}}
            ]
        },
    }
    for label, body in bodies.items():
        result = get_alert(_alert_client(body), "alert-1")
        assert result["symptoms"] == [], label
        assert result.get("symptoms_note") is None, (
            f"{label}: understood the response and found none — a confirmed none, not 'unknown'"
        )


def test_get_alert_flags_an_unrecognized_symptom_shape() -> None:
    """A body we could not walk must not read as 'this alert has no symptoms'."""
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_alert_client({"someFutureContainer": [{"symptomId": "s1"}]}), "alert-1")

    assert result["symptoms"] == []
    assert "unconfirmed" in result.get("symptoms_note", "")


def test_get_alert_flags_symptoms_that_could_not_be_fetched() -> None:
    """A failed symptoms call is also an empty list — it must not pass as 'none'."""
    from vmware_aria.ops.alerts import get_alert

    result = get_alert(_alert_client(RuntimeError("boom")), "alert-1")

    assert result["symptoms"] == []
    assert result.get("symptoms_note"), "a fetch failure must be visible, not silently empty"
    assert "could not be retrieved" in result["symptoms_note"]


# ---------------------------------------------------------------------------
# The notes have to be documented where the agent reads (踩坑 形态 #4/#6)
# ---------------------------------------------------------------------------


def test_mcp_docstrings_tell_the_agent_the_empty_result_may_be_unconfirmed() -> None:
    """An agent only knows to check a field if the tool description names it."""
    from vmware_aria.mcp_server.tools.alerts import get_alert
    from vmware_aria.mcp_server.tools.fleet import fleet_domain_list

    domain_doc = fleet_domain_list.__doc__ or ""
    assert "note" in domain_doc, "fleet_domain_list must document the unconfirmed-shape note"

    alert_doc = get_alert.__doc__ or ""
    assert "symptoms_note" in alert_doc, "get_alert must document symptoms_note"

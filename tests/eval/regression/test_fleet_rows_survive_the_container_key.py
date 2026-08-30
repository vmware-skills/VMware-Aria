""""No rows" must not depend on having guessed the container key (形态 #1).

2026-08-30, VCF Operations 9.1.0.0 build 25541561, read-only run:

* ``list_fleet_certificates``      0 rows against a fleet holding **32**
* ``list_fleet_password_accounts`` 0 rows against a fleet holding **26**
* ``list_findings``                rows returned, ``name`` column empty

The first two were reported as a container-key problem, and the report is
right — but the interesting part is *why* one container key can do this at all.
``_extract_rows`` was a list of guessed key names; a body whose rows arrive
under a name nobody guessed produces the same empty list as a fleet with no
certificates. ``_UNRECOGNIZED_SHAPE_NOTE`` keeps that from reading as a clean
bill of health, and it did its job here — but a tool that answers "unknown" for
a 32-certificate fleet is still not answering.

The response *schemas* of these three query endpoints are NOT recorded: only
their *paths* are VERIFIED (tests/eval/spec/vcf91_fleet_operations.json). That
is exactly the reason the parse must stop depending on the name. So these tests
do not assert one spelling — they assert the property the name-guessing broke:
**every way an appliance can package a list of records must yield the records,
and any body that does not must be flagged, never silently emptied.** A new
container key on a future build cannot re-break this without also making one of
these cases fail.

The domains body below *is* recorded (VCF Operations 9.1 API reference,
Integrations -> "Get Domain Summary"), and is asserted verbatim: it is the one
container key in this module we know, and dropping it again must be red.

Both ends are pinned. A parser that answered "unrecognised" for everything
would pass every positive case above and be useless, so the controls carry the
same weight: an empty fleet still reports a *confirmed* empty, and a body with
no record list in it at all still reports unconfirmed.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from vmware_aria.ops.fleet import _UNRECOGNIZED_SHAPE_NOTE

# ---------------------------------------------------------------------------
# The estate, as the tester counted it on the appliance
# ---------------------------------------------------------------------------

_CERT_COUNT = 32
_ACCOUNT_COUNT = 26

_CERT_ROWS = [
    {
        "commonName": f"vcf-node-{i:02d}.lab.local",
        "issuer": "CN=VMware Engineering",
        "validTo": "2027-01-31T00:00:00Z",
        "status": "VALID",
        "resourceName": f"vcf-node-{i:02d}",
        "thumbprint": f"AA:BB:{i:02X}",
    }
    for i in range(_CERT_COUNT)
]

_ACCOUNT_ROWS = [
    {
        "username": f"svc-{i:02d}",
        "resourceName": f"vcf-node-{i:02d}",
        "status": "ACTIVE",
        "expiry": "2026-12-01T00:00:00Z",
        "lastRotated": "2026-06-01T00:00:00Z",
    }
    for i in range(_ACCOUNT_COUNT)
]

#: GET /api/integrations/vcf/{integrationId}/domains — recorded from the VCF
#: Operations 9.1 API reference. ``VCFDomainSummaries``: three sibling arrays,
#: never a single ``domains`` list.
_DOMAIN_SUMMARIES_WIRE = {
    "configuredDomains": [
        {"id": "dom-1", "name": "wld-01", "type": "WORKLOAD", "status": "ACTIVE"}
    ],
    "notConfiguredDomains": [],
    "removedDomains": [],
}


def _client(payload) -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.post.return_value = payload
    client.get.return_value = payload
    return client


def _packagings(rows: list[dict], *names: str) -> list[tuple[str, object]]:
    """Every shape an appliance plausibly uses to hand back ``rows``.

    Only the first is a name this skill ever guessed. The rest stand in for the
    builds whose container key we do not have on record — including the two
    that are not a named container at all.
    """
    cases: list[tuple[str, object]] = [(f"{{{names[0]}: [...]}}", {names[0]: rows})]
    cases += [(f"{{{n}: [...]}}", {n: rows}) for n in names[1:]]
    cases += [
        ("bare JSON array body", rows),
        ("{elements: [...]} (VCF fleet convention)", {"elements": rows}),
        (
            "unguessed key alongside pageInfo",
            {"pageInfo": {"totalCount": len(rows)}, f"{names[0]}Summaries": rows},
        ),
        (
            "unguessed key alongside a hypermedia links array",
            {"links": [{"href": "/x", "rel": "self"}], f"{names[0]}Details": rows},
        ),
    ]
    return cases


# ---------------------------------------------------------------------------
# Certificates: 32 on the appliance, 0 in the answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "body"),
    _packagings(_CERT_ROWS, "certificates", "certificateList"),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_the_fleets_certificates_come_back_however_they_are_packaged(label, body) -> None:
    from vmware_aria.ops.fleet import list_fleet_certificates

    result = list_fleet_certificates(_client(body), limit=None)

    assert result["returned"] == _CERT_COUNT, (
        f"{label}: the fleet holds {_CERT_COUNT} certificates and the tool "
        f"returned {result['returned']}"
    )
    assert result.get("note") is None, "the shape was parsed, so nothing is unconfirmed"
    assert result["items"][0]["subject"] == "vcf-node-00.lab.local"
    assert result["items"][0]["valid_to"] == "2027-01-31T00:00:00Z"


@pytest.mark.parametrize(
    ("label", "body"),
    _packagings(_ACCOUNT_ROWS, "accounts", "passwordAccounts"),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_the_fleets_password_accounts_come_back_however_they_are_packaged(label, body) -> None:
    from vmware_aria.ops.fleet import list_fleet_password_accounts

    result = list_fleet_password_accounts(_client(body), limit=None)

    assert result["returned"] == _ACCOUNT_COUNT, (
        f"{label}: the fleet holds {_ACCOUNT_COUNT} managed accounts and the "
        f"tool returned {result['returned']}"
    )
    assert result.get("note") is None
    assert result["items"][0]["username"] == "svc-00"


# ---------------------------------------------------------------------------
# Findings: rows arrived, the name column did not
# ---------------------------------------------------------------------------


def test_a_findings_name_comes_from_ruleName() -> None:
    """The Findings model names the rule in ``ruleName``, beside ``ruleUuid``."""
    from vmware_aria.ops.fleet import list_findings

    body = {
        "findings": [
            {
                "ruleUuid": "1f0c-…-9ab",
                "ruleName": "Cluster CPU contention above threshold",
                "severity": "CRITICAL",
                "category": "PERFORMANCE",
                "affectedObjectsCount": 4,
            }
        ]
    }
    row = list_findings(_client(body))["items"][0]

    assert row["name"] == "Cluster CPU contention above threshold", (
        "the findings table shipped an empty name column on real hardware"
    )
    assert row["rule_uuid"] == "1f0c-…-9ab"
    assert row["affected_objects_count"] == 4


@pytest.mark.parametrize("key", ["name", "title"])
def test_a_findings_name_still_falls_back_to_the_older_keys(key: str) -> None:
    from vmware_aria.ops.fleet import list_findings

    body = {"findings": [{"ruleUuid": "r-1", key: "older build wording"}]}
    assert list_findings(_client(body))["items"][0]["name"] == "older build wording"


@pytest.mark.parametrize(
    ("label", "body"),
    _packagings([{"ruleUuid": "r-1", "ruleName": "n", "severity": "WARNING"}], "findings"),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_findings_come_back_however_they_are_packaged(label, body) -> None:
    from vmware_aria.ops.fleet import list_findings

    result = list_findings(_client(body))
    assert result["returned"] == 1, label
    assert result.get("note") is None


# ---------------------------------------------------------------------------
# Recorded shape: the domains container key, asserted verbatim
# ---------------------------------------------------------------------------


def test_the_recorded_VCFDomainSummaries_body_still_parses() -> None:
    """Dropping DOMAIN_BUCKETS must be red — this body is on record."""
    from vmware_aria.ops.fleet import list_fleet_domains

    result = list_fleet_domains(_client(_DOMAIN_SUMMARIES_WIRE), "int-1")

    assert result["returned"] == 1
    assert result["items"][0]["name"] == "wld-01"
    assert result["items"][0]["configuration_state"] == "configured"
    assert result.get("note") is None


def test_a_removed_domain_does_not_read_as_a_live_one() -> None:
    from vmware_aria.ops.fleet import list_fleet_domains

    body = {
        "configuredDomains": [],
        "notConfiguredDomains": [],
        "removedDomains": [{"id": "dom-9", "name": "gone", "status": "REMOVED"}],
    }
    row = list_fleet_domains(_client(body), "int-1")["items"][0]
    assert row["configuration_state"] == "removed"


# ---------------------------------------------------------------------------
# Controls: "none" and "unknown" must stay different answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"certificates": []}, id="named container, empty"),
        pytest.param({}, id="empty body"),
        pytest.param([], id="empty array body"),
    ],
)
def test_a_genuinely_empty_fleet_reports_a_confirmed_none(body) -> None:
    from vmware_aria.ops.fleet import list_fleet_certificates

    result = list_fleet_certificates(_client(body))

    assert result["returned"] == 0
    assert result["total"] == 0
    assert result.get("note") is None, (
        "this fleet really has no certificates — saying 'unknown' here would "
        "make the tool useless in exactly the case it should be reassuring"
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"pageInfo": {"totalCount": 32}}, id="a count but no records"),
        pytest.param({"status": "OK", "elapsedMs": 12}, id="no list anywhere"),
        pytest.param("not json we understand", id="not an object at all"),
        pytest.param(
            {"alpha": [{"a": 1}], "beta": [{"b": 2}]},
            id="two candidate record lists — which is which is a guess",
        ),
    ],
)
def test_a_body_with_no_readable_records_is_flagged_not_emptied(body) -> None:
    from vmware_aria.ops.fleet import list_fleet_certificates

    result = list_fleet_certificates(_client(body))

    assert result["returned"] == 0
    assert result.get("note") == _UNRECOGNIZED_SHAPE_NOTE, (
        "an empty answer we cannot vouch for must say so — that is the whole "
        "of 形态 #1"
    )


def test_a_named_container_wins_over_an_ambiguous_sibling() -> None:
    """Names still earn their keep: they settle what shape alone cannot.

    Reading by shape only works while there is one list of records to read.
    Two of them is a coin toss, and the generic container names are what stops
    a body that *does* name its rows from being thrown away as ambiguous.
    """
    from vmware_aria.ops.fleet import list_fleet_certificates

    body = {"items": _CERT_ROWS, "facets": [{"field": "status", "count": 32}]}
    result = list_fleet_certificates(_client(body), limit=None)

    assert result["returned"] == _CERT_COUNT
    assert result["items"][0]["subject"] == "vcf-node-00.lab.local"
    assert result.get("note") is None


def test_an_empty_but_present_bucket_is_still_a_confirmed_none_for_domains() -> None:
    from vmware_aria.ops.fleet import list_fleet_domains

    body = {"configuredDomains": [], "notConfiguredDomains": [], "removedDomains": []}
    result = list_fleet_domains(_client(body), "int-1")

    assert result["returned"] == 0
    assert result.get("note") is None

"""VCF Operations 9.1 Fleet + Diagnostics read operations (suite-api, OpsToken).

Four read-only queries added for VCF Operations 9.1:

* ``list_fleet_certificates``      POST /fleet-management/certificate-management/certificates/query
* ``list_fleet_password_accounts`` POST /fleet-management/password-management/accounts/query
* ``list_fleet_domains``           GET  /integrations/vcf/{integrationId}/domains
* ``list_findings``                POST /diagnostics/findings/query

The four *paths* are VERIFIED against the VCF Operations 9.1 OpenAPI
(vcf-operations-openapi.json) and seeded into
``tests/eval/spec/vcf91_fleet_operations.json`` — the spec-conformance
regression guards them against 踩坑 #36. The response *schemas* are not
verified, and are therefore read without depending on having guessed a name:
the row container is located by name where we know one and by *shape* where we
do not (:func:`_extract_rows`), and individual fields go through ``.get`` so an
absent one yields an empty value rather than a traceback (踩坑 形态 #1).

Guessing names was not merely fragile, it failed in the field: on a real 9.1
appliance a fleet holding 32 certificates and 26 managed password accounts
reported zero of each, because a name-matching parser cannot tell a container
it did not anticipate from a fleet that owns nothing.

All API text passes through ``sanitize()`` to strip control characters and cap
length (prompt-injection defence).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.fleet")

#: Envelope note attached when the appliance returned a non-empty response whose
#: container key we did not recognise. Only the query *paths* are VERIFIED
#: against the VCF 9.1 spec — the response *schemas* are not. Without this note
#: an unrecognised 9.1 shape yields ``items:[], total:0`` and an agent reports
#: "no expiring certificates" / "no findings": a dangerous false all-clear
#: (形态 #1: 空结果读作没问题). The note flags the empty result as unconfirmed.
_UNRECOGNIZED_SHAPE_NOTE = (
    "response shape unrecognized — empty result is unconfirmed; the appliance "
    "returned data in a form this tool did not recognise, so treat the empty "
    "list as 'unknown', not a confirmed 'none'. This needs real-appliance "
    "verification of the VCF 9.1 response schema."
)

# Endpoint paths, relative to the client base_url (``/suite-api/api``). Kept as
# named constants so the new spec-conformance test can assert each is present in
# the VCF 9.1 spec index (a mechanical doc<->code link, 踩坑 形态 #6).
CERT_QUERY_PATH = "/fleet-management/certificate-management/certificates/query"
PASSWORD_ACCOUNT_QUERY_PATH = "/fleet-management/password-management/accounts/query"
FINDINGS_QUERY_PATH = "/diagnostics/findings/query"

#: The domains endpoint answers with the ``VCFDomainSummaries`` model: three
#: sibling arrays, not one ``domains`` list. Taken from the VCF Operations 9.1
#: API reference (Integrations -> "Get Domain Summary"), whose documented
#: example body is ``{"configuredDomains": [], "notConfiguredDomains": [],
#: "removedDomains": []}``. Looking only for a single container is why a fleet
#: with one configured domain reported none on a real 9.1 appliance.
#:
#: The bucket is carried onto each row rather than flattened away: a removed
#: domain must not read as a live one just because it arrived in the same body.
DOMAIN_BUCKETS = {
    "configuredDomains": "configured",
    "notConfiguredDomains": "not_configured",
    "removedDomains": "removed",
}


def _domains_path(integration_id: str) -> str:
    """Path for one VCF integration's domains. Kept here so the id is the only variable."""
    return f"/integrations/vcf/{integration_id}/domains"


#: Container names used across VCF/suite-api collections irrespective of what
#: is being listed. Tried after each endpoint's own names, before falling back
#: to reading the shape.
_GENERIC_CONTAINER_KEYS = ("items", "values", "elements", "content", "results")

#: Sibling arrays that ride along with a suite-api body and are never the rows:
#: hypermedia navigation and per-request diagnostics. Excluded before deciding
#: whether the body holds exactly one list of records, so that a response whose
#: real container key we did not guess is still readable when a ``links`` array
#: sits beside it.
_NON_ROW_KEYS = frozenset({"links", "_links", "errors", "warnings", "messages"})


def _dict_rows(rows: list) -> list[dict]:
    """The dict members of a wire array; anything else is not a record."""
    return [row for row in rows if isinstance(row, dict)]


def _extract_rows(data: Any, *keys: str) -> tuple[list[dict], bool]:
    """Return ``(rows, recognized)`` from a VCF 9.1 query response, defensively.

    Only the *paths* of these endpoints are VERIFIED against the 9.1 spec; the
    response *schemas* are not (see the module docstring). This used to be a
    list of guessed container names, and on a real 9.1 appliance the guess
    missed: a fleet holding 32 certificates and 26 managed accounts answered
    with zero of each, because a body whose rows arrive under a name nobody
    guessed is indistinguishable — to a name-matching parser — from a fleet
    that owns nothing.

    So the name is now the fast path, not the only path. Rows are found by:

    1. a bare array body (the whole response *is* the collection);
    2. one of the endpoint's own container names, then the generic ones;
    3. failing both, the *shape*: exactly one key whose value is a list holding
       records, ignoring the hypermedia and diagnostic siblings that are never
       rows. One list of records in the body has only one thing it can be.

    Ambiguity is not resolved by preference. Two candidate record lists means
    picking one would be a guess, so the body is reported unrecognised — the
    caller then says "unknown" rather than inventing a reading of it.

    ``recognized`` distinguishes a *genuine* empty inventory from a *shape we
    did not understand*: ``True`` when a container was located (even holding an
    empty list) or the body was empty, ``False`` when the body carried content
    we could not read as records. A ``False`` empty result must not be reported
    as "none" — that is the false all-clear this flag exists to prevent.
    """
    # A response that is itself the array: there is no container to name.
    if isinstance(data, list):
        return _dict_rows(data), True
    if not isinstance(data, dict):
        return [], False
    # An empty body is a genuine "nothing", not a shape we failed to read.
    if not data:
        return [], True

    for key in (*keys, *_GENERIC_CONTAINER_KEYS):
        val = data.get(key)
        if isinstance(val, list):
            return _dict_rows(val), True

    # Name unknown — read the shape instead. Only lists that actually hold
    # records count; an empty list under an unknown key could be the rows or
    # could be anything else, and saying which would be the guess this is
    # replacing.
    candidates = [
        (key, val)
        for key, val in data.items()
        if key not in _NON_ROW_KEYS and isinstance(val, list) and any(isinstance(r, dict) for r in val)
    ]
    if len(candidates) == 1:
        key, val = candidates[0]
        _log.info(
            "VCF fleet response used container key %r, which is not one of the "
            "names this skill knows (%s); read it by shape. Worth recording in "
            "tests/eval/spec if it is what this appliance always sends.",
            key,
            ", ".join(keys),
        )
        return _dict_rows(val), True
    return [], False


def _fleet_extra(recognized: bool) -> dict[str, str]:
    """Envelope extras: attach the unconfirmed-shape note when unrecognised."""
    return {} if recognized else {"note": _UNRECOGNIZED_SHAPE_NOTE}


# ---------------------------------------------------------------------------
# list_fleet_certificates
# ---------------------------------------------------------------------------


def _summarize_certificate(row: dict) -> dict:
    """Project one fleet certificate record onto high-signal summary fields."""
    return {
        "subject": sanitize(
            str(row.get("commonName") or row.get("subject") or row.get("name") or ""),
            max_len=300,
        ),
        "issuer": sanitize(str(row.get("issuer") or ""), max_len=300),
        "valid_to": sanitize(
            str(row.get("validTo") or row.get("notAfter") or row.get("expiryDate") or "")
        ),
        "status": sanitize(str(row.get("status") or row.get("certificateStatus") or "")),
        "resource": sanitize(
            str(row.get("resourceName") or row.get("product") or row.get("resource") or "")
        ),
        "thumbprint": sanitize(str(row.get("thumbprint") or row.get("serialNumber") or "")),
    }


def list_fleet_certificates(client: AriaClient, limit: int | None = 50) -> dict:
    """List certificate status/expiry across the VCF fleet.

    POST /fleet-management/certificate-management/certificates/query with an
    empty filter body (returns the full inventory; the fleet certificate set is
    small, so results are capped client-side by ``limit``).

    Args:
        client: Authenticated Aria/VCF Operations API client.
        limit: Max rows to return. ``None`` returns all.

    Returns:
        Family envelope: certificate summaries under ``items``, plus
        returned/limit/total/truncated/hint. ``total`` is the full count
        returned by the query (the endpoint is unpaged at the fleet scale).
    """
    data = client.post(CERT_QUERY_PATH, json_data={}, retries=1)
    rows, recognized = _extract_rows(data, "certificates", "certificateList")
    total = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]
    return paginated(
        [_summarize_certificate(r) for r in rows],
        limit=limit,
        total=total,
        **_fleet_extra(recognized),
    )


# ---------------------------------------------------------------------------
# list_fleet_password_accounts
# ---------------------------------------------------------------------------


def _summarize_account(row: dict) -> dict:
    """Project one managed password-account record onto summary fields."""
    return {
        "username": sanitize(str(row.get("username") or row.get("userName") or row.get("name") or "")),
        "resource": sanitize(
            str(row.get("resourceName") or row.get("resource") or row.get("product") or "")
        ),
        "status": sanitize(str(row.get("status") or row.get("accountStatus") or "")),
        "expiry": sanitize(
            str(row.get("expiry") or row.get("expiryDate") or row.get("validTo") or "")
        ),
        "last_rotated": sanitize(str(row.get("lastRotated") or row.get("lastChanged") or "")),
    }


def list_fleet_password_accounts(client: AriaClient, limit: int | None = 50) -> dict:
    """List managed password-account status across the VCF fleet.

    POST /fleet-management/password-management/accounts/query with an empty
    filter body. Read-only: this does NOT rotate or set any password (the
    rotation endpoint ``PUT .../accounts/{key}/password`` is deliberately not
    wired into this skill).

    Args:
        client: Authenticated Aria/VCF Operations API client.
        limit: Max rows to return. ``None`` returns all.

    Returns:
        Family envelope: account summaries under ``items``.
    """
    data = client.post(PASSWORD_ACCOUNT_QUERY_PATH, json_data={}, retries=1)
    rows, recognized = _extract_rows(data, "accounts", "passwordAccounts")
    total = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]
    return paginated(
        [_summarize_account(r) for r in rows],
        limit=limit,
        total=total,
        **_fleet_extra(recognized),
    )


# ---------------------------------------------------------------------------
# list_fleet_domains
# ---------------------------------------------------------------------------


def _summarize_domain(row: dict) -> dict:
    """Project one VCF domain record onto summary fields."""
    return {
        "id": sanitize(str(row.get("id") or row.get("domainId") or "")),
        "name": sanitize(str(row.get("name") or row.get("domainName") or "")),
        "type": sanitize(str(row.get("type") or row.get("domainType") or "")),
        "status": sanitize(str(row.get("status") or row.get("state") or "")),
    }


def _extract_domain_rows(data: Any) -> tuple[list[tuple[dict, str]], bool]:
    """Return ``([(row, configuration_state), ...], recognized)`` from a domains response.

    Reads every ``VCFDomainSummaries`` bucket present, tagging each row with the
    bucket it came from. A bucket that is present but empty still counts as
    recognised — that is what lets a fleet with genuinely no domains report a
    confirmed "none" instead of an unconfirmed one.

    Falls back to the single-container shapes for any appliance that answers
    with a plain ``domains`` list; those rows carry no bucket, since none was
    stated.
    """
    if not isinstance(data, dict):
        return [], False

    tagged: list[tuple[dict, str]] = []
    matched = False
    for key, state in DOMAIN_BUCKETS.items():
        bucket = data.get(key)
        if isinstance(bucket, list):
            matched = True
            tagged.extend((row, state) for row in bucket if isinstance(row, dict))
    if matched:
        return tagged, True

    rows, recognized = _extract_rows(data, "domains", "domainList")
    return [(row, "") for row in rows], recognized


def list_fleet_domains(client: AriaClient, integration_id: str, limit: int | None = 50) -> dict:
    """List the SDDC/workload domains behind one registered VCF integration.

    GET /integrations/vcf/{integration_id}/domains. The integration id is the
    UUID of the VCF integration registered in Operations (Administration ->
    Integrations -> VCF); the operator supplies it — this skill does not expose
    a VCF-integration listing tool.

    Args:
        client: Authenticated Aria/VCF Operations API client.
        integration_id: UUID of the registered VCF integration.
        limit: Max rows to return. ``None`` returns all.

    Returns:
        Family envelope: domain summaries under ``items``, each carrying a
        ``configuration_state`` of configured / not_configured / removed — the
        VCFDomainSummaries bucket it came from. Empty when the bucket was not
        stated by the appliance.
    """
    data = client.get(_domains_path(integration_id))
    tagged, recognized = _extract_domain_rows(data)
    total = len(tagged)
    if limit and limit > 0:
        tagged = tagged[:limit]
    return paginated(
        [{**_summarize_domain(r), "configuration_state": state} for r, state in tagged],
        limit=limit,
        total=total,
        **_fleet_extra(recognized),
    )


# ---------------------------------------------------------------------------
# list_findings
# ---------------------------------------------------------------------------


def _summarize_finding(row: dict) -> dict:
    """Project one diagnostic finding onto summary fields.

    Field names (ruleUuid/severity/category/affectedObjectsCount) are the ones
    named in the verified VCF 9.1 Findings spec; everything is still read
    through ``.get`` so a missing field degrades to empty.

    A finding is named by the rule that raised it, and 9.1 spells that
    ``ruleName`` — the sibling of the ``ruleUuid`` already read here. Reading
    only ``name``/``title`` is why the findings table came back on a real
    appliance with every row present and the name column blank, which reads
    as "these findings have no names" rather than "we looked in the wrong
    field". Those two stay as fallbacks for older builds.
    """
    return {
        "rule_uuid": sanitize(str(row.get("ruleUuid") or row.get("id") or "")),
        "name": sanitize(
            str(row.get("ruleName") or row.get("name") or row.get("title") or ""),
            max_len=300,
        ),
        "severity": sanitize(str(row.get("severity") or "")),
        "category": sanitize(str(row.get("category") or "")),
        "finding_type": sanitize(str(row.get("findingType") or row.get("type") or "")),
        "affected_objects_count": row.get("affectedObjectsCount"),
    }


def list_findings(
    client: AriaClient,
    severities: list[str] | None = None,
    categories: list[str] | None = None,
    finding_types: list[str] | None = None,
    limit: int | None = 50,
) -> dict:
    """List diagnostic findings, optionally filtered by severity/category/type.

    POST /diagnostics/findings/query. The filter fields severities / categories
    / findingTypes are the verified request-body keys; each is omitted from the
    body when not supplied so an empty query returns all findings.

    Args:
        client: Authenticated Aria/VCF Operations API client.
        severities: Optional severity filter, e.g. ["CRITICAL", "WARNING"].
        categories: Optional category filter.
        finding_types: Optional findingType filter.
        limit: Max rows to return. ``None`` returns all.

    Returns:
        Family envelope: finding summaries under ``items``.
    """
    body: dict[str, Any] = {}
    if severities:
        body["severities"] = [str(s) for s in severities]
    if categories:
        body["categories"] = [str(c) for c in categories]
    if finding_types:
        body["findingTypes"] = [str(t) for t in finding_types]

    data = client.post(FINDINGS_QUERY_PATH, json_data=body, retries=1)
    rows, recognized = _extract_rows(data, "findings", "findingList")
    total = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]
    return paginated(
        [_summarize_finding(r) for r in rows],
        limit=limit,
        total=total,
        **_fleet_extra(recognized),
    )

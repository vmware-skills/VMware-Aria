"""VCF Operations 9.1 Fleet + Diagnostics read operations (suite-api, OpsToken).

Four read-only queries added for VCF Operations 9.1:

* ``list_fleet_certificates``      POST /fleet-management/certificate-management/certificates/query
* ``list_fleet_password_accounts`` POST /fleet-management/password-management/accounts/query
* ``list_fleet_domains``           GET  /integrations/vcf/{integrationId}/domains
* ``list_findings``                POST /diagnostics/findings/query

The four *paths* are VERIFIED against the VCF Operations 9.1 OpenAPI
(vcf-operations-openapi.json) and seeded into
``tests/eval/spec/vcf91_fleet_operations.json`` — the spec-conformance
regression guards them against 踩坑 #36. The response *field names*, by
contrast, are read defensively (``.get`` / degrade-to-empty, 踩坑 形态 #1):
an absent field yields an empty value, never a crash, so a shape that differs
slightly on a real appliance still returns rows instead of a traceback.

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


def _extract_rows(data: Any, *keys: str) -> tuple[list[dict], bool]:
    """Return ``(rows, recognized)`` from a VCF 9.1 query response, defensively.

    VCF 9.1 query responses wrap their rows under a container key whose exact
    name is not pinned in our spec (only the path is VERIFIED). Trying several
    plausible container names and degrading to an empty list keeps a slightly
    different real-appliance shape from crashing the tool (踩坑 形态 #1).

    ``recognized`` distinguishes a *genuine* empty inventory from a *shape we
    did not understand*: it is ``True`` when a known container key held a list
    (even an empty one) or when the response was an empty dict, and ``False``
    when the response was a *non-empty* dict whose keys we do not recognise. A
    ``False`` empty result must not be reported as "none" — that is the false
    all-clear this flag exists to prevent.
    """
    if isinstance(data, dict):
        for key in keys:
            val = data.get(key)
            if isinstance(val, list):
                return [row for row in val if isinstance(row, dict)], True
        # Empty dict -> genuinely empty (recognized); a non-empty dict with no
        # known container key -> unrecognised shape, empty result unconfirmed.
        return [], not bool(data)
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
    rows, recognized = _extract_rows(data, "certificates", "certificateList", "items", "values")
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
    rows, recognized = _extract_rows(data, "accounts", "passwordAccounts", "items", "values")
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

    rows, recognized = _extract_rows(data, "domains", "domainList", "items", "values")
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
    """
    return {
        "rule_uuid": sanitize(str(row.get("ruleUuid") or row.get("id") or "")),
        "name": sanitize(str(row.get("name") or row.get("title") or ""), max_len=300),
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
    rows, recognized = _extract_rows(data, "findings", "findingList", "items", "values")
    total = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]
    return paginated(
        [_summarize_finding(r) for r in rows],
        limit=limit,
        total=total,
        **_fleet_extra(recognized),
    )

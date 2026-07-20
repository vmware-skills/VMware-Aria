"""Aria Operations report management: list definitions, generate, list, get, delete.

Reports are generated from report definition templates.  Typical workflow:
  1. list_report_definitions() — find the template ID
  2. generate_report(definition_id, resource_ids) — trigger generation, get report_id
  3. get_report(report_id) — poll until status == COMPLETED, then use download_url
  4. delete_report(report_id) — clean up after download

All API responses pass through sanitize() to strip control characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

from vmware_aria.ops._paging import CollectionTotal, iter_collection

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient
    from vmware_aria.notify.audit import AuditLogger

_log = logging.getLogger("vmware-aria.ops.reports")


# ---------------------------------------------------------------------------
# list_report_definitions
# ---------------------------------------------------------------------------


def list_report_definitions(
    client: AriaClient,
    name_filter: str | None = None,
    limit: int = 100,
) -> dict:
    """List available report definition templates.

    Args:
        client: Authenticated Aria Operations API client.
        name_filter: Optional substring to filter by definition name (case-insensitive).
        limit: Maximum number of definitions to return (1–500).

    Returns:
        Result envelope with report definition dicts under ``items``, each with
        id, name, description, subject_type. ``total`` carries the collection's
        ``pageInfo.totalCount``, except under a name_filter — that filter is
        applied client-side, so the server's count describes the unfiltered
        collection, not this result.
    """
    limit = max(1, min(limit, 500))

    # Walk every page so a name_filter match beyond the first page is not
    # invisible; stop once `limit` results have been collected.
    collection_total = CollectionTotal()
    results = []
    for d in iter_collection(
        client, "/reportdefinitions", "reportDefinitions", total_sink=collection_total
    ):
        name = sanitize(d.get("name", ""), max_len=300)
        if name_filter and name_filter.lower() not in name.lower():
            continue
        results.append({
            "id": sanitize(d.get("id", "")),
            "name": name,
            "description": sanitize(d.get("description", ""), max_len=500),
            # ReportDefinition `subject` is an ARRAY OF STRINGS (resource
            # kinds), not an object (2026-06-08 spec audit).
            "subject_type": sanitize(", ".join(d.get("subject") or []), max_len=200),
            "owner": sanitize(d.get("owner", ""), max_len=200),
        })
        if len(results) >= limit:
            break
    return paginated(
        results, limit=limit, total=None if name_filter else collection_total.value
    )


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------


def generate_report(
    client: AriaClient,
    definition_id: str,
    resource_ids: list[str] | None = None,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Trigger generation of a report from a report definition template.

    Args:
        client: Authenticated Aria Operations API client.
        definition_id: Report definition (template) UUID.
        resource_ids: Resource UUIDs the report is generated against. The
            suite-api requires exactly one root resource per report — the
            first ID is used; pass the cluster/datacenter UUID to cover its
            children. Required (find IDs via list_resources).
        audit_logger: Optional audit logger.
        target_name: Target name for audit log.

    Returns:
        Dict with report_id, status, and definition_id.
    """
    if not definition_id:
        raise ValueError(
            "definition_id must be a non-empty report definition UUID. Run "
            "list_report_definitions and copy an exact 'id' value."
        )
    if not resource_ids:
        raise ValueError(
            "resource_ids must contain at least one resource UUID — the Report "
            "creation API requires a root resourceId. Run list_resources to find "
            "one (e.g. the target cluster or datacenter) and copy its 'id'."
        )
    if len(resource_ids) > 1:
        _log.warning(
            "Report API accepts a single root resource; using '%s' and ignoring %d more",
            resource_ids[0],
            len(resource_ids) - 1,
        )

    # Correct Report creation body (2026-06-08 user report — the old
    # {"reportDefinition": {"id": ...}} nesting is not in the spec):
    # flat reportDefinitionId + resourceId strings.
    payload: dict = {
        "id": None,
        "resourceId": resource_ids[0],
        "reportDefinitionId": definition_id,
        "subject": [],
    }

    data = client.post("/reports", json_data=payload)
    report_id = sanitize(data.get("id", ""))

    result = {
        "report_id": report_id,
        "definition_id": definition_id,
        "status": sanitize(data.get("status", "PENDING")),
        "note": "Poll get_report(report_id) until status == COMPLETED, then use download_url.",
    }

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="generate_report",
            resource=f"report/{report_id}",
            skill="aria",
            parameters={"definition_id": definition_id, "resource_ids": resource_ids},
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# list_reports
# ---------------------------------------------------------------------------


def list_reports(
    client: AriaClient,
    definition_id: str | None = None,
    limit: int = 50,
    status: str | None = None,
    name_filter: str | None = None,
) -> dict:
    """List generated reports, optionally filtered by report definition.

    Args:
        client: Authenticated Aria Operations API client.
        definition_id: Optional report definition UUID to filter results.
        limit: Maximum number of reports to return (1–200).
        status: Optional server-side status filter (e.g. COMPLETED, RUNNING).
        name_filter: Optional server-side report name filter.

    Returns:
        Result envelope with report summary dicts under ``items``, each with
        id, name, status, completion_time_ms. GET /reports is unpaged, so the
        whole matching set is in hand and ``total`` states it exactly.
    """
    limit = max(1, min(limit, 200))
    # GET /reports supports server-side name/resourceId/status/subject filters
    # but has NO reportDefinitionId or pageSize query param (2026-06-08 user
    # report + spec audit) — push status/name down where available, then filter
    # definition_id and apply the limit client-side.
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if name_filter:
        params["name"] = name_filter
    data = client.get("/reports", params=params) if params else client.get("/reports")
    items = data.get("reports", [])
    if definition_id:
        items = [r for r in items if r.get("reportDefinitionId") == definition_id]
    total = len(items)
    if total > limit:
        # Honest truncation: GET /reports can't page server-side, so a large
        # instance can return more matches than `limit`. Tell the caller how
        # many were dropped rather than silently returning a partial slice.
        _log.warning(
            "list_reports returning %d of %d matching reports (limit=%d); raise "
            "limit or narrow with status/name/definition_id to see the rest.",
            limit,
            total,
            limit,
        )
    items = items[:limit]

    # The wire field is `completionTime` — generationTime/finishTime
    # don't exist (2026-06-08 spec audit).
    rows = [
        {
            "id": sanitize(r.get("id", "")),
            "name": sanitize(r.get("name", ""), max_len=300),
            "status": sanitize(r.get("status", "")),
            "definition_id": sanitize(r.get("reportDefinitionId", "")),
            "completion_time_ms": r.get("completionTime"),
            "owner": sanitize(r.get("owner", ""), max_len=200),
        }
        for r in items
    ]
    return paginated(rows, limit=limit, total=total)


# ---------------------------------------------------------------------------
# get_report
# ---------------------------------------------------------------------------


def get_report(
    client: AriaClient,
    report_id: str,
) -> dict:
    """Get status and download URL for a generated report.

    Args:
        client: Authenticated Aria Operations API client.
        report_id: The report UUID.

    Returns:
        Dict with id, name, status, download_url (PDF), csv_url.
        status values: PENDING, RUNNING, COMPLETED, FAILED.
    """
    if not report_id:
        raise ValueError(
            "report_id must be a non-empty generated-report UUID. Run list_reports "
            "and copy an exact 'id' — this is the id of a report *run*, not the "
            "report definition id from list_report_definitions."
        )

    data = client.get(f"/reports/{report_id}")
    base_url = client._base_url  # e.g. https://aria-host:443/suite-api/api

    # `format` is a documented param; values per spec are PDF and CSV
    # (default PDF). Uppercase matches the documented literals.
    download_url = f"{base_url}/reports/{report_id}/download?format=PDF"
    csv_url = f"{base_url}/reports/{report_id}/download?format=CSV"

    return {
        "id": sanitize(data.get("id", "")),
        "name": sanitize(data.get("name", ""), max_len=300),
        "status": sanitize(data.get("status", "")),
        "definition_id": sanitize(data.get("reportDefinitionId", "")),
        # wire field is `completionTime` (generationTime/finishTime don't exist)
        "completion_time_ms": data.get("completionTime"),
        "download_url": download_url,
        "csv_url": csv_url,
    }


# ---------------------------------------------------------------------------
# delete_report
# ---------------------------------------------------------------------------


def delete_report(
    client: AriaClient,
    report_id: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Delete a generated report.

    Args:
        client: Authenticated Aria Operations API client.
        report_id: The report UUID to delete.
        audit_logger: Optional audit logger.
        target_name: Target name for audit log.

    Returns:
        Dict confirming deletion.
    """
    if not report_id:
        raise ValueError(
            "report_id must be a non-empty generated-report UUID. Run list_reports "
            "and copy an exact 'id' — this is the id of a report *run*, not the "
            "report definition id from list_report_definitions."
        )

    client.delete(f"/reports/{report_id}")

    result = {"report_id": report_id, "action": "deleted"}

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="delete_report",
            resource=f"report/{report_id}",
            skill="aria",
            parameters={"report_id": report_id},
            result="ok",
        )

    return result

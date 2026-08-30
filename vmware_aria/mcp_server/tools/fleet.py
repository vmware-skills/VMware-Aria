"""FLEET + PromQL tools (5, read-only) — VCF Operations 9.1.

fleet_certificate_list, fleet_password_account_list, fleet_domain_list,
findings_list, promql_query.

All read-only (risk_level="low", readOnlyHint=True). These surface VCF
Operations 9.1 fleet cert/password/domain status, diagnostic findings, and
real-time PromQL metrics — reads only; no rotation, no writes.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp

_HINT = "Run 'vmware-aria doctor' to verify connectivity."


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def fleet_certificate_list(limit: Optional[int] = 50, target: Optional[str] = None) -> dict:
    """[READ] List certificate status and expiry across the VCF fleet.

    Use this to spot certificates that are expired or expiring soon across all
    VCF components managed by Operations 9.1. Returns per-certificate summaries
    (subject, issuer, valid_to, status, resource, thumbprint) in the family
    paginated envelope (items/returned/limit/total/truncated/hint). Read-only:
    it does not renew or replace any certificate. Gotcha: response field names
    are read defensively — a field absent on your appliance shows as empty
    rather than failing the call.

    Args:
        limit: Max certificates to return (default 50; None returns all).
        target: Aria/VCF Operations target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.fleet import list_fleet_certificates as _fn

        return _fn(server._get_connection(target), limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "fleet_certificate_list"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def fleet_password_account_list(limit: Optional[int] = 50, target: Optional[str] = None) -> dict:
    """[READ] List managed password-account status across the VCF fleet.

    Use this to review which fleet accounts Operations manages and their
    rotation/expiry status. Returns per-account summaries (username, resource,
    status, expiry, last_rotated) in the paginated envelope. Read-only: this
    never rotates or sets a password — the rotation endpoint is deliberately
    not wired into this skill. Gotcha: response field names are read
    defensively; unknown fields degrade to empty.

    Args:
        limit: Max accounts to return (default 50; None returns all).
        target: Aria/VCF Operations target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.fleet import list_fleet_password_accounts as _fn

        return _fn(server._get_connection(target), limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "fleet_password_account_list"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def fleet_domain_list(integration_id: str, limit: Optional[int] = 50, target: Optional[str] = None) -> dict:
    """[READ] List the SDDC/workload domains behind one registered VCF integration.

    Use this to enumerate the domains of a VCF integration registered in
    Operations. The integration_id is the UUID shown under Administration ->
    Integrations -> VCF in the Operations UI; the operator supplies it (this
    skill does not list VCF integrations). Returns domain summaries (id, name,
    type, status, configuration_state) in the paginated envelope, where
    configuration_state is configured / not_configured / removed — a removed
    domain is not a live one. A 404 means the integration_id is wrong — copy
    the exact UUID from the Operations Integrations page. Gotcha: if the
    envelope carries a "note", the appliance answered in a shape this tool did
    not recognise and an empty items list means UNKNOWN, not "no domains" — do
    not report the fleet as domain-free in that case.

    Args:
        integration_id: UUID of the registered VCF integration.
        limit: Max domains to return (default 50; None returns all).
        target: Aria/VCF Operations target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.fleet import list_fleet_domains as _fn

        return _fn(server._get_connection(target), integration_id=integration_id, limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "fleet_domain_list"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def findings_list(
    severities: Optional[str] = None,
    categories: Optional[str] = None,
    finding_types: Optional[str] = None,
    limit: Optional[int] = 50,
    target: Optional[str] = None,
) -> dict:
    """[READ] List Operations diagnostic findings, optionally filtered.

    Use this to review current diagnostic findings (misconfigurations, health
    rule hits) across the environment. Filters are comma-separated strings;
    omit a filter to match all. Returns finding summaries (rule_uuid, name,
    severity, category, finding_type, affected_objects_count) in the paginated
    envelope. Note: these are general operational findings, NOT compliance
    benchmark results — for hardening/compliance use vmware-harden.

    Args:
        severities: Comma-separated severity filter, e.g. "CRITICAL,WARNING".
        categories: Comma-separated category filter.
        finding_types: Comma-separated findingType filter.
        limit: Max findings to return (default 50; None returns all).
        target: Aria/VCF Operations target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    def _split(v: Optional[str]) -> Optional[list[str]]:
        return [s.strip() for s in v.split(",") if s.strip()] if v else None

    try:
        from vmware_aria.ops.fleet import list_findings as _fn

        return _fn(
            server._get_connection(target),
            severities=_split(severities),
            categories=_split(categories),
            finding_types=_split(finding_types),
            limit=limit,
        )
    except Exception as e:
        return {"error": server._safe_error(e, "findings_list"), "hint": _HINT}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def promql_query(
    query: str,
    time: Optional[str] = None,
    source_id: Optional[str] = None,
    limit: Optional[int] = 50,
    target: Optional[str] = None,
) -> dict:
    """[READ] Run a real-time PromQL instant query (VCF Operations 9.1 VODAP service).

    Use this for near-real-time (~2s) metrics via a Prometheus-compatible
    instant query, complementing the historical rollups from
    get_resource_metrics. Requires the real-time metrics (VODAP) integration to
    be registered; if it is not, the tool returns an actionable error. Returns
    result series (labels, timestamp, value) in the paginated envelope plus
    result_type/status. Gotcha: the query reaches a sibling service whose base
    path (/data-query-service) is INFERRED — every result carries
    base_path_confirmed=False until confirmed against a live appliance.

    Args:
        query: PromQL expression (required), e.g. "cpu_usage_average{}".
        time: Optional evaluation timestamp (RFC3339 or Unix seconds).
        source_id: Optional data-source id to scope the query.
        limit: Max result series to return (default 50; None returns all).
        target: Aria/VCF Operations target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.promql import run_promql_query as _fn

        return _fn(
            server._get_connection(target),
            query=query,
            time=time,
            source_id=source_id,
            limit=limit,
        )
    except Exception as e:
        return {"error": server._safe_error(e, "promql_query"), "hint": _HINT}

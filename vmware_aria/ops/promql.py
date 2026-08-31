"""VCF Operations 9.1 real-time (VODAP) PromQL query, with 2-hop token exchange.

The real-time metrics service exposes a Prometheus-compatible instant-query
endpoint (``GET /api/v1/query``) on a *separate* service base and with a
*different* credential than the suite-api. Reaching it is two hops:

1. ``GET  /integrations/services``  — locate the service whose type is
   ``VCF_VODAP`` and read its ``serviceKeys``.
2. ``POST /auth/token/exchange``    — exchange the acquired OpsToken for a
   service-scoped Bearer JWT, passing ``{"serviceKeys": ...}``.

The PromQL query itself is then issued to ``<host>/data-query-service/api/v1/query``
with ``Authorization: Bearer <jwt>`` (NOT the OpsToken header).

Verification (踩坑 #36): the two suite-api paths above and the query path are
all present in ``tests/eval/spec/vcf91_fleet_operations.json``. The
``/data-query-service`` base *prefix* is marked INFERRED there — it comes from
the Swagger UI location, not a wire capture, so it must be confirmed against a
live appliance. Every returned envelope carries ``base_path_confirmed: False``
to surface that caveat to the caller. Response field access is defensive
(``.get`` / degrade-to-empty, 踩坑 形态 #1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize
from vmware_policy.compat import Requires

from vmware_aria.connection import AriaApiError

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.promql")

#: Integration-service type that fronts the real-time metrics (VODAP) service.
VODAP_SERVICE_TYPE = "VCF_VODAP"

#: suite-api paths (relative to the client base_url). Named constants so the
#: VCF-spec conformance test can assert each is a VERIFIED endpoint.
INTEGRATIONS_SERVICES_PATH = "/integrations/services"
TOKEN_EXCHANGE_PATH = "/auth/token/exchange"

#: VODAP real-time metrics is a 9.x capability: neither /integrations/services
#: nor /auth/token/exchange appears in the vROps 8.6 operation index kept under
#: tests/eval/spec/. Declared beside the paths for the same reason as the fleet
#: block: the requirement travels with the request it describes.
REQUIRES_VODAP = Requires(
    product="VCF Operations",
    minimum=(9, 0),
    feature="Real-time (VODAP) PromQL metrics",
)

#: Real-time metrics service base prefix and query path. INFERRED base — see
#: module docstring. Validated against the VCF spec by the new regression test.
DATA_QUERY_SERVICE_BASE = "/data-query-service"
PROMQL_QUERY_PATH = "/api/v1/query"

#: Path fragment the client base_url uses; swapped for DATA_QUERY_SERVICE_BASE
#: to reach the sibling service on the same host.
_SUITE_API_FRAGMENT = "/suite-api/api"


def _extract_rows(data: Any, *keys: str) -> tuple[list[dict], bool]:
    """Return ``(rows, recognized)`` from a response, degrading to empty (形态 #1).

    ``recognized`` is ``False`` only when the response is a *non-empty* dict
    whose container keys we do not recognise — the case where an empty result
    must not be read as a confident "absent". A matched key (even an empty list)
    or an empty dict counts as recognised.
    """
    if isinstance(data, dict):
        for key in keys:
            val = data.get(key)
            if isinstance(val, list):
                return [row for row in val if isinstance(row, dict)], True
        return [], not bool(data)
    return [], False


def _find_vodap_service(client: AriaClient) -> tuple[dict | None, bool]:
    """Locate the registered VCF_VODAP integration service.

    Returns ``(service_or_None, services_shape_recognized)``. The second flag
    lets the caller distinguish "VODAP is genuinely not registered" from "we
    could not parse the /integrations/services response" — the latter must NOT
    emit a confident "enable VODAP" instruction (形态 #1: 空结果读作没问题).
    """
    data = client.get(INTEGRATIONS_SERVICES_PATH, requires=REQUIRES_VODAP)
    services, recognized = _extract_rows(
        data, "services", "integrationServices", "items", "values"
    )
    for svc in services:
        svc_type = str(svc.get("type") or svc.get("serviceType") or "").upper()
        if svc_type == VODAP_SERVICE_TYPE:
            return svc, recognized
    return None, recognized


def _exchange_vodap_token(client: AriaClient) -> str:
    """Run the 2-hop exchange and return a Bearer JWT for the VODAP service.

    Raises ``AriaApiError`` with a teaching hint when the VODAP service is not
    registered, its response shape was unrecognised, it exposes no serviceKeys,
    or the exchange returns no token — all actionable configuration states, not
    bugs.
    """
    svc, recognized = _find_vodap_service(client)
    if svc is None:
        if not recognized:
            # The services response was a non-empty but unrecognised shape: we
            # cannot conclude VODAP is absent. Do NOT tell the operator to
            # "enable it" — it may already be registered (形态 #1).
            raise AriaApiError(
                "Verify the real-time metrics (VODAP) integration in Operations "
                "(Administration -> Integrations), then retry; historical "
                "metrics still work via get_resource_metrics. Reason: "
                "/integrations/services returned an unrecognised shape, so "
                "whether VODAP is registered could not be determined — it may "
                "well be."
            )
        raise AriaApiError(
            "No VCF_VODAP integration service is registered on this Operations "
            "instance, so real-time PromQL metrics are unavailable. Enable the "
            "real-time metrics (VODAP) integration in Operations, then retry. "
            "Historical metrics remain available via get_resource_metrics."
        )
    service_keys = svc.get("serviceKeys") or svc.get("serviceKey") or svc.get("keys")
    if not service_keys:
        # Posting {"serviceKeys": null} only yields a generic 400 that teaches
        # the operator nothing. Raise the authored VODAP hint instead (LOW-1).
        raise AriaApiError(
            "Check the real-time metrics (VODAP) integration's health in "
            "Operations (Administration -> Integrations), then retry; historical "
            "metrics still work via get_resource_metrics. Reason: VCF_VODAP is "
            "registered but exposes no serviceKeys, so the token exchange cannot "
            "be scoped to it."
        )
    resp = client.post(
        TOKEN_EXCHANGE_PATH,
        json_data={"serviceKeys": service_keys},
        retries=1,
        requires=REQUIRES_VODAP,
    )
    token = resp.get("token") or resp.get("accessToken") or resp.get("access_token")
    if not token:
        raise AriaApiError(
            "The token exchange for the VCF_VODAP service returned no token. "
            "Verify the real-time metrics integration is healthy in Operations "
            "(Administration -> Integrations), then retry."
        )
    return str(token)


def _data_query_url(client: AriaClient) -> str:
    """Build the absolute PromQL query URL on the sibling data-query service."""
    base = client.base_url
    if _SUITE_API_FRAGMENT in base:
        root = base.replace(_SUITE_API_FRAGMENT, DATA_QUERY_SERVICE_BASE)
    else:
        # Fallback: keep everything up to the first path segment and append base.
        root = base.rstrip("/") + DATA_QUERY_SERVICE_BASE
    return f"{root}{PROMQL_QUERY_PATH}"


def _summarize_series(row: dict) -> dict:
    """Project one Prometheus result entry onto summary fields, defensively.

    Prometheus instant-query rows look like ``{"metric": {...labels...},
    "value": [<ts>, "<val>"]}``; range rows carry ``values`` instead. Both are
    read through ``.get`` so a shape variation degrades rather than crashes.
    """
    labels = row.get("metric") if isinstance(row.get("metric"), dict) else {}
    value = row.get("value")
    timestamp: Any = None
    scalar: Any = None
    if isinstance(value, list) and len(value) == 2:
        timestamp, scalar = value[0], value[1]
    return {
        "labels": {sanitize(str(k)): sanitize(str(v)) for k, v in labels.items()},
        "timestamp": timestamp,
        "value": sanitize(str(scalar)) if scalar is not None else None,
    }


def run_promql_query(
    client: AriaClient,
    query: str,
    time: str | None = None,
    source_id: str | None = None,
    limit: int | None = 50,
) -> dict:
    """Run a PromQL instant query against the real-time (VODAP) metrics service.

    Args:
        client: Authenticated Aria/VCF Operations API client.
        query: PromQL expression (required), e.g. ``cpu_usage_average{}``.
        time: Optional evaluation timestamp (RFC3339 or Unix seconds).
        source_id: Optional data-source id to scope the query.
        limit: Max result series to return. ``None`` returns all.

    Returns:
        Family envelope: result series under ``items`` (labels/timestamp/value),
        plus ``result_type``, ``status``, ``query`` and ``base_path_confirmed``
        (always False — the ``/data-query-service`` base is INFERRED and needs
        real-appliance confirmation).

    Raises:
        ValueError: when ``query`` is empty.
        AriaApiError: when the VODAP service is unavailable or the query fails.
    """
    if not query or not query.strip():
        raise ValueError(
            "promql_query requires a non-empty PromQL 'query' expression, "
            "e.g. cpu_usage_average{} or avg(mem_usage_average)."
        )

    token = _exchange_vodap_token(client)
    url = _data_query_url(client)
    params: dict[str, Any] = {"query": query}
    if time:
        params["time"] = time
    if source_id:
        params["sourceId"] = source_id
    if limit and limit > 0:
        params["limit"] = limit

    try:
        data = client.raw_request(
            "GET", url, headers={"Authorization": f"Bearer {token}"}, params=params
        )
    except AriaApiError as exc:
        # A 401/403 here is the data-query service rejecting the *exchanged
        # Bearer JWT* — NOT a suite-api credential problem. The connection
        # layer's generic 401 hint points at VMWARE_ARIA_<TARGET>_PASSWORD,
        # which is wrong for this path; re-word toward the VODAP service token
        # / integration health instead (LOW-2).
        if exc.status_code in (401, 403):
            raise AriaApiError(
                f"Verify the real-time metrics (VODAP) integration is healthy "
                f"and its service keys valid in Operations (Administration -> "
                f"Integrations), then retry. VODAP refused the exchanged service "
                f"token (HTTP {exc.status_code}) — this is not a suite-api "
                f"password problem.",
                status_code=exc.status_code,
                method="GET",
                path=url,
            ) from exc
        raise

    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    result = inner.get("result")
    rows = [r for r in result if isinstance(r, dict)] if isinstance(result, list) else []
    total = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]

    return paginated(
        [_summarize_series(r) for r in rows],
        limit=limit,
        total=total,
        result_type=sanitize(str(inner.get("resultType") or "")),
        status=sanitize(str(data.get("status") or "")),
        query=sanitize(query, max_len=500),
        base_path_confirmed=False,
    )

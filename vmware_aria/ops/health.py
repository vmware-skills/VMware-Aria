"""Aria Operations platform health: node and service status, release, collector groups.

All API responses pass through sanitize() to strip control characters and limit length.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vmware_policy import paginated, sanitize

from vmware_aria.connection import AriaApiError, parse_release_info

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.health")

#: The only two service health values observed on a live appliance (8.18.7,
#: 2026-09-13). Anything else is reported as unrecognised, never folded into
#: either verdict by exclusion.
_SERVICE_OK = "OK"
_SERVICE_FAILED = frozenset({"ERROR"})


def _describe_failure(exc: AriaApiError) -> str:
    return f"HTTP {exc.status_code}" if exc.status_code is not None else "no HTTP response"


# ---------------------------------------------------------------------------
# read_product_version
# ---------------------------------------------------------------------------


def read_product_version(client: AriaClient) -> dict:
    """Read which product and release the target is running.

    ``GET /versions/current`` answers while node status is 503 (live 8.18.7).
    Returns ``release_name``, ``product_name``, ``product_version`` (e.g.
    ``"8.18.7"``), ``product_line`` (``"8.x"`` / ``"9.x"``), ``build_number``
    and ``version_error``: ``None`` when the version was read, otherwise why
    not. Never raises for an API failure — the version is context, and a
    health check must still answer without it.
    """
    try:
        data = client.get("/versions/current", retries=0)
    except AriaApiError as exc:
        return {
            **parse_release_info(None),
            "version_error": f"GET /versions/current failed ({_describe_failure(exc)}).",
        }
    info = parse_release_info(data)
    error = None
    if info["product_version"] is None:
        error = (
            "GET /versions/current answered, but its releaseName carried no "
            "version number; the product version is unknown."
        )
    return {**info, "version_error": error}


# ---------------------------------------------------------------------------
# get_aria_health
# ---------------------------------------------------------------------------


def _read_node_status(client: AriaClient) -> tuple[str, int | None, str]:
    """The node's own status, system time and details; a 503 is an answer.

    NodeStatus per spec: ``status`` ("ONLINE" only when every service runs),
    ``systemTime``, optional ``details``. The endpoint answers 503 whenever
    it is not ONLINE — while booting, and also with one stuck service — and
    the 503 body still carries ``status`` and ``systemTime`` (踩坑 #37: the
    health check must never crash on it). Other errors propagate.
    """
    try:
        data = client.get("/deployment/node/status", retries=0)
        http_note = ""
    except AriaApiError as exc:
        if exc.status_code != 503:
            raise
        data = exc.body if isinstance(exc.body, dict) else {}
        http_note = " (HTTP 503 at /deployment/node/status)"
    status = sanitize(str(data.get("status") or "")) or ("OFFLINE" if http_note else "")
    system_time = data.get("systemTime")
    details = sanitize(str(data.get("details") or ""), max_len=300)
    return status, system_time if isinstance(system_time, int) else None, http_note + (
        f" {details}" if details else ""
    )


def _read_services(client: AriaClient) -> tuple[list[dict] | None, str | None]:
    """Per-service health from ``GET /deployment/node/services/info``.

    Returns ``(services, None)`` or ``(None, reason)`` — never ``[]`` for an
    unread breakdown, which would read as "no services are failing".
    """
    try:
        data = client.get("/deployment/node/services/info", retries=0)
    except AriaApiError as exc:
        return None, f"GET /deployment/node/services/info failed ({_describe_failure(exc)})."
    rows = data.get("service") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None, "GET /deployment/node/services/info answered without a 'service' list."
    services = [
        {
            "name": sanitize(str(r.get("name") or "")),
            "health": sanitize(str(r.get("health") or "")).upper(),
            "details": sanitize(str(r.get("details") or ""), max_len=300),
            "uptime_ms": r.get("uptime") if isinstance(r.get("uptime"), int) else None,
            "started_on_ms": r.get("startedOn") if isinstance(r.get("startedOn"), int) else None,
        }
        for r in rows
        if isinstance(r, dict)
    ]
    return services, None


def _assess(node_status: str, services: list[dict] | None) -> str:
    """HEALTHY / DEGRADED / DOWN / UNKNOWN — see get_aria_health."""
    if services is None:
        return "HEALTHY" if node_status == "ONLINE" else "UNKNOWN"
    ok = [s for s in services if s["health"] == _SERVICE_OK]
    failed = [s for s in services if s["health"] in _SERVICE_FAILED]
    unrecognized = len(services) - len(ok) - len(failed)
    if failed and ok:
        return "DEGRADED"
    if failed and not unrecognized:
        return "DOWN"
    if not failed and not unrecognized and node_status == "ONLINE":
        return "HEALTHY"
    return "UNKNOWN"


_ASSESSMENT_TEXT = {
    "HEALTHY": "The node reports ONLINE and no service reports a failure.",
    "DEGRADED": (
        "Some services report OK and others do not. The node flag is OFFLINE "
        "whenever any one service is not running, so this is not an outage: "
        "what the failed services provide may be unavailable, the rest answers."
    ),
    "DOWN": "No service reports OK.",
    "UNKNOWN": (
        "The node is not ONLINE and the per-service breakdown does not settle "
        "whether that is an outage or one failed service."
    ),
}


def get_aria_health(client: AriaClient) -> dict:
    """Check the health of the Aria Operations platform itself.

    Reads three endpoints: node status, per-service health, and the release.

    Returns:
        ``overall_status``: the node's own status verbatim ("ONLINE"/"OFFLINE").
        ONLINE only when *every* service runs, so a single stuck service makes
        it OFFLINE while the platform keeps serving data — it is not an outage
        signal on its own. ``assessment``: HEALTHY (ONLINE, no failed service),
        DEGRADED (some services OK, some ERROR), DOWN (none OK), UNKNOWN (not
        ONLINE and the breakdown cannot tell). ``healthy``: assessment is
        HEALTHY. ``services`` (None when unreadable, with ``services_error``),
        ``services_not_ok`` / ``services_unrecognized`` (names), plus
        ``system_time_ms``, ``details`` and the fields of
        :func:`read_product_version`. A 503 from node status is a status, not
        an error; any other node-status failure raises ``AriaApiError``.
    """
    node_status, system_time, http_note = _read_node_status(client)
    services, services_error = _read_services(client)
    assessment = _assess(node_status, services)
    not_ok = None if services is None else [s["name"] for s in services if s["health"] in _SERVICE_FAILED]
    unrecognized = None if services is None else [
        s["name"] for s in services if s["health"] != _SERVICE_OK and s["health"] not in _SERVICE_FAILED
    ]
    details = f"Node reports {node_status or 'no status'}{http_note}. {_ASSESSMENT_TEXT[assessment]}"
    if not_ok:
        details += f" Not OK: {', '.join(not_ok)}."
    if services_error:
        details += f" {services_error}"
    return {
        "overall_status": node_status,
        "assessment": assessment,
        "healthy": assessment == "HEALTHY",
        "system_time_ms": system_time,
        "services": services,
        "services_not_ok": not_ok,
        "services_unrecognized": unrecognized,
        "services_error": services_error,
        **read_product_version(client),
        "details": details,
    }


# ---------------------------------------------------------------------------
# list_collector_groups
# ---------------------------------------------------------------------------


def _collectors_by_id(client: AriaClient) -> dict[str, dict]:
    """Index GET /collectors results by string id.

    Collector model = {id, uuId, name, state (UP/DOWN), local,
    adapterInstanceIds} — there is no collectorType or hostname field.
    Failures degrade to an empty index (logged) so group listing still works.
    """
    try:
        data = client.get("/collectors")
    except Exception as exc:
        _log.warning("Could not fetch collectors for group enrichment: %s", exc)
        return {}

    items = data.get("collector")
    if not isinstance(items, list):
        items = data.get("collectors")
    if not isinstance(items, list):
        items = []
    return {str(c.get("id", "")): c for c in items if isinstance(c, dict)}


def list_collector_groups(client: AriaClient) -> dict:
    """List collector groups and their member collector status.

    CollectorGroup = {id, name, description, collectorId: [ints],
    systemDefined} — members are an array of collector IDs, not objects
    (2026-06-08 spec audit). Member details (name, state UP/DOWN, local)
    are enriched via one extra GET /collectors call.

    Args:
        client: Authenticated Aria Operations API client.

    Returns:
        Result envelope with collector group dicts under ``items``, each with
        id, name, description, system_defined, collector_count, and member
        collectors (id, name, state, local). GET /collectorgroups is unpaged,
        so the result is always complete: ``truncated`` is False.
    """
    data = client.get("/collectorgroups")
    groups = data.get("collectorGroups", [])
    collectors = _collectors_by_id(client) if groups else {}

    results = []
    for g in groups:
        member_ids = g.get("collectorId") or []
        members = []
        for cid in member_ids:
            c = collectors.get(str(cid), {})
            members.append(
                {
                    "id": sanitize(str(cid)),
                    "name": sanitize(c.get("name", "")),
                    "state": sanitize(c.get("state", "")),
                    "local": c.get("local", None),
                }
            )
        results.append(
            {
                "id": sanitize(g.get("id", "")),
                "name": sanitize(g.get("name", "")),
                "description": sanitize(g.get("description", ""), max_len=300),
                "system_defined": g.get("systemDefined", None),
                "collector_count": len(member_ids),
                "collectors": members,
            }
        )
    return paginated(results)

"""Aria Operations platform health: node status and collector group status.

All API responses pass through sanitize() to strip control characters and limit length.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vmware_policy import paginated, sanitize

from vmware_aria.connection import AriaApiError

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.health")


# ---------------------------------------------------------------------------
# get_aria_health
# ---------------------------------------------------------------------------


def get_aria_health(client: AriaClient) -> dict:
    """Check the health status of the Aria Operations platform itself.

    Queries the deployment node status endpoint to determine if all
    Aria Ops internal services are running correctly.

    Args:
        client: Authenticated Aria Operations API client.

    Returns:
        Dict with overall_status (ONLINE/OFFLINE), healthy bool,
        system_time_ms, and details. Note: the endpoint itself returns
        HTTP 503 when the node is offline.
    """
    # NodeStatus per spec contains exactly: status ("ONLINE" when all
    # services run, else "OFFLINE"), systemTime, and optional details /
    # humanlyReadableSystemTime. The old code read invented fields
    # (clusterStatus.clusterVipAddress as "overall_status" — an IP address,
    # and a services[] array that does not exist). 2026-06-08 user report.
    try:
        # retries=0: for a health check a 503 IS the answer ("not ONLINE"), so
        # don't spend the transient back-off retrying — return OFFLINE at once.
        data = client.get("/deployment/node/status", retries=0)
    except AriaApiError as exc:
        # /deployment/node/status returns HTTP 503 while the node is still
        # booting or one or more suite-api services are not yet ONLINE (the
        # gateway is served by the same node it reports on). For a health
        # check that 503 IS the answer — "platform not online" — not a
        # transport failure to propagate as a traceback. 2026-06-09 user
        # report (#6): the command crashed precisely when it was most needed.
        if exc.status_code == 503:
            return {
                "overall_status": "OFFLINE",
                "healthy": False,
                "system_time_ms": None,
                "details": (
                    "Aria Operations returned HTTP 503 at /deployment/node/status — "
                    "the platform is starting up or one or more services are not "
                    "ONLINE. Wait for the cluster to finish booting and retry."
                ),
            }
        raise

    status = sanitize(data.get("status", ""))
    return {
        "overall_status": status,
        "healthy": status == "ONLINE",
        "system_time_ms": data.get("systemTime", None),
        "details": sanitize(str(data.get("details", "")), max_len=500),
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

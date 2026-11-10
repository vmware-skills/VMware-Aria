"""Aria Operations alert management: list, get, acknowledge, cancel, list definitions.

Write operations (acknowledge, cancel) are audit-logged.
All API responses pass through sanitize() to strip control characters.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

from vmware_aria.ops._collection import iso_utc_or_none
from vmware_aria.ops._ids import require_uuid
from vmware_aria.ops._paging import (  # noqa: F401 — compatibility/runtime export
    MAX_LIMIT,
    CollectionTotal,
    _MAX_TOTAL,
    iter_collection,
    next_offset,
    paginate,
    validate_page_args,
)

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient
    from vmware_aria.notify.audit import AuditLogger

from vmware_aria.ops._alert_shapes import (
    _LOOKUP_NOT_NEEDED as _LOOKUP_NOT_NEEDED,
    _MAX_SYMPTOM_DEPTH as _MAX_SYMPTOM_DEPTH,
    _SEVERITY_RANK as _SEVERITY_RANK,
    _SYMPTOM_CONTAINER_KEYS as _SYMPTOM_CONTAINER_KEYS,
    _SYMPTOM_LEAF_KEYS as _SYMPTOM_LEAF_KEYS,
    _definition_id as _definition_id,
    _definition_severity as _definition_severity,
    _max_state_severity as _max_state_severity,
    _own_name as _own_name,
    _own_severity as _own_severity,
    _summarize_symptom as _summarize_symptom,
    _walk_symptoms as _walk_symptoms,
)

from vmware_aria.ops._alert_definitions import (
    _VALID_CRITICALITIES_DEF as _VALID_CRITICALITIES_DEF,
    create_alert_definition as create_alert_definition,
    delete_alert_definition as delete_alert_definition,
    list_alert_definitions as list_alert_definitions,
    list_symptom_definitions as list_symptom_definitions,
    set_alert_definition_state as set_alert_definition_state,
)



_log = logging.getLogger("vmware-aria.ops.alerts")

_VALID_CRITICALITIES = {"INFORMATION", "WARNING", "IMMEDIATE", "CRITICAL"}

_ALERT_ID_HINT = (
    "Run list_alerts to see open alerts and copy an exact 'id' — note that the "
    "alert UUID is not the affected resource UUID."
)

# Severity ranking for picking the max across AlertDefinition states[]




# ---------------------------------------------------------------------------
# Batched id lookups — names the alert payloads do not carry
# ---------------------------------------------------------------------------

#: Ids per ``GET /resources?resourceId=..&resourceId=..`` request. A resource
#: UUID costs ~48 characters of query string, so 100 keeps the URL near 5 KB —
#: the same bound ``get_top_consumers`` uses against HTTP 414.
_RESOURCE_ID_CHUNK = 100

#: Ids per ``GET /symptomdefinitions?id=..&id=..`` request. Definition ids are
#: long and URL-encode their spaces
#: (``SymptomDefinition-vCenter%20Operations%20Adapter-...``, ~90 characters),
#: so the chunk is half the resource one for the same URL length.
_DEFINITION_ID_CHUNK = 50

#: Per-row outcome of a batched lookup. "not_found" and "failed" are kept apart
#: because they call for different actions: the appliance answered and does not
#: have that id (deleted, or a stale reference), versus the request itself
#: failing, where retrying may well work.
_LOOKUP_RESOLVED = "resolved"
_LOOKUP_NOT_FOUND = "not_found"
_LOOKUP_FAILED = "failed"
_LOOKUP_NO_ID = "no_definition_id"


def _fetch_by_ids(
    fetch: Callable[[list[str]], Any],
    ids: Iterable[str],
    container: str,
    id_key: str,
    chunk: int,
) -> tuple[dict[str, dict], set[str]]:
    """Fetch rows for ``ids`` from an id-filterable collection, in chunks.

    Both collections this serves accept the id parameter repeated — confirmed
    2026-09-13 on Aria Operations 8.18.7: seven definition ids in one
    ``/symptomdefinitions`` request returned all seven, seven ``resourceId``
    values in one ``/resources`` request returned all seven, and an unknown id
    is silently omitted rather than reported. So the cost is
    ``ceil(unique ids / chunk)`` requests, never one per row.

    Rows are keyed by their own id rather than trusted positionally: this
    appliance family has a recorded habit of ignoring a filter parameter
    (``/symptoms?alertId=`` returns all 81 symptoms on 8.18.7), and keying by
    the row's own id is what keeps an ignored filter from attaching a name to
    the wrong alert. Rows for ids nobody asked for are dropped.

    Those rows are also evidence. A response carrying any row that was not
    asked for (or a row with no id) did not apply the filter, so the requested
    ids it omits were never looked up — they may be on a page we did not get.
    Reporting them as "not returned by the appliance (deleted or stale)" would
    state as fact something the answer does not say, so they count as failed.

    Args:
        fetch: Performs one request for a batch of ids. Callers pass a lambda
            around ``client.get`` with the literal path, so the spec
            conformance scan still sees which endpoint is called.
        ids: Ids to resolve; empty values and duplicates are dropped.
        container: Response key holding the row list.
        id_key: Row key holding each row's id.
        chunk: Ids per request.

    Returns:
        ``(found, failed)``: rows by id, and the ids whose request failed or
        answered in a shape this could not read. An id in neither was answered
        for and not returned — "not found", which is not the same as "failed".
    """
    unique = list(dict.fromkeys(str(i) for i in ids if i))
    found: dict[str, dict] = {}
    failed: set[str] = set()
    for start in range(0, len(unique), chunk):
        batch = unique[start : start + chunk]
        try:
            data = fetch(batch)
        except Exception as exc:
            _log.warning("Batched lookup of %d ids failed: %s", len(batch), exc)
            failed.update(batch)
            continue
        rows = data.get(container) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            _log.warning("Batched lookup answered without a '%s' list; %d ids unresolved", container, len(batch))
            failed.update(batch)
            continue
        wanted = set(batch)
        filter_ignored = False
        for row in rows:
            row_id = row.get(id_key) if isinstance(row, dict) else None
            if row_id is not None and str(row_id) in wanted:
                found[str(row_id)] = row
            else:
                filter_ignored = True
        if filter_ignored:
            unanswered = [i for i in batch if i not in found]
            if unanswered:
                _log.warning(
                    "Batched lookup answered '%s' with rows that were not requested — "
                    "the id filter was ignored; %d ids left unresolved, not reported as missing",
                    container,
                    len(unanswered),
                )
                failed.update(unanswered)
    return found, failed


def _lookup_outcome(item_id: str, found: dict[str, dict], failed: set[str]) -> str:
    if item_id in found:
        return _LOOKUP_RESOLVED
    return _LOOKUP_FAILED if item_id in failed else _LOOKUP_NOT_FOUND


def _unresolved_note(
    what: str, failed: int, not_found: int, consequence: str, extra: tuple[str, ...] = ()
) -> str | None:
    """Explain unresolved lookups, or ``None`` when every one resolved.

    ``extra`` carries already-worded reasons a value is unknown that are not a
    failed or missing lookup (a resolved row with no name, a symptom with no
    definition id to look up); empty strings are skipped.
    """
    parts = []
    if failed:
        parts.append(
            f"{failed} {what} could not be retrieved (the lookup failed, or the "
            f"appliance ignored the id filter — retry)"
        )
    if not_found:
        parts.append(f"{not_found} {what} were not returned by the appliance (deleted or stale)")
    parts.extend(p for p in extra if p)
    if not parts:
        return None
    return "; ".join(parts) + f". {consequence}"


# ---------------------------------------------------------------------------
# list_alerts
# ---------------------------------------------------------------------------


def _resolve_alert_resources(
    client: AriaClient, alerts: list[dict]
) -> tuple[dict[str, dict], str | None]:
    """Name and kind for each affected resource, one batched request per chunk.

    The Alert model carries only ``resourceId``, so a page of alerts named no
    object at all — 2026-09-13 on 8.18.7 every row read as a bare UUID.

    Returns:
        ``({resource_id: {"name", "kind"}}, note)``. Resources that did not
        resolve are absent from the map, and a resolved one Aria holds no name
        for maps to ``name: None``; ``note`` says how many of each and why, so
        every null ``resource_name`` has an explanation. It is ``None`` when all
        resolved with a name (or no alert named a resource).
    """
    resource_ids = [str(a.get("resourceId") or "") for a in alerts]
    found, failed = _fetch_by_ids(
        lambda batch: client.get(
            "/resources", params={"resourceId": batch, "pageSize": _RESOURCE_ID_CHUNK}
        ),
        resource_ids,
        container="resourceList",
        id_key="identifier",
        chunk=_RESOURCE_ID_CHUNK,
    )
    names: dict[str, dict] = {}
    for rid, row in found.items():
        key = row.get("resourceKey") if isinstance(row.get("resourceKey"), dict) else {}
        names[rid] = {
            "name": sanitize(str(key.get("name") or ""), max_len=300) or None,
            "kind": sanitize(str(key.get("resourceKindKey") or "")) or None,
        }
    unique = set(filter(None, resource_ids))
    outcomes = [_lookup_outcome(rid, found, failed) for rid in unique]
    nameless = sum(1 for entry in names.values() if entry["name"] is None)
    note = _unresolved_note(
        "affected resource(s)",
        failed=outcomes.count(_LOOKUP_FAILED),
        not_found=outcomes.count(_LOOKUP_NOT_FOUND),
        extra=(
            f"{nameless} affected resource(s) were returned with no name in Aria Operations"
            if nameless else "",
        ),
        consequence=(
            "Their rows carry resource_name null, which means the name is "
            "unknown — not that the alert has no resource. Resolve one with "
            "investigate_alert or get_resource(resource_id)."
        ),
    )
    return names, note



#: Server-side page requested from POST /alerts/query. The appliance may return
#: fewer; nothing here depends on it returning exactly this many.
_ALERT_PAGE_SIZE = MAX_LIMIT


def _walk_alert_pages(
    client: AriaClient, query: dict[str, Any], wanted: int
) -> tuple[list[dict], int | None]:
    """Collect up to ``wanted`` alerts from POST /alerts/query, and the total.

    Walks 0-based ``page``/``pageSize`` from the beginning, which is the same
    pair every other suite-api collection in this skill pages by, and the pair
    the alerts endpoints document.

    It stops on any of: enough rows, a short page, an exhausted
    ``pageInfo.totalCount``, the module's safety cap — or **a page that adds no
    alert this walk has not already seen**. That last one is not defensive
    padding. This endpoint has a recorded habit of accepting query parameters
    and ignoring them: ``status`` and ``criticality`` were silently dropped
    here until the 2026-06-08 report moved them into the request body. If
    ``page`` is dropped the same way, every request returns page zero, and a
    walk that trusted the page number would collect the same alerts for ever.
    Tracking ids makes that case terminate with a short answer instead — short
    being visible in ``returned``, where a duplicate-filled one would not be.

    Returns:
        The alerts collected, and ``pageInfo.totalCount`` if the appliance
        reported one. ``None`` when it did not: this endpoint is not documented
        here as carrying ``pageInfo``, and a total inferred from what we
        happened to fetch would read as fact (踩坑 #36).
    """
    collected: list[dict] = []
    seen: set[str] = set()
    total_count: int | None = None
    page = 0
    while len(collected) < wanted:
        # Pure query endpoint — idempotent, safe to retry transient gateways.
        data = client.post(
            "/alerts/query",
            json_data=query,
            params={"page": page, "pageSize": _ALERT_PAGE_SIZE},
            retries=1,
        )
        items = data.get("alerts", []) or []
        reported = (data.get("pageInfo") or {}).get("totalCount")
        if isinstance(reported, int):
            total_count = reported

        fresh: list[dict] = []
        for alert in items:
            key = alert.get("alertId")
            if key is not None:
                if key in seen:
                    continue
                seen.add(str(key))
            fresh.append(alert)
        if not fresh:
            break
        collected.extend(fresh)

        if len(items) < _ALERT_PAGE_SIZE:
            break
        if total_count is not None and len(collected) >= total_count:
            break
        if len(collected) >= _MAX_TOTAL:
            _log.warning(
                "list_alerts hit the %d-alert safety cap; narrow with "
                "criticality or resource_id.",
                _MAX_TOTAL,
            )
            break
        page += 1
    return collected, total_count


def list_alerts(
    client: AriaClient,
    active_only: bool = True,
    criticality: str | None = None,
    resource_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List alerts from Aria Operations.

    Args:
        client: Authenticated Aria Operations API client.
        active_only: Return only active (non-cancelled) alerts.
        criticality: Filter by criticality: INFORMATION, WARNING, IMMEDIATE, CRITICAL.
        resource_id: Scope alerts to a specific resource UUID.
        limit: Maximum number of alerts to return (1–500). A page size, not a
            ceiling: out-of-range values are rejected, not clamped.
        offset: Alerts to skip before collecting this page. 0 or more; pass the
            previous response's ``next_offset`` to walk the whole set.

    Returns:
        Result envelope with alert summary dicts under ``items``. ``total``
        carries ``pageInfo.totalCount`` where the appliance reports one and is
        ``None`` where it does not — an invented total reads as fact.

        The envelope carries ``next_offset``: pass it back as ``offset`` for
        the next page and stop when it is ``None``. Do not loop on
        ``truncated`` — that says this page is not the whole set, which stays
        true on the last page of a walk.

    Until 2026-08-30 this fetched a single page and clamped ``limit`` to 500
    with ``max(1, min(limit, 500))``, so on an estate with 2,783 alerts the
    other 2,283 could not be reached under any combination of parameters —
    while the envelope's hint advised raising the limit, which the clamp
    silently undid.

    Paging walks server pages from page 0 and skips ``offset`` rows, rather
    than computing ``page = offset // pageSize``. The appliance chooses its own
    page size and need not honour the one we ask for, and if it returns 100
    where we asked 500 then page arithmetic lands the window somewhere else
    entirely — silently, because every row in it is a real alert. Walking costs
    one request per 500 rows skipped and cannot be wrong about which rows those
    are.
    """
    if criticality and criticality.upper() not in _VALID_CRITICALITIES:
        raise ValueError(
            f"Invalid criticality '{criticality}'. "
            f"Must be one of: {', '.join(sorted(_VALID_CRITICALITIES))}. "
            f"Pass one of those values to --criticality (case-insensitive), or "
            f"omit it to list alerts at every criticality."
        )

    validate_page_args(limit, offset)

    # GET /alerts only supports id/resourceId/page/pageSize — status and
    # criticality params were silently ignored (2026-06-08 user report).
    # Server-side filtering goes through POST /alerts/query (AlertQuery).
    query: dict[str, Any] = {"compositeOperator": "AND"}
    if active_only:
        query["activeOnly"] = True
    if criticality:
        query["alertCriticality"] = [criticality.upper()]
    if resource_id:
        query["resource-query"] = {"resourceId": [resource_id]}

    fetched, total_count = _walk_alert_pages(client, query, offset + limit)
    items = paginate(fetched, limit, offset)
    resources, resource_names_note = _resolve_alert_resources(client, items)

    # Alert model fields (2026-06-08 spec audit): criticality is `alertLevel`,
    # the display name is `alertDefinitionName`. There is no alertName,
    # criticality, resourceName, or info field — the resource name and kind
    # come from the batched GET /resources lookup above.
    rows = [
        {
            "id": sanitize(a.get("alertId", "")),
            "name": sanitize(a.get("alertDefinitionName", ""), max_len=300),
            "criticality": sanitize(a.get("alertLevel", "")),
            "status": sanitize(a.get("status", "")),
            "alert_impact": sanitize(a.get("alertImpact", "")),
            "resource_id": sanitize(a.get("resourceId", "")),
            "resource_name": resources.get(str(a.get("resourceId") or ""), {}).get("name"),
            "resource_kind": resources.get(str(a.get("resourceId") or ""), {}).get("kind"),
            "start_time_ms": a.get("startTimeUTC", None),
            "update_time_ms": a.get("updateTimeUTC", None),
            "start_time_utc": iso_utc_or_none(a.get("startTimeUTC")),
            "update_time_utc": iso_utc_or_none(a.get("updateTimeUTC")),
            "alert_definition_id": sanitize(a.get("alertDefinitionId", "")),
            "alert_definition_name": sanitize(a.get("alertDefinitionName", ""), max_len=300),
            "control_state": sanitize(a.get("controlState", "")),
        }
        for a in items
    ]
    return paginated(
        rows,
        limit=limit,
        total=total_count,
        next_offset=next_offset(len(rows), limit, offset, total_count),
        resource_names_note=resource_names_note,
    )


# ---------------------------------------------------------------------------
# get_alert
# ---------------------------------------------------------------------------


# Keys that hold a *nested level* of a contributingsymptoms body rather than a
# symptom itself, and the keys that identify a leaf symptom object. The 9.1
# body is `{"contributingSymptoms": [ {"alertId": ..., "contributingSymptoms":
# {"contributingSymptoms": [ <symptom>, ... ]}} ]}` — three levels, and the
# same key name repeated at each. Unwrapping one level (what this did before)
# yields the per-alert wrappers, whose every symptom field is absent, so all
# five CRITICAL alerts on a real 9.1 appliance came back with symptoms that
# said nothing. Recursing instead of hard-coding three hops keeps the flat
# `{"symptoms": [...]}` body older appliances send working too.
# Guards against a self-referential body walking forever. The real shape is
# three deep; anything past this is not a shape we claim to understand.

#: Attached to get_alert when the symptoms body could not be walked. Without it
#: an unparsed body and a genuinely quiet alert are the same answer — `[]` —
#: and an agent reads "nothing is wrong" off a tool that simply failed to read
#: the response (形态 #1).
_UNPARSED_SYMPTOMS_NOTE = (
    "contributing-symptoms response shape unrecognized — the empty symptom "
    "list is unconfirmed, not a confirmed 'this alert has no symptoms'. The "
    "appliance answered in a form this tool did not recognise; treat it as "
    "unknown and inspect the alert in the Operations UI."
)

#: Attached when the symptoms call itself failed. Same hazard, different cause:
#: a 500 or a timeout also lands as an empty list.
_SYMPTOMS_UNAVAILABLE_NOTE = (
    "contributing symptoms could not be retrieved — the empty symptom list "
    "reflects a failed lookup, not an alert without symptoms. Retry, or "
    "inspect the alert in the Operations UI."
)














def _summarize_with_definitions(client: AriaClient, leaves: list[dict]) -> tuple[list[dict], str]:
    """Summarize symptom leaves, naming the ones that carry no name or severity.

    Only symptoms missing a name or severity are looked up, and their
    definition ids are deduplicated into ``ceil(unique / chunk)`` requests —
    one for any real alert — so a symptom count never becomes a request count.

    Returns:
        ``(symptoms, note)``; ``note`` is ``""`` when every lookup resolved.
    """
    needy = [s for s in leaves if _definition_id(s) and not (_own_name(s) and _own_severity(s))]
    found, failed = _fetch_by_ids(
        lambda batch: client.get(
            "/symptomdefinitions", params={"id": batch, "pageSize": _DEFINITION_ID_CHUNK}
        ),
        [_definition_id(s) for s in needy],
        container="symptomDefinitions",
        id_key="id",
        chunk=_DEFINITION_ID_CHUNK,
    )
    needy_ids = {id(s) for s in needy}
    summaries = []
    for s in leaves:
        if id(s) in needy_ids:
            def_id = _definition_id(s)
            summaries.append(_summarize_symptom(s, found.get(def_id), _lookup_outcome(def_id, found, failed)))
        elif _own_name(s) and _own_severity(s):
            summaries.append(_summarize_symptom(s))
        else:
            summaries.append(_summarize_symptom(s, lookup=_LOOKUP_NO_ID))

    unique_needy = {_definition_id(s) for s in needy}
    outcomes = [_lookup_outcome(i, found, failed) for i in unique_needy]
    # A symptom lands on no_definition_id only when it lacks a name or a
    # severity and has no id to look either up by — also an unknown, not a blank.
    no_id = sum(1 for s in summaries if s["definition_lookup"] == _LOOKUP_NO_ID)
    note = _unresolved_note(
        "symptom definition(s)",
        failed=outcomes.count(_LOOKUP_FAILED),
        not_found=outcomes.count(_LOOKUP_NOT_FOUND),
        extra=(
            f"{no_id} symptom(s) carry no definition id, so their missing name or "
            f"severity could not be looked up" if no_id else "",
        ),
        consequence=(
            "Symptoms whose definition_lookup is not 'resolved' may show an "
            "empty name or severity — that means unknown, not blank. Their "
            "symptom_definition_id is still exact."
        ),
    )
    return summaries, note or ""


def _get_contributing_symptoms(client: AriaClient, alert_id: str) -> tuple[list[dict], str, str]:
    """Fetch triggered symptoms via GET /alerts/contributingsymptoms?id=<alertId>.

    The Alert model has no alertSymptomList — triggered symptoms come from this
    separate endpoint (2026-06-08 spec audit). Returns ``(symptoms, note,
    definitions_note)``, where a non-empty ``note`` says the empty list is
    unconfirmed and a non-empty ``definitions_note`` says some symptoms could
    not be named. Failures still degrade to an empty list (logged) so a
    symptoms hiccup never breaks get_alert, but they no longer pass silently
    as "no symptoms".
    """
    try:
        data = client.get("/alerts/contributingsymptoms", params={"id": alert_id})
    except Exception as exc:
        _log.warning("Could not fetch contributing symptoms for alert %s: %s", alert_id, exc)
        return [], _SYMPTOMS_UNAVAILABLE_NOTE, ""

    rows, recognized = _walk_symptoms(data)
    if not recognized:
        _log.warning("Unrecognized contributing-symptoms shape for alert %s", alert_id)
    symptoms, definitions_note = _summarize_with_definitions(client, rows)
    return symptoms, "" if recognized else _UNPARSED_SYMPTOMS_NOTE, definitions_note


def get_alert(client: AriaClient, alert_id: str, resolve_symptom_resources: bool = True) -> dict:
    """Get full details for a specific alert.

    Triggered symptoms are fetched from GET /alerts/contributingsymptoms.
    Recommendations are not included — they hang off the alert definition,
    not the alert. The Alert model has no resourceName field; resolve the
    name via get_resource(resource_id) when needed.

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: The alert UUID.
        resolve_symptom_resources: Name the object each symptom is on. On
            8.18.7 no contributing symptom carries a resource id, so this pages
            ``GET /symptoms`` (about one request per 1,000 symptoms on the
            appliance, at most 20) plus one batched ``GET /resources``. Writes
            capturing a before-state pass False; the symptoms then carry no
            resource fields.

    Returns:
        Dict with alert details and contributing symptom list. A
        ``symptoms_note`` key is present only when the symptom list is empty
        for a reason other than the alert having no symptoms — an unrecognised
        response shape, or a lookup that failed.

        Each symptom carries ``definition_lookup``: ``resolved`` (name and
        severity came from its symptom definition), ``not_needed`` (the
        instance named itself), ``not_found`` / ``failed`` (the definition
        could not be read — an empty name or severity is then unknown), or
        ``no_definition_id``. A ``symptom_definitions_note`` key is present
        only when some definition did not resolve.
    """
    alert_id = require_uuid(alert_id, "alert_id", "alert", _ALERT_ID_HINT)

    data = client.get(f"/alerts/{alert_id}")
    symptoms, symptoms_note, definitions_note = _get_contributing_symptoms(client, alert_id)
    resources_note = ""
    if resolve_symptom_resources:
        # Imported here: symptom_resources imports this module's lookup helpers.
        from vmware_aria.ops.symptom_resources import attach_symptom_resources

        symptoms, resources_note = attach_symptom_resources(client, symptoms)
    result = {
        "id": sanitize(data.get("alertId", "")),
        "name": sanitize(data.get("alertDefinitionName", ""), max_len=300),
        "criticality": sanitize(data.get("alertLevel", "")),
        "status": sanitize(data.get("status", "")),
        "alert_impact": sanitize(data.get("alertImpact", "")),
        "resource_id": sanitize(data.get("resourceId", "")),
        "start_time_ms": data.get("startTimeUTC", None),
        "update_time_ms": data.get("updateTimeUTC", None),
        "cancel_time_ms": data.get("cancelTimeUTC", None),
        # cancelTimeUTC is 0 on an alert that was never cancelled: no ISO form.
        "start_time_utc": iso_utc_or_none(data.get("startTimeUTC")),
        "update_time_utc": iso_utc_or_none(data.get("updateTimeUTC")),
        "cancel_time_utc": iso_utc_or_none(data.get("cancelTimeUTC")),
        "control_state": sanitize(data.get("controlState", "")),
        "alert_definition_id": sanitize(data.get("alertDefinitionId", "")),
        "alert_definition_name": sanitize(data.get("alertDefinitionName", ""), max_len=300),
        "symptoms": symptoms,
    }
    if symptoms_note:
        result["symptoms_note"] = symptoms_note
    if definitions_note:
        result["symptom_definitions_note"] = definitions_note
    if resources_note:
        result["symptom_resources_note"] = resources_note
    return result


# ---------------------------------------------------------------------------
# acknowledge_alert
# ---------------------------------------------------------------------------


def acknowledge_alert(
    client: AriaClient,
    alert_id: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Acknowledge an active alert by taking ownership of it.

    The suite-api has no dedicated "acknowledge" operation (2026-06-08 user
    report — POST /alerts/{id}/acknowledge does not exist). The closest
    semantic equivalent is POST /alerts?action=takeownership, which assigns
    the alert to the calling user (control state ASSIGNED).

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: The alert UUID to acknowledge (take ownership of).
        audit_logger: Optional audit logger; operation is logged if provided.
        target_name: Target name for audit log record.

    Returns:
        Dict confirming the operation with alert id and new control_state.
    """
    alert_id = require_uuid(alert_id, "alert_id", "alert", _ALERT_ID_HINT)

    # Capture before state
    before = {}
    try:
        # Before-state for the audit row: skip the symptom-resource walk, which
        # would add up to 20 GETs to every write.
        before = get_alert(client, alert_id, resolve_symptom_resources=False)
    except Exception as exc:
        _log.warning("Could not retrieve before-state for alert %s: %s", alert_id, exc)

    client.post("/alerts", json_data={"uuids": [alert_id]}, params={"action": "takeownership"})

    result = {
        "alert_id": alert_id,
        "action": "takeownership",
        "control_state": "ASSIGNED",
    }

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="acknowledge",
            resource=f"alert/{alert_id}",
            skill="aria",
            parameters={"alert_id": alert_id},
            before_state=before,
            after_state=result,
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# cancel_alert
# ---------------------------------------------------------------------------


def cancel_alert(
    client: AriaClient,
    alert_id: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Cancel (dismiss) an active alert.

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: The alert UUID to cancel.
        audit_logger: Optional audit logger; operation is logged if provided.
        target_name: Target name for audit log record.

    Returns:
        Dict confirming the cancellation.
    """
    alert_id = require_uuid(alert_id, "alert_id", "alert", _ALERT_ID_HINT)

    before = {}
    try:
        # Before-state for the audit row: skip the symptom-resource walk, which
        # would add up to 20 GETs to every write.
        before = get_alert(client, alert_id, resolve_symptom_resources=False)
    except Exception as exc:
        _log.warning("Could not retrieve before-state for alert %s: %s", alert_id, exc)

    # DELETE /alerts/{id} does not exist (2026-06-08 user report). Cancelling
    # goes through POST /alerts?action=cancel with a uuids body.
    client.post("/alerts", json_data={"uuids": [alert_id]}, params={"action": "cancel"})

    result = {
        "alert_id": alert_id,
        "action": "cancelled",
        "status": "CANCELLED",
    }

    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="cancel",
            resource=f"alert/{alert_id}",
            skill="aria",
            parameters={"alert_id": alert_id},
            before_state=before,
            after_state=result,
            result="ok",
        )

    return result


# ---------------------------------------------------------------------------
# list_alert_definitions
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# create_alert_definition
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# set_alert_definition_state  (enable / disable)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# delete_alert_definition
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# list_symptom_definitions  (helper for create_alert_definition)
# ---------------------------------------------------------------------------

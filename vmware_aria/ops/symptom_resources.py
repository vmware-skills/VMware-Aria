"""Which object each triggered symptom is on.

Found 2026-09-15 on Aria Operations 8.18.7. ``alert get`` on "vCenter app
health is affected" listed two "vCenter appliance health service is down"
symptoms with an empty ``resource_id`` and ``condition``, so which services
were down could not be read off the alert.

The captured payloads say where the answer is:

* the contributing-symptom leaf carries only ``symptomId``, ``symptomSetId``,
  ``symptomDefinitionsIds`` and an empty ``alertConditions``;
* the symptom *instance* in ``GET /symptoms`` carries ``resourceId`` (the
  ``mem`` and ``system`` service objects), ``statKey``
  (``SERVICE|AVAILABILITY``) and ``message`` (``HT not equal 0 != 1``);
* but ``GET /symptoms?id=..`` — and ``POST /symptoms/query`` with an id list —
  ignore the filter and answer every symptom (78 on the appliance).

So the instances are found by walking ``GET /symptoms`` (still sending the ids,
for appliances that honour them), keying rows by their own id, and stopping
once every wanted id is found. A symptom whose instance could not be read keeps
an empty ``resource_id`` and ``resource_lookup`` says why: ``failed`` (the read
failed, or the walk stopped before the end) is not ``not_found`` (every page
was read and the instance was not there).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vmware_policy import sanitize

from vmware_aria.ops.alerts import _RESOURCE_ID_CHUNK, _fetch_by_ids, _unresolved_note

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.symptom_resources")

#: Server page size for GET /symptoms (the suite-api maximum).
_SYMPTOM_PAGE_SIZE = 1000

#: Safety cap on symptoms walked for one alert.
_SYMPTOM_MAX_TOTAL = 20000

RESOLVED = "resolved"
NOT_NEEDED = "not_needed"
NOT_FOUND = "not_found"
FAILED = "failed"
NO_SYMPTOM_ID = "no_symptom_id"
NO_RESOURCE_ON_INSTANCE = "instance_names_no_resource"


def _walk_symptom_instances(client: AriaClient, wanted: set[str]) -> tuple[dict[str, dict], bool]:
    """Instance rows for ``wanted`` symptom ids, and whether the walk settles the rest.

    ``complete`` is True only when every wanted id was found, or the appliance
    reported ``pageInfo.totalCount`` and that many distinct symptoms were read.
    A short page is not proof of the end: a server may cap its page size below
    the one asked for, and reading 500 rows of 3,000 as "all of them" would
    report unread symptoms as not found. Without a total the walk goes on to an
    empty page and stays incomplete, so the unfound are unknown, never
    ``not_found``. A page that fails or cannot be read stops the walk; rows
    found on earlier pages are kept.
    """
    found: dict[str, dict] = {}
    seen: set[str] = set()
    total: int | None = None
    page = 0
    while True:
        try:
            data = client.get(
                "/symptoms", params={"id": sorted(wanted), "page": page, "pageSize": _SYMPTOM_PAGE_SIZE}
            )
        except Exception as exc:  # noqa: BLE001 — rows already found stand; the rest become unknown
            _log.warning("GET /symptoms page %d failed: %s", page, exc)
            return found, False
        rows = data.get("symptom") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            _log.warning("GET /symptoms page %d answered without a 'symptom' list", page)
            return found, False
        info = data.get("pageInfo")
        reported = info.get("totalCount") if isinstance(info, dict) else None
        if isinstance(reported, int) and not isinstance(reported, bool):
            total = reported
        new_rows = 0
        for row in rows:
            row_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(row_id, str):
                continue
            if row_id not in seen:
                seen.add(row_id)
                new_rows += 1
            if row_id in wanted:
                found[row_id] = row
        if len(found) == len(wanted):
            return found, True
        if total is not None and len(seen) >= total:
            return found, True
        if not rows or not new_rows or len(seen) >= _SYMPTOM_MAX_TOTAL:
            # An empty page without a total, a page of nothing new (the page
            # parameter ignored too), or the cap: what was not seen is unknown.
            _log.warning("GET /symptoms walk stopped after %d symptoms, %d of %d found", len(seen), len(found), len(wanted))
            return found, False
        page += 1


def _with_instance(symptom: dict, row: dict | None, outcome: str) -> dict:
    if row is None:
        return {**symptom, "stat_key": "", "resource_lookup": outcome}
    resource_id = sanitize(str(row.get("resourceId") or ""))
    return {
        **symptom,
        "resource_id": resource_id,
        "stat_key": sanitize(str(row.get("statKey") or ""), max_len=200),
        "condition": symptom["condition"] or sanitize(str(row.get("message") or ""), max_len=300),
        "resource_lookup": RESOLVED if resource_id else NO_RESOURCE_ON_INSTANCE,
    }


def _locate(client: AriaClient, symptoms: list[dict]) -> list[dict]:
    wanted = {s["id"] for s in symptoms if s["id"] and not s["resource_id"]}
    instances, complete = _walk_symptom_instances(client, wanted) if wanted else ({}, True)
    located = []
    for s in symptoms:
        if s["resource_id"]:
            located.append({**s, "stat_key": "", "resource_lookup": NOT_NEEDED})
        elif not s["id"]:
            located.append({**s, "stat_key": "", "resource_lookup": NO_SYMPTOM_ID})
        else:
            row = instances.get(s["id"])
            located.append(_with_instance(s, row, NOT_FOUND if complete else FAILED))
    return located


def _name(client: AriaClient, located: list[dict]) -> tuple[list[dict], str | None]:
    found, failed = _fetch_by_ids(
        lambda batch: client.get("/resources", params={"resourceId": batch, "pageSize": _RESOURCE_ID_CHUNK}),
        [s["resource_id"] for s in located],
        container="resourceList",
        id_key="identifier",
        chunk=_RESOURCE_ID_CHUNK,
    )
    named = []
    for s in located:
        key = (found.get(s["resource_id"]) or {}).get("resourceKey")
        key = key if isinstance(key, dict) else {}
        named.append({
            **s,
            "resource_name": sanitize(str(key.get("name") or ""), max_len=300) or None,
            "resource_kind": sanitize(str(key.get("resourceKindKey") or "")) or None,
        })
    ids = {s["resource_id"] for s in located if s["resource_id"]}
    unresolved = [i for i in ids if i not in found]
    note = _unresolved_note(
        "symptom resource(s)",
        failed=sum(1 for i in unresolved if i in failed),
        not_found=sum(1 for i in unresolved if i not in failed),
        consequence="Their symptoms carry resource_name null: the name is unknown, the resource_id is exact.",
    )
    return named, note


def attach_symptom_resources(client: AriaClient, symptoms: list[dict]) -> tuple[list[dict], str]:
    """Add ``resource_id``, ``resource_name``, ``resource_kind``, ``stat_key`` and ``resource_lookup``.

    ``condition`` is filled from the instance's ``message`` when the leaf gave
    none. Costs one ``GET /symptoms`` walk (only when some symptom lacks a
    resource id) and one batched ``GET /resources`` lookup per alert.

    Returns:
        ``(symptoms, note)``; ``note`` is ``""`` when every symptom's resource
        and name were read.
    """
    if not symptoms:
        return symptoms, ""
    located = _locate(client, symptoms)
    outcomes = [s["resource_lookup"] for s in located]
    instance_note = _unresolved_note(
        "symptom instance(s)",
        failed=outcomes.count(FAILED),
        not_found=outcomes.count(NOT_FOUND),
        extra=(
            f"{outcomes.count(NO_RESOURCE_ON_INSTANCE)} symptom instance(s) name no resource"
            if NO_RESOURCE_ON_INSTANCE in outcomes else "",
        ),
        consequence=(
            "Those symptoms keep an empty resource_id: which object they are on is unknown, "
            "not absent. Retry, or list the alert resource's children with get_resource_relationships."
        ),
    )
    named, name_note = _name(client, located)
    return named, " ".join(n for n in (instance_note, name_note) if n)

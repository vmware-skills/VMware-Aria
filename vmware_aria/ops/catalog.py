"""What a resource reports, what it is, and what it is attached to.

Three read-only lookups an agent needs *before* it queries metrics or walks the
inventory: the stat keys a resource reports (joined with its kind's definition
for name and unit), the resource's properties, and its parent/child
relationships.

The rule every function here follows: a failed or unrecognised read must never
look like "none". A primary read that fails raises ``AriaApiError`` from the
connection layer; a primary read that answers in a shape this module does not
understand raises ``AriaApiError`` with ``status_code=200`` naming the path. A
*secondary* read — the kind's definitions behind a key, the PARENT/CHILD labels
behind a relationship — leaves the rows in place and says the join is
undetermined, the way ``get_resource_metrics`` reports ``undetermined``.

All endpoints measured on Aria Operations 8.18.7 (2026-09-13) and present in
both the 8.6 and 9.1 suite-api indexes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vmware_policy import paginated, sanitize

from vmware_aria.connection import AriaApiError
from vmware_aria.ops._ids import require_uuid
from vmware_aria.ops._paging import next_offset, paginate, validate_page_args

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

_log = logging.getLogger("vmware-aria.ops.catalog")

#: The relationship types the implementation accepts — and the appliance: on
#: 8.18.7 ANCESTOR and DESCENDANT answer HTTP 400, anything else 404.
RELATIONSHIP_TYPES = ("ALL", "PARENT", "CHILD")

#: Server page size for relationship collections (the suite-api maximum).
_REL_PAGE_SIZE = 1000

#: Safety cap on related resources walked for one resource and type.
_REL_MAX_TOTAL = 20000

#: At most this many unjoined keys are named in ``unjoined_keys``.
_MAX_UNJOINED_LISTED = 50

_DEFAULT_ADAPTER_KIND = "VMWARE"


def _unrecognised(path: str, what: str) -> AriaApiError:
    """A 200 whose body cannot be read. Kept under ``_safe_error``'s 300 chars."""
    return AriaApiError(
        f"Aria Operations GET {path} answered HTTP 200 with a body this tool does "
        f"not recognise ({what}). The contents are unknown, not empty: do not "
        f"report them as none.",
        status_code=200,
        method="GET",
        path=path,
    )


def _why_unread(exc: AriaApiError, path: str) -> str:
    if exc.status_code == 200:
        return f"GET {path} answered with a body this tool does not recognise"
    status = f"HTTP {exc.status_code}" if exc.status_code is not None else "no HTTP response"
    return f"GET {path} failed ({status})"


def _text(value: Any) -> str | None:
    """Sanitized non-empty string, else ``None``."""
    if value is None:
        return None
    text = sanitize(str(value))
    return text or None


def _require_id(resource_id: str | None) -> str:
    return require_uuid(
        resource_id,
        "resource_id",
        "resource",
        "Run list_resources (filter with name_filter= or resource_kind=) and copy an exact 'id' value.",
    )


def _envelope(rows: list[dict], limit: int, offset: int, total: int | None, **extra: Any) -> dict:
    page = paginate(rows, limit, offset)
    return paginated(
        page,
        limit=limit,
        total=total,
        next_offset=next_offset(len(page), limit, offset, total),
        **extra,
    )


def _matches(needle: str | None, *fields: str | None) -> bool:
    if not needle:
        return True
    low = needle.lower()
    return any(f is not None and low in f.lower() for f in fields)


# ---------------------------------------------------------------------------
# list_metric_keys
# ---------------------------------------------------------------------------


def list_metric_keys(
    client: AriaClient,
    resource_id: str | None = None,
    resource_kind: str | None = None,
    adapter_kind: str = _DEFAULT_ADAPTER_KIND,
    key_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """The stat keys one resource reports, or the keys a resource kind defines.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Resource UUID — list the keys it actually reports, joined
            with its kind's definitions. Exclusive with ``resource_kind``.
        resource_kind: Resource kind key (e.g. VirtualMachine) — list the keys
            the kind defines. Exclusive with ``resource_id``.
        adapter_kind: Adapter kind of ``resource_kind``. Ignored with
            ``resource_id``, whose adapter kind is read from the resource.
        key_filter: Case-insensitive substring matched against key or name.
        limit: Page size, 1-500.
        offset: Rows to skip; pass back the previous ``next_offset``.

    Returns:
        Envelope of rows sorted by key. Resource mode rows: ``key``, ``name``,
        ``unit`` and ``definition`` — ``found``, ``found_by_instance`` (an
        instanced key such as ``guestfilesystem:/boot|usage`` joined to
        ``guestfilesystem|usage``), ``not_defined_for_kind``, or ``not_read``
        (the definitions could not be read; name and unit are then unknown).
        Kind mode rows: ``key``, ``name``, ``unit``. Also ``source``,
        ``resource_id``, ``resource_kind``, ``adapter_kind``,
        ``definitions_status`` (read / undetermined), ``definitions_note``,
        ``unjoined_count`` and ``unjoined_keys`` (``None`` when not read),
        ``next_offset``.

    Raises:
        ValueError: Neither or both of resource_id / resource_kind, or a bad page.
        AriaApiError: The resource, its stat keys, or (kind mode) the kind's
            definitions could not be read or were unrecognisable.
    """
    if bool(resource_id) == bool(resource_kind):
        raise ValueError(
            "Pass exactly one of resource_id (the keys one resource reports; get the "
            "UUID from list_resources) or resource_kind (the keys a kind defines, "
            "e.g. VirtualMachine)."
        )
    validate_page_args(limit, offset)
    if resource_kind:
        return _kind_keys(client, adapter_kind, resource_kind, key_filter, limit, offset)
    return _resource_keys(client, resource_id, key_filter, limit, offset)


def _read_definitions(client: AriaClient, adapter_kind: str, resource_kind: str) -> dict[str, dict]:
    path = f"/adapterkinds/{adapter_kind}/resourcekinds/{resource_kind}/statkeys"
    data = client.get(f"/adapterkinds/{adapter_kind}/resourcekinds/{resource_kind}/statkeys")
    rows = data.get("resourceTypeAttributes") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _unrecognised(path, "no 'resourceTypeAttributes' list")
    readable = [r for r in rows if isinstance(r, dict) and isinstance(r.get("key"), str)]
    if len(readable) != len(rows):
        raise _unrecognised(path, f"{len(rows) - len(readable)} of {len(rows)} definition rows have no string 'key'")
    return {r["key"]: r for r in readable}


def _kind_keys(
    client: AriaClient, adapter_kind: str, resource_kind: str, key_filter: str | None, limit: int, offset: int
) -> dict:
    defs = _read_definitions(client, adapter_kind, resource_kind)
    rows = [
        {"key": sanitize(k), "name": _text(d.get("name")), "unit": _text(d.get("unit"))}
        for k, d in sorted(defs.items())
    ]
    rows = [r for r in rows if _matches(key_filter, r["key"], r["name"])]
    result = _envelope(
        rows, limit, offset, len(rows),
        source="resource_kind", resource_id=None,
        resource_kind=sanitize(resource_kind), adapter_kind=sanitize(adapter_kind),
        definitions_status="read", definitions_note=None, unjoined_count=0, unjoined_keys=[],
    )
    if not rows:
        what = f"no defined key or name contains '{sanitize(key_filter, 100)}'" if key_filter else "Aria defines no stat keys for this kind"
        result = {**result, "hint": f"{what.capitalize()}. A defined key is still not collected on every resource."}
    return result


def _parse_statkeys(path: str, data: Any) -> list[str]:
    rows = data.get("stat-key") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _unrecognised(path, "no 'stat-key' list")
    keys = [r["key"] for r in rows if isinstance(r, dict) and isinstance(r.get("key"), str)]
    if len(keys) != len(rows):
        raise _unrecognised(path, f"{len(rows) - len(keys)} of {len(rows)} 'stat-key' rows have no string 'key'")
    return keys


def _strip_instance(key: str) -> str:
    """``guestfilesystem:/boot|usage`` -> ``guestfilesystem|usage``; others unchanged."""
    group, sep, rest = key.partition("|")
    if not sep or ":" not in group:
        return key
    return f"{group.split(':', 1)[0]}|{rest}"


def _join(key: str, defs: dict[str, dict] | None) -> dict:
    row: dict[str, Any] = {"key": sanitize(key), "name": None, "unit": None}
    if defs is None:
        return {**row, "definition": "not_read"}
    if key in defs:
        found, how = defs[key], "found"
    elif _strip_instance(key) in defs:
        found, how = defs[_strip_instance(key)], "found_by_instance"
    else:
        return {**row, "definition": "not_defined_for_kind"}
    return {**row, "name": _text(found.get("name")), "unit": _text(found.get("unit")), "definition": how}


def _resource_kinds(client: AriaClient, resource_id: str) -> tuple[str | None, str | None]:
    """Adapter and resource kind of a resource. Raises for a missing resource (404).

    This read is also the existence check: on 8.18.7 ``statkeys`` for an id that
    does not exist answers 200 ``{"stat-key": []}``, indistinguishable from a
    real resource with no keys.
    """
    data = client.get(f"/resources/{resource_id}")
    key = data.get("resourceKey") if isinstance(data, dict) else None
    if not isinstance(key, dict):
        return None, None
    adapter, kind = key.get("adapterKindKey"), key.get("resourceKindKey")
    return (adapter if isinstance(adapter, str) else None, kind if isinstance(kind, str) else None)


def _resource_keys(client: AriaClient, resource_id: str, key_filter: str | None, limit: int, offset: int) -> dict:
    adapter, kind = _resource_kinds(client, resource_id)
    path = f"/resources/{resource_id}/statkeys"
    keys = _parse_statkeys(path, client.get(f"/resources/{resource_id}/statkeys"))

    defs: dict[str, dict] | None = None
    note: str | None = None
    if adapter and kind:
        def_path = f"/adapterkinds/{adapter}/resourcekinds/{kind}/statkeys"
        try:
            defs = _read_definitions(client, adapter, kind)
        except AriaApiError as exc:
            note = _why_unread(exc, def_path)
    else:
        note = f"GET /resources/{resource_id} named no adapter/resource kind"
    if note:
        note += ", so each key's name, unit and definition are unknown (definition: not_read), not missing."

    rows = [_join(k, defs) for k in sorted(set(keys))]
    rows = [r for r in rows if _matches(key_filter, r["key"], r["name"])]
    unjoined = None if defs is None else [r["key"] for r in rows if r["definition"] == "not_defined_for_kind"]
    result = _envelope(
        rows, limit, offset, len(rows),
        source="resource", resource_id=sanitize(resource_id),
        resource_kind=_text(kind), adapter_kind=_text(adapter),
        definitions_status="read" if defs is not None else "undetermined", definitions_note=note,
        unjoined_count=None if unjoined is None else len(unjoined),
        unjoined_keys=None if unjoined is None else unjoined[:_MAX_UNJOINED_LISTED],
    )
    if rows:
        return result
    if key_filter and keys:
        hint = (
            f"None of the {len(set(keys))} keys this resource reports has a key or name "
            f"containing '{sanitize(key_filter, 100)}'. Try a shorter filter such as 'mem'."
        )
    else:
        hint = (
            "This resource exists and reports no stat keys at all: it may be newly "
            "discovered or not collected. Check its collector with list_collector_groups."
        )
    return {**result, "hint": hint}


# ---------------------------------------------------------------------------
# get_resource_properties
# ---------------------------------------------------------------------------


def get_resource_properties(
    client: AriaClient,
    resource_id: str,
    name_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """The properties Aria holds for a resource (current values).

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Resource UUID.
        name_filter: Case-insensitive substring matched against the property name.
        limit: Page size, 1-500.
        offset: Rows to skip; pass back the previous ``next_offset``.

    Returns:
        Envelope of ``{name, value}`` rows sorted by name (``value`` is a string,
        or ``None`` when the property carries none), plus ``resource_id`` and
        ``next_offset``. The endpoint is unpaged, so ``total`` is exact.

    Raises:
        ValueError: Empty resource_id or a bad page.
        AriaApiError: The read failed (a missing resource is a 404) or its body
            was unrecognisable.
    """
    resource_id = _require_id(resource_id)
    validate_page_args(limit, offset)
    path = f"/resources/{resource_id}/properties"
    data = client.get(f"/resources/{resource_id}/properties")
    rows = data.get("property") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _unrecognised(path, "no 'property' list")
    readable = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]
    if len(readable) != len(rows):
        raise _unrecognised(path, f"{len(rows) - len(readable)} of {len(rows)} 'property' rows have no string 'name'")

    props = sorted(
        ({"name": sanitize(r["name"]), "value": _text(r.get("value"))} for r in readable),
        key=lambda p: p["name"],
    )
    props = [p for p in props if _matches(name_filter, p["name"])]
    result = _envelope(props, limit, offset, len(props), resource_id=sanitize(resource_id))
    if props:
        return result
    if name_filter and readable:
        hint = f"None of the {len(readable)} properties has a name containing '{sanitize(name_filter, 100)}'."
    else:
        hint = "Aria holds no properties for this resource."
    return {**result, "hint": hint}


# ---------------------------------------------------------------------------
# get_resource_relationships
# ---------------------------------------------------------------------------


def _related_page(client: AriaClient, resource_id: str, rtype: str, page: int) -> dict:
    params = {"page": page, "pageSize": _REL_PAGE_SIZE}
    if rtype == "ALL":
        return client.get(f"/resources/{resource_id}/relationships", params=params)
    return client.get(f"/resources/{resource_id}/relationships/{rtype}", params=params)


def _walk_related(client: AriaClient, resource_id: str, rtype: str) -> tuple[list[dict], bool]:
    """Every related-resource row of one type, and whether the walk was complete."""
    path = f"/resources/{resource_id}/relationships" + ("" if rtype == "ALL" else f"/{rtype}")
    rows: list[dict] = []
    page = 0
    while True:
        data = _related_page(client, resource_id, rtype, page)
        batch = data.get("resourceList") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise _unrecognised(path, "no 'resourceList' list")
        bad = sum(not (isinstance(r, dict) and isinstance(r.get("identifier"), str) and r["identifier"]) for r in batch)
        if bad:
            raise _unrecognised(path, f"{bad} of {len(batch)} rows have no 'identifier'")
        rows.extend(batch)
        info = data.get("pageInfo")
        total = info.get("totalCount") if isinstance(info, dict) else None
        if len(batch) < _REL_PAGE_SIZE or (isinstance(total, int) and len(rows) >= total):
            return rows, True
        if len(rows) >= _REL_MAX_TOTAL:
            _log.warning("relationship walk for %s (%s) hit the %d-row cap", resource_id, rtype, _REL_MAX_TOTAL)
            return rows, False
        page += 1


def _direction_labels(client: AriaClient, resource_id: str) -> tuple[dict[str, set[str]] | None, str | None]:
    labels: dict[str, set[str]] = {}
    for rtype in ("PARENT", "CHILD"):
        try:
            rows, complete = _walk_related(client, resource_id, rtype)
        except AriaApiError as exc:
            path = f"/resources/{resource_id}/relationships/{rtype}"
            return None, (
                f"{_why_unread(exc, path)}, so which rows are parents and which are "
                f"children is unknown (direction null). The rows themselves are complete."
            )
        if not complete:
            return None, f"{rtype} relationships exceeded {_REL_MAX_TOTAL} rows, so direction is unknown (null)."
        labels[rtype] = {r["identifier"] for r in rows}
    return labels, None


def _direction(rid: str, labels: dict[str, set[str]] | None) -> str | None:
    if labels is None:
        return None
    parent, child = rid in labels["PARENT"], rid in labels["CHILD"]
    if parent and child:
        return "both"
    return "parent" if parent else "child" if child else "other"


def get_resource_relationships(
    client: AriaClient,
    resource_id: str,
    relationship_type: str = "ALL",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Resources related to one resource, with the direction of each relationship.

    Args:
        client: Authenticated Aria Operations API client.
        resource_id: Resource UUID.
        relationship_type: ``ALL``, ``PARENT`` or ``CHILD`` — exactly.
        limit: Page size, 1-500.
        offset: Rows to skip; pass back the previous ``next_offset``.

    Returns:
        Envelope of ``{id, name, kind, adapter_kind, direction}`` rows sorted by
        kind, name, id. ``direction`` is ``parent`` / ``child`` / ``both`` /
        ``other`` (in ALL but in neither PARENT nor CHILD), or ``None`` when the
        PARENT/CHILD labels could not be read — ``direction_note`` then says why.
        Also ``resource_id``, ``relationship_type``, ``next_offset``. ``total``
        is ``None`` only if the walk hit its safety cap.

    Raises:
        ValueError: Bad relationship_type, empty resource_id, or a bad page.
        AriaApiError: The requested relationship read failed (a missing resource
            is a 404) or its body was unrecognisable.
    """
    if relationship_type not in RELATIONSHIP_TYPES:
        raise ValueError(
            f"relationship_type must be exactly one of ALL, PARENT, CHILD (upper case); "
            f"got {relationship_type!r}. ANCESTOR/DESCENDANT are not accepted: walk "
            f"PARENT one level at a time instead."
        )
    resource_id = _require_id(resource_id)
    validate_page_args(limit, offset)

    rows, complete = _walk_related(client, resource_id, relationship_type)
    note: str | None = None
    if relationship_type == "ALL":
        labels, note = _direction_labels(client, resource_id)
    else:
        labels = {"PARENT": set(), "CHILD": set()}
        labels[relationship_type] = {r["identifier"] for r in rows}

    items = sorted(
        (
            {
                "id": sanitize(r["identifier"]),
                "name": sanitize((r.get("resourceKey") or {}).get("name") or ""),
                "kind": sanitize((r.get("resourceKey") or {}).get("resourceKindKey") or ""),
                "adapter_kind": sanitize((r.get("resourceKey") or {}).get("adapterKindKey") or ""),
                "direction": _direction(r["identifier"], labels),
            }
            for r in {r["identifier"]: r for r in rows}.values()
        ),
        key=lambda i: (i["kind"], i["name"], i["id"]),
    )
    if not complete:
        note = (note + " " if note else "") + (
            f"The walk stopped at {_REL_MAX_TOTAL} related resources, so this is not the whole set."
        )
    result = _envelope(
        items, limit, offset, len(items) if complete else None,
        resource_id=sanitize(resource_id), relationship_type=relationship_type, direction_note=note,
    )
    if items:
        return result
    what = "relationships (parent or child)" if relationship_type == "ALL" else f"{relationship_type} relationships"
    return {**result, "hint": f"Aria records no {what} for this resource."}

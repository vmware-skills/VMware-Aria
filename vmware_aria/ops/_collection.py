"""Strict collection reads and value coercion for the maintenance / alert-note tools.

``_paging.iter_collection`` reads a missing container key as an empty page
(``data.get(key, [])``). That is fine where an empty answer is harmless, and
wrong where "no rows" is itself the answer a caller acts on: "no maintenance
schedules" and "no notes on this alert" must not be what an unreadable body
looks like (形态 #1). ``read_window`` reports whether every page it read was
recognised, so the caller can say "unknown" instead of "none".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize

from vmware_aria.connection import AriaApiError
from vmware_aria.ops._paging import _MAX_TOTAL, _PAGE_SIZE

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient


@dataclass(frozen=True)
class Window:
    """One ``[offset, offset + limit)`` window of a collection.

    Attributes:
        rows: The raw row dicts in the window.
        total: ``pageInfo.totalCount`` when the appliance reported one.
        recognised: False when any page lacked a list under the container key,
            or held a row that is not an object — the window is then partial at
            best and its emptiness means nothing.
    """

    rows: tuple[dict, ...]
    total: int | None
    recognised: bool


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_window(
    client: AriaClient,
    path: str,
    container: str,
    *,
    limit: int,
    offset: int,
    extra_params: dict[str, Any] | None = None,
    page_size: int = _PAGE_SIZE,
) -> Window:
    """Read rows ``offset`` to ``offset + limit`` from a 0-based paged collection.

    Pages are walked until enough rows are held, a short page ends the
    collection, ``totalCount`` is reached, or the family's safety cap is hit.
    A transport or HTTP error propagates: a failed read is an error, never an
    empty window.

    Args:
        client: Authenticated Aria Operations API client.
        path: Collection path, e.g. ``/maintenanceschedules``.
        container: Response key holding the row list, e.g. ``schedules``.
        limit: Validated page size.
        offset: Validated rows to skip.
        extra_params: Endpoint filters; ``page`` and ``pageSize`` are added.
        page_size: Server-side page size.

    Returns:
        The window, its total, and whether every page was recognised.
    """
    need = offset + limit
    rows: list[dict] = []
    total: int | None = None
    page = 0
    while True:
        data = client.get(path, params={**(extra_params or {}), "page": page, "pageSize": page_size})
        items = data.get(container) if isinstance(data, dict) else None
        if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
            return Window(tuple(rows[offset:need]), None, False)
        page_info = data.get("pageInfo") if isinstance(data.get("pageInfo"), dict) else {}
        if _is_int(page_info.get("totalCount")):
            total = page_info["totalCount"]
        rows.extend(items)
        if not items or len(items) < page_size or len(rows) >= need or len(rows) >= _MAX_TOTAL:
            break
        if total is not None and len(rows) >= total:
            break
        page += 1
    return Window(tuple(rows[offset:need]), total, True)


def text_or_none(value: Any, max_len: int = 500) -> str | None:
    """Sanitized text, or ``None`` when the field is absent."""
    return None if value is None else sanitize(str(value), max_len=max_len)


def int_or_none(value: Any) -> int | None:
    """An integer field, or ``None`` when absent or not an integer."""
    return value if _is_int(value) else None


def iso_utc_or_none(value: Any) -> str | None:
    """Epoch milliseconds as ISO-8601 UTC (``2023-11-14T22:13:20.000Z``), or ``None``.

    ``None`` for anything that is not a positive integer: Aria writes
    ``cancelTimeUTC: 0`` on an alert that was never cancelled, and that is not
    a moment in January 1970.
    """
    if not _is_int(value) or value <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(value // 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{value % 1000:03d}Z"


def list_or_none(value: Any) -> list | None:
    """A list field with integers kept and everything else sanitized, or ``None``."""
    if not isinstance(value, list):
        return None
    return [v if _is_int(v) else sanitize(str(v), max_len=100) for v in value]


def describe_read_failure(exc: Exception) -> str:
    """Agent-safe text for a failed read embedded in a result.

    ``AriaApiError`` is authored by ``connection.py`` and carries a teaching
    hint; anything else is reduced to its type, for the reason ``_safe_error``
    gives — its text was not written for an agent and may carry detail it
    should not see.
    """
    if isinstance(exc, AriaApiError):
        return sanitize(str(exc), max_len=300)
    return f"{type(exc).__name__}: read failed."

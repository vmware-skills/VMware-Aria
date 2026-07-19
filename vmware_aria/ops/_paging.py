"""Shared pagination for suite-api collection GET endpoints.

Several list endpoints (alertdefinitions, symptomdefinitions, reportdefinitions)
cap a single response at a server-side page limit and return the remainder
across 0-based ``page``/``pageSize`` pages with a ``pageInfo.totalCount``. The
previous single-page fetch made a client-side name filter incomplete — a match
beyond the first page was invisible. ``iter_collection`` walks every page so a
name search sees the whole collection, mirroring the list_resources pagination
pattern.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient

# Server-side page size for definition-style collections. These are small,
# low-volume collections, so a single large page usually suffices; pagination
# is a correctness guard for the rare large deployment.
_PAGE_SIZE = 500

# Safety cap on total items walked, mirroring list_resources — never pull an
# unbounded collection into memory.
_MAX_TOTAL = 20000


class CollectionTotal:
    """Sink for a collection's server-reported ``pageInfo.totalCount``.

    ``iter_collection`` stops as soon as its caller has enough rows, so the
    caller never sees the raw pages and cannot read the count itself. Passing
    a sink lets the count reach the result envelope, where a known total is
    what distinguishes a complete page from a possibly-truncated one.

    ``value`` stays ``None`` when the server omits ``pageInfo``.
    """

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: int | None = None


def iter_collection(
    client: AriaClient,
    path: str,
    container_key: str,
    *,
    extra_params: dict[str, Any] | None = None,
    page_size: int = _PAGE_SIZE,
    max_total: int = _MAX_TOTAL,
    total_sink: CollectionTotal | None = None,
) -> Iterator[dict]:
    """Yield every item from a paginated suite-api collection endpoint.

    Walks 0-based pages until a short page or ``pageInfo.totalCount`` is reached
    (or the safety cap), guarding both so servers that omit pageInfo still
    terminate.

    Args:
        client: Authenticated Aria Operations API client.
        path: Collection path, e.g. "/alertdefinitions".
        container_key: JSON key holding the item array, e.g. "alertDefinitions".
        extra_params: Endpoint-specific query params (e.g. resourceKind); ``page``
            and ``pageSize`` are added per request.
        page_size: Server-side page size to request.
        max_total: Safety cap on total items walked.
        total_sink: Optional sink receiving ``pageInfo.totalCount`` as soon as a
            page reports one, so the caller can state the collection size even
            when it stops iterating early.

    Yields:
        Each raw item dict from every page, in order.
    """
    fetched = 0
    page = 0
    while True:
        params: dict[str, Any] = dict(extra_params or {})
        params["page"] = page
        params["pageSize"] = page_size
        data = client.get(path, params=params)
        items = data.get(container_key, []) or []
        total = (data.get("pageInfo") or {}).get("totalCount")
        # Fill the sink before yielding: a caller that has enough rows abandons
        # the generator mid-page and never resumes it, so anything recorded
        # after the yield loop would never reach the caller.
        if total is not None and total_sink is not None:
            total_sink.value = total
        if not items:
            break
        for item in items:
            yield item
            fetched += 1
        # Termination: a short page (fewer than a full pageSize) is the last one;
        # an exhausted totalCount means we've seen everything. Guard both for
        # servers that omit pageInfo, plus the safety cap.
        if len(items) < page_size:
            break
        if total is not None and fetched >= total:
            break
        if fetched >= max_total:
            break
        page += 1

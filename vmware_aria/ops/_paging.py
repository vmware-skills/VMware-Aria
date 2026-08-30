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

#: Largest page a caller may ask for. This was already the number the list ops
#: enforced; what changed on 2026-08-30 is that it is a page size rather than a
#: ceiling. It used to be applied as ``max(1, min(limit, 500))``, which quietly
#: rewrote the caller's argument and left the envelope's hint — "Raise limit
#: ... to see the rest" — advising the one thing that could not work. On the
#: reporting estate that put 2,283 alerts out of reach under any parameters.
MAX_LIMIT = 500


def validate_page_args(limit: int, offset: int) -> None:
    """Reject a page window that cannot mean what it says.

    ``limit`` is a page size: an integer from 1 to :data:`MAX_LIMIT`. It is
    never a synonym for "unlimited", "none" or "the default" — across this
    family ``limit=0`` had picked up all four readings, and here the clamp
    turned both ``0`` and ``-50`` into ``1`` without saying so.

    A *negative* limit is worse than ambiguous elsewhere in the family:
    ``items[offset:offset + limit]`` is legal Python that quietly returns a
    shorter page than asked for.

    ``offset`` is a count of rows to skip: an integer from 0 up.

    Raises:
        ValueError: If either value is outside its range. The message names the
            range and points at ``offset``, because "limit too large" and "I
            need more rows" are the same request — and pointing at ``limit``
            instead is exactly the advice that failed on real hardware.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_LIMIT:
        raise ValueError(
            f"Invalid limit {limit!r}: pass an integer from 1 to {MAX_LIMIT}. "
            f"To read more rows, keep limit in range and pass the response's "
            f"'next_offset' back as 'offset' until it is null. It is a page "
            f"size, not a way to ask for everything."
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError(
            f"Invalid offset {offset!r}: it is the number of rows to skip and "
            f"must be an integer of 0 or more. Start at 0 and pass the "
            f"response's 'next_offset' back as 'offset' for each following "
            f"page, stopping when it is null."
        )


def next_offset(returned: int, limit: int, offset: int, total: int | None) -> int | None:
    """The ``offset`` for the next page, or ``None`` when this page is the last.

    This — not ``truncated`` — is what a paging loop terminates on. The
    envelope's ``truncated`` answers "is ``items`` the whole collection?", so
    on the last page of a paged walk it is still true: that page holds three
    rows of ten. Reading it as "there is more to fetch" is what makes a loop
    run for ever.

    With a ``total`` the answer is exact. Without one, a page filled exactly to
    the limit cannot be told apart from a page that was cut short, so it is
    reported as having a successor: one more call that comes back empty is a
    cheaper mistake than rows the caller never learns exist.

    Args:
        returned: Rows in this page.
        limit: The validated page size that produced it.
        offset: The validated offset this page started at.
        total: ``pageInfo.totalCount`` where the appliance reported one, else
            ``None``.

    Returns:
        The next offset, or ``None`` if this page ends the collection.
    """
    if returned <= 0:
        return None
    consumed = offset + returned
    if total is not None:
        return consumed if consumed < total else None
    return consumed if returned >= limit else None


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


def paginate(items: list[dict], limit: int, offset: int) -> list[dict]:
    """Return the ``limit``-sized window of ``items`` starting at ``offset``.

    Callers validate first, so out-of-range values do not reach here. The guard
    stays anyway: it is the last thing between a negative limit and a page
    silently missing its final row.
    """
    if limit <= 0:
        return []
    start = max(offset, 0)
    return items[start : start + limit]

"""The 500 clamp is a page size now, not a ceiling, and the walk terminates.

Real-hardware finding, 2026-08-30. Four list tools here clamped ``limit`` with
``max(1, min(limit, 500))`` and offered nothing else — no offset, no page, no
cursor. On the tester's estate that left **2,283 alerts unreachable under any
combination of parameters**, while the envelope's hint said "Raise limit ... to
see the rest". Raising the limit is precisely what the clamp silently undid, so
the one instruction the tool gave could not work. A hint that cannot work is a
defect on its own: it does not merely fail to help, it spends the agent's next
call and its credibility.

Three of the four already walked every server page internally
(``iter_collection``) and simply stopped at ``limit``, so the rows behind the
clamp were being fetched and thrown away. ``list_alerts`` asked
``/alerts/query`` for one page of ``pageSize`` and never looked further.

The clamp itself stays — 500 rows is a page an agent can hold — but it is now
enforced by rejection rather than by quietly rewriting the argument, and there
is an ``offset`` to move it with. The envelope carries ``next_offset``: the
value to pass back, or ``None`` when this page ends the collection.

``truncated`` is deliberately not that signal. It answers "is ``items`` the
whole collection?", which stays true on the last page of a walk.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from vmware_aria.ops._paging import MAX_LIMIT

#: (module, function) for every clamped list op, and the container key its
#: backing endpoint answers with.
LIST_OPS = [
    ("vmware_aria.ops.alerts", "list_alerts", None),
    ("vmware_aria.ops.alerts", "list_alert_definitions", "alertDefinitions"),
    ("vmware_aria.ops.alerts", "list_symptom_definitions", "symptomDefinitions"),
    ("vmware_aria.ops.reports", "list_report_definitions", "reportDefinitions"),
]

IDS = [f for _, f, _ in LIST_OPS]

DEFINITION_OPS = [(m, f, k) for m, f, k in LIST_OPS if k is not None]


def _op(import_path: str, fn_name: str):
    return getattr(importlib.import_module(import_path), fn_name)


def _rows(n: int) -> list[dict]:
    return [
        {
            "id": f"item-{i}",
            "alertId": f"item-{i}",
            "name": f"item-{i}",
            "alertDefinitionName": f"item-{i}",
        }
        for i in range(n)
    ]


def _client(rows: list[dict], container: str | None, *, page_size: int = 500) -> MagicMock:
    """A suite-api client that pages the way the appliance does.

    ``page``/``pageSize`` are honoured and ``pageInfo.totalCount`` is reported,
    which is what the definition endpoints do; ``list_alerts`` gets the same
    treatment through POST /alerts/query.
    """

    def page_of(params: dict | None) -> dict:
        params = params or {}
        size = int(params.get("pageSize", page_size))
        page = int(params.get("page", 0))
        window = rows[page * size : (page + 1) * size]
        return {"pageInfo": {"totalCount": len(rows)}}, window, size

    def get(path, params=None, **kw):
        meta, window, _ = page_of(params)
        return {**meta, container or "results": list(window)}

    def post(path, json_data=None, params=None, **kw):
        meta, window, _ = page_of(params)
        return {**meta, "alerts": list(window)}

    client = MagicMock()
    client.get.side_effect = get
    client.post.side_effect = post
    return client


def _walk(fn, client, page_size: int, label: str, max_calls: int = 20):
    """Follow the op's own ``next_offset`` until it stops. Returns ids + calls."""
    seen: list[str] = []
    offset = 0
    calls = 0
    while True:
        calls += 1
        assert calls <= max_calls, (
            f"{label} paging did not terminate within {max_calls} calls "
            f"(page_size={page_size}, last offset={offset})"
        )
        page = fn(client, limit=page_size, offset=offset)
        assert "next_offset" in page, (
            f"{label} returned no next_offset — an agent has nothing to page by"
        )
        seen.extend(row["id"] for row in page["items"])
        nxt = page["next_offset"]
        if nxt is None:
            return seen, calls
        assert isinstance(nxt, int) and nxt > offset, (
            f"{label} next_offset {nxt!r} does not advance past {offset}"
        )
        offset = nxt


# ---------------------------------------------------------------------------
# The load-bearing test: the loop stops, and sees every row exactly once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("import_path", "fn_name", "container"), LIST_OPS, ids=IDS)
def test_paging_loop_terminates_and_sees_every_row_once(import_path, fn_name, container) -> None:
    fn = _op(import_path, fn_name)
    rows = _rows(10)  # not a multiple of 3 — the last page is partial
    seen, calls = _walk(
        fn, _client(rows, container, page_size=4), page_size=3, label=fn_name
    )
    assert seen == [r["id"] for r in rows], (
        f"{fn_name} paging lost, duplicated or reordered rows: {seen}"
    )
    assert calls == 4, f"{fn_name} took {calls} calls to read 10 rows in pages of 3"


@pytest.mark.parametrize(("import_path", "fn_name", "container"), LIST_OPS, ids=IDS)
def test_the_offset_window_does_not_align_with_the_server_page(
    import_path, fn_name, container
) -> None:
    """The offset is a row count, not a page number.

    The appliance decides its own page size and may not honour the one we ask
    for. An op that turned ``offset`` into ``page = offset // page_size`` would
    hand back a window starting somewhere else entirely — and silently, since
    every row in it is a real row. So the walk starts from page 0 and skips
    rows, and this pins a window that straddles a server page boundary.
    """
    fn = _op(import_path, fn_name)
    page = fn(_client(_rows(10), container, page_size=4), limit=3, offset=3)
    assert [r["id"] for r in page["items"]] == ["item-3", "item-4", "item-5"]


@pytest.mark.parametrize(("import_path", "fn_name", "container"), LIST_OPS, ids=IDS)
def test_rows_past_the_500_clamp_are_reachable(import_path, fn_name, container) -> None:
    """The estate finding, in miniature.

    2,783 alerts, 500 of them reachable and 2,283 not, under any parameters.
    The clamp is now a page size, so the far end of a collection larger than it
    can be reached by paging to it.
    """
    fn = _op(import_path, fn_name)
    rows = _rows(1200)
    client = _client(rows, container, page_size=500)

    page = fn(client, limit=MAX_LIMIT, offset=1000)
    assert page["returned"] == 200
    assert [r["id"] for r in page["items"][:2]] == ["item-1000", "item-1001"]
    assert page["next_offset"] is None, "1200 rows read to the end is the end"

    first = fn(client, limit=MAX_LIMIT, offset=0)
    assert first["returned"] == MAX_LIMIT
    assert first["next_offset"] == MAX_LIMIT
    assert first["total"] == 1200


@pytest.mark.parametrize(("import_path", "fn_name", "container"), LIST_OPS, ids=IDS)
def test_a_limit_above_the_clamp_is_rejected_not_silently_lowered(
    import_path, fn_name, container
) -> None:
    """This is what made the hint a lie.

    ``max(1, min(limit, 500))`` accepted ``limit=3000``, returned 500 rows and
    said "Raise limit ... to see the rest". The caller had raised it. Rejecting
    says so; clamping cannot.
    """
    fn = _op(import_path, fn_name)
    with pytest.raises(ValueError) as exc:
        fn(_client(_rows(1200), container), limit=3000)
    message = str(exc.value)
    assert str(MAX_LIMIT) in message
    assert "offset" in message, "say how to reach the rest, since limit cannot"


# ---------------------------------------------------------------------------
# Controls — a tool that always says "stop" would pass everything above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("import_path", "fn_name", "container"), LIST_OPS, ids=IDS)
def test_a_short_collection_needs_no_second_call(import_path, fn_name, container) -> None:
    fn = _op(import_path, fn_name)
    page = fn(_client(_rows(2), container), limit=50, offset=0)
    assert page["returned"] == 2
    assert page["truncated"] is False
    assert page["next_offset"] is None


@pytest.mark.parametrize(("import_path", "fn_name", "container"), DEFINITION_OPS, ids=IDS[1:])
def test_a_partial_first_page_still_reports_truncated(import_path, fn_name, container) -> None:
    """The control against "report truncated: false and stop unconditionally".

    That passes every termination test above while telling an agent three rows
    are all ten.
    """
    fn = _op(import_path, fn_name)
    page = fn(_client(_rows(10), container), limit=3, offset=0)
    assert page["truncated"] is True
    assert page["total"] == 10
    assert page["next_offset"] == 3


@pytest.mark.parametrize(("import_path", "fn_name", "container"), DEFINITION_OPS, ids=IDS[1:])
def test_truncated_stays_true_on_the_last_page(import_path, fn_name, container) -> None:
    """Pinning the decision: the stop signal is next_offset, not truncated."""
    fn = _op(import_path, fn_name)
    page = fn(_client(_rows(10), container), limit=3, offset=9)
    assert page["returned"] == 1
    assert page["truncated"] is True
    assert page["next_offset"] is None


# ---------------------------------------------------------------------------
# limit=0 and negative limit — rejected, never silently reinterpreted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("import_path", "fn_name", "container"), LIST_OPS, ids=IDS)
@pytest.mark.parametrize("bad_limit", [0, -1, -50])
def test_out_of_range_limit_is_rejected(import_path, fn_name, container, bad_limit) -> None:
    """``max(1, min(limit, 500))`` turned 0 and -50 into 1 without a word.

    Across the family ``limit=0`` had meant unlimited, none, the default and an
    error in different tools. Here it means none of them: it is out of range.
    """
    fn = _op(import_path, fn_name)
    with pytest.raises(ValueError, match="limit"):
        fn(_client(_rows(10), container), limit=bad_limit)


@pytest.mark.parametrize(("import_path", "fn_name", "container"), LIST_OPS, ids=IDS)
def test_negative_offset_is_rejected(import_path, fn_name, container) -> None:
    fn = _op(import_path, fn_name)
    with pytest.raises(ValueError, match="offset"):
        fn(_client(_rows(10), container), limit=3, offset=-5)


# ---------------------------------------------------------------------------
# The MCP surface — a tool promises paging exactly when it can do it
# ---------------------------------------------------------------------------


def _tools() -> dict:
    """The live FastMCP registry — the same view an MCP client gets."""
    import asyncio

    from vmware_aria.mcp_server.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


PAGED_TOOLS = [
    "list_alerts",
    "list_alert_definitions",
    "list_symptom_definitions",
    "list_report_definitions",
]


@pytest.mark.parametrize("tool_name", PAGED_TOOLS)
def test_the_paged_tools_take_an_offset_and_say_how_to_stop(tool_name) -> None:
    tool = _tools()[tool_name]
    assert "offset" in tool.inputSchema.get("properties", {}), (
        f"{tool_name} cannot be asked for the next page"
    )
    assert "next_offset" in (tool.description or ""), (
        f"{tool_name} never names the key a paging loop stops on"
    )


@pytest.mark.parametrize("tool_name", PAGED_TOOLS)
def test_every_paged_tool_actually_forwards_the_offset(tool_name) -> None:
    """Advertising ``offset`` and dropping it is worse than not having it.

    The schema test above passes on a wrapper that accepts ``offset`` and never
    passes it down: the tool then returns page one for every offset an agent
    tries, and an agent that trusts the schema walks in a circle. Mutation
    testing found exactly that hole — removing ``offset=offset`` from one
    wrapper's call left the whole suite green.
    """
    from unittest.mock import patch

    import vmware_aria.mcp_server.server as srv

    container = dict(zip(PAGED_TOOLS, [None, "alertDefinitions", "symptomDefinitions", "reportDefinitions"]))[tool_name]
    client = _client(_rows(10), container, page_size=4)

    with patch.object(srv, "_get_connection", return_value=client):
        result = getattr(srv, tool_name)(limit=3, offset=6)

    assert "error" not in result, result
    assert [row["id"] for row in result["items"]] == ["item-6", "item-7", "item-8"], (
        f"{tool_name} did not forward limit/offset to its ops function"
    )


def test_paginate_itself_never_reaches_python_negative_slicing() -> None:
    """The helper's own guard, tested where it can still fail.

    Validation rejects a negative limit before ``paginate`` sees one, so no
    test that goes through an op can tell whether this guard survives —
    mutating it away passed the entire suite. It is the last thing between
    ``items[0:-1]`` and a page quietly missing its final row, the shape the
    family-wide audit found at 26 call sites.
    """
    from vmware_aria.ops._paging import paginate

    rows = _rows(10)
    for bad_limit in (-1, -3, -9):
        window = paginate(rows, bad_limit, 0)
        assert window != rows[0:bad_limit], (
            f"limit={bad_limit} fell through to Python negative slicing"
        )
        assert window == [], f"limit={bad_limit} produced a page: {window}"
    assert paginate(rows, 0, 0) == []


def test_no_tool_advertises_paging_it_cannot_do() -> None:
    """The instruction and the argument have to arrive together.

    Caught in the writing of this change: a bulk edit put the "pass next_offset
    back as offset" paragraph onto ``list_reports``, which takes no offset and
    returns none. That is the same defect as the hint this change exists to
    fix — a tool telling an agent to do something the tool cannot do — planted
    by the fix for it (形态 #5).
    """
    liars = [
        name
        for name, tool in _tools().items()
        if "next_offset" in (tool.description or "")
        and "offset" not in tool.inputSchema.get("properties", {})
    ]
    assert liars == [], f"tools promising an offset they do not accept: {liars}"


# ---------------------------------------------------------------------------
# The CLI reads the same envelope, and dropped the same part of it
# ---------------------------------------------------------------------------


def test_the_cli_says_where_the_next_page_starts(capsys) -> None:
    from vmware_aria.cli import _print_next_page

    _print_next_page({"next_offset": 50, "total": 2783})
    out = capsys.readouterr().out
    assert "--offset 50" in out, "the follow-on command has to be printable"
    assert "2783" in out


def test_the_cli_stays_quiet_on_the_last_page(capsys) -> None:
    """The control: a message on every page is a message on none of them."""
    from vmware_aria.cli import _print_next_page

    _print_next_page({"next_offset": None, "total": 2783})
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# list_alerts specifically: an appliance that ignores `page` must not loop
# ---------------------------------------------------------------------------


def test_list_alerts_stops_when_the_appliance_ignores_the_page_parameter() -> None:
    """Silent parameter-dropping is documented behaviour of this API.

    POST /alerts/query already ignored ``status`` and ``criticality`` as query
    params (2026-06-08 user report) — that is why they moved into the body. If
    it ignores ``page`` too, every request returns page zero, and a walk that
    trusts the page number would collect the same rows for ever.

    The walk stops instead: it tracks the ids it has seen, and a page that adds
    nothing new is the end of the road. The answer is then short rather than
    wrong, and short is visible in ``returned``.

    The collection has to be big enough that the walk actually loops. A first
    version of this test used ten rows, so the very first response was a short
    page and the walk ended there — it passed without ever reaching the branch
    it was written for, which is the same "verified where the defect cannot
    appear" mistake (形态 #3) as the rest of this change is about.
    """
    from vmware_aria.ops.alerts import list_alerts

    rows = _rows(1200)
    requested: list[int] = []

    def post(path, json_data=None, params=None, **kw):
        params = params or {}
        requested.append(int(params.get("page", 0)))
        size = int(params.get("pageSize", 500))
        return {"alerts": [dict(r) for r in rows[:size]]}  # page is ignored

    client = MagicMock()
    client.post.side_effect = post

    result = list_alerts(client, limit=500, offset=500)
    assert len(requested) >= 2, (
        "the walk must have asked for a second page for this to prove anything"
    )
    assert requested[:2] == [0, 1], "pages are requested by number, from zero"
    assert result["returned"] == 0, (
        "the appliance served page zero twice, so there was no second page of "
        "rows to hand back — better a visibly empty answer than 500 duplicates"
    )
    assert result["next_offset"] is None

    # And the first page is still served correctly off the same appliance.
    first = list_alerts(client, limit=500, offset=0)
    ids = [row["id"] for row in first["items"]]
    assert ids[:2] == ["item-0", "item-1"]
    assert len(set(ids)) == len(ids) == 500, "no duplicates reach the caller"


def test_list_alerts_reports_no_total_when_the_appliance_omits_pageinfo() -> None:
    """An invented total is worse than none — it reads as fact (形态 #1).

    POST /alerts/query is not documented here as carrying ``pageInfo``, and
    this skill has been burned before by API shapes written from memory
    (踩坑 #36). Absent means ``total: None``, and the walk falls back to the
    conservative rule.
    """
    from vmware_aria.ops.alerts import list_alerts

    rows = _rows(10)

    def post(path, json_data=None, params=None, **kw):
        size = int((params or {}).get("pageSize", 500))
        page = int((params or {}).get("page", 0))
        return {"alerts": [dict(r) for r in rows[page * size : (page + 1) * size]]}

    client = MagicMock()
    client.post.side_effect = post

    result = list_alerts(client, limit=3, offset=0)
    assert result["total"] is None
    assert result["next_offset"] == 3, "no total is not a reason to stop early"

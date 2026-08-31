"""``/reports`` rows have no ``name``, and ``completionTime`` is not milliseconds.

Both fields were reported straight from a real VCF Operations 9.1 appliance
(2026-08-31). They are separate bugs with the same shape: a value read under a
convention that belongs to a different endpoint, failing silently.
"""

from __future__ import annotations

from vmware_aria.ops.reports import _completion_time, _report_title

#: The complete key set of one real ``GET /reports`` row, copied from the live
#: appliance. Note what is absent: there is no ``name``. Tests that invent a
#: row shape are how the original bug survived — a hand-written fixture with a
#: ``name`` key describes an endpoint that does not exist.
REAL_REPORT_ROW = {
    "completionTime": "Sun Aug 30 04:40:08 UTC 2026",
    "description": "Utilization Report - vSphere Clusters",
    "id": "9a3f0c62-1d4e-4d0a-8f52-0c2f6b1a7e11",
    "links": [],
    "owner": "admin",
    "publish": False,
    "reportDefinitionId": "51d1b0a2-0c7e-4a3e-9e21-8b8a5f2c9d34",
    "resourceId": "0f2c9d31-6b1a-4e77-9c02-3a5e8d7b4f10",
    "status": "COMPLETED",
    "subject": [],
}


def test_the_title_comes_from_description_not_the_absent_name() -> None:
    assert "name" not in REAL_REPORT_ROW, "fixture drifted from the real row"
    assert _report_title(REAL_REPORT_ROW) == "Utilization Report - vSphere Clusters"


def test_a_definition_row_still_prefers_its_own_name() -> None:
    """``/reportdefinitions`` has both keys and they mean different things.

    Preferring ``description`` unconditionally would swap a definition's title
    for its blurb — fixing one endpoint by breaking the other.
    """
    row = {"name": "Cluster Utilization", "description": "Longer explanatory blurb"}
    assert _report_title(row) == "Cluster Utilization"


def test_the_filter_and_the_column_can_no_longer_disagree() -> None:
    """The asymmetry is what made this expensive to diagnose.

    The server-side ``name`` filter worked while the displayed column was empty,
    so a caller could filter by a title the tool never showed them. Both sides
    now read the same title, so a value that filters is a value that displays.
    """
    title = _report_title(REAL_REPORT_ROW)
    assert title, "an empty title is exactly the state that hid the bug"
    assert title.lower() in REAL_REPORT_ROW["description"].lower()


def test_a_date_string_does_not_masquerade_as_milliseconds() -> None:
    raw, ms = _completion_time(REAL_REPORT_ROW)
    assert raw == "Sun Aug 30 04:40:08 UTC 2026"
    assert ms is None, "a human-readable date must not be handed out as epoch ms"


def test_a_real_epoch_still_reaches_the_ms_field() -> None:
    for value in (1756528808000, "1756528808000"):
        raw, ms = _completion_time({"completionTime": value})
        assert ms == 1756528808000, f"{value!r} should parse"
        assert raw


def test_absent_and_odd_values_do_not_crash_or_invent() -> None:
    assert _completion_time({}) == ("", None)
    assert _completion_time({"completionTime": None}) == ("", None)
    # bool is an int subclass; True must not become epoch 1.
    assert _completion_time({"completionTime": True}) == ("", None)

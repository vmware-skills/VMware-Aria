"""``limit`` must bound the answer, not the scan (形态 #1 / 形态 #4).

2026-08-30, VCF Operations 9.1.0.0 build 25541561, read-only run. The tester
asked ``list_anomalies`` for the top 3 anomalies. The estate's worst object by
a wide margin — the vCenter adapter instance, 18 active anomalies — **was not
in the answer**.

Not a ranking bug: a scoping one. ``limit`` was passed down as the listing's
``pageSize``, so the tool fetched ``limit`` VMs, then sorted *those*. The sort
ran after the truncation, which makes the result "some anomalies off whichever
page came back first" while the docstring promises "the worst ones". On any
estate with more objects than the page, the answer is arbitrary — and it is
arbitrary *silently*, because a ranked list of three plausible rows is exactly
what a correct answer looks like.

Pinned from both ends. Ranking everything is only worth doing if it stays
cheap and still terminates, so the cost is asserted too: the listing is walked
in server-sized pages (not one page per VM), the metric fan-out stays bulk
(踩坑 #31 — never one round trip per object), and an estate smaller than the
limit is answered completely in a single pass rather than sent back for a
second call it does not need.
"""
from __future__ import annotations

import math

import pytest

from vmware_aria.ops.anomaly import _TOTAL_ANOMALIES_STAT_KEY

#: The estate as the tester found it: the top anomaly is the vCenter adapter
#: instance with 18, and it sorts *last* in the inventory listing — which is
#: precisely why a scan capped at the answer size never reached it.
_TOP_OBJECT = "vc"
_TOP_COUNT = 18.0


class _FakeAria:
    """A paging /resources collection plus a bulk stats endpoint.

    Honours ``page``/``pageSize`` the way the appliance does, so a caller that
    asks for a page of ``limit`` genuinely receives only that page. A mock that
    returned the whole inventory regardless would hide the very bug under test.
    """

    def __init__(self, counts: dict[str, float], server_page_size: int | None = None) -> None:
        self._counts = counts
        self._order = list(counts)
        #: An appliance picks its own page size and need not honour the one it
        #: was asked for — the same hazard ``_walk_alert_pages`` documents.
        self._server_page_size = server_page_size
        self.get_calls: list[tuple[str, dict | None]] = []
        self.post_calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params: dict | None = None, **_kw) -> dict:
        self.get_calls.append((path, params))
        if path != "/resources":
            raise AssertionError(f"unexpected GET {path}")
        params = params or {}
        page = int(params.get("page", 0))
        size = self._server_page_size or int(params.get("pageSize", 1000))
        window = self._order[page * size : page * size + size]
        return {
            "resourceList": [
                {
                    "identifier": rid,
                    "resourceKey": {"name": rid, "resourceKindKey": "VirtualMachine"},
                }
                for rid in window
            ],
            "pageInfo": {"totalCount": len(self._order), "page": page, "pageSize": size},
        }

    def post(self, path: str, json_data: dict | None = None, **_kw) -> dict:
        self.post_calls.append((path, json_data))
        if path != "/resources/stats/query":
            raise AssertionError(f"unexpected POST {path}")
        ids = (json_data or {}).get("resourceId", [])
        return {
            "values": [
                {
                    "resourceId": rid,
                    "stat-list": {
                        "stat": [
                            {
                                "statKey": {"key": _TOTAL_ANOMALIES_STAT_KEY},
                                "timestamps": [1000],
                                "data": [self._counts[rid]],
                            }
                        ]
                    },
                }
                for rid in ids
            ]
        }

    def stats_posts(self) -> list[tuple[str, dict | None]]:
        return [c for c in self.post_calls if c[0] == "/resources/stats/query"]

    def listing_gets(self) -> list[tuple[str, dict | None]]:
        return [c for c in self.get_calls if c[0] == "/resources"]


def _estate(n_quiet: int = 60) -> _FakeAria:
    """``n_quiet`` mildly anomalous VMs, then the estate's worst object last."""
    counts: dict[str, float] = {f"vm-{i:02d}": float(i % 5) for i in range(n_quiet)}
    counts[_TOP_OBJECT] = _TOP_COUNT
    return _FakeAria(counts)


# ---------------------------------------------------------------------------
# The finding: the top anomaly was missing from a small-limit answer
# ---------------------------------------------------------------------------


def test_the_worst_anomaly_is_in_the_answer_even_at_limit_three() -> None:
    from vmware_aria.ops.anomaly import list_anomalies

    client = _estate()
    result = list_anomalies(client, limit=3)

    names = [r["resource_name"] for r in result["items"]]
    assert names[0] == _TOP_OBJECT, (
        f"limit=3 must return the three WORST anomalies; got {names}. "
        f"'{_TOP_OBJECT}' ({int(_TOP_COUNT)} anomalies) is the estate's top "
        f"object and was absent on real hardware because limit truncated the "
        f"scan before the sort."
    )
    assert result["items"][0]["anomaly_count"] == _TOP_COUNT
    assert result["returned"] == 3


def test_limit_is_not_pushed_down_as_the_listing_page_size() -> None:
    """The listing page size is the server's, not the caller's answer size."""
    from vmware_aria.ops.anomaly import list_anomalies

    client = _estate()
    list_anomalies(client, limit=3)

    page_sizes = {(p or {}).get("pageSize") for _, p in client.listing_gets()}
    assert 3 not in page_sizes, (
        "limit reached the wire as pageSize — that is the bug: it bounds the "
        f"scan instead of the answer. pageSizes seen: {sorted(page_sizes)}"
    )


def test_every_object_is_ranked_so_the_flagged_total_is_exact() -> None:
    """A complete scan knows how many objects are flagged, so it says so."""
    from vmware_aria.ops.anomaly import list_anomalies

    client = _estate()
    result = list_anomalies(client, limit=3)

    # 61 objects; those with a zero count are not flagged.
    flagged = sum(1 for v in client._counts.values() if v)
    assert result["total"] == flagged, (
        "a scan that covered the estate knows the size of the answer set"
    )
    assert result["truncated"] is True
    assert result["scan_complete"] is True
    assert result["scanned"] == len(client._counts)


# ---------------------------------------------------------------------------
# Control: ranking everything must not cost a round trip per object (踩坑 #31)
# ---------------------------------------------------------------------------


def test_a_full_estate_scan_stays_bulk_and_server_paged() -> None:
    from vmware_aria.ops.anomaly import _STATS_CHUNK, list_anomalies

    client = _FakeAria({f"vm-{i:04d}": float(i % 7) for i in range(2500)})
    result = list_anomalies(client, limit=10)

    assert len(client.listing_gets()) == math.ceil(2500 / 1000), (
        "the inventory is walked in server-sized pages, not one call per object"
    )
    assert len(client.stats_posts()) == math.ceil(2500 / _STATS_CHUNK), (
        "the metric fan-out is bulk and chunked — never one POST per VM"
    )
    assert result["scanned"] == 2500
    assert all(
        len((body or {}).get("resourceId", [])) <= _STATS_CHUNK
        for _, body in client.stats_posts()
    ), "no single bulk body may exceed the chunk size"


# ---------------------------------------------------------------------------
# Control: a short answer is complete, and costs one pass
# ---------------------------------------------------------------------------


def test_fewer_flagged_than_limit_returns_them_all_without_a_second_call() -> None:
    from vmware_aria.ops.anomaly import list_anomalies

    client = _FakeAria({"vm-a": 0.0, "vm-b": 4.0, "vm-c": 0.0, "vm-d": 1.0, "vm-e": 0.0})
    result = list_anomalies(client, limit=50)

    assert result["returned"] == 2, "both flagged VMs come back"
    assert [r["resource_name"] for r in result["items"]] == ["vm-b", "vm-d"]
    assert result["truncated"] is False, "nothing is left to fetch"
    assert result["hint"] is None
    assert len(client.listing_gets()) == 1
    assert len(client.stats_posts()) == 1


# ---------------------------------------------------------------------------
# Control: the bound on a full scan is stated, not hidden
# ---------------------------------------------------------------------------


def test_a_scan_stopped_by_the_cap_says_the_ranking_is_partial() -> None:
    """Ranking everything has a price; past the cap the answer says so."""
    from vmware_aria.ops.anomaly import _MAX_SCAN, list_anomalies

    oversized = _MAX_SCAN + 250
    client = _FakeAria({f"vm-{i:05d}": float(i % 3) for i in range(oversized)})
    result = list_anomalies(client, limit=10)

    assert result["scanned"] == _MAX_SCAN
    assert result["scan_complete"] is False
    assert result["vm_total"] == oversized
    assert result["truncated"] is True, (
        f"{oversized - _MAX_SCAN} objects went unranked — any could outrank "
        f"every row shown"
    )
    assert result["note"], "a partial ranking must say so in words, not by omission"


def test_the_cap_holds_when_the_appliance_ignores_the_page_size_we_ask_for() -> None:
    """The cap bounds objects examined, not pages walked.

    A page-boundary cap is only a cap while the appliance uses the page size it
    was given. It need not: this endpoint family has a recorded habit of
    accepting parameters and choosing otherwise. A build answering in 2000-row
    pages would sail 1000 objects past a 5000-object cap, and every one of them
    costs a metric read the caller did not agree to.
    """
    from vmware_aria.ops.anomaly import _MAX_SCAN, list_anomalies

    client = _FakeAria(
        {f"vm-{i:05d}": float(i % 3) for i in range(_MAX_SCAN + 3000)},
        server_page_size=2000,
    )
    result = list_anomalies(client, limit=10)

    assert result["scanned"] == _MAX_SCAN, (
        "the appliance chose 2000-row pages; the cap is on objects, not pages"
    )
    assert result["scan_complete"] is False


def test_limit_out_of_range_is_rejected_not_silently_rewritten() -> None:
    """Family rule (see _paging.validate_page_args): a page size is not a wish."""
    from vmware_aria.ops.anomaly import list_anomalies

    client = _estate(5)
    for bad in (0, -50, 10_000):
        with pytest.raises(ValueError, match="[Ll]imit"):
            list_anomalies(client, limit=bad)


def test_scoping_to_one_resource_still_costs_one_stats_call() -> None:
    """The single-resource path never lists the inventory at all."""
    from vmware_aria.ops.anomaly import list_anomalies

    client = _FakeAria({"res-1": 7.0})
    result = list_anomalies(client, resource_id="res-1")

    assert client.listing_gets() == []
    assert len(client.stats_posts()) == 1
    assert result["items"][0]["anomaly_count"] == 7.0

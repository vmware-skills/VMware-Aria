"""Regression: stats fan-out must be ONE bulk query, not a per-VM N+1.

Audit finding (current tree): ``list_anomalies`` (anomaly.py) and
``list_rightsizing_recommendations`` (capacity.py) listed VMs and then issued a
per-VM ``GET /resources/{id}/stats/latest`` in a loop — up to ~101 HTTP
round-trips for a full listing. Both now resolve every resource's latest stats
through a single bulk ``POST /resources/stats/query`` taking a ``resourceId``
array (the same endpoint get_resource_metrics uses).

These tests pin that behavior: a listing of many VMs must issue exactly ONE
bulk stats POST and ZERO per-resource ``stats/latest`` GETs.
"""
from __future__ import annotations


class _FakeClient:
    """Records get/post calls; returns canned listing + bulk-stats responses."""

    def __init__(self, resource_ids: list[str], stat_key_values: dict[str, float]) -> None:
        self._resource_ids = resource_ids
        self._stat_key_values = stat_key_values
        self.get_calls: list[tuple[str, dict | None]] = []
        self.post_calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params: dict | None = None, **_kwargs) -> dict:
        self.get_calls.append((path, params))
        if path == "/resources":
            return {
                "resourceList": [
                    {
                        "identifier": rid,
                        "resourceKey": {"name": f"name-{rid}", "resourceKindKey": "VirtualMachine"},
                    }
                    for rid in self._resource_ids
                ]
                # short page (no pageInfo / < pageSize) → list_resources stops
            }
        raise AssertionError(f"unexpected GET {path} — should use bulk stats POST")

    def post(self, path: str, json_data: dict | None = None, **_kwargs) -> dict:
        self.post_calls.append((path, json_data))
        # Bulk stats/query response shape: values[].{resourceId, stat-list.stat[]}
        ids = (json_data or {}).get("resourceId", [])
        keys = (json_data or {}).get("statKey", [])
        return {
            "values": [
                {
                    "resourceId": rid,
                    "stat-list": {
                        "stat": [
                            {"statKey": {"key": k}, "timestamps": [1000], "data": [self._stat_key_values[k]]}
                            for k in keys
                        ]
                    },
                }
                for rid in ids
            ]
        }

    def _stats_latest_gets(self) -> list[tuple[str, dict | None]]:
        return [c for c in self.get_calls if "stats/latest" in c[0]]

    def _bulk_stats_posts(self) -> list[tuple[str, dict | None]]:
        return [c for c in self.post_calls if c[0] == "/resources/stats/query"]


def test_list_anomalies_issues_one_bulk_stats_query_not_per_vm_loop() -> None:
    from vmware_aria.ops.anomaly import _TOTAL_ANOMALIES_STAT_KEY, list_anomalies

    client = _FakeClient(
        resource_ids=[f"vm-{i}" for i in range(50)],
        stat_key_values={_TOTAL_ANOMALIES_STAT_KEY: 3.0},
    )
    results = list_anomalies(client, limit=50)

    assert len(client._bulk_stats_posts()) == 1, (
        "list_anomalies must fan out via exactly ONE bulk POST /resources/stats/query"
    )
    assert client._stats_latest_gets() == [], (
        "no per-VM GET /resources/{id}/stats/latest — that is the N+1 being removed"
    )
    # The single bulk call carries the full resourceId array.
    _, body = client._bulk_stats_posts()[0]
    assert len(body["resourceId"]) == 50
    # Output shape preserved: every flagged VM has a non-zero anomaly_count.
    assert results and all(r["anomaly_count"] == 3.0 for r in results)
    assert set(results[0]) == {"resource_id", "resource_name", "anomaly_count", "metric_key"}


def test_list_rightsizing_issues_one_bulk_stats_query_not_per_vm_loop() -> None:
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _FakeClient(
        resource_ids=[f"vm-{i}" for i in range(50)],
        stat_key_values={
            "OnlineCapacityAnalytics|cpu|recommendedSize": 2.0,
            "OnlineCapacityAnalytics|mem|recommendedSize": 4096.0,
        },
    )
    results = list_rightsizing_recommendations(client, limit=50)

    assert len(client._bulk_stats_posts()) == 1, (
        "list_rightsizing_recommendations must fan out via exactly ONE bulk "
        "POST /resources/stats/query"
    )
    assert client._stats_latest_gets() == [], (
        "no per-VM GET /resources/{id}/stats/latest — that is the N+1 being removed"
    )
    _, body = client._bulk_stats_posts()[0]
    assert len(body["resourceId"]) == 50
    # Output shape preserved.
    assert results and len(results) == 50
    assert set(results[0]) == {"id", "name", "recommended_cpu", "recommended_memory"}
    assert results[0]["recommended_cpu"] == 2.0
    assert results[0]["recommended_memory"] == 4096.0

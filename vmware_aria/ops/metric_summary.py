"""Summaries of metric series: the few numbers a reader needs instead of every point.

``get_resource_metrics`` returns one ``{timestamp_ms, value}`` per collection
interval — 4 metrics over 72 hours was ~625 points each on Aria Operations
8.18.7 (2026-09-15), and finding when ``badge|health`` fell from 100 to 25 took
a local script. A summary gives n / min / max / avg / p95 / latest and the change
points: the timestamps where the value differed from the point before.

p95 is over the points as returned, so it describes the series at its own
resolution: with ``interval_type="DAYS"`` and ``rollup_type="AVG"`` it is the
p95 of daily averages, not of the 5-minute samples (Aria#22).

A continuously varying metric (CPU usage) changes at nearly every point, so
the change points are capped, keeping the most recent, and ``change_count``
still says how many there were.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

#: Change points returned per metric; the most recent are kept.
MAX_CHANGE_POINTS = 50

#: Fewest numeric points for a p95: with 20, the top 5% is one whole point.
MIN_POINTS_FOR_P95 = 20


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _p95(values: list[float]) -> tuple[float | None, str | None]:
    """Nearest-rank 95th percentile: an observed value, never an interpolation."""
    if len(values) < MIN_POINTS_FOR_P95:
        return None, (
            f"{len(values)} numeric points; p95 needs at least {MIN_POINTS_FOR_P95}. "
            "Widen the window or use a finer interval."
        )
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1], None


def summarize_series(points: list[dict]) -> dict:
    """Summarize one metric's points, in the order Aria returned them (oldest first).

    Args:
        points: ``{timestamp_ms, value}`` dicts as ``get_resource_metrics`` returns them.

    Returns:
        ``n`` (numeric points), ``min``, ``max``, ``avg``, ``latest``,
        ``p95`` (nearest rank; ``None`` with ``p95_unavailable`` saying why
        when there are fewer than ``MIN_POINTS_FOR_P95`` numeric points),
        ``first_timestamp_ms``, ``latest_timestamp_ms`` (all ``None`` when no
        point is numeric), ``change_count``, ``change_points`` (each
        ``{timestamp_ms, from, to}``, at most ``MAX_CHANGE_POINTS``, most recent
        kept), ``change_points_truncated``, and ``non_numeric_points`` — points
        whose value is not a finite number, counted rather than averaged.
    """
    numeric = [(p.get("timestamp_ms"), p.get("value")) for p in points if _is_number(p.get("value"))]
    values = [value for _, value in numeric]
    p95, p95_unavailable = _p95(values)
    changes = [
        {"timestamp_ms": ts, "from": previous, "to": value}
        for (_, previous), (ts, value) in pairwise(numeric)
        if value != previous
    ]
    return {
        "n": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": sum(values) / len(values) if values else None,
        "p95": p95,
        "p95_unavailable": p95_unavailable,
        "latest": values[-1] if values else None,
        "first_timestamp_ms": numeric[0][0] if numeric else None,
        "latest_timestamp_ms": numeric[-1][0] if numeric else None,
        "change_count": len(changes),
        "change_points": changes[-MAX_CHANGE_POINTS:],
        "change_points_truncated": len(changes) > MAX_CHANGE_POINTS,
        "non_numeric_points": len(points) - len(numeric),
    }

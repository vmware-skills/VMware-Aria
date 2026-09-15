"""Summaries of metric series: the few numbers a reader needs instead of every point.

``get_resource_metrics`` returns one ``{timestamp_ms, value}`` per collection
interval — 4 metrics over 72 hours was ~625 points each on Aria Operations
8.18.7 (2026-09-15), and finding when ``badge|health`` fell from 100 to 25 took
a local script. A summary gives n / min / max / avg / latest and the change
points: the timestamps where the value differed from the point before.

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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def summarize_series(points: list[dict]) -> dict:
    """Summarize one metric's points, in the order Aria returned them (oldest first).

    Args:
        points: ``{timestamp_ms, value}`` dicts as ``get_resource_metrics`` returns them.

    Returns:
        ``n`` (numeric points), ``min``, ``max``, ``avg``, ``latest``,
        ``first_timestamp_ms``, ``latest_timestamp_ms`` (all ``None`` when no
        point is numeric), ``change_count``, ``change_points`` (each
        ``{timestamp_ms, from, to}``, at most ``MAX_CHANGE_POINTS``, most recent
        kept), ``change_points_truncated``, and ``non_numeric_points`` — points
        whose value is not a finite number, counted rather than averaged.
    """
    numeric = [(p.get("timestamp_ms"), p.get("value")) for p in points if _is_number(p.get("value"))]
    values = [value for _, value in numeric]
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
        "latest": values[-1] if values else None,
        "first_timestamp_ms": numeric[0][0] if numeric else None,
        "latest_timestamp_ms": numeric[-1][0] if numeric else None,
        "change_count": len(changes),
        "change_points": changes[-MAX_CHANGE_POINTS:],
        "change_points_truncated": len(changes) > MAX_CHANGE_POINTS,
        "non_numeric_points": len(points) - len(numeric),
    }

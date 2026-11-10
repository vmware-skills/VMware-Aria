"""Metric resolution validation and response projection."""

from __future__ import annotations


from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize


if TYPE_CHECKING:
    pass


VALID_INTERVAL_TYPES = ("MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS")


VALID_ROLLUP_TYPES = ("AVG", "MAX", "MIN", "SUM", "COUNT", "LATEST")


def normalize_interval(interval_type: str, interval_quantity: int, rollup_type: str) -> tuple[str, int, str]:
    """Upper-case and check a metrics resolution; raise ValueError on anything else.

    Entry points call this before connecting, so a typo is a teaching error
    rather than a connection attempt.
    """
    interval_type = interval_type.upper()
    if interval_type not in VALID_INTERVAL_TYPES:
        raise ValueError(
            f"interval_type {interval_type!r} is not supported. Use one of "
            f"{', '.join(VALID_INTERVAL_TYPES)}, e.g. interval_type='DAYS' with "
            "interval_quantity=1 for daily values."
        )
    if interval_quantity < 1:
        raise ValueError(
            f"interval_quantity must be 1 or more (got {interval_quantity}): it is "
            "the number of interval_type units each point covers."
        )
    rollup_type = rollup_type.upper()
    if rollup_type not in VALID_ROLLUP_TYPES:
        raise ValueError(
            f"rollup_type {rollup_type!r} is not supported. Use one of "
            f"{', '.join(VALID_ROLLUP_TYPES)}: AVG for typical values, MAX for peaks."
        )
    return interval_type, interval_quantity, rollup_type


def _interval_report(data: dict, interval_type: str, interval_quantity: int, rollup_type: str) -> dict:
    """The resolution asked for, and the one(s) Aria echoed on each stat.

    Live 8.18.7 echoes ``rollUpType`` and ``intervalUnit`` per stat (seen with
    MINUTES/5 and HOURS/1). What it does with a span it does not support has
    not been observed, so the echo is reported rather than assumed to match.
    An echo missing a field or unreadable is "not said" (counted in
    ``series_without_echo``), never a difference.
    """
    requested = {"type": interval_type, "quantity": interval_quantity, "rollup": rollup_type}
    applied: list[dict] = []
    without_echo = 0
    for value_entry in data.get("values", []):
        stat_container = value_entry.get("stat-list") or value_entry.get("statList") or {}
        for stat in stat_container.get("stat", []):
            seen = _echoed_resolution(stat)
            if seen is None:
                without_echo += 1
            elif seen not in applied:
                applied.append(seen)
    report: dict[str, Any] = {**requested, "applied": applied or None, "series_without_echo": without_echo}
    if any(a != requested for a in applied):
        report["note"] = (
            "The resolution Aria reports in 'applied' differs from the one requested; "
            "read the points at the applied resolution."
        )
    return report


def _echoed_resolution(stat: dict) -> dict | None:
    """``{type, quantity, rollup}`` from one stat's echo, or None when incomplete."""
    unit = stat.get("intervalUnit")
    if not isinstance(unit, dict):
        return None
    echo_type, quantity, rollup = unit.get("intervalType"), unit.get("quantifier"), stat.get("rollUpType")
    if isinstance(quantity, str) and quantity.strip().isdigit():
        quantity = int(quantity)
    if not (isinstance(echo_type, str) and echo_type and isinstance(rollup, str) and rollup):
        return None
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        return None
    return {"type": sanitize(echo_type).upper(), "quantity": quantity, "rollup": sanitize(rollup).upper()}


def _parse_stat_series(data: dict) -> dict[str, list[dict]]:
    """Map statKey -> points from a stats/query body.

    Response nests stats under values[].stat-list.stat[] (hyphenated wire key;
    some renderings show statList — parse both defensively).
    """
    result: dict[str, list[dict]] = {}
    for value_entry in data.get("values", []):
        stat_container = value_entry.get("stat-list") or value_entry.get("statList") or {}
        for stat in stat_container.get("stat", []):
            key = sanitize(stat.get("statKey", {}).get("key", ""))
            timestamps = stat.get("timestamps", [])
            values = stat.get("data", [])
            result[key] = [{"timestamp_ms": ts, "value": v} for ts, v in zip(timestamps, values)]
    return result

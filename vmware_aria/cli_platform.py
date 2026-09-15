"""CLI: ``vmware-aria health node`` and ``vmware-aria health adapters``.

Kept out of ``cli.py`` (already over 1000 lines). Importing this module — which
``cli.py`` does at its end — registers both commands on ``health_app``. The
connection helper is looked up on ``vmware_aria.cli`` at call time so tests that
patch ``cli._get_connection`` reach these commands too.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.table import Table

from vmware_aria.cli import (
    ConfigOption,
    TargetOption,
    _friendly_errors,
    _json_output,
    _print_next_page,
    console,
    health_app,
)
from vmware_policy import audited

_PRESSURE_STYLE = {"NORMAL": "green", "ELEVATED": "yellow", "HIGH": "red", "UNKNOWN": "yellow"}


def _num(value: Any) -> str:
    return "-" if value is None else f"{value:.2f}"


def _add_metric_rows(table: Table, readings: dict, label_prefix: str = "") -> None:
    for key, m in readings.items():
        unit = f" ({m['unit']})" if m["unit"] else ""
        window = m["window"] or {}
        table.add_row(
            f"{label_prefix}{key}{unit}",
            _num(m["latest"]),
            _num(window.get("min")),
            _num(window.get("avg")),
            _num(window.get("max")),
        )


def _print_node(node: dict, window_hours: int) -> None:
    pressure = node["memory_pressure"]
    style = _PRESSURE_STYLE.get(pressure["level"], "yellow")
    console.print(f"\n[bold]{node['name']}[/bold]  status: {node['collection_status'] or '(none)'}")
    console.print(f"Memory pressure: [{style}]{pressure['level']}[/{style}] — {pressure['basis']}")
    table = Table(box=None, pad_edge=False, title=f"Latest and {window_hours}h window")
    for column in ("Metric", "Latest", "Min", "Avg", "Max"):
        table.add_column(column, justify="left" if column == "Metric" else "right")
    _add_metric_rows(table, node["memory"])
    _add_metric_rows(table, node["swap"])
    _add_metric_rows(table, node["heap"])
    _add_metric_rows(table, node["heap_components"], label_prefix="heap component ")
    console.print(table)
    if node["watchdog_restarts"] is None:
        console.print(f"Watchdog restarts: unknown — {node['watchdog_note']}")
    else:
        restarts = ", ".join(f"{svc}={_num(m['latest'])}" for svc, m in node["watchdog_restarts"].items())
        console.print(f"Watchdog restarts (latest): {restarts}")
    for item in node["missing"]:
        console.print(f"[yellow]Missing {item['key']}: {item['reason']} — {item['detail']}[/yellow]")


@health_app.command("node")
@_friendly_errors
@audited("get_aria_node_resources")
def health_node(
    hours: Annotated[int, typer.Option("--hours", help="Window for min/avg/max, 1-720 hours")] = 24,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full result as JSON")] = False,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Aria node memory, swap, heap and watchdog restarts, with a memory-pressure indicator."""
    from vmware_aria import cli
    from vmware_aria.ops.platform import get_aria_node_resources

    client, _ = cli._get_connection(target, config)
    data = get_aria_node_resources(client, window_hours=hours)
    if as_json:
        _json_output(data)
        return
    if data["nodes"] is None:
        console.print(f"[yellow]Nodes not read: {data['nodes_error']}[/yellow]")
        return
    for node in data["nodes"]:
        _print_node(node, hours)
    for key in ("units_error", "latest_error", "window_error", "watchdog_error"):
        if data[key]:
            console.print(f"[yellow]{key}: {data[key]}[/yellow]")


@health_app.command("adapters")
@_friendly_errors
@audited("list_adapters")
def health_adapters(
    kind: Annotated[str | None, typer.Option("--kind", help="Adapter kind key, e.g. VMWARE (case-insensitive)")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Page size, 1-500")] = 100,
    offset: Annotated[
        int, typer.Option("--offset", help="Rows to skip; the next-page offset is printed below the table")
    ] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full result as JSON")] = False,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Adapter instances, when each last collected, and whether that is stale."""
    from vmware_aria import cli
    from vmware_aria.ops.platform import list_adapters

    client, _ = cli._get_connection(target, config)
    result = list_adapters(client, adapter_kind=kind, limit=limit, offset=offset)
    if as_json:
        _json_output(result)
        return
    table = Table(title=f"Adapter instances (ages by {result['reference_clock']} clock)", show_lines=False)
    for column in ("Name", "Kind", "Last collected", "Interval", "Stale", "Resources", "Metrics"):
        table.add_column(column)
    stale_label = {True: "[red]yes[/red]", False: "[green]no[/green]", None: "[yellow]unknown[/yellow]"}
    for r in result["items"]:
        age = "-" if r["last_collected_age_s"] is None else f"{r['last_collected_age_s']}s ago"
        interval = "-" if r["monitoring_interval_min"] is None else f"{r['monitoring_interval_min']:g} min"
        table.add_row(
            r["name"],
            r["adapter_kind"],
            age,
            interval,
            stale_label[r["stale"]],
            str(r["resources_collected"]),
            str(r["metrics_collected"]),
        )
    console.print(table)
    for r in result["items"]:
        if r["message"]:
            console.print(f"{r['name']}: {r['message']}")
    if not result["items"]:
        console.print(f"No adapter instances match. Kinds present: {', '.join(result['adapter_kinds_present'])}")
    _print_next_page(result)

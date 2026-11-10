"""Cli Capacity callbacks using the existing facade runtime."""

from __future__ import annotations


from typing import Annotated

import typer


from vmware_aria.cli_options import TargetOption as TargetOption, ConfigOption as ConfigOption


def register_callbacks(runtime):
    @runtime.capacity_app.command("overview")
    @runtime._friendly_errors
    @runtime.audited("get_capacity_overview")
    def capacity_overview(
        cluster_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """Get capacity recommendations for a cluster."""
        from vmware_aria.ops.capacity import get_capacity_overview

        client, _ = runtime._get_connection(target, config)
        runtime._json_output(get_capacity_overview(client, cluster_id))

    @runtime.capacity_app.command("remaining")
    @runtime._friendly_errors
    @runtime.audited("get_remaining_capacity")
    def capacity_remaining(
        resource_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """Get remaining capacity metrics for a cluster or host."""
        from vmware_aria.ops.capacity import get_remaining_capacity

        client, _ = runtime._get_connection(target, config)
        runtime._json_output(get_remaining_capacity(client, resource_id))

    @runtime.capacity_app.command("time-remaining")
    @runtime._friendly_errors
    @runtime.audited("get_time_remaining")
    def capacity_time_remaining(
        resource_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """Predict when a cluster will exhaust its capacity dimensions."""
        from vmware_aria.ops.capacity import get_time_remaining

        client, _ = runtime._get_connection(target, config)
        runtime._json_output(get_time_remaining(client, resource_id))

    @runtime.capacity_app.command("rightsizing")
    @runtime._friendly_errors
    @runtime.audited("list_rightsizing_recommendations")
    def capacity_rightsizing(
        resource_id: Annotated[str | None, typer.Option("--resource-id", help="Scope to a specific VM")] = None,
        limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """List VM rightsizing recommendations."""
        from vmware_aria.ops.capacity import list_rightsizing_recommendations

        client, _ = runtime._get_connection(target, config)
        result = list_rightsizing_recommendations(client, resource_id=resource_id, limit=limit)
        items = result["items"]

        table = runtime.Table(title="Rightsizing (OnlineCapacityAnalytics recommendedSize)", show_lines=False)
        table.add_column("VM Name", style="bold")
        table.add_column("Power")
        # min_width: on a narrow terminal Rich shrinks columns with an ellipsis, and
        # a clipped "reclaima…" is the state this column exists to show.
        table.add_column("Status", min_width=len("none published"))
        table.add_column("vCPU now→rec")
        table.add_column("Mem GiB now→rec")
        table.add_column("Disk GB")
        table.add_column("Act.")

        # Three states, three renderings — a reclaimable VM and one with nothing
        # published must not both come out as an empty pair of cells, because
        # "no data" is exactly the misreading the sizing_status field exists to
        # prevent. A zero from the engine means reclaimable (KB 379521), never a
        # recommendation of zero.
        _status_style = {
            "recommendation": "[green]recommendation[/]",
            "reclaimable": "[yellow]reclaimable[/]",
            "none_published": "[dim]none published[/]",
        }

        def _cell(value: object) -> str:
            return "—" if value is None else str(value)

        def _gib(kb: object) -> str:
            return "—" if kb is None else f"{float(kb) / 1_048_576:.1f}"

        _arrow = {"oversized": "↓", "undersized": "↑", "right_sized": "="}

        def _change(now: str, rec: str, direction: object) -> str:
            # The raw recommendation is MHz/KB; these cells are converted so the
            # comparison against the current size is readable, and the direction is
            # marked rather than left for the reader to infer (legend under the table;
            # an arrow survives an 80-column terminal where a word gets clipped).
            return f"{now}→{rec} {_arrow.get(str(direction), '?')}" if direction else f"{now}→{rec}"

        def _disk(gb: object) -> str:
            return "—" if gb is None else f"{float(gb):.1f}"

        for r in items:
            status = r.get("sizing_status", "")
            power = "template" if r.get("is_template") else _cell(r.get("power_state")).replace("Powered ", "")
            table.add_row(
                (r["name"] or r["id"])[:40],
                power,
                _status_style.get(status, status),
                _change(_cell(r.get("current_vcpus")), _cell(r.get("recommended_vcpus")), r.get("cpu_direction")),
                _change(_gib(r.get("current_memory_kb")), _gib(r.get("recommended_memory")), r.get("memory_direction")),
                _disk(r.get("recommended_diskspace")),
                "[green]yes[/]" if r.get("actionable") else "[dim]no[/]",
            )

        runtime.console.print(table)
        runtime.console.print(
            "[dim]↓ oversized  ↑ undersized  = right-sized  (vs current config; GiB rounded). "
            "Act. = powered on, not a template, and off its recommendation.[/]"
        )
        for r in items:
            for caveat in r.get("caveats") or []:
                runtime.console.print(f"[dim]• {(r['name'] or r['id'])[:40]}: {caveat}[/]")
        if result.get("properties_note"):
            # Without it a failed property read shows only per-row caveats, never why.
            runtime.console.print(f"[yellow]{result['properties_note']}[/]")
        if result.get("history_note"):
            runtime.console.print(f"[yellow]{result['history_note']}[/]")

    return (
        capacity_overview,
        capacity_remaining,
        capacity_time_remaining,
        capacity_rightsizing,
    )

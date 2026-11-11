"""Cli Resources callbacks using the existing facade runtime."""

from __future__ import annotations


from typing import Annotated

import typer


import vmware_aria.cli_options as _compat_exports_0
TargetOption = _compat_exports_0.TargetOption
ConfigOption = _compat_exports_0.ConfigOption


def register_callbacks(runtime):
    @runtime.resource_app.command("list")
    @runtime._friendly_errors
    @runtime.audited("list_resources")
    def resource_list(
        kind: Annotated[
            str, typer.Option("--kind", "-k", help="Resource kind, or 'all' for every kind")
        ] = "VirtualMachine",
        limit: Annotated[int, typer.Option("--limit", "-n", help="Max results (paginated)")] = 50,
        name_filter: Annotated[str | None, typer.Option("--name", help="Filter by name substring")] = None,
        collection_status: Annotated[
            str | None,
            typer.Option(
                "--collection-status",
                help="Keep objects with this data-collection status, e.g. NO_DATA_RECEIVING",
            ),
        ] = None,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """List resources by kind (VM, HostSystem, ClusterComputeResource, etc.)."""
        from vmware_aria.ops.resources import list_resources

        client, _ = runtime._get_connection(target, config)
        all_kinds = kind.strip().lower() == "all"
        result = list_resources(
            client,
            resource_kind=None if all_kinds else kind,
            limit=limit,
            name_filter=name_filter,
            collection_status=collection_status,
        )

        table = runtime.Table(title=f"Resources ({kind})", show_lines=False)
        table.add_column("Name", style="bold")
        if all_kinds:
            table.add_column("Kind")
        table.add_column("ID")
        table.add_column("Health")
        # "Aria state" is Aria's lifecycle state (STARTED for a powered-off VM too);
        # "Collection" is whether data is arriving — the column an "Objects are not
        # receiving data" alert is about.
        table.add_column("Aria state")
        table.add_column("Collection")

        for r in result["items"]:
            health = (
                f"{r['health_color']} ({r['health_score']})" if r.get("health_score") is not None else r["health_color"]
            )
            cells = [r["name"], *([r["kind"]] if all_kinds else []), r["id"][:36], health]
            table.add_row(*cells, r["aria_state"], r["collection_status"] or "—")

        runtime.console.print(table)
        if result.get("note"):
            runtime.console.print(f"[yellow]{result['note']}[/]")

    @runtime.resource_app.command("get")
    @runtime._friendly_errors
    @runtime.audited("get_resource")
    def resource_get(
        resource_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """Get full details for a specific resource by ID."""
        from vmware_aria.ops.resources import get_resource

        client, _ = runtime._get_connection(target, config)
        runtime._json_output(get_resource(client, resource_id))

    @runtime.resource_app.command("metrics")
    @runtime._friendly_errors
    @runtime.audited("get_resource_metrics")
    def resource_metrics(
        resource_id: str,
        metrics: Annotated[
            str, typer.Option("--metrics", "-m", help="Comma-separated metric keys")
        ] = "cpu|usage_average,mem|usage_average",
        hours: Annotated[int, typer.Option("--hours", help="History window in hours")] = 1,
        summary: Annotated[
            bool,
            typer.Option(
                "--summary",
                help="Per metric n/min/max/avg/p95/latest and the times the value changed, instead of every point",
            ),
        ] = False,
        interval_type: Annotated[
            str,
            typer.Option("--interval-type", help="Span each point covers: MINUTES, HOURS, DAYS, WEEKS, MONTHS"),
        ] = "MINUTES",
        interval_quantity: Annotated[
            int, typer.Option("--interval-quantity", help="Units of --interval-type per point (DAYS + 1 = daily)")
        ] = 5,
        rollup: Annotated[
            str, typer.Option("--rollup", help="Value per point: AVG, MAX, MIN, SUM, COUNT, LATEST")
        ] = "AVG",
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """Fetch time-series metrics for a resource (raw points, or --summary)."""
        import time as _time

        from vmware_aria.ops.resources import get_resource_metrics, normalize_interval

        interval_type, interval_quantity, rollup = normalize_interval(interval_type, interval_quantity, rollup)
        client, _ = runtime._get_connection(target, config)
        metric_keys = [k.strip() for k in metrics.split(",") if k.strip()]
        end_ms = int(_time.time() * 1000)
        begin_ms = end_ms - (hours * 3_600_000)
        result = get_resource_metrics(
            client,
            resource_id,
            metric_keys,
            begin_time_ms=begin_ms,
            end_time_ms=end_ms,
            rollup_type=rollup,
            interval_type=interval_type,
            interval_quantity=interval_quantity,
            summary=summary,
        )
        runtime._json_output(result)

    @runtime.resource_app.command("health")
    @runtime._friendly_errors
    @runtime.audited("get_resource_health")
    def resource_health(
        resource_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """Get health badge scores for a resource."""
        from vmware_aria.ops.resources import get_resource_health

        client, _ = runtime._get_connection(target, config)
        runtime._json_output(get_resource_health(client, resource_id))

    @runtime.resource_app.command("top")
    @runtime._friendly_errors
    @runtime.audited("get_top_consumers")
    def resource_top(
        metric: Annotated[str, typer.Option("--metric", help="Metric key to rank by")] = "cpu|usage_average",
        kind: Annotated[str, typer.Option("--kind", "-k", help="Resource kind")] = "VirtualMachine",
        top_n: Annotated[int, typer.Option("--top", "-n", help="Number of top consumers")] = 10,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """List top resource consumers by a specific metric."""
        from vmware_aria.ops.resources import get_top_consumers

        client, _ = runtime._get_connection(target, config)
        result = get_top_consumers(client, metric_key=metric, resource_kind=kind, top_n=top_n)
        items = result["items"]

        table = runtime.Table(title=f"Top {top_n} by {metric}", show_lines=False)
        table.add_column("Rank", justify="right")
        table.add_column("Name", style="bold")
        table.add_column("Value")
        table.add_column("Unit")

        for i, r in enumerate(items, 1):
            table.add_row(str(i), r["name"], str(r.get("value", "")), r.get("unit", ""))

        runtime.console.print(table)
        if result["hint"]:
            runtime.console.print(f"[yellow]{result['hint']}[/yellow]")

    return (
        resource_list,
        resource_get,
        resource_metrics,
        resource_health,
        resource_top,
    )

"""Cli Alert Display callbacks using the existing facade runtime."""

from __future__ import annotations


def register_callbacks(runtime):
    def _alert_started(alert: dict) -> str:
        iso = alert.get("start_time_utc")
        return f"{iso[:10]} {iso[11:16]}" if iso else "—"

    def _alert_resource(alert: dict) -> str:
        # Alert model has no resourceName; list_alerts resolves it in one batched
        # lookup. An unresolved name prints as "?" — unknown, not "no resource".
        return alert.get("resource_name") or ("?" if alert["resource_id"] else "")

    def _print_alert_table(items: list[dict]) -> None:
        table = runtime.Table(title="Aria Operations Alerts", show_lines=False)
        table.add_column("ID", no_wrap=True, min_width=36)
        table.add_column("Name", style="bold")
        table.add_column("Criticality")
        table.add_column("Status")
        table.add_column("Started (UTC)", no_wrap=True)
        table.add_column("Resource")
        table.add_column("Resource ID", no_wrap=True, min_width=36)
        for a in items:
            table.add_row(
                a["id"],
                a["name"][:60],
                a["criticality"],
                a["status"],
                runtime._alert_started(a),
                runtime._alert_resource(a)[:40],
                a["resource_id"],
            )
        runtime.console.print(table)

    def _print_alert_records(items: list[dict]) -> None:
        """One block per alert; each UUID is a single word, so wrapping never splits it."""
        from rich.markup import escape

        runtime.console.print("[bold]Aria Operations Alerts[/bold]")
        for a in items:
            runtime.console.print(
                f"{a['id']}  {escape(a['criticality'])}  {escape(a['status'])}  Started {runtime._alert_started(a)} UTC",
                highlight=False,
            )
            runtime.console.print(f"  {escape(a['name'])}", highlight=False)
            if a["resource_id"]:
                runtime.console.print(
                    f"  on {escape(runtime._alert_resource(a))} ({a['resource_id']})", highlight=False
                )

    return (
        _alert_started,
        _alert_resource,
        _print_alert_table,
        _print_alert_records,
    )

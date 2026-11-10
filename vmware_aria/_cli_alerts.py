"""Cli Alerts callbacks using the existing facade runtime."""

from __future__ import annotations


from typing import Annotated

import typer


from vmware_aria.cli_options import TargetOption as TargetOption, ConfigOption as ConfigOption


def register_callbacks(runtime):
    @runtime.alert_app.command("list")
    @runtime._friendly_errors
    @runtime.audited("list_alerts")
    def alert_list(
        active_only: Annotated[bool, typer.Option("--active/--all", help="Active alerts only")] = True,
        criticality: Annotated[str | None, typer.Option("--criticality", help="Filter by criticality")] = None,
        limit: Annotated[int, typer.Option("--limit", "-n", help="Page size, 1-500")] = 50,
        offset: Annotated[
            int, typer.Option("--offset", help="Rows to skip; the next-page offset is printed below the table")
        ] = 0,
        as_json: Annotated[
            bool, typer.Option("--json", help="Print the result envelope as JSON instead of a table")
        ] = False,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """List alerts, optionally filtered by criticality. IDs are printed whole."""
        from vmware_aria.ops.alerts import list_alerts

        client, _ = runtime._get_connection(target, config)
        _result = list_alerts(client, active_only=active_only, criticality=criticality, limit=limit, offset=offset)
        if as_json:
            runtime._json_output(_result)
            return

        # A table narrower than this cannot hold two 36-character IDs: rich shrinks
        # every column, IDs included, to "ba793832-5…", which cannot be pasted into
        # `alert get` (2026-09-15). Narrower terminals get one record per alert.
        if runtime.console.width >= runtime._ALERT_TABLE_MIN_WIDTH:
            runtime._print_alert_table(_result["items"])
        else:
            runtime._print_alert_records(_result["items"])
        if _result.get("resource_names_note"):
            runtime.console.print(f"[yellow]{_result['resource_names_note']}[/yellow]")
        runtime._print_next_page(_result)

    @runtime.alert_app.command("get")
    @runtime._friendly_errors
    @runtime.audited("get_alert")
    def alert_get(
        alert_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """Get full details for a specific alert."""
        from vmware_aria.ops.alerts import get_alert

        client, _ = runtime._get_connection(target, config)
        runtime._json_output(get_alert(client, alert_id))

    @runtime.alert_app.command("acknowledge")
    @runtime._friendly_errors
    @runtime.guarded("acknowledge_alert", risk_level="medium")
    def alert_acknowledge(
        alert_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
    ) -> None:
        """Acknowledge an active alert."""
        from vmware_aria.ops.alerts import acknowledge_alert

        if not yes:
            runtime.typer.confirm(f"Acknowledge alert {alert_id}?", abort=True)

        client, cfg = runtime._get_connection(target, config)
        target_name = target or cfg.default_target or "default"
        result = acknowledge_alert(client, alert_id, audit_logger=runtime._audit, target_name=target_name)
        runtime._json_output(result)

    @runtime.alert_app.command("cancel")
    @runtime._friendly_errors
    @runtime.guarded("cancel_alert", risk_level="medium")
    def alert_cancel(
        alert_id: str,
        target: TargetOption = None,
        config: ConfigOption = None,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
    ) -> None:
        """Cancel (dismiss) an active alert."""
        from vmware_aria.ops.alerts import cancel_alert

        # Two prompts when a human is driving; `--yes` for a caller who has already
        # decided. The prompts defend the mistyped id, not a determined caller —
        # this repo's own docs say so, since `yes |` satisfies them anyway. Removing
        # the flag would have bought no safety and broken every script using it.
        if not yes and not runtime._confirm_destructive("alert", alert_id, verb="cancel"):
            runtime.console.print("[yellow]Aborted.[/yellow]")
            raise runtime.typer.Exit(1)

        client, cfg = runtime._get_connection(target, config)
        target_name = target or cfg.default_target or "default"
        result = cancel_alert(client, alert_id, audit_logger=runtime._audit, target_name=target_name)
        runtime._json_output(result)

    @runtime.alert_app.command("definitions")
    @runtime._friendly_errors
    @runtime.audited("list_alert_definitions")
    def alert_definitions(
        name_filter: Annotated[str | None, typer.Option("--name", help="Filter by name substring")] = None,
        limit: Annotated[int, typer.Option("--limit", "-n", help="Page size, 1-500")] = 50,
        offset: Annotated[
            int, typer.Option("--offset", help="Rows to skip; the next-page offset is printed below the table")
        ] = 0,
        target: TargetOption = None,
        config: ConfigOption = None,
    ) -> None:
        """List alert definitions (templates that generate alerts)."""
        from vmware_aria.ops.alerts import list_alert_definitions

        client, _ = runtime._get_connection(target, config)
        _result = list_alert_definitions(client, name_filter=name_filter, limit=limit, offset=offset)
        items = _result["items"]

        table = runtime.Table(title="Alert Definitions", show_lines=False)
        table.add_column("Name", style="bold")
        table.add_column("Criticality")
        table.add_column("Resource Kind")
        table.add_column("Impact")

        # AlertDefinition has no top-level active field; criticality is the max
        # severity across the definition's states[].
        for d in items:
            table.add_row(d["name"][:60], d["criticality"], d["resource_kind"], d["impact"])

        runtime.console.print(table)
        runtime._print_next_page(_result)

    return (
        alert_list,
        alert_get,
        alert_acknowledge,
        alert_cancel,
        alert_definitions,
    )

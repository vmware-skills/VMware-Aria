"""CLI entry point for VMware Aria Operations.

Provides commands for resource monitoring, alert management, capacity planning,
anomaly detection, and platform health checks.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vmware_aria.notify.audit import AuditLogger

_audit = AuditLogger()

app = typer.Typer(
    name="vmware-aria",
    help="VMware Aria Operations monitoring, alerting, and capacity planning.",
    no_args_is_help=True,
)
console = Console()

# ─── Sub-command groups ──────────────────────────────────────────────────────

resource_app = typer.Typer(help="Resource queries: list, get, metrics, health, top consumers.")
alert_app = typer.Typer(help="Alert management: list, get, acknowledge, cancel, definitions.")
capacity_app = typer.Typer(help="Capacity planning: overview, remaining, time-remaining, rightsizing.")
anomaly_app = typer.Typer(help="Anomaly detection: list anomalies, risk badge.")
health_app = typer.Typer(help="Platform health: Aria node status, collector groups.")
report_app = typer.Typer(help="Report management: list definitions, generate, list, get, delete.")

app.add_typer(resource_app, name="resource")
app.add_typer(alert_app, name="alert")
app.add_typer(capacity_app, name="capacity")
app.add_typer(anomaly_app, name="anomaly")
app.add_typer(health_app, name="health")
app.add_typer(report_app, name="report")


@app.command("mcp")
def mcp_cmd() -> None:
    """Start the MCP server (stdio transport).

    Single-command entry point for MCP clients:
        vmware-aria mcp

    Equivalent to the legacy `vmware-aria-mcp` console script.
    """
    import sys

    if sys.version_info < (3, 10):
        msg = (
            f"ERROR: vmware-aria MCP server requires Python >= 3.10 "
            f"(got {sys.version_info.major}.{sys.version_info.minor}).\n"
            f"Interpreter: {sys.executable}\n"
            "Fix: uv python install 3.12 && "
            "uv tool install --python 3.12 --force vmware-aria"
        )
        typer.echo(msg, err=True)
        raise typer.Exit(2)

    from vmware_aria.mcp_server.server import main as _mcp_main

    _mcp_main()


# ─── Type aliases ────────────────────────────────────────────────────────────

TargetOption = Annotated[str | None, typer.Option("--target", "-t", help="Target name from config")]
ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")]


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _friendly_errors(fn):
    """Print expected operational errors as one red line instead of a traceback.

    AriaApiError already carries a teaching message (status + path + fix);
    config problems surface as FileNotFoundError/KeyError/OSError. Anything
    else is a real bug and keeps its traceback for debugging.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from vmware_aria.connection import AriaApiError

        try:
            return fn(*args, **kwargs)
        except (AriaApiError, FileNotFoundError, KeyError, OSError) as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1) from exc
        finally:
            # Each command builds a fresh ConnectionManager via
            # _get_connection; release its auth token(s) so per-invocation
            # tokens don't accumulate server-side on Aria.
            _close_open_connections()

    return wrapper


# ConnectionManagers created during the current command, closed by the
# _friendly_errors wrapper once the command returns. The CLI is single-shot
# (one command per process), so a module-level list is safe here.
_OPEN_MANAGERS: list = []


def _close_open_connections() -> None:
    """Release tokens for every manager opened during this command."""
    while _OPEN_MANAGERS:
        mgr = _OPEN_MANAGERS.pop()
        try:
            mgr.disconnect_all()
        except Exception:  # cleanup must never mask the command's own result
            pass


def _get_connection(target: str | None, config_path: Path | None = None):
    """Return (AriaClient, AppConfig).

    The created ConnectionManager is registered for cleanup so its auth
    token is released when the command finishes (see _friendly_errors).
    """
    from vmware_aria.config import load_config
    from vmware_aria.connection import ConnectionManager

    cfg = load_config(config_path)
    mgr = ConnectionManager(cfg)
    _OPEN_MANAGERS.append(mgr)
    name = target or cfg.default_target
    return mgr.connect(name), cfg


def _json_output(data: object) -> None:
    """Print data as formatted JSON."""
    console.print_json(json.dumps(data, indent=2, default=str))


# ─── Init ────────────────────────────────────────────────────────────────────


@app.command()
def init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config without asking")] = False,
    skip_test: Annotated[bool, typer.Option("--skip-test", help="Skip the post-setup connection test")] = False,
) -> None:
    """Interactive first-run setup: write config.yaml + .env, then offer a test.

    Prompts for one Aria Operations target (host, username, auth source, port,
    TLS verification) and its password. The password is stored grep-safe
    (``b64:``) in ~/.vmware-aria/.env at mode 0600 — never plaintext on disk.
    """
    from vmware_aria.init_wizard import run_init

    raise typer.Exit(run_init(force=force, skip_test=skip_test))


# ─── Doctor ──────────────────────────────────────────────────────────────────


@app.command()
@_friendly_errors
def doctor(
    skip_auth: Annotated[bool, typer.Option("--skip-auth", help="Skip authentication check")] = False,
    config: ConfigOption = None,
) -> None:
    """Run pre-flight diagnostics for Aria Operations connectivity."""
    from vmware_aria.doctor import run_doctor

    ok = run_doctor(config_path=config, skip_auth=skip_auth)
    raise typer.Exit(0 if ok else 1)


# ═══════════════════════════════════════════════════════════════════════════════
# RESOURCE commands
# ═══════════════════════════════════════════════════════════════════════════════


@resource_app.command("list")
@_friendly_errors
def resource_list(
    kind: Annotated[str, typer.Option("--kind", "-k", help="Resource kind")] = "VirtualMachine",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results (paginated)")] = 50,
    name_filter: Annotated[str | None, typer.Option("--name", help="Filter by name substring")] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List resources by kind (VM, HostSystem, ClusterComputeResource, etc.)."""
    from vmware_aria.ops.resources import list_resources

    client, _ = _get_connection(target, config)
    items = list_resources(client, resource_kind=kind, limit=limit, name_filter=name_filter)["items"]

    table = Table(title=f"Resources ({kind})", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Health")
    table.add_column("Status")

    for r in items:
        health = (
            f"{r['health_color']} ({r['health_score']})" if r.get("health_score") is not None else r["health_color"]
        )
        table.add_row(r["name"], r["id"][:36], health, r["status"])

    console.print(table)


@resource_app.command("get")
@_friendly_errors
def resource_get(
    resource_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Get full details for a specific resource by ID."""
    from vmware_aria.ops.resources import get_resource

    client, _ = _get_connection(target, config)
    _json_output(get_resource(client, resource_id))


@resource_app.command("metrics")
@_friendly_errors
def resource_metrics(
    resource_id: str,
    metrics: Annotated[
        str, typer.Option("--metrics", "-m", help="Comma-separated metric keys")
    ] = "cpu|usage_average,mem|usage_average",
    hours: Annotated[int, typer.Option("--hours", help="History window in hours")] = 1,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Fetch time-series metrics for a resource."""
    import time as _time

    from vmware_aria.ops.resources import get_resource_metrics

    client, _ = _get_connection(target, config)
    metric_keys = [k.strip() for k in metrics.split(",") if k.strip()]
    end_ms = int(_time.time() * 1000)
    begin_ms = end_ms - (hours * 3_600_000)
    result = get_resource_metrics(client, resource_id, metric_keys, begin_time_ms=begin_ms, end_time_ms=end_ms)
    _json_output(result)


@resource_app.command("health")
@_friendly_errors
def resource_health(
    resource_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Get health badge scores for a resource."""
    from vmware_aria.ops.resources import get_resource_health

    client, _ = _get_connection(target, config)
    _json_output(get_resource_health(client, resource_id))


@resource_app.command("top")
@_friendly_errors
def resource_top(
    metric: Annotated[str, typer.Option("--metric", help="Metric key to rank by")] = "cpu|usage_average",
    kind: Annotated[str, typer.Option("--kind", "-k", help="Resource kind")] = "VirtualMachine",
    top_n: Annotated[int, typer.Option("--top", "-n", help="Number of top consumers")] = 10,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List top resource consumers by a specific metric."""
    from vmware_aria.ops.resources import get_top_consumers

    client, _ = _get_connection(target, config)
    items = get_top_consumers(client, metric_key=metric, resource_kind=kind, top_n=top_n)["items"]

    table = Table(title=f"Top {top_n} by {metric}", show_lines=False)
    table.add_column("Rank", justify="right")
    table.add_column("Name", style="bold")
    table.add_column("Value")
    table.add_column("Unit")

    for i, r in enumerate(items, 1):
        table.add_row(str(i), r["name"], str(r.get("value", "")), r.get("unit", ""))

    console.print(table)


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT commands
# ═══════════════════════════════════════════════════════════════════════════════


@alert_app.command("list")
@_friendly_errors
def alert_list(
    active_only: Annotated[bool, typer.Option("--active/--all", help="Active alerts only")] = True,
    criticality: Annotated[str | None, typer.Option("--criticality", help="Filter by criticality")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List alerts, optionally filtered by criticality."""
    from vmware_aria.ops.alerts import list_alerts

    client, _ = _get_connection(target, config)
    items = list_alerts(client, active_only=active_only, criticality=criticality, limit=limit)["items"]

    table = Table(title="Aria Operations Alerts", show_lines=False)
    table.add_column("ID")
    table.add_column("Name", style="bold")
    table.add_column("Criticality")
    table.add_column("Status")
    table.add_column("Resource ID")

    # Alert model has no resourceName — show resourceId (resolve names via
    # `vmware-aria resource get <id>`).
    for a in items:
        table.add_row(a["id"][:36], a["name"][:60], a["criticality"], a["status"], a["resource_id"][:36])

    console.print(table)


@alert_app.command("get")
@_friendly_errors
def alert_get(
    alert_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Get full details for a specific alert."""
    from vmware_aria.ops.alerts import get_alert

    client, _ = _get_connection(target, config)
    _json_output(get_alert(client, alert_id))


@alert_app.command("acknowledge")
@_friendly_errors
def alert_acknowledge(
    alert_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
) -> None:
    """Acknowledge an active alert."""
    from vmware_aria.ops.alerts import acknowledge_alert

    if not yes:
        typer.confirm(f"Acknowledge alert {alert_id}?", abort=True)

    client, cfg = _get_connection(target, config)
    target_name = target or cfg.default_target or "default"
    result = acknowledge_alert(client, alert_id, audit_logger=_audit, target_name=target_name)
    _json_output(result)


@alert_app.command("cancel")
@_friendly_errors
def alert_cancel(
    alert_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
) -> None:
    """Cancel (dismiss) an active alert."""
    from vmware_aria.ops.alerts import cancel_alert

    if not yes:
        typer.confirm(f"Cancel alert {alert_id}? This will dismiss it.", abort=True)

    client, cfg = _get_connection(target, config)
    target_name = target or cfg.default_target or "default"
    result = cancel_alert(client, alert_id, audit_logger=_audit, target_name=target_name)
    _json_output(result)


@alert_app.command("definitions")
@_friendly_errors
def alert_definitions(
    name_filter: Annotated[str | None, typer.Option("--name", help="Filter by name substring")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List alert definitions (templates that generate alerts)."""
    from vmware_aria.ops.alerts import list_alert_definitions

    client, _ = _get_connection(target, config)
    items = list_alert_definitions(client, name_filter=name_filter, limit=limit)["items"]

    table = Table(title="Alert Definitions", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Criticality")
    table.add_column("Resource Kind")
    table.add_column("Impact")

    # AlertDefinition has no top-level active field; criticality is the max
    # severity across the definition's states[].
    for d in items:
        table.add_row(d["name"][:60], d["criticality"], d["resource_kind"], d["impact"])

    console.print(table)


# ═══════════════════════════════════════════════════════════════════════════════
# CAPACITY commands
# ═══════════════════════════════════════════════════════════════════════════════


@capacity_app.command("overview")
@_friendly_errors
def capacity_overview(
    cluster_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Get capacity recommendations for a cluster."""
    from vmware_aria.ops.capacity import get_capacity_overview

    client, _ = _get_connection(target, config)
    _json_output(get_capacity_overview(client, cluster_id))


@capacity_app.command("remaining")
@_friendly_errors
def capacity_remaining(
    resource_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Get remaining capacity metrics for a cluster or host."""
    from vmware_aria.ops.capacity import get_remaining_capacity

    client, _ = _get_connection(target, config)
    _json_output(get_remaining_capacity(client, resource_id))


@capacity_app.command("time-remaining")
@_friendly_errors
def capacity_time_remaining(
    resource_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Predict when a cluster will exhaust its capacity dimensions."""
    from vmware_aria.ops.capacity import get_time_remaining

    client, _ = _get_connection(target, config)
    _json_output(get_time_remaining(client, resource_id))


@capacity_app.command("rightsizing")
@_friendly_errors
def capacity_rightsizing(
    resource_id: Annotated[str | None, typer.Option("--resource-id", help="Scope to a specific VM")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List VM rightsizing recommendations."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client, _ = _get_connection(target, config)
    items = list_rightsizing_recommendations(client, resource_id=resource_id, limit=limit)["items"]

    table = Table(title="Rightsizing (OnlineCapacityAnalytics recommendedSize)", show_lines=False)
    table.add_column("VM Name", style="bold")
    table.add_column("Recommended CPU")
    table.add_column("Recommended Mem")

    for r in items:
        table.add_row(
            (r["name"] or r["id"])[:40],
            str(r.get("recommended_cpu", "")),
            str(r.get("recommended_memory", "")),
        )

    console.print(table)


# ═══════════════════════════════════════════════════════════════════════════════
# ANOMALY commands
# ═══════════════════════════════════════════════════════════════════════════════


@anomaly_app.command("list")
@_friendly_errors
def anomaly_list(
    resource_id: Annotated[str | None, typer.Option("--resource-id", help="Scope to a resource")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List anomalies detected by Aria Operations."""
    from vmware_aria.ops.anomaly import list_anomalies

    client, _ = _get_connection(target, config)
    items = list_anomalies(client, resource_id=resource_id, limit=limit)["items"]

    table = Table(title="Anomaly Counts (System Attributes|total_alarms)", show_lines=False)
    table.add_column("Resource", style="bold")
    table.add_column("Anomaly Count")

    for a in items:
        table.add_row(
            (a["resource_name"] or a["resource_id"])[:40],
            str(a.get("anomaly_count", "")),
        )

    console.print(table)


@anomaly_app.command("risk")
@_friendly_errors
def anomaly_risk(
    resource_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Get risk badge score for a resource."""
    from vmware_aria.ops.anomaly import get_resource_riskbadge

    client, _ = _get_connection(target, config)
    _json_output(get_resource_riskbadge(client, resource_id))


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH commands
# ═══════════════════════════════════════════════════════════════════════════════


@health_app.command("status")
@_friendly_errors
def health_status(
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Check Aria Operations platform health (all internal services)."""
    from vmware_aria.ops.health import get_aria_health

    client, _ = _get_connection(target, config)
    data = get_aria_health(client)

    overall = "[green]ONLINE[/green]" if data["healthy"] else f"[red]{data['overall_status'] or 'OFFLINE'}[/red]"
    console.print(f"\nAria Operations Platform: {overall}")
    if data.get("details"):
        console.print(f"Details: {data['details']}")


@health_app.command("collectors")
@_friendly_errors
def health_collectors(
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List collector groups and member collector status."""
    from vmware_aria.ops.health import list_collector_groups

    client, _ = _get_connection(target, config)
    groups = list_collector_groups(client)["items"]

    # Collector model has no collectorType/hostname; state is UP/DOWN and
    # `local` marks the built-in collector on the Aria node.
    for g in groups:
        console.print(f"\n[bold]{g['name']}[/bold] (id: {g['id']}) — {g['collector_count']} collector(s)")
        table = Table(show_header=True, show_lines=False)
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("State")
        table.add_column("Local")
        for c in g["collectors"]:
            state_style = "green" if c["state"] == "UP" else "red"
            table.add_row(c["id"], c["name"], f"[{state_style}]{c['state']}[/{state_style}]", str(c["local"]))
        console.print(table)


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT commands
# ═══════════════════════════════════════════════════════════════════════════════


@report_app.command("definitions")
@_friendly_errors
def report_definitions(
    name_filter: Annotated[str | None, typer.Option("--name", help="Filter by name substring")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List available report definition templates."""
    from vmware_aria.ops.reports import list_report_definitions

    client, _ = _get_connection(target, config)
    items = list_report_definitions(client, name_filter=name_filter, limit=limit)["items"]

    table = Table(title="Report Definitions", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Subject Type")
    table.add_column("Owner")

    for d in items:
        table.add_row(d["name"][:60], d["id"][:36], d["subject_type"], d["owner"])

    console.print(table)


@report_app.command("generate")
@_friendly_errors
def report_generate(
    definition_id: str,
    resource_ids: Annotated[str | None, typer.Option("--resources", help="Comma-separated resource UUIDs")] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Trigger report generation from a definition template."""
    from vmware_aria.ops.reports import generate_report

    client, cfg = _get_connection(target, config)
    target_name = target or cfg.default_target or "default"
    rids = [r.strip() for r in resource_ids.split(",")] if resource_ids else None
    result = generate_report(
        client, definition_id=definition_id, resource_ids=rids, audit_logger=_audit, target_name=target_name
    )
    console.print(f"Report queued: [bold]{result['report_id']}[/bold] (status: {result['status']})")
    console.print("Poll with: vmware-aria report get <report_id>")


@report_app.command("list")
@_friendly_errors
def report_list(
    definition_id: Annotated[str | None, typer.Option("--definition-id", help="Filter by definition UUID")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List generated reports."""
    from vmware_aria.ops.reports import list_reports

    client, _ = _get_connection(target, config)
    items = list_reports(client, definition_id=definition_id, limit=limit)["items"]

    table = Table(title="Generated Reports", show_lines=False)
    table.add_column("ID")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Owner")

    for r in items:
        status_style = "green" if r["status"] == "COMPLETED" else "yellow" if r["status"] == "RUNNING" else "white"
        table.add_row(r["id"][:36], r["name"][:50], f"[{status_style}]{r['status']}[/{status_style}]", r["owner"])

    console.print(table)


@report_app.command("get")
@_friendly_errors
def report_get(
    report_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Get status and download URL for a generated report."""
    from vmware_aria.ops.reports import get_report

    client, _ = _get_connection(target, config)
    data = get_report(client, report_id)
    status_style = "green" if data["status"] == "COMPLETED" else "yellow"
    console.print(f"Report [bold]{data['id']}[/bold]: [{status_style}]{data['status']}[/{status_style}]")
    if data["status"] == "COMPLETED":
        console.print(f"PDF: {data['download_url']}")
        console.print(f"CSV: {data['csv_url']}")


@report_app.command("delete")
@_friendly_errors
def report_delete(
    report_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a generated report."""
    from vmware_aria.ops.reports import delete_report

    if not yes:
        typer.confirm(f"Delete report {report_id}?", abort=True)

    client, cfg = _get_connection(target, config)
    target_name = target or cfg.default_target or "default"
    result = delete_report(client, report_id=report_id, audit_logger=_audit, target_name=target_name)
    console.print(f"[green]Deleted report {result['report_id']}[/green]")

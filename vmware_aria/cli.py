"""CLI entry point for VMware Aria Operations.

Provides commands for resource monitoring, alert management, capacity planning,
anomaly detection, and platform health checks.
"""

from __future__ import annotations

import sys as _sys
import functools
import json
from pathlib import Path
from typing import Optional, Annotated

import typer
from rich.console import Console
from rich.table import Table
from vmware_policy import PolicyDenied, audited, cli_local, guarded

from vmware_aria.notify.audit import AuditLogger
import sys
# Registers this skill's environment resolver, so environment-scoped policy
# rules apply to @guarded CLI writes exactly as they do to MCP tools.
import vmware_aria.policy_environment  # noqa: F401 — imported to register the resolver; do not remove


def _harden_console_encoding() -> None:
    """Never let one unrepresentable glyph kill a command.

    On a console whose encoding cannot carry the characters we print -- cp936 on
    the Chinese Windows boxes this family is tested on, or any ASCII locale --
    ``print`` raises ``UnicodeEncodeError`` and the whole command dies with a
    traceback. ``--help`` died that way in four repos. A mangled dash is a
    cosmetic loss; a dead ``--help`` is an outage, so the error handler is
    relaxed rather than the vocabulary narrowed.

    Best effort: ``reconfigure`` is absent when stdout has been replaced by a
    plain object (pytest capture, some MCP hosts), and losing the hardening
    there is not worth an exception at import.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


_harden_console_encoding()

_audit = AuditLogger()

app = typer.Typer(
    name="vmware-aria",
    help="VMware Aria Operations monitoring, alerting, and capacity planning.",
    no_args_is_help=True,
)
console = Console()


def _print_next_page(result: dict) -> None:
    """Say where the next page starts, or say nothing when there is none.

    A table headed "Aria Operations Alerts" over 50 rows reads as the alert
    list. On the estate that reported this it was 50 of 2,783, and the CLI
    took ``["items"]`` and dropped the part of the envelope that said so.
    """
    nxt = result.get("next_offset")
    if nxt is None:
        return
    total = result.get("total")
    of_total = f" of {total}" if total is not None else ""
    console.print(
        f"[yellow]Showing rows up to {nxt}{of_total}. "
        f"Re-run with --offset {nxt} for the next page.[/]"
    )

# ─── Sub-command groups ──────────────────────────────────────────────────────

resource_app = typer.Typer(help="Resource queries: list, get, metrics, health, top, keys, properties, relationships.")
alert_app = typer.Typer(help="Alert management: list, get, acknowledge, cancel, definitions.")
capacity_app = typer.Typer(help="Capacity planning: overview, remaining, time-remaining, rightsizing.")
anomaly_app = typer.Typer(help="Anomaly detection: list anomalies, risk badge.")
health_app = typer.Typer(help="Platform health: Aria node status, collector groups.")
report_app = typer.Typer(help="Report management: list definitions, generate, list, get, delete.")
fleet_app = typer.Typer(help="VCF 9.1 fleet: certificates, password accounts, domains, findings, PromQL.")

app.add_typer(resource_app, name="resource")
app.add_typer(alert_app, name="alert")
app.add_typer(capacity_app, name="capacity")
app.add_typer(anomaly_app, name="anomaly")
app.add_typer(health_app, name="health")
app.add_typer(report_app, name="report")
app.add_typer(fleet_app, name="fleet")


@app.command("mcp")
@cli_local("starts the MCP server; its tools audit themselves")
def mcp_cmd(
    expose: Optional[str] = typer.Option(
        None,
        "--expose",
        help="Which tools to offer: all (default) or read. Limits what clients see; "
        "not authorization: the account's permissions still decide.",
    ),
    allow_write: Optional[str] = typer.Option(
        None,
        "--allow-write",
        help="With --expose read: comma-separated write tools to offer as well.",
    ),
    transport: Optional[str] = typer.Option(
        None,
        "--transport",
        help="stdio (default), https, or http (loopback only, behind a TLS reverse proxy). "
        "https/http need a token: 'vmware-audit token create NAME'.",
    ),
    host: Optional[str] = typer.Option(None, "--host", help="Bind address (default 127.0.0.1)."),
    port: Optional[int] = typer.Option(None, "--port", help="Port (default 8443)."),
    cert: Optional[str] = typer.Option(None, "--cert", help="TLS certificate (PEM) for https."),
    key: Optional[str] = typer.Option(None, "--key", help="TLS private key (PEM) for https."),
    public_name: Optional[str] = typer.Option(
        None,
        "--public-name",
        help="Host names clients use, comma-separated; other Host/Origin values are refused.",
    ),
    token_file: Optional[str] = typer.Option(
        None, "--token-file", help="Token hash file (default $OPS_HOME/mcp-tokens)."
    ),
) -> None:
    """Start the MCP server (stdio by default; --transport https serves the network).

    Single-command entry point for MCP clients:
        vmware-aria mcp

    Equivalent to the legacy `vmware-aria-mcp` console script for stdio (the console script takes no flags).
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

    _mcp_main(
        expose=expose, allow_write=allow_write, transport=transport, host=host, port=port,
        cert=cert, key=key, public_name=public_name, token_file=token_file,
    )


# ─── Type aliases ────────────────────────────────────────────────────────────

import vmware_aria.cli_options as _compat_exports_0
TargetOption = _compat_exports_0.TargetOption
ConfigOption = _compat_exports_0.ConfigOption


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _friendly_errors(fn):
    """Print expected operational errors as one red line instead of a traceback.

    AriaApiError already carries a teaching message (status + path + fix);
    config problems surface as FileNotFoundError/KeyError/OSError; a rejected
    argument is a ValueError raised by the ops layer with the remedy in its
    message, reported as a usage error (exit 2). Anything else is a real bug
    and keeps its traceback for debugging.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from vmware_aria.connection import AriaApiError

        try:
            return fn(*args, **kwargs)
        except PolicyDenied as exc:
            # A deny rule or closed maintenance window refused this write —
            # @guarded ran guard() before the body and already wrote the
            # status="denied" audit row. Teach which rule fired, not a traceback.
            rule = f" [dim](rule: {exc.result.rule})[/]" if exc.result.rule else ""
            console.print(f"[red]Denied by policy: {exc.result.reason}[/]{rule}")
            raise typer.Exit(1) from exc
        except (AriaApiError, FileNotFoundError, KeyError, OSError) as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1) from exc
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(2) from exc
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
@audited("init")
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
@audited("doctor")
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











from vmware_aria._cli_resources import register_callbacks as _register_cli_resources

(
    resource_list,
    resource_get,
    resource_metrics,
    resource_health,
    resource_top,
) = _register_cli_resources(_sys.modules[__name__])


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT commands
# ═══════════════════════════════════════════════════════════════════════════════


#: Narrowest terminal at which the alert table keeps both UUID columns whole.
_ALERT_TABLE_MIN_WIDTH = 160









from vmware_aria._cli_alert_display import register_callbacks as _register_cli_alert_display

(
    _alert_started,
    _alert_resource,
    _print_alert_table,
    _print_alert_records,
) = _register_cli_alert_display(_sys.modules[__name__])











from vmware_aria._cli_alerts import register_callbacks as _register_cli_alerts

(
    alert_list,
    alert_get,
    alert_acknowledge,
    alert_cancel,
    alert_definitions,
) = _register_cli_alerts(_sys.modules[__name__])


# ═══════════════════════════════════════════════════════════════════════════════
# CAPACITY commands
# ═══════════════════════════════════════════════════════════════════════════════









from vmware_aria._cli_capacity import register_callbacks as _register_cli_capacity

(
    capacity_overview,
    capacity_remaining,
    capacity_time_remaining,
    capacity_rightsizing,
) = _register_cli_capacity(_sys.modules[__name__])


# ═══════════════════════════════════════════════════════════════════════════════
# ANOMALY commands
# ═══════════════════════════════════════════════════════════════════════════════


@anomaly_app.command("list")
@_friendly_errors
@audited("list_anomalies")
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

    table = Table(title="Total Anomalies (System Attributes|total_alarms)", show_lines=False)
    table.add_column("Resource", style="bold")
    table.add_column("Anomaly Count")

    for a in items:
        table.add_row(
            (a["resource_name"] or a["resource_id"])[:40],
            str(a.get("anomaly_count", "")),
        )

    console.print(table)
    # Checked on a live 8.18.7 key catalogue: total_alarms is "Total Anomalies",
    # and the alert count is a separate key. vcsa read 5 here with no alerts.
    console.print(
        "[dim]Aria's name for this key is Total Anomalies. It is not the alert count, "
        "which is System Attributes|total_alert_count.[/]"
    )


@anomaly_app.command("risk")
@_friendly_errors
@audited("get_resource_riskbadge")
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
@audited("get_aria_health")
def health_status(
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Check Aria Operations platform health: node, each service, and the release."""
    from vmware_aria.ops.health import get_aria_health

    client, _ = _get_connection(target, config)
    data = get_aria_health(client)

    style = {"HEALTHY": "green", "DEGRADED": "yellow", "UNKNOWN": "yellow"}.get(data["assessment"], "red")
    console.print(f"\nAria Operations Platform: [{style}]{data['assessment']}[/{style}]")
    console.print(f"Node status: {data['overall_status'] or '(none)'}")
    version = data["release_name"] or f"unknown ({data['version_error']})"
    if data["product_line"]:
        version += f" — {data['product_line']} line"
    console.print(f"Version: {version}")
    if data["services"] is None:
        console.print(f"Services: not read ({data['services_error']})")
    else:
        table = Table(show_header=True, show_lines=False)
        table.add_column("Service")
        table.add_column("Health")
        table.add_column("Details")
        for svc in data["services"]:
            svc_style = "green" if svc["health"] == "OK" else "red"
            table.add_row(svc["name"], f"[{svc_style}]{svc['health']}[/{svc_style}]", svc["details"])
        console.print(table)
    console.print(f"Details: {data['details']}")


@health_app.command("collectors")
@_friendly_errors
@audited("list_collector_groups")
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
@audited("list_report_definitions")
def report_definitions(
    name_filter: Annotated[str | None, typer.Option("--name", help="Filter by name substring")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Page size, 1-500")] = 50,
    offset: Annotated[int, typer.Option("--offset", help="Rows to skip; the next-page offset is printed below the table")] = 0,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List available report definition templates."""
    from vmware_aria.ops.reports import list_report_definitions

    client, _ = _get_connection(target, config)
    _result = list_report_definitions(
        client, name_filter=name_filter, limit=limit, offset=offset
    )
    items = _result["items"]

    table = Table(title="Report Definitions", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Subject Type")
    table.add_column("Owner")

    for d in items:
        table.add_row(d["name"][:60], d["id"][:36], d["subject_type"], d["owner"])

    console.print(table)
    _print_next_page(_result)


@report_app.command("generate")
@_friendly_errors
@guarded("generate_report", risk_level="medium")
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
@audited("list_reports")
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
@audited("get_report")
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


def _confirm_destructive(resource_type: str, resource_id: str, verb: str = "delete") -> bool:
    """Two prompts before an irreversible operation.

    The family's rule is that a CLI command whose MCP tool is annotated
    ``destructiveHint=True`` asks twice. It was enforced by one repo's test
    rather than a family gate, so this file asked once for `report delete` —
    a command whose own tool description says "Irreversible". Same shape as
    NSX-Security's helper, deliberately: a reader who has seen one should
    recognise the other.
    """
    console.print(
        f"[bold red]WARNING:[/] This will permanently {verb} {resource_type} "
        f"'[bold]{resource_id}[/]'. This cannot be undone."
    )
    if not typer.confirm("Are you sure you want to proceed?", default=False):
        return False
    return typer.confirm(
        f"Second confirmation: {verb.upper()} {resource_type} '{resource_id}'?", default=False
    )


@report_app.command("delete")
@_friendly_errors
@guarded("delete_report", risk_level="medium")
def report_delete(
    report_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
) -> None:
    """Delete a generated report."""
    from vmware_aria.ops.reports import delete_report

    if not yes and not _confirm_destructive("report", report_id):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)

    client, cfg = _get_connection(target, config)
    target_name = target or cfg.default_target or "default"
    result = delete_report(client, report_id=report_id, audit_logger=_audit, target_name=target_name)
    console.print(f"[green]Deleted report {result['report_id']}[/green]")


# ═══════════════════════════════════════════════════════════════════════════════
# FLEET commands (VCF Operations 9.1 — all read-only)
# ═══════════════════════════════════════════════════════════════════════════════


@fleet_app.command("certificates")
@_friendly_errors
@audited("list_fleet_certificates")
def fleet_certificates(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List certificate status/expiry across the VCF fleet."""
    from vmware_aria.ops.fleet import list_fleet_certificates

    client, _ = _get_connection(target, config)
    items = list_fleet_certificates(client, limit=limit)["items"]

    table = Table(title="Fleet Certificates", show_lines=False)
    table.add_column("Subject", style="bold")
    table.add_column("Status")
    table.add_column("Valid To")
    table.add_column("Resource")
    for r in items:
        table.add_row(r["subject"], r["status"], r["valid_to"], r["resource"])
    console.print(table)


@fleet_app.command("passwords")
@_friendly_errors
@audited("list_fleet_password_accounts")
def fleet_passwords(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List managed password-account status across the VCF fleet (read-only)."""
    from vmware_aria.ops.fleet import list_fleet_password_accounts

    client, _ = _get_connection(target, config)
    items = list_fleet_password_accounts(client, limit=limit)["items"]

    table = Table(title="Fleet Password Accounts", show_lines=False)
    table.add_column("Username", style="bold")
    table.add_column("Resource")
    table.add_column("Status")
    table.add_column("Expiry")
    for r in items:
        table.add_row(r["username"], r["resource"], r["status"], r["expiry"])
    console.print(table)


@fleet_app.command("domains")
@_friendly_errors
@audited("list_fleet_domains")
def fleet_domains(
    integration_id: Annotated[str, typer.Argument(help="VCF integration UUID (from Operations Integrations page)")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List SDDC/workload domains behind one registered VCF integration."""
    from vmware_aria.ops.fleet import list_fleet_domains

    client, _ = _get_connection(target, config)
    result = list_fleet_domains(client, integration_id=integration_id, limit=limit)

    table = Table(title="VCF Domains", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Configuration")
    table.add_column("ID")
    for r in result["items"]:
        table.add_row(r["name"], r["type"], r["status"], r["configuration_state"], r["id"][:36])
    console.print(table)
    # Reading only ["items"] here is what made an unparsed body indistinguishable
    # from an empty fleet: the note was in the envelope and nobody ever saw it.
    if result.get("note"):
        console.print(f"[yellow]Note: {result['note']}[/yellow]")


@fleet_app.command("findings")
@_friendly_errors
@audited("list_findings")
def fleet_findings(
    severities: Annotated[str | None, typer.Option("--severities", help="Comma-separated, e.g. CRITICAL,WARNING")] = None,
    categories: Annotated[str | None, typer.Option("--categories", help="Comma-separated category filter")] = None,
    finding_types: Annotated[str | None, typer.Option("--types", help="Comma-separated findingType filter")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List Operations diagnostic findings (not compliance — use vmware-harden for that)."""
    from vmware_aria.ops.fleet import list_findings

    def _split(v: str | None) -> list[str] | None:
        return [s.strip() for s in v.split(",") if s.strip()] if v else None

    client, _ = _get_connection(target, config)
    items = list_findings(
        client,
        severities=_split(severities),
        categories=_split(categories),
        finding_types=_split(finding_types),
        limit=limit,
    )["items"]

    table = Table(title="Diagnostic Findings", show_lines=False)
    table.add_column("Severity")
    table.add_column("Name", style="bold")
    table.add_column("Category")
    table.add_column("Affected", justify="right")
    for r in items:
        table.add_row(r["severity"], r["name"], r["category"], str(r.get("affected_objects_count", "")))
    console.print(table)


@fleet_app.command("promql")
@_friendly_errors
@audited("promql_query")
def fleet_promql(
    query: Annotated[str, typer.Argument(help="PromQL expression, e.g. 'cpu_usage_average{}'")],
    at_time: Annotated[str | None, typer.Option("--time", help="Evaluation timestamp (RFC3339 or Unix seconds)")] = None,
    source_id: Annotated[str | None, typer.Option("--source", help="Data-source id to scope the query")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max result series")] = 50,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Run a real-time PromQL instant query (VCF 9.1 VODAP service; base path INFERRED)."""
    from vmware_aria.ops.promql import run_promql_query

    client, _ = _get_connection(target, config)
    result = run_promql_query(client, query=query, time=at_time, source_id=source_id, limit=limit)
    _json_output(result)


# resource keys / properties / relationships live in cli_catalog.py (registered on import).
import vmware_aria.cli_catalog  # noqa: E402,F401
# `health node` / `health adapters` live in cli_platform.py; importing it registers them.
from vmware_aria import cli_platform  # noqa: E402,F401
# `api search` / `api get` (HLD §17 D-18) live in cli_api.py; importing it registers them.
from vmware_aria import cli_api  # noqa: E402,F401
# Maintenance and alert notes/recommendations register onto the apps above from
# cli_workflow.py (keeps this file under its size limit). Must stay last.
from vmware_aria import cli_workflow  # noqa: E402,F401

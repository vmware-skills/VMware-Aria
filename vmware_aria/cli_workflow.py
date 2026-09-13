"""CLI: resource maintenance mode, maintenance schedules, alert notes and recommendations.

Commands register onto the Typer apps defined in ``cli.py``, which imports
this module on its last line — so ``cli.py`` stays near its size limit instead
of growing past it.

Writes follow ``alert acknowledge`` in ``cli.py``: ``@guarded`` under the MCP
tool's own name and risk (one deny rule scopes both surfaces), one confirmation
prompt with ``--yes`` for a caller who has already decided, and ``--dry-run``,
which prints the API call and makes none — no connection, no request.

``cli._get_connection`` / ``cli._audit`` / ``cli._json_output`` are read through
the module at call time so tests patching them on ``cli`` reach these commands.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.table import Table
from vmware_policy import guarded

from vmware_aria import cli as _cli
from vmware_aria.cli import ConfigOption, TargetOption, _friendly_errors, alert_app, app, console

maintenance_app = typer.Typer(help="Resource maintenance mode: start, end, schedules.")
app.add_typer(maintenance_app, name="maintenance")

DryRunOption = Annotated[bool, typer.Option("--dry-run", help="Print the API call without executing it")]
YesOption = Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")]
LimitOption = Annotated[int, typer.Option("--limit", "-n", help="Page size, 1-500")]
OffsetOption = Annotated[int, typer.Option("--offset", help="Rows to skip; the next-page offset is printed below the table")]


def _plain(text: str, style: str = "") -> None:
    """Print without Rich markup or wrapping — ids, paths and errors verbatim."""
    console.print(text, style=style, markup=False, highlight=False, soft_wrap=True)


def _print_dry_run(
    method: str,
    path: str,
    target: str | None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> None:
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    _plain("[DRY-RUN] No changes will be made.", "bold magenta")
    _plain(f"  Target:   {target or '(default target from config)'}", "magenta")
    _plain(f"  API call: {method} /suite-api/api{path}{'?' + query if query else ''}", "magenta")
    if body is not None:
        _plain(f"  Body:     {json.dumps(body, ensure_ascii=False)}", "magenta")
    _plain("  Run without --dry-run to execute.", "magenta")


def _refuse(exc: ValueError) -> None:
    _plain(f"Error: {exc}", "red")
    raise typer.Exit(2) from exc


def _window_params(duration: int | None, end: int | None) -> dict[str, int] | None:
    from vmware_aria.ops.maintenance import maintenance_window_params

    try:
        return maintenance_window_params(duration, end)
    except ValueError as exc:
        _refuse(exc)
        return None  # unreachable; _refuse always exits


def _connect(target: str | None, config: Any) -> tuple[Any, str]:
    client, cfg = _cli._get_connection(target, config)
    return client, target or cfg.default_target or "default"


# ═══════════════════════════════════════════════════════════════════════════════
# MAINTENANCE commands
# ═══════════════════════════════════════════════════════════════════════════════


@maintenance_app.command("start")
@_friendly_errors
@guarded("start_resource_maintenance", risk_level="medium")
def maintenance_start(
    resource_id: Annotated[str, typer.Argument(help="Resource UUID (from `vmware-aria resource list`)")],
    duration: Annotated[int | None, typer.Option("--duration", help="Window length in minutes")] = None,
    end: Annotated[int | None, typer.Option("--end", help="Window end, epoch milliseconds")] = None,
    dry_run: DryRunOption = False,
    yes: YesOption = False,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Put a resource in maintenance: Aria stops alerting on it and collecting its data.

    With neither --duration nor --end the resource stays in maintenance until
    `vmware-aria maintenance end`.
    """
    from vmware_aria.ops.maintenance import start_resource_maintenance

    params = _window_params(duration, end)
    if dry_run:
        _print_dry_run("PUT", f"/resources/{resource_id}/maintained", target, params=params)
        return
    if duration is not None:
        window = f"for {duration} minutes"
    elif end is not None:
        window = f"until {end} (epoch ms)"
    else:
        window = "with NO end (until `vmware-aria maintenance end`)"
    if not yes:
        typer.confirm(f"Put resource {resource_id} in maintenance {window}? Alerting and collection stop.", abort=True)

    client, target_name = _connect(target, config)
    result = start_resource_maintenance(
        client, resource_id, duration_minutes=duration, end_time_ms=end, audit_logger=_cli._audit, target_name=target_name
    )
    _cli._json_output(result)


@maintenance_app.command("end")
@_friendly_errors
@guarded("end_resource_maintenance", risk_level="medium")
def maintenance_end(
    resource_id: Annotated[str, typer.Argument(help="Resource UUID (from `vmware-aria resource list`)")],
    dry_run: DryRunOption = False,
    yes: YesOption = False,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Take a resource out of maintenance: Aria resumes alerting and collection."""
    from vmware_aria.ops.maintenance import end_resource_maintenance

    if dry_run:
        _print_dry_run("DELETE", f"/resources/{resource_id}/maintained", target)
        return
    if not yes:
        typer.confirm(f"Take resource {resource_id} out of maintenance? Alerting and collection resume.", abort=True)

    client, target_name = _connect(target, config)
    try:
        result = end_resource_maintenance(client, resource_id, audit_logger=_cli._audit, target_name=target_name)
    except ValueError as exc:
        _refuse(exc)
    _cli._json_output(result)


@maintenance_app.command("schedules")
@_friendly_errors
def maintenance_schedules(
    resource_id: Annotated[str | None, typer.Option("--resource-id", help="Only schedules for this resource")] = None,
    limit: LimitOption = 50,
    offset: OffsetOption = 0,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List maintenance schedules."""
    from vmware_aria.ops.maintenance import list_maintenance_schedules

    client, _ = _cli._get_connection(target, config)
    result = list_maintenance_schedules(client, resource_id=resource_id, limit=limit, offset=offset)

    table = Table(title="Maintenance Schedules", show_lines=False)
    for column in ("Name", "Type", "Start", "Duration (min)", "Recurrence", "Expires", "ID"):
        table.add_column(column)
    for s in result["items"]:
        start = "" if s["start_hour"] is None else f"{s['start_hour']:02d}:{s['start_minute'] or 0:02d} {s['time_zone'] or ''}"
        expires = s["expiration_date"] or (f"after {s['expire_runs']} runs" if s["expire_runs"] is not None else "")
        table.add_row(
            s["name"] or "", s["schedule_type"] or "", start, str(s["duration_minutes"] or ""),
            str(s["recurrence"] or ""), expires, s["id"] or "",
        )
    console.print(table)
    if result.get("schedules_note"):
        _plain(result["schedules_note"], "yellow")
    _cli._print_next_page(result)


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT notes and recommendations
# ═══════════════════════════════════════════════════════════════════════════════


@alert_app.command("notes")
@_friendly_errors
def alert_notes(
    alert_id: str,
    limit: LimitOption = 50,
    offset: OffsetOption = 0,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List the notes on an alert."""
    from vmware_aria.ops.alert_notes import list_alert_notes

    client, _ = _cli._get_connection(target, config)
    result = list_alert_notes(client, alert_id, limit=limit, offset=offset)

    table = Table(title=f"Notes on alert {alert_id}", show_lines=False)
    for column in ("Created (ms)", "Type", "User", "Note"):
        table.add_column(column)
    for n in result["items"]:
        table.add_row(str(n["created_time_ms"] or ""), n["type"] or "", n["user_name"] or "", n["note"] or "")
    console.print(table)
    if result.get("notes_note"):
        _plain(result["notes_note"], "yellow")
    _cli._print_next_page(result)


@alert_app.command("note-add")
@_friendly_errors
@guarded("add_alert_note", risk_level="low")
def alert_note_add(
    alert_id: str,
    text: Annotated[str, typer.Argument(help="Note text: who is handling the alert, what was done")],
    dry_run: DryRunOption = False,
    yes: YesOption = False,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Add a note to an alert."""
    from vmware_aria.ops.alert_notes import add_alert_note

    if not text.strip():
        _refuse(ValueError("The note text is empty — say who is handling the alert or what was done."))
    if dry_run:
        _print_dry_run("POST", f"/alerts/{alert_id}/notes", target, body={"content": text.strip()})
        return
    if not yes:
        typer.confirm(f"Add this note to alert {alert_id}?", abort=True)

    client, target_name = _connect(target, config)
    result = add_alert_note(client, alert_id, text, audit_logger=_cli._audit, target_name=target_name)
    _cli._json_output(result)


@alert_app.command("recommendations")
@_friendly_errors
def alert_recommendations(
    alert_id: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Show the prioritized recommendations for an alert (from its alert definition)."""
    from vmware_aria.ops.alert_recommendations import get_alert_recommendations

    client, _ = _cli._get_connection(target, config)
    _cli._json_output(get_alert_recommendations(client, alert_id))

"""``vmware-aria resource keys | properties | relationships``.

Registered onto ``cli.resource_app`` from here so ``cli.py`` (already over a
thousand lines) grows by one import. ``cli`` imports this module at its end,
after every name used below is defined; the connection helper is looked up on
the ``cli`` module at call time so tests and callers patch one place.
"""

from __future__ import annotations

import functools
from typing import Annotated

import typer
from rich.table import Table

from vmware_aria import cli as _cli
from vmware_aria.cli import ConfigOption, TargetOption
from vmware_policy import audited

LimitOption = Annotated[int, typer.Option("--limit", "-n", help="Page size, 1-500")]
OffsetOption = Annotated[int, typer.Option("--offset", help="Rows to skip; the next-page offset is printed below the table")]


def _bad_input_is_one_line(fn):
    """A rejected argument (bad --type, --limit 0) is a red line, not a traceback.

    ``cli._friendly_errors`` does not catch ``ValueError``; the ops layer raises
    it with the remedy in the message, so print that and exit 2 (usage error).
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            _cli.console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(2) from exc

    return wrapper


def _print_notes(result: dict, *notes: str) -> None:
    for key in notes:
        if result.get(key):
            _cli.console.print(f"[yellow]{result[key]}[/]")
    if result.get("next_offset") is not None:
        _cli._print_next_page(result)
    elif result.get("hint"):
        _cli.console.print(f"[yellow]{result['hint']}[/]")


@_cli.resource_app.command("keys")
@_cli._friendly_errors
@_bad_input_is_one_line
@audited("list_metric_keys")
def resource_keys(
    resource_id: Annotated[str | None, typer.Argument(help="Resource UUID; omit and pass --kind for a kind's definitions")] = None,
    kind: Annotated[str | None, typer.Option("--kind", "-k", help="Resource kind, e.g. VirtualMachine")] = None,
    adapter_kind: Annotated[str, typer.Option("--adapter-kind", help="Adapter kind for --kind")] = "VMWARE",
    key_filter: Annotated[str | None, typer.Option("--filter", "-f", help="Substring of key or name, e.g. 'mem|'")] = None,
    limit: LimitOption = 100,
    offset: OffsetOption = 0,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List the metric keys a resource reports (with name and unit), or a kind defines."""
    from vmware_aria.ops.catalog import list_metric_keys

    client, _ = _cli._get_connection(target, config)
    result = list_metric_keys(
        client, resource_id=resource_id, resource_kind=kind, adapter_kind=adapter_kind,
        key_filter=key_filter, limit=limit, offset=offset,
    )
    table = Table(title=f"Metric keys ({result['resource_kind'] or 'unknown kind'})", show_lines=False)
    for column in ("Key", "Name", "Unit") + (("Definition",) if result["source"] == "resource" else ()):
        table.add_column(column, overflow="fold")
    for row in result["items"]:
        cells = [row["key"], row["name"] or "", row["unit"] or ""]
        table.add_row(*cells, *((row["definition"],) if result["source"] == "resource" else ()))
    _cli.console.print(table)
    if result["unjoined_count"]:
        _cli.console.print(f"[yellow]{result['unjoined_count']} key(s) are not defined for the kind.[/]")
    _print_notes(result, "definitions_note")


@_cli.resource_app.command("properties")
@_cli._friendly_errors
@_bad_input_is_one_line
@audited("get_resource_properties")
def resource_properties(
    resource_id: str,
    name_filter: Annotated[str | None, typer.Option("--name", help="Substring of the property name, e.g. 'summary|'")] = None,
    limit: LimitOption = 100,
    offset: OffsetOption = 0,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List a resource's properties (power state, parent host, extraConfig, ...)."""
    from vmware_aria.ops.catalog import get_resource_properties

    client, _ = _cli._get_connection(target, config)
    result = get_resource_properties(client, resource_id, name_filter=name_filter, limit=limit, offset=offset)
    table = Table(title="Properties", show_lines=False)
    table.add_column("Name", overflow="fold")
    table.add_column("Value", overflow="fold")
    for row in result["items"]:
        table.add_row(row["name"], "" if row["value"] is None else row["value"])
    _cli.console.print(table)
    _print_notes(result)


@_cli.resource_app.command("relationships")
@_cli._friendly_errors
@_bad_input_is_one_line
@audited("get_resource_relationships")
def resource_relationships(
    resource_id: str,
    relationship_type: Annotated[str, typer.Option("--type", help="ALL, PARENT or CHILD")] = "ALL",
    limit: LimitOption = 100,
    offset: OffsetOption = 0,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List resources related to a resource (parents and children)."""
    from vmware_aria.ops.catalog import get_resource_relationships

    client, _ = _cli._get_connection(target, config)
    result = get_resource_relationships(
        client, resource_id, relationship_type=relationship_type, limit=limit, offset=offset
    )
    table = Table(title=f"Relationships ({result['relationship_type']})", show_lines=False)
    table.add_column("Direction")
    table.add_column("Kind", overflow="fold")
    table.add_column("Name", overflow="fold")
    table.add_column("ID", overflow="fold")
    for row in result["items"]:
        table.add_row(row["direction"] or "unknown", row["kind"], row["name"], row["id"])
    _cli.console.print(table)
    _print_notes(result, "direction_note")

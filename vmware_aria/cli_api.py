"""``vmware-aria api search | get`` — the read-only API escape hatch (HLD §17 D-18).

The CLI twins of the ``search_api_operations`` and ``get_api_path`` MCP tools,
audited under those names. ``cli.py`` imports this module near its end;
``cli._get_connection`` / ``cli._json_output`` are read through the module at
call time so tests patching them on ``cli`` reach these commands.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from vmware_policy import audited

from vmware_aria import cli as _cli
from vmware_aria.cli import ConfigOption, TargetOption
from vmware_aria.cli_catalog import _bad_input_is_one_line

api_app = typer.Typer(help="Read-only raw API: search the operation index, GET an indexed path.")
_cli.app.add_typer(api_app, name="api")


def _parse_params(pairs: list[str] | None) -> dict[str, str] | None:
    if not pairs:
        return None
    params: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise ValueError(f"--param takes NAME=VALUE (got {pair!r}), e.g. --param pageSize=20.")
        params[name] = value
    return params


@api_app.command("search")
@_cli._friendly_errors
@_bad_input_is_one_line
@audited("search_api_operations")
def api_search(
    query: Annotated[str, typer.Argument(help="Words to find, e.g. 'resource properties'")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Operations to show, 1-100")] = 20,
) -> None:
    """Search the suite-api operation index shipped with this skill (no appliance contacted)."""
    from vmware_aria.ops.api_escape import search_api_operations

    result = search_api_operations(query, limit=limit)
    table = Table(title=f"API operations matching '{escape(result['query'])}'")
    for column in ("Method", "Path", "Indexes", "Summary"):
        table.add_column(column, overflow="fold")
    for op in result["operations"]:
        method = op["method"] if op["callable_with_get_api_path"] else f"[dim]{op['method']}[/]"
        table.add_row(method, escape(op["path"]), ", ".join(op["indexes"]), escape(op["summary"]))
    _cli.console.print(table)
    _cli.console.print(
        f"[dim]{result['returned']} of {result['total_matches']} shown. Only GET paths can be read "
        "with 'vmware-aria api get'.[/]"
    )


@api_app.command("get")
@_cli._friendly_errors
@_bad_input_is_one_line
@audited("get_api_path")
def api_get(
    path: Annotated[str, typer.Argument(help="Indexed GET path, e.g. /api/resources/<uuid>/properties")],
    param: Annotated[
        list[str] | None, typer.Option("--param", "-p", help="Query parameter NAME=VALUE; repeatable")
    ] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """GET one indexed suite-api path; prints the sanitized, size-capped JSON answer."""
    from vmware_aria.ops.api_escape import get_api_path, check_request

    params = _parse_params(param)
    check_request(path, params)  # refuse before connecting
    client, _ = _cli._get_connection(target, config)
    _cli._json_output(get_api_path(client, path, params))

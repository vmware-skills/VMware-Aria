"""Every CLI command that performs a write is wrapped by @guarded (HLD I-1, I-8).

A write CLI command must route through vmware_policy's guard() + audit_call() —
the same enforcement @vmware_tool gives the MCP surface — so ``vmware-aria alert
cancel`` run through Bash is authorized and audited to ~/.vmware/audit.db exactly
like the ``cancel_alert`` MCP tool. Without @guarded a CLI write bypassed policy
and landed only in the legacy per-skill log (the gap HLD §2.1 documents).

The write set is DERIVED, never hand-listed (踩坑 #43): a tool annotated
``readOnlyHint=False`` is a write; the ops functions its body calls — reached by
a bare name OR ``module.func`` on an ops-module import (Aria's tools alias, e.g.
``from ...ops.alerts import cancel_alert as _cancel``) — are the state-changing
ops; a CLI ``@command`` calling one is a write command and must carry @guarded.

Two Aria-specific derivation facts, both of which would silently shrink the set
if handled wrong (the "empty result read as no problem" shape):

* The CLI is a single ``vmware_aria/cli.py`` module, not a ``cli/`` package.
* The four confirmed-gate destructive tools (acknowledge_alert, cancel_alert,
  delete_alert_definition, delete_report) are defined in
  ``mcp_server/server.py``, NOT under ``mcp_server/tools/``. The ops scan must
  cover the whole ``mcp_server/`` tree or acknowledge/cancel/delete_report are
  never derived as write ops and their CLI commands look read-only.

Aria's CLI surfaces 4 of the 7 write MCP tools — alert acknowledge/cancel and
report generate/delete. The other three writes (create_alert_definition,
set_alert_definition_state, delete_alert_definition) are MCP-only; there is no
CLI command for them, so the derived CLI write count is 4, not 7.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[3]
CLI_FILE = _REPO / "vmware_aria" / "cli.py"
# Whole server tree, not just tools/: the confirmed-gate destructive tools live
# in mcp_server/server.py (see module docstring).
TOOLS_DIR = _REPO / "vmware_aria" / "mcp_server"
assert CLI_FILE.is_file(), f"CLI module not found at {CLI_FILE} — the scan would find nothing"
assert TOOLS_DIR.is_dir(), f"MCP server tree not found at {TOOLS_DIR} — the derivation would be empty"


def _cli_files() -> list[pathlib.Path]:
    """CLI source files. Aria is a single module; a future cli/ package still works."""
    cli_pkg = _REPO / "vmware_aria" / "cli"
    if cli_pkg.is_dir():
        return sorted(cli_pkg.rglob("*.py"))
    return [CLI_FILE]


def _write_tool_names() -> frozenset[str]:
    from vmware_aria.mcp_server.server import mcp

    return frozenset(
        t.name
        for t in asyncio.run(mcp.list_tools())
        if getattr(getattr(t, "annotations", None), "readOnlyHint", None) is False
    )


def _ops_refs(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """(local name -> REAL ops function name, ops-module aliases).

    An aliased import (``from ops.mod import realname as _alias``) maps
    ``_alias -> realname`` so an aliased call resolves to the same op an
    un-aliased import names. Aria's MCP tools alias their ops (``... as _cancel``)
    while the CLI imports the real name; both must derive to the real name or the
    MCP→ops→CLI intersection is empty.
    """
    func_map: dict[str, str] = {}
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            parts = n.module.split(".")
            if "ops" in parts:
                if parts[-1] == "ops":
                    mods.update(a.asname or a.name for a in n.names)
                else:
                    for a in n.names:
                        func_map[a.asname or a.name] = a.name
    return func_map, mods


def _ops_calls(node: ast.AST, func_map: dict[str, str], mods: set[str]) -> set[str]:
    """Real ops function names called in ``node`` — via ``f()`` or ``mod.f()``."""
    out: set[str] = set()
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        if isinstance(f, ast.Name) and f.id in func_map:
            out.add(func_map[f.id])
        elif (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id in mods
        ):
            out.add(f.attr)
    return out


def _write_ops() -> frozenset[str]:
    targets = _write_tool_names()
    assert targets, "no write MCP tools (readOnlyHint=False) — derivation would be vacuous"
    ops: set[str] = set()
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        func_map, mods = _ops_refs(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in targets:
                ops |= _ops_calls(node, func_map, mods)
    return frozenset(ops)


def _decorator_names(node: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for d in node.decorator_list:
        t = d.func if isinstance(d, ast.Call) else d
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, ast.Attribute):
            names.add(t.attr)
    return names


def _cli_write_commands() -> tuple[list[str], list[str]]:
    """(write commands, of those the ones missing @guarded)."""
    write_ops = _write_ops()
    assert write_ops, "no write ops derived — vacuous"
    writing: list[str] = []
    unguarded: list[str] = []
    for path in _cli_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        func_map, mods = _ops_refs(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not any(
                isinstance(d, ast.Call)
                and isinstance(getattr(d, "func", None), ast.Attribute)
                and d.func.attr == "command"
                for d in node.decorator_list
            ):
                continue
            if _ops_calls(node, func_map, mods) & write_ops:
                label = f"{path.name}:{node.name}"
                writing.append(label)
                if "guarded" not in _decorator_names(node):
                    unguarded.append(label)
    return writing, unguarded


def test_every_write_cli_command_is_guarded():
    writing, unguarded = _cli_write_commands()
    # Aria's CLI exposes 4 of the 7 write MCP tools (the alert-definition writes
    # are MCP-only). The floor is the real derived count — a check matching
    # almost nothing is worse than none.
    assert len(writing) >= 4, (
        f"only {len(writing)} write CLI commands derived ({writing}) — the "
        f"MCP→ops→CLI derivation is likely stale; a check matching almost nothing "
        f"is worse than none."
    )
    assert not unguarded, (
        f"these CLI commands call a [WRITE] ops function but are not @guarded, so "
        f"they bypass policy + audit (HLD I-1): {unguarded}"
    )


def test_high_blast_radius_commands_are_derived_and_guarded():
    """Pin named commands so a broad-but-wrong derivation cannot pass the floor.

    ``alert_acknowledge`` and ``report_delete`` back onto ops (acknowledge_alert,
    delete_report) defined-and-imported in ``mcp_server/server.py``. Their
    presence proves the ops scan covered the whole ``mcp_server/`` tree — a scan
    limited to ``mcp_server/tools/`` would never derive them and both commands
    would look read-only.
    """
    writing, _ = _cli_write_commands()
    names = {w.split(":", 1)[1] for w in writing}
    for must in ("alert_acknowledge", "report_delete"):
        assert must in names, (
            f"{must} is no longer derived as a write command — the readOnlyHint→"
            f"ops→command derivation stopped resolving it (did the ops scan drop "
            f"mcp_server/server.py?)"
        )

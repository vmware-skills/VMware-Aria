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


def _scoped_refs(tree: ast.AST, node: ast.FunctionDef) -> tuple[dict[str, str], set[str]]:
    """``_ops_refs`` as seen from inside ``node``: its own imports win.

    ``_ops_refs(tree)`` walks every import in the file into one map, so two
    function-local imports under the same alias collide — ``delete_alert_definition``
    and ``delete_report`` both import their op ``as _delete``, and the file-wide
    map kept only the last, making ``delete_alert_definition`` look like it calls
    ``delete_report``. A name the function imports itself resolves to that import.
    """
    func_map, mods = _ops_refs(tree)
    local_map, local_mods = _ops_refs(node)
    return {**func_map, **local_map}, mods | local_mods


def _op_to_mcp_tools() -> dict[str, set[str]]:
    """Write ops function -> the MCP write tools whose body calls it.

    Scans the whole ``mcp_server/`` tree for the same reason ``_write_ops`` does:
    the confirmed-gate tools live in ``server.py``, not ``tools/``.
    """
    targets = _write_tool_names()
    out: dict[str, set[str]] = {}
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in targets:
                for op in _ops_calls(node, *_scoped_refs(tree, node)):
                    out.setdefault(op, set()).add(node.name)
    return out


def _mcp_tool_fn(name: str):
    """The decorated MCP tool function, wherever in ``mcp_server`` it is defined."""
    import importlib

    for path in sorted(TOOLS_DIR.rglob("*.py")):
        rel = path.relative_to(_REPO).with_suffix("")
        if rel.name == "__main__":
            continue  # importing __main__ runs the server
        mod = importlib.import_module(".".join(rel.parts).removesuffix(".__init__"))
        fn = getattr(mod, name, None)
        if fn is not None and hasattr(fn, "_risk_level"):
            return fn
    raise AssertionError(f"MCP tool {name!r} not found under {TOOLS_DIR}")


# Guarded CLI writes whose ops function backs more than one MCP write tool —
# listed for an explicit decision, never guessed. Guarded CLI writes with no MCP
# twin keep their own name and are listed with a reason. The test fails if an
# entry stops holding, so neither list can go stale.
_AMBIGUOUS: dict[str, str] = {}
_CLI_ONLY: dict[str, str] = {}


def test_guarded_cli_writes_carry_their_mcp_tool_name():
    """A deny rule names a tool; it must stop the CLI twin of that tool too (HLD I-3).

    ``@guarded`` defaults the tool name to the function's ``__name__``, so
    ``report delete`` was guarded as ``report_delete`` while its MCP twin is
    ``delete_report`` — a rule denying ``delete_report`` refused the agent and
    let the same delete through the CLI, and the two surfaces wrote the one audit
    sink under two names. The twin is DERIVED: the MCP write tool that calls the
    same ops function the command calls.
    """
    from vmware_aria import cli

    op_tools = _op_to_mcp_tools()
    write_ops = frozenset(op_tools)
    checked: list[str] = []
    mismatched: list[str] = []
    stale: list[str] = []
    for path in _cli_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            fn = getattr(cli, node.name, None)
            if not getattr(fn, "_is_guarded", False):
                continue  # unguarded writes are the test above's finding
            ops = _ops_calls(node, *_scoped_refs(tree, node)) & write_ops
            twins = set().union(*(op_tools[o] for o in ops)) if ops else set()
            if node.name in _CLI_ONLY:
                if twins:
                    stale.append(f"{node.name} is listed CLI-only but maps to {sorted(twins)}")
                continue
            if node.name in _AMBIGUOUS:
                if len(twins) <= 1:
                    stale.append(f"{node.name} is listed ambiguous but maps to {sorted(twins)}")
                continue
            assert twins, (
                f"{node.name} is @guarded but calls no MCP write tool's ops — list it "
                f"in _CLI_ONLY with a reason, or the derivation is stale"
            )
            assert len(twins) == 1, (
                f"{node.name} maps to several MCP tools {sorted(twins)} — list it in "
                f"_AMBIGUOUS rather than guess"
            )
            (twin,) = twins
            checked.append(node.name)
            twin_risk = _mcp_tool_fn(twin)._risk_level
            if fn._guarded_tool != twin:
                mismatched.append(f"{node.name}: guarded as {fn._guarded_tool!r}, MCP tool {twin!r}")
            elif fn._risk_level != twin_risk:
                mismatched.append(
                    f"{node.name}: risk {fn._risk_level!r}, MCP tool {twin!r} risk {twin_risk!r}"
                )
    assert not stale, "allowlist entries no longer hold: " + "; ".join(stale)
    assert len(checked) >= 4, f"only {checked} checked — derivation likely stale"
    assert not mismatched, (
        "these CLI writes are guarded under a different name or risk than their "
        "MCP tool, so one deny rule does not scope both surfaces — pass the MCP "
        "tool name to @guarded(...): " + "; ".join(mismatched)
    )

"""Safety boundary tests — verify write/destructive ops have layered guards.

Aria's safety architecture (per family spec — CLAUDE.md 安全规范: aria 写操作
要求审计日志；破坏性操作需确认门), enforced at the layer where each guard
actually lives:

1. **ops layer** — every write function accepts ``audit_logger`` and logs the
   operation (audit is mandatory family-wide).
2. **MCP layer** — every destructive tool has a ``confirm: bool = False``
   parameter returning a blast-radius preview until explicitly confirmed (MCP
   cannot prompt interactively). HLD §7, revised 2026-09-16; ``confirmed`` is
   a deprecated alias. The behaviour is pinned in tests/test_gated_writes.py.
3. **CLI layer** — every destructive command prompts via ``typer.confirm``
   (with a ``--yes`` escape hatch for automation).

History: this file originally asserted ``double_confirm`` inside ops/
functions — a guard that by design lives in the CLI/MCP layers, so the test
failed permanently while real gaps (delete_alert_definition / delete_report
MCP tools missing the confirmed gate) went unnoticed. Rewritten 2026-06-08.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = REPO_ROOT / "vmware_aria" / "ops"
# The CLI is cli.py plus cli_workflow.py (maintenance and alert-workflow
# commands registered onto the same app); a command may live in either.
CLI_PATHS = [REPO_ROOT / "vmware_aria" / "cli.py", REPO_ROOT / "vmware_aria" / "cli_workflow.py"]
# Confirmed-gate tools live in server.py and in tools/*.py (the maintenance
# pair), so the whole server tree is searched.
MCP_DIR = REPO_ROOT / "vmware_aria" / "mcp_server"

# ops write functions: must accept audit_logger AND call .log on it.
OPS_WRITE_FUNCTIONS: list[tuple[str, str]] = [
    ("alerts.py", "acknowledge_alert"),
    ("alerts.py", "cancel_alert"),
    ("alerts.py", "create_alert_definition"),
    ("alerts.py", "set_alert_definition_state"),
    ("alerts.py", "delete_alert_definition"),
    ("reports.py", "generate_report"),
    ("reports.py", "delete_report"),
    ("maintenance.py", "start_resource_maintenance"),
    ("maintenance.py", "end_resource_maintenance"),
    ("alert_notes.py", "add_alert_note"),
]

# MCP destructive tools: must gate on confirm=False preview.
MCP_CONFIRMED_TOOLS = [
    "acknowledge_alert",
    "cancel_alert",
    "delete_alert_definition",
    "delete_report",
    "start_resource_maintenance",
    "end_resource_maintenance",
]

# CLI destructive commands: must prompt via typer.confirm.
CLI_CONFIRM_COMMANDS = [
    "alert_acknowledge",
    "alert_cancel",
    "report_delete",
    "maintenance_start",
    "maintenance_end",
    "alert_note_add",
]


def _find_function(paths: Path | list[Path], func_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    candidates = [paths] if isinstance(paths, Path) else paths
    assert candidates, "no files to search — the check would find nothing"
    for path in candidates:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                return node
    raise AssertionError(f"{func_name} not found in {[str(p) for p in candidates]}")


@pytest.mark.unit
class TestOpsAuditLayer:
    """Layer 1: every ops write function takes audit_logger and logs."""

    @pytest.mark.parametrize("file_name,func_name", OPS_WRITE_FUNCTIONS)
    def test_write_op_audits(self, file_name: str, func_name: str) -> None:
        node = _find_function(OPS_DIR / file_name, func_name)
        arg_names = {a.arg for a in node.args.args + node.args.kwonlyargs}
        assert "audit_logger" in arg_names, (
            f"{func_name} in {file_name} must accept audit_logger (family audit rule)"
        )
        source = ast.dump(node)
        assert "audit_logger" in source and "log" in source, (
            f"{func_name} in {file_name} must call audit_logger.log(...)"
        )


@pytest.mark.unit
class TestMcpConfirmedLayer:
    """Layer 2: destructive MCP tools default to a confirm=False preview."""

    @pytest.mark.parametrize("func_name", MCP_CONFIRMED_TOOLS)
    def test_mcp_tool_has_confirm_gate(self, func_name: str) -> None:
        node = _find_function(sorted(MCP_DIR.rglob("*.py")), func_name)
        args = node.args.args
        defaults = dict(zip([a.arg for a in args][len(args) - len(node.args.defaults):], node.args.defaults))
        assert "confirm" in defaults, (
            f"MCP tool {func_name} must take confirm: bool = False "
            "(2026-06-08: delete_alert_definition/delete_report shipped without a gate)"
        )
        default = defaults["confirm"]
        assert isinstance(default, ast.Constant) and default.value is False, (
            f"MCP tool {func_name}: confirm must default to False — a bare call previews"
        )
        # Read the body without its docstring: the prose says "previews", and a
        # check that the docstring satisfies is a check of nothing.
        body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        source = "".join(ast.dump(n) for n in body)
        assert "resolve_confirm" in source and "gate" in source, (
            f"MCP tool {func_name} must route through the shared gate (write_gate.gate)"
        )


@pytest.mark.unit
class TestCliConfirmLayer:
    """Layer 3: destructive CLI commands prompt before executing."""

    @pytest.mark.parametrize("func_name", CLI_CONFIRM_COMMANDS)
    def test_cli_command_prompts(self, func_name: str) -> None:
        node = _find_function(CLI_PATHS, func_name)
        source = ast.dump(node)
        assert "confirm" in source, (
            f"CLI command {func_name} must prompt via typer.confirm "
            "(with --yes escape) before a destructive operation"
        )

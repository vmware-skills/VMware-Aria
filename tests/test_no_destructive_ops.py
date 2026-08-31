"""Safety boundary tests — verify write/destructive ops have layered guards.

Aria's safety architecture (per family spec — CLAUDE.md 安全规范: aria 写操作
要求审计日志；破坏性操作需确认门), enforced at the layer where each guard
actually lives:

1. **ops layer** — every write function accepts ``audit_logger`` and logs the
   operation (audit is mandatory family-wide).
2. **MCP layer** — every destructive tool has a ``confirmed: bool = False``
   parameter returning a preview until explicitly confirmed (MCP cannot
   prompt interactively).
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
CLI_PATH = REPO_ROOT / "vmware_aria" / "cli.py"
MCP_PATH = REPO_ROOT / "vmware_aria" / "mcp_server" / "server.py"

# ops write functions: must accept audit_logger AND call .log on it.
OPS_WRITE_FUNCTIONS: list[tuple[str, str]] = [
    ("alerts.py", "acknowledge_alert"),
    ("alerts.py", "cancel_alert"),
    ("alerts.py", "create_alert_definition"),
    ("alerts.py", "set_alert_definition_state"),
    ("alerts.py", "delete_alert_definition"),
    ("reports.py", "generate_report"),
    ("reports.py", "delete_report"),
]

# MCP destructive tools: must gate on confirmed=False preview.
MCP_CONFIRMED_TOOLS = [
    "acknowledge_alert",
    "cancel_alert",
    "delete_alert_definition",
    "delete_report",
]

# CLI destructive commands: must prompt via typer.confirm.
CLI_CONFIRM_COMMANDS = [
    "alert_acknowledge",
    "alert_cancel",
    "report_delete",
]


def _find_function(path: Path, func_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    raise AssertionError(f"{func_name} not found in {path}")


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
    """Layer 2: destructive MCP tools default to a confirmed=False preview."""

    @pytest.mark.parametrize("func_name", MCP_CONFIRMED_TOOLS)
    def test_mcp_tool_has_confirmed_gate(self, func_name: str) -> None:
        node = _find_function(MCP_PATH, func_name)
        arg_names = {a.arg for a in node.args.args + node.args.kwonlyargs}
        assert "confirmed" in arg_names, (
            f"MCP tool {func_name} must take confirmed: bool = False "
            "(2026-06-08: delete_alert_definition/delete_report shipped without it)"
        )
        source = ast.dump(node)
        assert "preview" in source, (
            f"MCP tool {func_name} must return a preview payload when not confirmed"
        )


@pytest.mark.unit
class TestCliConfirmLayer:
    """Layer 3: destructive CLI commands prompt before executing."""

    @pytest.mark.parametrize("func_name", CLI_CONFIRM_COMMANDS)
    def test_cli_command_prompts(self, func_name: str) -> None:
        node = _find_function(CLI_PATH, func_name)
        source = ast.dump(node)
        assert "confirm" in source, (
            f"CLI command {func_name} must prompt via typer.confirm "
            "(with --yes escape) before a destructive operation"
        )

"""Aria's irreversible CLI commands ask twice, and take no bypass flag.

The family's rule — a CLI command whose MCP tool is annotated
``destructiveHint=True`` prompts twice and offers no way to skip it — lived in
one repo's test rather than a family gate, so this repo asked once and offered
``--yes``. `delete_report`'s own tool description says "Irreversible".

A second prompt beside a documented ``--yes`` is decoration, which is why the
flag went with the fix rather than after it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from vmware_aria import cli


def _invoke(args: list[str], answers: str):
    with patch.object(cli, "_get_connection", return_value=(MagicMock(), MagicMock())), \
         patch.object(cli, "_audit", MagicMock()), \
         patch("vmware_aria.ops.reports.delete_report") as del_report, \
         patch("vmware_aria.ops.alerts.cancel_alert") as cancel:
        del_report.return_value = {"report_id": "r-1"}
        cancel.return_value = {"alert_id": "a-1"}
        result = CliRunner().invoke(cli.app, args, input=answers)
    return result, del_report, cancel


def test_report_delete_needs_both_answers() -> None:
    result, del_report, _ = _invoke(["report", "delete", "r-1"], "y\ny\n")
    assert result.exit_code == 0, result.output
    del_report.assert_called_once()


def test_report_delete_stops_at_the_second_prompt() -> None:
    """The guard is the abort. A prompt that prints and proceeds is decoration."""
    result, del_report, _ = _invoke(["report", "delete", "r-1"], "y\nn\n")
    assert result.exit_code != 0
    del_report.assert_not_called()


def test_alert_cancel_needs_both_answers() -> None:
    result, _, cancel = _invoke(["alert", "cancel", "a-1"], "y\ny\n")
    assert result.exit_code == 0, result.output
    cancel.assert_called_once()


def test_alert_cancel_stops_at_the_second_prompt() -> None:
    result, _, cancel = _invoke(["alert", "cancel", "a-1"], "y\nn\n")
    assert result.exit_code != 0
    cancel.assert_not_called()


def test_neither_command_offers_a_way_to_skip_the_prompts() -> None:
    """--yes on an irreversible command undoes the whole guard.

    Both carried one. Everywhere else in the family the bypass exists only on
    `mcp-config install`, which writes a local config file.
    """
    import ast
    import pathlib

    src = pathlib.Path(cli.__file__).read_bytes().decode("utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in ("alert_cancel", "report_delete"):
            continue
        if "--yes" in ast.unparse(node.args):
            offenders.append(node.name)
    assert not offenders, f"bypass flag reintroduced on: {offenders}"

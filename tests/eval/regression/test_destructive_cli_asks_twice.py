"""Aria's irreversible CLI commands ask twice — and keep their ``--yes``.

`delete_report`'s own tool description says "Irreversible" and the command asked
once, while the rest of the family asks twice for anything annotated
``destructiveHint=True``. That gap is what these tests close.

``--yes`` deliberately stays. Removing it was tried and reverted: it buys no
safety, because `yes | vmware-aria report delete r-1` satisfies any number of
prompts — this family's own capabilities.md says the prompts "defend the
mistyped command, not a determined caller". What it does buy is a broken script
for everyone who had automated the documented flag. Two prompts for the human,
one declared flag for the caller who has already decided.
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


def test_the_documented_bypass_still_works() -> None:
    """``--yes`` is documented in cli-reference.md and must keep working.

    It was removed in the first cut of this change on a consistency argument,
    and the argument was wrong: the flag adds no exposure that `yes |` does not
    already give, and removing it silently breaks every script that used it.
    Pinned so the same reasoning does not get re-applied later.
    """
    result, del_report, _ = _invoke(["report", "delete", "r-1", "--yes"], "")
    assert result.exit_code == 0, result.output
    del_report.assert_called_once()

    result, _, cancel = _invoke(["alert", "cancel", "a-1", "--yes"], "")
    assert result.exit_code == 0, result.output
    cancel.assert_called_once()

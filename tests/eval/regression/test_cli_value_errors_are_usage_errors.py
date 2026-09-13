"""A rejected argument must print one red line, not a traceback.

Live 2026-09-13: ``vmware-aria resource get ""`` printed a full Rich traceback.
The ops layer raises ``ValueError`` with the remedy in its message (e.g.
"resource_id must be a non-empty Aria resource UUID ... Run list_resources"),
but ``cli._friendly_errors`` caught only AriaApiError / config errors, so the
teaching message arrived buried under a stack trace.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
def test_empty_resource_id_is_a_one_line_usage_error(monkeypatch) -> None:
    from typer.testing import CliRunner

    import vmware_aria.cli as cli

    monkeypatch.setattr(cli, "_get_connection", lambda target, config: (MagicMock(), None))
    result = CliRunner().invoke(cli.app, ["resource", "get", ""], env={"COLUMNS": "200"})

    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    text = " ".join(result.output.split())
    assert "resource_id must be a non-empty" in text
    assert "Traceback" not in result.output

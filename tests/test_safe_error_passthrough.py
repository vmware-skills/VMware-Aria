"""A teaching message the agent never sees is not a teaching message.

``_safe_error`` reduces unrecognised exceptions to ``"<Class>: operation
failed."`` so raw suite-api text cannot leak. The allowlist it checks against
was an enumeration, and an enumeration drifts: ``OSError`` was missing from it,
so the one exception ``config.py`` raises — the missing-password error, this
family's most common first-run failure — reached an MCP agent as
``OSError: operation failed.``

That message's entire remedy is the env var name it carries, so redacting it
left the agent with a failure it could not act on and no way to discover the
fix. The defect was invisible from the CLI, which prints the message in full,
and invisible to the error-quality eval, which reads the message at the raise
site rather than what survives the wrapper.

So the rule is the inverse of an enumeration: every exception this skill raises
on purpose passes through, and only genuinely unplanned ones are reduced.
"""

from __future__ import annotations

import pytest

from vmware_aria.connection import AriaApiError
from vmware_aria.mcp_server._shared import _safe_error

TEACHING = "Resource 'vm-99' not found. List the parent collection first to get a valid UUID."

ENV_KEY = "VMWARE_ARIA_PROD_PASSWORD"
MISSING_PASSWORD = f"Password not found. Set environment variable: {ENV_KEY}"


def test_missing_password_keeps_the_env_var_name():
    """The single OSError config.py raises — and the whole point of it is the name."""
    out = _safe_error(OSError(MISSING_PASSWORD), "list_resources")
    assert ENV_KEY in out
    assert "operation failed" not in out


def test_aria_api_error_keeps_its_message():
    """The connection layer's teaching errors are the ones agents act on."""
    assert _safe_error(AriaApiError(TEACHING, status_code=404), "resource_get") == TEACHING


def test_not_an_aria_endpoint_keeps_its_hint():
    """connection.py raises ConnectionError when a host answers but is not suite-api."""
    msg = (
        "token acquisition succeeded but the response carried no 'token' field — "
        "verify 'host' and 'port', then run 'vmware-aria doctor'."
    )
    assert "vmware-aria doctor" in _safe_error(ConnectionError(msg), "t")


@pytest.mark.parametrize("exc_type", [ValueError, FileNotFoundError, KeyError, PermissionError])
def test_validation_errors_still_pass_through(exc_type):
    assert "vm-99" in _safe_error(exc_type(TEACHING), "t")


def test_unplanned_exceptions_are_still_reduced():
    """The redaction this allowlist exists for has to keep working."""
    out = _safe_error(RuntimeError("https://admin:hunter2@aria.internal/suite-api/api/auth"), "t")
    assert out == "RuntimeError: operation failed."
    assert "hunter2" not in out


def test_message_is_still_truncated():
    """Length capping is the other half of the guard."""
    assert len(_safe_error(AriaApiError("x" * 900), "t")) <= 300

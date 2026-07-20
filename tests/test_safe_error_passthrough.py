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

The first repair overshot. Admitting bare ``OSError`` admitted every OS-level
failure with it, and ``sanitize()`` only strips control characters and
truncates — it redacts nothing. ``socket.gaierror`` carries the hostname that
failed to resolve; neither it nor the TLS errors beside it were authored by this
package. So the passthrough is now the narrow ``ConfigError``, which is what
``config.py`` actually raises.
"""

from __future__ import annotations

import socket
import ssl

import pytest

from vmware_aria.config import ConfigError, TargetConfig
from vmware_aria.connection import AriaApiError
from vmware_aria.mcp_server._shared import _safe_error

TEACHING = "Resource 'vm-99' not found. List the parent collection first to get a valid UUID."

ENV_KEY = "VMWARE_ARIA_PROD_PASSWORD"
HOSTNAME = "aria-prod.corp.example.com"


def test_missing_password_keeps_the_env_var_name(monkeypatch):
    """The one error config.py raises — and the whole point of it is the name.

    Raised through ``get_password`` rather than fabricated, so the test pins the
    exception the package actually produces. Fabricating it was how the previous
    version kept passing while the real type changed.
    """
    monkeypatch.delenv(ENV_KEY, raising=False)
    with pytest.raises(ConfigError) as exc_info:
        TargetConfig(host="h", username="u").get_password("prod")

    out = _safe_error(exc_info.value, "list_resources")
    assert ENV_KEY in out
    assert "operation failed" not in out


def test_os_level_failures_no_longer_carry_the_hostname():
    """The reason the passthrough is ``ConfigError`` and not its base class.

    A DNS failure names the host it could not resolve. That text is the
    resolver's, not this package's, and it reaches the agent unredacted for as
    long as ``OSError`` is on the allowlist — which is what mutating this test
    demonstrates: put ``OSError`` back and this is the assertion that goes red.
    """
    out = _safe_error(socket.gaierror(8, f"nodename nor servname provided: {HOSTNAME}"), "t")
    assert out == "gaierror: operation failed."
    assert HOSTNAME not in out


def test_tls_errors_are_reduced_despite_inheriting_valueerror():
    """The reduction has to run *before* the allowlist, not inside it.

    ``ssl.SSLCertVerificationError`` inherits from ``ValueError`` as well as
    ``OSError``, and ``ValueError`` has been on the allowlist throughout — so
    removing ``OSError`` on its own changes nothing here. Its message quotes the
    certificate subject and the hostname it was checked against.
    """
    exc = ssl.SSLCertVerificationError(
        1,
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
        f"certificate in certificate chain, subject 'CN={HOSTNAME},O=Corp' (_ssl.c:1006)",
    )
    assert isinstance(exc, ValueError), "the co-inheritance this guard exists for is gone"

    out = _safe_error(exc, "list_resources")
    assert out == "SSLCertVerificationError: operation failed."
    assert HOSTNAME not in out


def test_config_error_is_still_an_oserror():
    """The CLI paths that predate the narrow type catch ``OSError``.

    Narrowing the MCP passthrough must not change what the CLI catches, or the
    same missing password that now teaches an agent would crash a terminal.
    """
    assert issubclass(ConfigError, OSError)


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

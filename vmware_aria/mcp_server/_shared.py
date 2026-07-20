"""Shared MCP plumbing for the vmware-aria tool modules.

The tool functions live in ``vmware_aria/mcp_server/tools/*.py`` grouped by domain
(resources, alerts, alert_definitions, capacity, anomaly, health, reports).
Each module registers its tools onto the single ``mcp`` instance defined here,
so this module must stay free of any import from the tool packages to avoid a
circular import (the tools import *from* ``_shared``, never the reverse).

What lives here:

* ``mcp`` — the one shared :class:`FastMCP` instance every tool registers on.
* ``_safe_error`` — agent-safe error stringifier (AriaApiError / ConnectionError
  teaching hints pass through; everything else is masked).
* ``_get_connection`` — lazy connection-manager helper.
* ``_target_name`` — audit display-name helper.
* ``_audit`` — the shared :class:`AuditLogger` for write tools.

``vmware_aria/mcp_server/server.py`` re-exports these (and every tool function) so the
historical import paths ``from vmware_aria.mcp_server.server import _safe_error, mcp, <fn>``
keep resolving.
"""


import logging
import os
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from vmware_policy import sanitize

from vmware_aria.config import load_config
from vmware_aria.connection import AriaApiError, ConnectionManager
from vmware_aria.notify.audit import AuditLogger

logger = logging.getLogger("vmware_aria.mcp_server")


def _safe_error(exc: Exception, tool: str) -> str:
    """Return an agent-safe error string; log full detail server-side only.

    Raw exception text can carry API response bodies, internal paths, or
    host:port pairs. Full traceback goes to the server log; the agent sees only
    a control-char-stripped, length-capped message.

    The rule is a property, not a list: every exception this skill raises on
    purpose passes through, and only genuinely unplanned ones are reduced. That
    covers the builtin validation errors, ``AriaApiError`` (the connection
    layer's teaching errors — "404: list the parent collection first", "503:
    platform booting"), and ``ConnectionError``, which ``connection.py`` raises
    when a host answers but is not an Aria suite-api endpoint.

    ``OSError`` is allowed because ``config.py`` raises exactly one — the
    missing-password error, this family's most common first-run failure, whose
    entire remedy is the env var name it carries. Its subclasses
    ``FileNotFoundError``, ``PermissionError`` and ``ConnectionError`` were
    already allowed, so admitting the base class widens exposure only to the
    remaining OS-level subtypes.

    Anything else is reduced to its type — an unplanned exception's text was
    written for a developer reading a traceback, not for an agent choosing what
    to do next, and it is the one that can carry credentials.
    """
    logger.error("Tool %s failed", tool, exc_info=True)
    if isinstance(
        exc,
        (AriaApiError, ValueError, FileNotFoundError, KeyError, PermissionError, ConnectionError, OSError),
    ):
        return sanitize(str(exc), 300)
    return f"{type(exc).__name__}: operation failed."


_audit = AuditLogger()

mcp = FastMCP(
    "vmware-aria",
    instructions=(
        "VMware Aria Operations (vRealize Operations) monitoring, alerting, and capacity planning. "
        "Query VM/host/cluster metrics, manage alerts, check capacity and rightsizing recommendations, "
        "detect anomalies, and monitor Aria platform health. "
        "For VM lifecycle operations use vmware-aiops. "
        "For NSX networking use vmware-nsx."
    ),
)

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

_conn_mgr: Optional[ConnectionManager] = None


def _get_connection(target: Optional[str] = None) -> Any:
    """Return an AriaClient, lazily initialising the connection manager."""
    global _conn_mgr  # noqa: PLW0603
    if _conn_mgr is None:
        config_path_str = os.environ.get("VMWARE_ARIA_CONFIG")
        config_path = Path(config_path_str) if config_path_str else None
        config = load_config(config_path)
        _conn_mgr = ConnectionManager(config)
    return _conn_mgr.connect(target)


def _target_name(target: Optional[str]) -> str:
    """Return display name for audit log entries."""
    return target or "default"

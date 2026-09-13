"""Pre-flight diagnostics for vmware-aria."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from rich.console import Console
from rich.table import Table
from vmware_policy.fsperms import check_secret_file

_log = logging.getLogger("vmware-aria.doctor")
console = Console()

#: A check that passed but deserves attention (e.g. a degraded platform that
#: still answers). It does not fail the pre-flight.
WARN = "warn"

#: Platform assessment -> check status. DEGRADED/UNKNOWN answer, so they warn;
#: only DOWN fails. See vmware_aria.ops.health.get_aria_health.
_ASSESSMENT_STATUS = {"HEALTHY": True, "DEGRADED": WARN, "UNKNOWN": WARN, "DOWN": False}


def _diagnosis(exc: Exception) -> str:
    """The error text without its "run the doctor" next step.

    Errors from the client end with a pointer to this command, which is right
    for a tool call and a loop inside the doctor's own report (2026-09-13).
    """
    return str(getattr(exc, "diagnosis", None) or exc)


def _platform_checks(name: str, client: object) -> list[tuple[str, object, str]]:
    """Version and platform-health rows for one connected target.

    The version comes from /versions/current, which answers during startup;
    the old probe of /deployment/node/status printed FAIL whenever the node
    was not fully ONLINE, and read a ``nodeType`` field NodeStatus never had.

    An unreadable version warns and does not fail the pre-flight: the health
    check treats it as context, and a low-privilege account (403) or a release
    name without a number says nothing about whether the target works.
    """
    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.health import get_aria_health, read_product_version

    rows: list[tuple[str, object, str]] = []
    version = read_product_version(client)
    if version["product_version"]:
        line = f"{version['release_name']} ({version['product_line']} line"
        build = version["build_number"]
        rows.append((f"Aria version ({name})", True, line + (f", build {build})" if build else ")")))
    else:
        rows.append((f"Aria version ({name})", WARN, f"Not read: {version['version_error']}"))

    try:
        health = get_aria_health(client)
    except AriaApiError as exc:
        rows.append((f"Aria platform ({name})", False, _diagnosis(exc)))
        return rows
    detail = f"{health['assessment']} — {health['details']}"
    failing = [
        f"{s['name']}: {s['details']}" for s in health["services"] or [] if s["name"] in (health["services_not_ok"] or [])
    ]
    if failing:
        detail += " " + "; ".join(failing)
    rows.append((f"Aria platform ({name})", _ASSESSMENT_STATUS[health["assessment"]], detail))
    return rows


def _target_checks(name: str, config: object) -> list[tuple[str, object, str]]:
    """Auth, version and platform rows for one target; always disconnects.

    Only a failure to connect is an auth failure. Anything that broke after the
    token was acquired used to fall into the auth handler, printing "Aria auth
    FAIL" under "Aria auth PASS — Token acquired" and skipping the disconnect
    (2026-09-13 review). It is the platform row now.
    """
    from vmware_aria.connection import ConnectionManager

    try:
        mgr = ConnectionManager(config)
        client = mgr.connect(name)
    except Exception as e:  # noqa: BLE001 — every connect failure is reported as a row
        return [(f"Aria auth ({name})", False, _diagnosis(e))]

    rows: list[tuple[str, object, str]] = [(f"Aria auth ({name})", True, "Token acquired")]
    try:
        rows.extend(_platform_checks(name, client))
    except Exception as e:  # noqa: BLE001 — reported as the platform row
        _log.exception("Platform checks for %s did not complete", name)
        rows.append((f"Aria platform ({name})", False, f"Checks did not complete: {_diagnosis(e)}"))
    finally:
        try:
            mgr.disconnect(name)
        except Exception as e:  # noqa: BLE001 — a failed disconnect must not hide the report
            _log.warning("Disconnect from %s failed: %s", name, e)
    return rows


def run_doctor(
    config_path: Path | None = None,
    skip_auth: bool = False,
) -> bool:
    """Run all pre-flight checks. Returns True if all pass."""
    from vmware_aria.config import (
        CONFIG_FILE,
        ENV_FILE,
        load_config,
        resolve_config_path,
    )

    checks: list[tuple[str, object, str]] = []

    # ── 1. Config file exists ────────────────────────────────────────────────
    # Resolved exactly as the tools resolve it — including $VMWARE_ARIA_CONFIG,
    # which this function used to skip. With the variable set it inspected
    # ~/.vmware-aria/config.yaml, found it fine, and reported PASS while every
    # tool call opened a different file (2026-08-30).
    path = resolve_config_path(config_path)
    if path.exists():
        checks.append(("Config file", True, str(path)))
    else:
        checks.append(
            (
                "Config file",
                False,
                f"Not found: {path}. Run `vmware-aria init` for guided setup, "
                f"or copy config.example.yaml to {CONFIG_FILE}",
            )
        )

    # ── 2. .env file permissions ─────────────────────────────────────────────
    if ENV_FILE.exists():
        try:
            # Three states, not two — see vmware_policy.fsperms. Windows has no
            # POSIX mode bits and `chmod 600` there exits 0 without changing
            # anything, so the old two-state check was permanently red with an
            # inert remedy.
            check = check_secret_file(ENV_FILE)
            checks.append((".env permissions", not check.is_failure, check.message))
        except OSError as e:
            checks.append((".env permissions", False, str(e)))
    else:
        checks.append((".env permissions", True, "No .env file (using shell env vars)"))

    # ── 3. Parse config / count targets ──────────────────────────────────────
    config = None
    try:
        config = load_config(path)
        target_count = len(config.targets)
        checks.append(("Config parse", True, f"{target_count} target(s) configured"))
    except Exception as e:
        checks.append(("Config parse", False, str(e)))

    if config is None:
        _print_table(checks)
        return False

    # ── 4. Password env vars set ─────────────────────────────────────────────
    for name, target_cfg in config.targets.items():
        try:
            _ = target_cfg.get_password(name)
            checks.append((f"Password ({name})", True, "Set"))
        except OSError as e:
            checks.append((f"Password ({name})", False, str(e)))

    # ── 5. Network connectivity (TCP to port 443) ────────────────────────────
    for name, target_cfg in config.targets.items():
        try:
            sock = socket.create_connection(
                (target_cfg.host, target_cfg.port),
                timeout=5,
            )
            sock.close()
            checks.append(
                (
                    f"Network ({name})",
                    True,
                    f"{target_cfg.host}:{target_cfg.port} reachable",
                )
            )
        except OSError as e:
            checks.append(
                (
                    f"Network ({name})",
                    False,
                    f"Cannot reach {target_cfg.host}:{target_cfg.port} - {e}",
                )
            )

    # ── 6 & 7. Aria Operations authentication, version, platform health ─────
    if not skip_auth:
        for name in config.targets:
            checks.extend(_target_checks(name, config))

    # ── 8. MCP server import check ───────────────────────────────────────────
    try:
        import vmware_aria.mcp_server.server  # noqa: F401

        checks.append(("MCP server import", True, "vmware_aria.mcp_server.server importable"))
    except ImportError as e:
        checks.append(("MCP server import", False, f"Import failed: {e}"))
    except Exception as e:
        checks.append(("MCP server import", False, str(e)))

    _print_table(checks)
    return all(passed is not False for _, passed, _ in checks)


def _print_table(checks: list[tuple[str, object, str]]) -> None:
    """Render the doctor results as a Rich table."""
    table = Table(title="vmware-aria Doctor", show_header=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    for name, passed, detail in checks:
        if passed == WARN:
            status = "[yellow]WARN[/yellow]"
        else:
            status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        table.add_row(name, status, detail)

    console.print(table)

"""Pre-flight diagnostics for vmware-aria."""

from __future__ import annotations

import logging
import socket
import stat
from pathlib import Path

from rich.console import Console
from rich.table import Table

_log = logging.getLogger("vmware-aria.doctor")
console = Console()


def _config_read_only() -> bool | None:
    """Best-effort read of ``read_only`` from the config file.

    Deliberately a copy of the helper in ``mcp_server.server`` rather than an
    import of it: importing that module registers every tool and applies the
    gate as a side effect, which would tie this check's result to whether the
    MCP server imports cleanly — something the doctor already checks separately.
    The two must be kept in step; if the server ever resolves its config
    differently, change both. Note ``load_config()`` is called with no argument
    on purpose, so it honours ``VMWARE_ARIA_CONFIG`` exactly as the gate does.
    """
    from vmware_aria.config import load_config

    try:
        return load_config().read_only
    except Exception:  # noqa: BLE001 — absent/unreadable config is not an error here
        return None


def _check_read_only() -> tuple[bool, str]:
    """Report the resolved read-only state and where it came from.

    Never fails — read-only being on is a posture, not a fault. It is here
    because an operator who set the switch had no way to confirm it took: the
    only signal was a line in the MCP server's start-up log.
    """
    from vmware_policy.readonly import read_only_status

    status = read_only_status("vmware-aria", _config_read_only())
    if not status.recognised:
        return True, (
            f"{status.source}={status.raw!r} is not a recognised value. It resolves "
            f"to ON (fail-closed), so every write tool is withheld — probably not "
            f"what was intended. Use true or false."
        )
    if status.enabled:
        return True, (
            f"ON (from {status.source}) — write tools are withheld from the MCP "
            f"registry. Clear that switch and restart the server to expose them."
        )
    return True, f"off (from {status.source}) — write tools are exposed"


def run_doctor(
    config_path: Path | None = None,
    skip_auth: bool = False,
) -> bool:
    """Run all pre-flight checks. Returns True if all pass."""
    from vmware_aria.config import CONFIG_FILE, ENV_FILE, load_config

    checks: list[tuple[str, bool, str]] = []

    # ── 1. Config file exists ────────────────────────────────────────────────
    path = config_path or CONFIG_FILE
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
            mode = ENV_FILE.stat().st_mode
            perms = stat.S_IMODE(mode)
            if perms & (stat.S_IRWXG | stat.S_IRWXO):
                checks.append(
                    (
                        ".env permissions",
                        False,
                        f"Permissions {oct(perms)} too open. Run: chmod 600 {ENV_FILE}",
                    )
                )
            else:
                checks.append((".env permissions", True, f"{oct(perms)} (owner-only)"))
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

    # ── 3b. Read-only mode ───────────────────────────────────────────────────
    # Reported before the early return below: the env-var switches work with no
    # config at all, so an operator with a broken config still needs to see
    # whether the deployment is locked down.
    checks.append(("Read-only mode", *_check_read_only()))

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

    # ── 6 & 7. Aria Operations authentication + version ──────────────────────
    if not skip_auth:
        for name, target_cfg in config.targets.items():
            try:
                from vmware_aria.connection import ConnectionManager

                mgr = ConnectionManager(config)
                client = mgr.connect(name)
                checks.append((f"Aria auth ({name})", True, "Token acquired"))

                # Get Aria version
                try:
                    # Health probe: an error status is itself the answer —
                    # skip the transient back-off so doctor stays snappy.
                    version_info = client.get("/deployment/node/status", retries=0)
                    node_type = version_info.get("nodeType", "unknown")
                    checks.append((f"Aria node type ({name})", True, node_type))
                except Exception as e:
                    checks.append((f"Aria node info ({name})", False, str(e)))

                mgr.disconnect(name)
            except Exception as e:
                checks.append((f"Aria auth ({name})", False, str(e)))

    # ── 8. MCP server import check ───────────────────────────────────────────
    try:
        import mcp_server.server  # noqa: F401

        checks.append(("MCP server import", True, "mcp_server.server importable"))
    except ImportError as e:
        checks.append(("MCP server import", False, f"Import failed: {e}"))
    except Exception as e:
        checks.append(("MCP server import", False, str(e)))

    _print_table(checks)
    return all(passed for _, passed, _ in checks)


def _print_table(checks: list[tuple[str, bool, str]]) -> None:
    """Render the doctor results as a Rich table."""
    table = Table(title="vmware-aria Doctor", show_header=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    for name, passed, detail in checks:
        status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        table.add_row(name, status, detail)

    console.print(table)

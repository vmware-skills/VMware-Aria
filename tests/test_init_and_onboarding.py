"""Regression tests for onboarding: the `vmware-aria init` wizard, the doctor
init reference (no false promise — 踩坑 #2), and teaching auth/TLS errors.

Aria is a REST/suite-api skill: config is a dict of targets and the password
env var is ``VMWARE_ARIA_<TARGET>_PASSWORD`` (see config.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vmware_aria import init_wizard


# ── init wizard ──────────────────────────────────────────────────────────────


@pytest.fixture
def _wizard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_dir = tmp_path / ".vmware-aria"
    monkeypatch.setattr(init_wizard, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(init_wizard, "CONFIG_FILE", cfg_dir / "config.yaml")
    monkeypatch.setattr(init_wizard, "ENV_FILE", cfg_dir / ".env")
    return cfg_dir


def _feed(monkeypatch: pytest.MonkeyPatch, answers: list[object], confirms: list[bool]) -> None:
    a = iter(answers)
    c = iter(confirms)
    monkeypatch.setattr(init_wizard.typer, "prompt", lambda *args, **kwargs: next(a))
    monkeypatch.setattr(init_wizard.typer, "confirm", lambda *args, **kwargs: next(c))


def test_init_writes_grep_safe_env(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_aria.config import _decode_secret

    # answers: target name, host, username, auth_source, port, password
    _feed(
        monkeypatch,
        answers=["lab-ops", "10.1.2.3", "admin", "LOCAL", 443, "S3cr3t!pw"],
        confirms=[True],  # verify_ssl
    )
    assert init_wizard.run_init(skip_test=True) == 0

    env_text = (_wizard_env / ".env").read_text(encoding="utf-8")
    assert "VMWARE_ARIA_LAB_OPS_PASSWORD=b64:" in env_text
    assert "S3cr3t!pw" not in env_text  # never plaintext on disk
    assert (_wizard_env / ".env").stat().st_mode & 0o777 == 0o600
    line = next(ln for ln in env_text.splitlines() if ln.startswith("VMWARE_ARIA_LAB_OPS_PASSWORD="))
    # b64: token round-trips back to the original password
    assert _decode_secret(line.split("=", 1)[1]) == "S3cr3t!pw"


def test_init_writes_dict_target_config(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    _feed(
        monkeypatch,
        answers=["prod", "aria.example.com", "admin", "LDAP", 8443, "pw"],
        confirms=[False],  # verify_ssl = False
    )
    assert init_wizard.run_init(skip_test=True) == 0

    raw = yaml.safe_load((_wizard_env / "config.yaml").read_text(encoding="utf-8"))
    # Aria config is a DICT of targets keyed by name, not a list.
    assert isinstance(raw["targets"], dict)
    assert raw["default_target"] == "prod"
    t = raw["targets"]["prod"]
    assert t == {
        "host": "aria.example.com",
        "username": "admin",
        "port": 8443,
        "verify_ssl": False,
        "auth_source": "LDAP",
    }


# ── doctor references a real init command (if referenced, must be registered) ──


def _init_registered() -> bool:
    from vmware_aria.cli import app

    # Typer derives an unnamed command's name from its callback at runtime, so
    # ``c.name`` is None here — match on the callback function name too.
    return any(c.name == "init" or getattr(c.callback, "__name__", None) == "init" for c in app.registered_commands)


def test_doctor_init_reference_is_backed_by_real_command():
    from vmware_aria import doctor

    src = Path(doctor.__file__).read_text(encoding="utf-8")
    if "vmware-aria init" in src:
        assert _init_registered(), "doctor recommends init but no such command is registered"


# ── auth/TLS errors teach where to fix the problem ───────────────────────────


def test_auth_hint_names_env_and_config_files():
    from vmware_aria.connection import _hint_for_status

    hint = _hint_for_status(401)
    assert ".vmware-aria/.env" in hint
    assert ".vmware-aria/config.yaml" in hint
    assert "VMWARE_ARIA_<TARGET>_PASSWORD" in hint


def test_tls_error_hint_suggests_verify_ssl():
    from vmware_aria.connection import _is_tls_verify_error

    assert _is_tls_verify_error(Exception("certificate verify failed: self signed"))
    assert not _is_tls_verify_error(Exception("Connection refused"))

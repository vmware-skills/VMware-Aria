"""Unit tests for vmware_aria.config."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from vmware_aria.config import AppConfig, TargetConfig, load_config


# ---------------------------------------------------------------------------
# TargetConfig tests
# ---------------------------------------------------------------------------


class TestTargetConfig:
    """Tests for TargetConfig dataclass and password retrieval."""

    def test_defaults(self) -> None:
        """TargetConfig should apply sensible defaults for optional fields."""
        t = TargetConfig(host="aria.example.com", username="admin")
        assert t.port == 443
        assert t.verify_ssl is True
        assert t.auth_source == "LOCAL"

    def test_frozen(self) -> None:
        """TargetConfig must be immutable (frozen dataclass)."""
        t = TargetConfig(host="aria.example.com", username="admin")
        with pytest.raises(Exception):
            t.host = "other.example.com"  # type: ignore[misc]

    def test_get_password_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_password should return value from env var following naming convention."""
        monkeypatch.setenv("VMWARE_ARIA_PROD_PASSWORD", "secret123")
        t = TargetConfig(host="aria.example.com", username="admin")
        assert t.get_password("prod") == "secret123"

    def test_get_password_hyphenated_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_password converts hyphens to underscores in env var name."""
        monkeypatch.setenv("VMWARE_ARIA_ARIA_LAB_PASSWORD", "labpass")
        t = TargetConfig(host="aria-lab.example.com", username="admin")
        assert t.get_password("aria-lab") == "labpass"

    def test_get_password_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_password should raise OSError when env var is not set."""
        monkeypatch.delenv("VMWARE_ARIA_PROD_PASSWORD", raising=False)
        t = TargetConfig(host="aria.example.com", username="admin")
        with pytest.raises(OSError, match="VMWARE_ARIA_PROD_PASSWORD"):
            t.get_password("prod")


# ---------------------------------------------------------------------------
# AppConfig tests
# ---------------------------------------------------------------------------


class TestAppConfig:
    """Tests for AppConfig lookup helpers."""

    def _make_config(self) -> AppConfig:
        targets = {
            "prod": TargetConfig(host="aria-prod.example.com", username="admin"),
            "lab": TargetConfig(host="10.0.0.100", username="admin", verify_ssl=False),
        }
        return AppConfig(targets=targets, default_target="prod")  # type: ignore[arg-type]

    def test_get_target_existing(self) -> None:
        cfg = self._make_config()
        t = cfg.get_target("lab")
        assert t is not None
        assert t.host == "10.0.0.100"

    def test_get_target_missing_returns_none(self) -> None:
        cfg = self._make_config()
        assert cfg.get_target("nonexistent") is None

    def test_get_target_strict_raises_on_missing(self) -> None:
        cfg = self._make_config()
        with pytest.raises(KeyError, match="ghost"):
            cfg.get_target_strict("ghost")

    def test_get_target_strict_includes_available(self) -> None:
        cfg = self._make_config()
        with pytest.raises(KeyError, match="prod"):
            cfg.get_target_strict("ghost")


# ---------------------------------------------------------------------------
# load_config tests
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for load_config from YAML file."""

    def test_load_minimal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_config should parse a minimal YAML file correctly."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            targets:
              prod:
                host: aria.example.com
                username: admin
            default_target: prod
        """), encoding="utf-8")
        monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(cfg_file))

        cfg = load_config(cfg_file)
        assert len(cfg.targets) == 1
        assert "prod" in cfg.targets
        assert cfg.targets["prod"].host == "aria.example.com"
        assert cfg.default_target == "prod"

    def test_load_all_fields(self, tmp_path: Path) -> None:
        """load_config should parse all optional fields including auth_source."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            targets:
              lab:
                host: 10.0.0.5
                username: localadmin
                port: 443
                verify_ssl: false
                auth_source: LDAP
            default_target: lab
        """), encoding="utf-8")

        cfg = load_config(cfg_file)
        t = cfg.targets["lab"]
        assert t.verify_ssl is False
        assert t.auth_source == "LDAP"
        assert t.port == 443

    def test_load_multiple_targets(self, tmp_path: Path) -> None:
        """load_config should handle multiple targets."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            targets:
              prod:
                host: prod.example.com
                username: admin
              staging:
                host: staging.example.com
                username: admin
            default_target: prod
        """), encoding="utf-8")

        cfg = load_config(cfg_file)
        assert len(cfg.targets) == 2
        assert "prod" in cfg.targets
        assert "staging" in cfg.targets

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        """load_config should raise FileNotFoundError for missing config."""
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError, match="nonexistent.yaml"):
            load_config(missing)

    def test_load_invalid_default_target_is_ignored(self, tmp_path: Path) -> None:
        """load_config should ignore a default_target that doesn't exist in targets."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            targets:
              prod:
                host: aria.example.com
                username: admin
            default_target: nonexistent
        """), encoding="utf-8")

        cfg = load_config(cfg_file)
        assert cfg.default_target is None

    def test_env_var_override_config_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VMWARE_ARIA_CONFIG env var should override default config path."""
        cfg_file = tmp_path / "custom.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            targets:
              env:
                host: env.example.com
                username: admin
        """), encoding="utf-8")
        monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(cfg_file))

        cfg = load_config()
        assert "env" in cfg.targets

    def test_load_empty_targets(self, tmp_path: Path) -> None:
        """load_config should handle a config with no targets."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("targets: {}\n", encoding="utf-8")

        cfg = load_config(cfg_file)
        assert len(cfg.targets) == 0
        assert cfg.default_target is None

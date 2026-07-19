"""A target must declare its environment for policy to scope rules by it.

Regression source: vmware-policy scopes rules by environment ("irreversible
work in production needs a second person"), but ``env`` used to be derived from
the *target's name*. Nobody names an Aria target the literal string
``production`` — they name it ``aria-prod`` — so every environment-scoped rule
was configured and inert. The environment is now an explicit declaration in
config.yaml::

    targets:
      prod:
        host: aria-ops.corp.local
        environment: production   # <- declares which rules apply

vmware-policy cannot read this skill's config itself, so ``vmware_aria/mcp_server/server.py``
registers a resolver at import. That registration is the whole control: without
it every target reads as undeclared and no environment-scoped rule can ever
fire. These tests pin both halves — the resolver is wired up, and what it
resolves reaches the policy decision.

Enforcement rolls out in two steps. The shipped baseline currently sets
``require_declared_environment: warn`` — an undeclared write runs and logs a
warning naming the fix. The next major release ships ``true`` and refuses it.
Both are pinned here (the ``baseline`` and ``enforcing`` fixtures) so that
release is a one-word change to a path already under test.
"""

import pytest
from vmware_policy.budget import reset_budget
from vmware_policy.decorators import PolicyDenied
from vmware_policy.environment import resolve_environment, set_environment_resolver
from vmware_policy.policy import reset_policy_engine

from vmware_aria.config import AppConfig, TargetConfig

import vmware_aria.mcp_server.server as server


@pytest.fixture(autouse=True)
def baseline(tmp_path, monkeypatch):
    """Point harness state at a tmp dir; no rules.yaml means the shipped baseline.

    That baseline is currently in its warn-only migration setting, so this is
    what an operator who has written no rules of their own gets today.
    """
    monkeypatch.setenv("OPS_HOME", str(tmp_path))
    monkeypatch.delenv("VMWARE_AUDIT_APPROVED_BY", raising=False)
    reset_policy_engine()
    reset_budget()
    yield
    # Restore the registration the server made at import, not None — leaving it
    # cleared would hand the rest of the session the unwired state these tests
    # exist to forbid.
    set_environment_resolver(server._environment_for)
    reset_policy_engine()
    reset_budget()


@pytest.fixture
def enforcing(tmp_path):
    """The same requirement switched on, as the next major release ships it."""
    (tmp_path / "rules.yaml").write_text("require_declared_environment: true\n")
    reset_policy_engine()


def _declare(monkeypatch, environment: str) -> None:
    """Register the real server resolver over a config declaring ``environment``."""
    config = AppConfig(
        targets={
            "prod-aria": TargetConfig(
                host="aria-ops.example.com",
                username="admin",
                environment=environment,
            )
        },
        default_target="prod-aria",
    )
    # Patch the mtime-cached loader the registered resolver calls, so the
    # resolver under test is the one the server actually installed — not a
    # stand-in.
    monkeypatch.setattr(server, "_cached_config", lambda: config)
    set_environment_resolver(server._environment_for)


@pytest.fixture
def stub_aria(monkeypatch):
    """Neutralise the suite-api calls; policy runs before the body either way."""
    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    monkeypatch.setattr(server, "_audit", None)


# ---------------------------------------------------------------------------
# The resolver is registered at all
# ---------------------------------------------------------------------------


def test_server_registers_an_environment_resolver(monkeypatch):
    """The silent-failure mode this change exists to remove.

    With no resolver every target reads as undeclared, so environment-scoped
    rules stay as inert as they were before — and nothing in the operator's
    config can fix it. It must be caught here rather than in the field.
    """
    _declare(monkeypatch, "lab")
    assert resolve_environment("prod-aria") == "lab"


def test_undeclared_target_resolves_to_empty(monkeypatch):
    _declare(monkeypatch, "")
    assert resolve_environment("prod-aria") == ""


def test_omitted_target_falls_back_to_the_default_target(monkeypatch):
    """Tools take ``target`` as optional; the default target's label must apply."""
    _declare(monkeypatch, "lab")
    assert resolve_environment("") == "lab"


def test_unknown_target_resolves_to_empty(monkeypatch):
    _declare(monkeypatch, "lab")
    assert resolve_environment("some-other-aria") == ""


# ---------------------------------------------------------------------------
# Migration window: undeclared writes warn, they do not break
# ---------------------------------------------------------------------------


def test_write_against_undeclared_target_warns_but_runs(monkeypatch, stub_aria):
    """The shipped setting is warn, so no existing install breaks on upgrade."""
    _declare(monkeypatch, "")
    result = server.acknowledge_alert(alert_id="alert-1", target="prod-aria")
    assert result.get("preview") is True


# ---------------------------------------------------------------------------
# Enforcing release: undeclared blocks writes, never reads
# ---------------------------------------------------------------------------


def test_write_against_undeclared_target_is_denied_when_enforcing(
    monkeypatch, enforcing, stub_aria
):
    _declare(monkeypatch, "")
    with pytest.raises(PolicyDenied) as excinfo:
        server.acknowledge_alert(alert_id="alert-1", target="prod-aria")
    assert excinfo.value.result.rule == "undeclared_environment"


def test_denial_names_the_config_key_to_add(monkeypatch, enforcing, stub_aria):
    """The error has to be actionable without opening the docs."""
    _declare(monkeypatch, "")
    with pytest.raises(PolicyDenied) as excinfo:
        server.acknowledge_alert(alert_id="alert-1", target="prod-aria")
    reason = str(excinfo.value)
    assert "environment" in reason
    assert "config.yaml" in reason


def test_write_against_declared_lab_target_succeeds(monkeypatch, enforcing, stub_aria):
    """Declaring an environment is what unblocks the work."""
    _declare(monkeypatch, "lab")
    result = server.acknowledge_alert(alert_id="alert-1", target="prod-aria")
    assert result.get("preview") is True


# ---------------------------------------------------------------------------
# Production additionally requires a named approver
# ---------------------------------------------------------------------------


def test_production_destructive_write_needs_an_approver(monkeypatch, stub_aria):
    """The two-person rule that existed in code but could never fire before."""
    _declare(monkeypatch, "production")
    with pytest.raises(PolicyDenied) as excinfo:
        server.delete_alert_definition(definition_id="def-1", target="prod-aria")
    assert "VMWARE_AUDIT_APPROVED_BY" in str(excinfo.value)


def test_production_destructive_write_runs_with_an_approver(
    monkeypatch, stub_aria
):
    _declare(monkeypatch, "production")
    monkeypatch.setenv("VMWARE_AUDIT_APPROVED_BY", "alice@corp")
    result = server.delete_alert_definition(definition_id="def-1", target="prod-aria")
    assert result.get("preview") is True


def test_lab_destructive_write_needs_no_approver(monkeypatch, stub_aria):
    """Only production carries the friction, or operators route around it."""
    _declare(monkeypatch, "lab")
    result = server.delete_alert_definition(definition_id="def-1", target="prod-aria")
    assert result.get("preview") is True


# ---------------------------------------------------------------------------
# Reads are never gated, under either setting
# ---------------------------------------------------------------------------


def test_read_against_undeclared_target_works(monkeypatch, stub_aria):
    """Inspection must keep working with no config change at all."""
    _declare(monkeypatch, "")
    monkeypatch.setattr(
        "vmware_aria.ops.alerts.list_alerts", lambda *a, **kw: [{"id": "alert-1"}]
    )
    assert server.list_alerts(target="prod-aria") == [{"id": "alert-1"}]


def test_read_against_undeclared_target_works_when_enforcing(
    monkeypatch, enforcing, stub_aria
):
    _declare(monkeypatch, "")
    monkeypatch.setattr(
        "vmware_aria.ops.alerts.list_alerts", lambda *a, **kw: [{"id": "alert-1"}]
    )
    assert server.list_alerts(target="prod-aria") == [{"id": "alert-1"}]

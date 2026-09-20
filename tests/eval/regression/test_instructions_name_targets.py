"""Regression: the MCP instructions name the configured targets, and how to pick one.

``initialize`` hands the client the server's ``instructions``, and for a skill
whose every tool takes ``target`` that string is the only place the client learns
which targets exist. While it was static, the model called tools with no target,
got whatever ``default_target`` happened to be, and answered confidently about
the wrong system: on 2026-09-15 Monitor's default was a standalone ESXi host, so
"how many VMs does the vCenter have" was answered from that host and "no vCenter
is configured" was reported for an estate that had one.

Three properties, because the failure has three shapes:

* the listing is real — the names and hosts in the operator's config appear;
* both marker phrases are present, so a client is told what exists *and* how to
  choose;
* a config that cannot be read does not stop the server from starting. The
  instructions are advisory; the tools report the config error themselves, with
  the remedy. This is the branch that must never raise.
"""

from __future__ import annotations

import pytest

from vmware_aria.config import AppConfig, TargetConfig
from vmware_aria.mcp_server import _shared

LISTING_MARKER = "Configured targets:"
RULE_MARKER = "Choosing a target:"


def _two_targets() -> AppConfig:
    """Two instances, the second the default — the shape the bug needed."""
    return AppConfig(
        targets={
            "lab-ops": TargetConfig(host="aria-lab.example.test", username="admin"),
            "prod-ops": TargetConfig(host="aria-prod.example.test", username="admin"),
        },
        default_target="prod-ops",
    )


def test_configured_target_names_and_hosts_appear(monkeypatch):
    monkeypatch.setattr(_shared, "load_config", _two_targets)
    text = _shared._target_instructions()

    assert "lab-ops (aria-lab.example.test)" in text
    assert "prod-ops (aria-prod.example.test, default)" in text
    # One listing, the two entries joined — not two sentences.
    assert (
        "Configured targets: lab-ops (aria-lab.example.test); "
        "prod-ops (aria-prod.example.test, default)." in text
    )
    # Only the default is labelled as such.
    assert text.count(", default)") == 1
    # The capability and routing sentences survive.
    assert "For VM lifecycle operations use vmware-aiops." in text


def test_both_markers_are_present(monkeypatch):
    monkeypatch.setattr(_shared, "load_config", _two_targets)
    text = _shared._target_instructions()

    assert LISTING_MARKER in text
    assert RULE_MARKER in text


def test_a_broken_config_still_yields_the_rule_and_never_raises(monkeypatch):
    def boom() -> AppConfig:
        raise FileNotFoundError("Config file not found: /nowhere/secret-path/config.yaml")

    monkeypatch.setattr(_shared, "load_config", boom)
    text = _shared._target_instructions()

    assert text, "a broken config must not leave the client with nothing"
    assert RULE_MARKER in text
    # Both markers stay, saying *why* there is no listing. Dropping the listing
    # silently is what lets a client read "this skill has no targets" off a skill
    # that simply could not read them — and that reading is the one that produces
    # a confident answer about a system nobody chose.
    assert LISTING_MARKER in text
    assert "could not be read (FileNotFoundError)" in text
    assert "For VM lifecycle operations use vmware-aiops." in text
    # No listing is invented, and the exception's text — which quotes the config
    # path — does not reach the client.
    assert "/nowhere/secret-path" not in text


def test_no_targets_configured_says_so_instead_of_listing_nothing(monkeypatch):
    monkeypatch.setattr(_shared, "load_config", lambda: AppConfig(targets={}))
    text = _shared._target_instructions()

    assert LISTING_MARKER in text
    assert RULE_MARKER in text
    assert "none yet" in text
    assert "~/.vmware-aria/config.yaml" in text
    # Not the empty listing "Configured targets: ." that reads as a truncation.
    assert "Configured targets: ." not in text


@pytest.mark.parametrize("raised", [ValueError("bad yaml"), OSError("permission denied")])
def test_no_loader_failure_escapes(monkeypatch, raised):
    """Whatever the loader raises, the server still gets a string.

    The guard is deliberately broad: a config file is operator-edited YAML, and
    every way it can be wrong must land on startup succeeding.
    """
    def boom() -> AppConfig:
        raise raised

    monkeypatch.setattr(_shared, "load_config", boom)
    assert RULE_MARKER in _shared._target_instructions()


def test_the_server_was_given_the_computed_listing_not_a_literal():
    """``instructions=`` must be the call, or the listing drifts from the config."""
    assert _shared.mcp.instructions == _shared._target_instructions()
    assert RULE_MARKER in (_shared.mcp.instructions or "")

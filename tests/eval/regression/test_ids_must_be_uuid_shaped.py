"""An id that is not one UUID is refused before it reaches Aria.

``require_alert_id`` / ``require_resource_id`` checked only that the value was
non-empty, so ``GREEN(100.0)`` (a pasted health cell) or two UUIDs joined by a
space went to the appliance and came back HTTP 400 — and a maintenance dry-run
showed ``ok`` for the joined pair. Every alert and resource id Aria issues is a
UUID, so the shape is checked locally, with an error that says what to paste.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

GOOD = "31aa1b41-2ae3-479b-8e25-176016ad874e"
OTHER = "ba793832-533a-4c87-bee4-217ed3747c85"
BAD = ["GREEN(100.0)", f"{GOOD} {OTHER}", GOOD[:-1], f"{GOOD}x", "vCenter-192.0.2.16", "31aa1b41"]


@pytest.mark.unit
@pytest.mark.parametrize("value", BAD)
def test_require_alert_id_refuses_what_is_not_one_uuid(value):
    from vmware_aria.ops.alert_notes import require_alert_id

    with pytest.raises(ValueError) as caught:
        require_alert_id(value)
    message = str(caught.value)
    assert "UUID" in message and "list_alerts" in message


@pytest.mark.unit
@pytest.mark.parametrize("value", BAD)
def test_require_resource_id_refuses_what_is_not_one_uuid(value):
    from vmware_aria.ops.maintenance import require_resource_id

    with pytest.raises(ValueError) as caught:
        require_resource_id(value)
    message = str(caught.value)
    assert "UUID" in message and "list_resources" in message


@pytest.mark.unit
def test_two_joined_uuids_are_named_as_such():
    from vmware_aria.ops.maintenance import require_resource_id

    with pytest.raises(ValueError, match="2 UUIDs"):
        require_resource_id(f"{GOOD} {OTHER}")


@pytest.mark.unit
@pytest.mark.parametrize("value", [GOOD, f"  {GOOD}\n", GOOD.upper()])
def test_one_uuid_is_accepted_and_stripped(value):
    from vmware_aria.ops.alert_notes import require_alert_id
    from vmware_aria.ops.maintenance import require_resource_id

    assert require_alert_id(value) == value.strip()
    assert require_resource_id(value) == value.strip()


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "   ", None, 42])
def test_empty_or_non_text_is_still_refused(value):
    from vmware_aria.ops.maintenance import require_resource_id

    with pytest.raises(ValueError, match="UUID"):
        require_resource_id(value)


def _calls(fn_path: str, *args):
    module_name, fn_name = fn_path.rsplit(".", 1)
    import importlib

    fn = getattr(importlib.import_module(module_name), fn_name)
    client = MagicMock(name="AriaClient")
    with pytest.raises(ValueError, match="UUID"):
        fn(client, *args)
    return client


@pytest.mark.unit
@pytest.mark.parametrize(
    "fn_path, args",
    [
        ("vmware_aria.ops.alerts.get_alert", ("GREEN(100.0)",)),
        ("vmware_aria.ops.investigate.investigate_alert", ("GREEN(100.0)",)),
        ("vmware_aria.ops.resources.get_resource", ("GREEN(100.0)",)),
        ("vmware_aria.ops.resources.get_resource_health", ("GREEN(100.0)",)),
        ("vmware_aria.ops.resources.get_resource_metrics", ("GREEN(100.0)", ["badge|health"])),
        ("vmware_aria.ops.catalog.get_resource_properties", ("GREEN(100.0)",)),
        ("vmware_aria.ops.catalog.get_resource_relationships", ("GREEN(100.0)",)),
    ],
)
def test_read_paths_refuse_a_malformed_id_without_calling_aria(fn_path, args):
    client = _calls(fn_path, *args)

    assert client.method_calls == []

"""A 404 from a 9.x-only endpoint must not be explained as a bad id.

Before this, ``list_fleet_certificates`` against an 8.x appliance produced:

    Aria Operations returned HTTP 404. Verify the id — list the parent
    collection first ... and copy an exact UUID.

There is no id in that call. The operator was sent to check something that was
never wrong, while the actual cause — the endpoint does not exist below VCF
Operations 9.0 — was never mentioned.
"""

from __future__ import annotations

import httpx
import pytest

from vmware_aria.connection import AriaApiError, AriaClient


def _client(monkeypatch, status: int, version: str | None) -> AriaClient:
    """An AriaClient whose transport answers `status`, reporting `version`."""
    client = AriaClient.__new__(AriaClient)
    client._product_version = version
    client._target = type("T", (), {"host": "ops.example.test"})()
    client._headers = lambda: {}

    def _request(method, path, **kw):
        return httpx.Response(status, request=httpx.Request(method, f"https://x{path}"))

    client._client = type("C", (), {"request": staticmethod(lambda m, p, **k: _request(m, p))})()
    return client


def test_404_on_an_8x_appliance_names_the_version_and_clears_the_id(monkeypatch):
    from vmware_aria.ops import fleet

    client = _client(monkeypatch, 404, "8.6.4")
    with pytest.raises(AriaApiError) as exc:
        fleet.list_fleet_certificates(client)

    msg = str(exc.value)
    assert "8.6.4" in msg and "9.0" in msg
    assert "id you passed is not the problem" in msg
    assert "copy an exact UUID" not in msg, "the misleading remedy is still being sent"


def test_404_on_a_9x_appliance_keeps_the_ordinary_remedy(monkeypatch):
    """The floor is met, so a 404 there is a real 404 — not a version story.

    Without this, every 404 on a fleet call would tell a 9.1 operator to upgrade
    to 9.0, burying whatever actually went wrong.
    """
    from vmware_aria.ops import fleet

    client = _client(monkeypatch, 404, "9.1.0.0200")
    with pytest.raises(AriaApiError) as exc:
        fleet.list_fleet_certificates(client)

    msg = str(exc.value)
    assert "copy an exact UUID" in msg
    assert "9.0 or newer" not in msg


def test_404_with_an_unreadable_version_still_names_the_floor(monkeypatch):
    """Third state: say what the capability needs without judging their build."""
    from vmware_aria.ops import fleet

    client = _client(monkeypatch, 404, None)
    with pytest.raises(AriaApiError) as exc:
        fleet.list_fleet_certificates(client)

    msg = str(exc.value)
    assert "could not be read" in msg
    assert "9.0+" in msg


def test_a_non_404_is_untouched_by_the_version_floor(monkeypatch):
    """Only 404 means "endpoint absent". A 503 is the platform booting."""
    from vmware_aria.ops import fleet

    client = _client(monkeypatch, 500, "8.6.4")
    with pytest.raises(AriaApiError) as exc:
        fleet.list_fleet_certificates(client)
    assert "9.0" not in str(exc.value)

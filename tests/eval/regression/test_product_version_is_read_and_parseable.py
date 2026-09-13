"""The appliance version must be readable, and readable as a version.

2026-09-13, real Aria Operations 8.18.7. ``/versions/current`` answers
``releaseName: "VMware Aria Operations 8.18.7"`` even while node status is 503.

``AriaClient.product_version()`` returned that whole string, and
``vmware_policy.compat.parse_version`` reads versions from the front — so it
parsed to ``None``, and every version-floor 404 on this appliance said "the
running version could not be read" while the version was sitting right there.

Also pinned here: an ``AriaApiError`` keeps the response body, because the 503
from node status carries ``systemTime`` and the error used to drop it.
"""

from __future__ import annotations

import httpx
import pytest
from vmware_policy.compat import Requires, parse_version, version_remedy

from tests.eval.regression._live_8187 import body
from vmware_aria.connection import _UNPROBED, AriaApiError, AriaClient, parse_release_info


def _client(answer) -> AriaClient:
    client = AriaClient.__new__(AriaClient)
    client._product_version = _UNPROBED
    client._target = type("T", (), {"host": "ops.example.test"})()
    client._headers = lambda: {}
    client._client = type("C", (), {"request": staticmethod(answer)})()
    return client


@pytest.mark.unit
def test_release_name_parses_to_a_comparable_version():
    info = parse_release_info(body("versions_current.json"))

    assert info["release_name"] == "VMware Aria Operations 8.18.7"
    assert info["product_name"] == "VMware Aria Operations"
    assert info["product_version"] == "8.18.7"
    assert info["product_line"] == "8.x"
    assert parse_version(info["product_version"]) == (8, 18, 7)


@pytest.mark.unit
@pytest.mark.parametrize("payload", [{}, {"releaseName": ""}, {"releaseName": "Operations"}, "nope"])
def test_no_version_in_the_release_name_is_none_not_a_guess(payload):
    info = parse_release_info(payload)

    assert info["product_version"] is None
    assert info["product_line"] is None


@pytest.mark.unit
def test_client_product_version_feeds_the_version_floor_a_real_number():
    def answer(method, path, **_kw):
        assert path == "/versions/current"
        return httpx.Response(200, json=body("versions_current.json"), request=httpx.Request(method, "https://x"))

    client = _client(answer)
    detected = client.product_version()

    assert detected == "8.18.7"
    remedy = version_remedy(Requires("VCF Operations", (9, 0), "fleet certificate inventory"), detected)
    assert "reports 8.18.7" in remedy, "a readable 8.18.7 must not be explained as 'could not be read'"


@pytest.mark.unit
def test_api_error_keeps_the_json_response_body():
    payload = body("node_status_503.json")

    def answer(method, path, **_kw):
        return httpx.Response(503, json=payload, request=httpx.Request(method, "https://x"))

    client = _client(answer)
    with pytest.raises(AriaApiError) as exc:
        client.get("/deployment/node/status", retries=0)

    assert exc.value.body == payload


@pytest.mark.unit
def test_api_error_body_is_none_for_a_non_json_response():
    def answer(method, path, **_kw):
        return httpx.Response(404, text="<html>Page Not Found</html>", request=httpx.Request(method, "https://x"))

    client = _client(answer)
    with pytest.raises(AriaApiError) as exc:
        client.get("/deployment/node/clusterstatus", retries=0)

    assert exc.value.body is None

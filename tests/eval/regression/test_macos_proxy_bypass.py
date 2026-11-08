"""A macOS proxy bypass must cover the Aria HTTP client, including login."""

from __future__ import annotations

import time
import urllib.request

import httpx

from vmware_aria import connection
from vmware_aria.config import TargetConfig


def test_system_proxy_bypass_routes_aria_auth_directly(monkeypatch):
    """httpx sees system proxies but does not apply macOS's bypass list."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host == "aria.invalid")

    seen = []

    def direct(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/auth/token/acquire"):
            return httpx.Response(
                200,
                json={"token": "test-token", "validity": int((time.time() + 3600) * 1000)},
            )
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        connection.httpx,
        "HTTPTransport",
        lambda **_kwargs: httpx.MockTransport(direct),
    )

    client = connection.AriaClient(
        TargetConfig(host="aria.invalid", username="admin", verify_ssl=False),
        "test-password",
    )
    try:
        assert client._token == "test-token"
        assert client._client.get("https://aria.invalid/suite-api/api/resources").status_code == 200
        assert any(path.endswith("/auth/token/acquire") for path in seen)
        assert any(path.endswith("/resources") for path in seen)
    finally:
        client.close()

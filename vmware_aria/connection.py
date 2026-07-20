"""Aria Operations REST API client with token-based authentication.

Authenticates via POST /suite-api/api/auth/token/acquire with username/password/authSource.
Stores the acquired token and re-acquires it automatically near expiry. Subsequent
requests carry it as ``Authorization: vRealizeOpsToken <token>`` — the documented
header literal on all versions including 8.6 (the ``OpsToken`` literal only appears
in newer Aria-branded docs and 401s on 8.6). Per the official spec, the token has a
6-hour sliding validity ("extended after each call and set to 6 hours from the last
call") and the acquire response's `validity` field is an epoch timestamp in
milliseconds — NOT a duration.

Base URL pattern: https://<aria-host>/suite-api/api/
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from vmware_aria.config import AppConfig, TargetConfig, load_config

_log = logging.getLogger("vmware-aria.connection")

# Token validity buffer: refresh 60 seconds before actual expiry
_EXPIRY_BUFFER_SEC = 60

# Transient gateway statuses worth one automatic retry (the node may be busy
# or a service may still be coming up). 4xx client errors are NOT retried.
_TRANSIENT_STATUS = frozenset({502, 503, 504})
_RETRY_DELAY_SEC = 2.0

# How long a successful liveness probe is trusted before re-probing. Every MCP
# tool call goes through connect(); without this, each one fires a full
# is_alive() HTTP round-trip to /deployment/node/status. A short TTL lets bursts
# of back-to-back calls reuse the cached client without re-probing, while still
# catching a dropped session within a few seconds.
_LIVENESS_TTL_SEC = 30.0


class AriaApiError(Exception):
    """An Aria Operations suite-api call returned an error or failed to connect.

    Carries a teaching message (status + path + how to fix) so end users see an
    actionable line instead of a raw httpx traceback. ``status_code`` is None
    for transport/timeout failures (no HTTP response was received).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.path = path


def _hint_for_status(status_code: int, path: str) -> str:
    """Return a short, actionable remediation hint for an HTTP error status."""
    if status_code == 404:
        return (
            f"Nothing exists at {path}. Verify the id — list the parent "
            "collection first (e.g. `vmware-aria resource list`, "
            "`vmware-aria alert list`, or the list_resources / list_alerts "
            "tools) and copy an exact UUID."
        )
    if status_code == 400:
        return (
            "Bad request — check the parameters and payload for this call "
            "against the tool's parameter descriptions; a malformed UUID or an "
            "out-of-range value is the usual cause."
        )
    if status_code == 503:
        return (
            "The platform is starting up or one or more services are not "
            "ONLINE. Wait for the cluster to finish booting and retry."
        )
    if status_code in (502, 504):
        return "The node is busy or a gateway timed out — retry shortly."
    if status_code >= 500:
        return "Server-side error — retry shortly; check Aria Operations health."
    if status_code in (401, 403):
        return (
            "Authentication/authorization failed — check username/auth_source in "
            "~/.vmware-aria/config.yaml and the password env var "
            "(VMWARE_ARIA_<TARGET>_PASSWORD) in ~/.vmware-aria/.env, plus the "
            "account's role."
        )
    return "Check the request and try again."


def _is_tls_verify_error(exc: Exception) -> bool:
    """True if a transport error looks like a TLS certificate verification failure."""
    text = str(exc).lower()
    return "certificate" in text or "ssl" in text or "verify" in text


class AriaClient:
    """REST client for a single Aria Operations instance."""

    def __init__(
        self, target: TargetConfig, password: str, username: str | None = None
    ) -> None:
        self._target = target
        self._base_url = f"https://{target.host}:{target.port}/suite-api/api"
        self._password = password
        # Resolved by the caller (ConnectionManager) alongside the password so
        # both halves of the credential come from the same read; falls back to
        # the configured username for direct construction.
        self._username = username or target.username
        self._token: str | None = None
        # Epoch seconds when the token expires
        self._token_expires_at: float = 0.0
        # Epoch seconds of the last is_alive() that returned True; gates the
        # liveness probe so a burst of connect() calls doesn't re-probe each time.
        self._liveness_checked_at: float = 0.0

        # Suppress urllib3's InsecureRequestWarning for self-signed certs.
        # urllib3.disable_warnings is class-targeted and idempotent; it avoids
        # the process-global side-effects of warnings.filterwarnings().
        if not target.verify_ssl:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._client = httpx.Client(
            base_url=self._base_url,
            verify=target.verify_ssl,
            timeout=30.0,
        )
        self._acquire_token()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _acquire_token(self) -> None:
        """Acquire a new OpsToken from Aria Operations.

        Token acquisition errors are translated into ``AriaApiError`` here —
        this runs both at connect time and mid-request (refresh inside
        ``_request()``), and a bare ``raise_for_status()`` would leak a raw
        httpx traceback on a wrong password (401), bad authSource (400), or a
        node that is still booting (503).
        """
        url = f"{self._base_url}/auth/token/acquire"
        payload = {
            "username": self._username,
            "password": self._password,
            "authSource": self._target.auth_source,
        }
        try:
            resp = self._client.post(url, json=payload, headers={"Accept": "application/json"})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (400, 401, 403):
                hint = (
                    "Check username/password/authSource in "
                    "~/.vmware-aria/config.yaml and the password env var in "
                    "~/.vmware-aria/.env."
                )
            else:
                hint = _hint_for_status(status, "/auth/token/acquire")
            raise AriaApiError(
                f"Aria Operations authentication to {self._target.host} failed: "
                f"POST /auth/token/acquire returned HTTP {status}. {hint} "
                f"Run 'vmware-aria doctor' to re-test this target's credentials "
                f"and connectivity.",
                status_code=status,
                method="POST",
                path="/auth/token/acquire",
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            tail = (
                " The certificate could not be verified — for a self-signed lab "
                "cert set `verify_ssl: false` for this target in "
                "~/.vmware-aria/config.yaml."
                if _is_tls_verify_error(exc)
                else " Check the host/port and network, then retry."
            )
            raise AriaApiError(
                f"Aria Operations authentication to {self._target.host} could not "
                f"connect: {exc}.{tail} Verify 'host' and 'port' for this target in "
                f"~/.vmware-aria/config.yaml, then run 'vmware-aria doctor'.",
                method="POST",
                path="/auth/token/acquire",
            ) from exc
        data = resp.json()

        token = data.get("token")
        if not token:
            raise ConnectionError(
                f"Aria Operations token acquisition to {self._target.host} "
                f"succeeded but the response carried no 'token' field — the host "
                f"is most likely not an Aria Operations suite-api endpoint. "
                f"Verify 'host' and 'port' for this target in "
                f"~/.vmware-aria/config.yaml, then run 'vmware-aria doctor'."
            )

        # `validity` is an epoch timestamp in MILLISECONDS (when the token
        # expires), not a duration. Default validity is 6 hours, sliding —
        # the server extends it on every call. 2026-06-08 user report: the
        # old code treated it as a duration (now + validity), producing an
        # expiry ~56 years in the future, so the token never refreshed and
        # sessions longer than 6h idle died with 401.
        validity_epoch_ms = data.get("validity")
        self._token = token
        if validity_epoch_ms:
            self._token_expires_at = validity_epoch_ms / 1000.0
        else:
            self._token_expires_at = time.time() + 6 * 3600
        _log.info(
            "Aria Operations token acquired for %s (expires in %.0fs)",
            self._target.host,
            self._token_expires_at - time.time(),
        )

    def _ensure_token(self) -> None:
        """Re-acquire token if expired or near expiry."""
        if time.time() >= (self._token_expires_at - _EXPIRY_BUFFER_SEC):
            _log.info("Token expired or near expiry, re-acquiring...")
            self._acquire_token()

    def _headers(self) -> dict[str, str]:
        """Request headers with vRealizeOpsToken authorization."""
        self._ensure_token()
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"vRealizeOpsToken {self._token}",
        }

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        retries: int = 1,
    ) -> httpx.Response:
        """Send one request, recovering from auth and transient failures.

        Layered per the error-recovery contract: (1) transport/timeout and
        transient gateway statuses (502/503/504) are retried once after a short
        delay; (2) a 401/403 triggers a single token re-acquisition; (3) any
        remaining error status is translated into an ``AriaApiError`` carrying a
        teaching message, so callers never surface a raw httpx traceback. 4xx
        client errors (e.g. 404 for a bad id) are NOT retried.
        """
        attempt = 0
        reauthed = False
        while True:
            try:
                resp = self._client.request(method, path, headers=self._headers(), params=params, json=json_data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < retries:
                    attempt += 1
                    time.sleep(_RETRY_DELAY_SEC)
                    continue
                tail = (
                    " The certificate could not be verified — for a self-signed "
                    "lab cert set `verify_ssl: false` for this target in "
                    "~/.vmware-aria/config.yaml."
                    if _is_tls_verify_error(exc)
                    else " Check the host/port and network, then retry."
                )
                raise AriaApiError(
                    f"Aria Operations {method} {path} could not connect: {exc}.{tail} "
                    f"Verify 'host' and 'port' for this target in "
                    f"~/.vmware-aria/config.yaml, then run 'vmware-aria doctor'.",
                    method=method,
                    path=path,
                ) from exc

            if resp.status_code in (401, 403) and not reauthed:
                # Re-acquire the token once, then re-issue through the top of
                # the loop so the retry is covered by the same transport-error
                # handling (the `reauthed` flag bounds this to a single retry).
                _log.info("Auth error on %s %s, re-acquiring token...", method, path)
                self._acquire_token()
                reauthed = True
                continue

            if resp.status_code in _TRANSIENT_STATUS and attempt < retries:
                attempt += 1
                time.sleep(_RETRY_DELAY_SEC)
                continue

            if resp.status_code >= 400:
                raise AriaApiError(
                    f"Aria Operations {method} {path} returned HTTP "
                    f"{resp.status_code}. {_hint_for_status(resp.status_code, path)} "
                    f"Run 'vmware-aria doctor' if every call to this target fails.",
                    status_code=resp.status_code,
                    method=method,
                    path=path,
                )
            return resp

    def get(self, path: str, params: dict[str, Any] | None = None, *, retries: int = 1) -> dict:
        """Single GET request. Returns parsed JSON response.

        Pass retries=0 for probes where an error status is itself the answer
        (e.g. a health check reading a 503 as "not ONLINE") to skip the
        transient back-off.
        """
        resp = self._request("GET", path, params=params, retries=retries)
        return resp.json() if resp.content else {}

    def post(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        retries: int = 0,
    ) -> dict:
        """POST request. Returns parsed JSON response.

        Defaults to retries=0: POST is not idempotent in general (e.g.
        creating an alert definition or queueing a report), so a transient
        502/504 after the server already accepted the request must not be
        replayed — that would duplicate the side effect. Idempotent callers
        (pure query endpoints like /alerts/query) opt in with retries=1.
        """
        resp = self._request("POST", path, params=params, json_data=json_data, retries=retries)
        return resp.json() if resp.content else {}

    def put(self, path: str, json_data: dict[str, Any] | None = None) -> dict:
        """PUT request. Returns parsed JSON response."""
        resp = self._request("PUT", path, json_data=json_data)
        return resp.json() if resp.content else {}

    def delete(self, path: str) -> None:
        """DELETE request."""
        self._request("DELETE", path)

    def is_alive(self) -> bool:
        """Check if the cached client + token are still usable.

        A reachable node that returns 5xx (e.g. 503 while still booting) is
        still "alive": the client and token work, the platform just isn't
        ready, so there's no point dropping and rebuilding the connection. Only
        auth failures (401/403) or transport errors mean the cached client is
        stale. retries=0 keeps the probe snappy — no back-off on every
        connect().
        """
        try:
            self._request("GET", "/deployment/node/status", retries=0)
            self._liveness_checked_at = time.time()
            return True
        except AriaApiError as exc:
            return exc.status_code is not None and exc.status_code not in (401, 403)
        except Exception:
            return False

    def is_alive_cached(self, ttl: float = _LIVENESS_TTL_SEC) -> bool:
        """Liveness check that skips the HTTP probe within ``ttl`` of the last success.

        connect() runs on every MCP tool call, so probing /deployment/node/status
        each time is wasteful for back-to-back calls. If the last is_alive() probe
        succeeded within ``ttl`` seconds, trust it and return True without a round
        trip. On a cache miss (or after the TTL expires) fall through to the real
        is_alive(), which re-probes and refreshes the timestamp on success. A
        probe failure does NOT update the timestamp, so the next call re-probes.
        """
        if self._liveness_checked_at and (time.time() - self._liveness_checked_at) < ttl:
            return True
        return self.is_alive()

    def close(self) -> None:
        """Release the auth token and close the HTTP client."""
        if self._token:
            try:
                # POST /auth/token/release takes no body — the token to
                # release is identified by the Authorization header.
                self._client.post(
                    "/auth/token/release",
                    headers=self._headers(),
                    json=None,
                )
            except Exception:
                pass
            finally:
                self._token = None
        self._client.close()


class ConnectionManager:
    """Manages connections to multiple Aria Operations targets."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._clients: dict[str, AriaClient] = {}

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> ConnectionManager:
        """Create a ConnectionManager from config, loading defaults if needed."""
        cfg = config or load_config()
        return cls(cfg)

    def connect(self, target_name: str | None = None) -> AriaClient:
        """Get or create an AriaClient for the specified target."""
        name = target_name or self._config.default_target
        if not name:
            configured = ", ".join(self._config.targets.keys())
            raise ValueError(
                f"No target specified and no default target configured. "
                f"Configured targets: {configured or '(none)'}. Pass target=<name> "
                f"explicitly, or set 'default_target' in ~/.vmware-aria/config.yaml."
            )

        if name in self._clients:
            if self._clients[name].is_alive_cached():
                return self._clients[name]
            # Stale client: release its token and close the HTTP connection
            # pool before replacing it, so sockets don't leak across reconnects.
            self._clients[name].close()
            del self._clients[name]

        target_cfg = self._config.get_target(name)
        if target_cfg is None:
            available = ", ".join(self._config.targets.keys())
            raise ValueError(
                f"Target '{name}' not found. Available: {available or '(none)'}. "
                f"Pass --target with one of those names, or add a '{name}' entry "
                f"under 'targets:' in ~/.vmware-aria/config.yaml and re-run."
            )

        # Resolve both halves of the credential together — a username left
        # behind by a rotation would pair with the new password and fail.
        password = target_cfg.get_password(name)
        username = target_cfg.get_username(name)
        client = AriaClient(target_cfg, password, username)
        self._clients[name] = client
        return client

    def disconnect(self, target_name: str) -> None:
        """Close and remove a client."""
        if target_name in self._clients:
            self._clients[target_name].close()
            del self._clients[target_name]

    def disconnect_all(self) -> None:
        """Disconnect from all targets."""
        for name in list(self._clients):
            self.disconnect(name)

    def list_targets(self) -> list[str]:
        """List available target names."""
        return list(self._config.targets.keys())

    def list_connected(self) -> list[str]:
        """List currently connected target names."""
        return list(self._clients.keys())

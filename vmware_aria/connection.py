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
import re
import time
from typing import Any

import httpx
from vmware_policy import sanitize
from vmware_policy.compat import Requires, version_remedy

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


#: The next step other callers are given when a call fails. Kept separate from
#: the diagnosis so the doctor can print the diagnosis without it: a doctor
#: report that says "run the doctor" is a loop, not a remedy (2026-09-13).
_DOCTOR_POINTER = "Run 'vmware-aria doctor' if every call to this target fails."
_DOCTOR_THEN = "Then run 'vmware-aria doctor'."

#: A dotted version inside a release name: "VMware Aria Operations 8.18.7".
_DOTTED_VERSION = re.compile(r"\d+(?:\.\d+)+")

#: The release name is shown to agents verbatim; the live one is 29 characters.
_RELEASE_NAME_MAX_LEN = 200


#: Sentinel for "the version probe has not run yet". ``None`` already means
#: "probed and could not read it", and collapsing the two would re-probe an
#: unreadable appliance on every single 404.
_UNPROBED = object()


class AriaApiError(Exception):
    """An Aria Operations suite-api call returned an error or failed to connect.

    Carries a teaching message (status + path + how to fix) so end users see an
    actionable line instead of a raw httpx traceback. ``status_code`` is None
    for transport/timeout failures (no HTTP response was received).

    ``body`` is the parsed JSON response body when there was one (``None`` for
    no response or a non-JSON page): the 503 from node status carries
    ``systemTime`` and the health check needs it. ``diagnosis`` is the message
    without the "run the doctor" next step, for the doctor to print.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        path: str | None = None,
        body: Any = None,
        diagnosis: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.path = path
        self.body = body
        self.diagnosis = diagnosis if diagnosis is not None else message


class NonJsonBodyError(AriaApiError):
    """A successful status whose body is not JSON — not an answer from suite-api.

    A login page, an SSO redirect or a proxy in front of the node answers 200
    with HTML. ``status_code`` is that 2xx status.
    """


class NotSuiteApiError(ConnectionError):
    """Token acquisition answered 200 without a token: not a suite-api endpoint.

    A ``ConnectionError`` as before, with the doctor-free ``diagnosis`` beside
    the message other callers receive.
    """

    def __init__(self, message: str, *, diagnosis: str) -> None:
        super().__init__(message)
        self.diagnosis = diagnosis


def _json_body(resp: httpx.Response) -> Any:
    """The parsed JSON body of an error response, or ``None`` if it has none."""
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _json_result(resp: httpx.Response, method: str, path: str, host: str) -> Any:
    """The parsed body of a successful response; ``{}`` when it has none.

    A 2xx whose body is not JSON used to surface as a bare ``JSONDecodeError``
    that no caller catches: the health check crashed on a login page, and the
    doctor reported it as an auth failure right under "Token acquired"
    (2026-09-13 review). It is translated here, once, like every error status
    (踩坑 #37), into a :class:`NonJsonBodyError`.
    """
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        content_type = sanitize(resp.headers.get("content-type") or "no content-type", max_len=80)
        head = (
            f"Aria Operations answered HTTP {resp.status_code} to {method} {path}, but the body "
            f"is not JSON ({content_type}). A login page, an SSO redirect or a proxy in front "
            f"of the node answers like this: check that this target's host and port reach "
            f"the Aria Operations node itself."
        )
        tail = f"Configured host: {host}."
        raise NonJsonBodyError(
            f"{head} {_DOCTOR_POINTER} {tail}",
            status_code=resp.status_code,
            method=method,
            path=path,
            diagnosis=f"{head} {tail}",
        ) from exc


def parse_release_info(data: Any) -> dict[str, Any]:
    """Read the product identity out of a ``GET /versions/current`` body.

    Live 8.18.7 (2026-09-13) answers ``releaseName: "VMware Aria Operations
    8.18.7"`` plus ``major: 1, minor: 77`` — and those two are the *API*
    version, not the product's, so they are never read. The version is the
    dotted number inside ``releaseName``; with none there every field is
    ``None`` rather than a guess (踩坑 #36). The name is API text and goes to
    agents, so it is sanitized (control and format characters, length).
    """
    release = data.get("releaseName") if isinstance(data, dict) else None
    release = sanitize(release, max_len=_RELEASE_NAME_MAX_LEN).strip() if isinstance(release, str) else ""
    match = _DOTTED_VERSION.search(release)
    version = match.group() if match else None
    build = data.get("buildNumber") if isinstance(data, dict) else None
    return {
        "release_name": release or None,
        "product_name": release[: match.start()].strip() or None if match else None,
        "product_version": version,
        "product_line": f"{version.split('.')[0]}.x" if version else None,
        "build_number": build if isinstance(build, int) else None,
    }


def _hint_for_status(status_code: int) -> str:
    """Return a short, actionable remediation hint for an HTTP error status.

    Deliberately free of the request path. The callers already name the failing
    call, and naming it here too put it in the message twice — 118 of the 416
    characters a 404 rendered, which pushed the closing remedy past
    ``sanitize()``'s 300-char cap so the agent never received it.
    """
    if status_code == 404:
        return (
            "Verify the id — list the parent collection first (e.g. "
            "`vmware-aria resource list`, or the list_resources / list_alerts "
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


def _transport_hint(exc: Exception) -> str:
    """Return the remedy for a connection/timeout failure, authored not quoted.

    The exception is read to choose the branch but never interpolated. Its text
    is whatever ssl/socket produced — for a TLS failure that is the certificate
    subject and the hostname it was checked against, for a DNS failure the name
    that failed to resolve. ``_safe_error`` passes ``AriaApiError`` through
    verbatim, so quoting the exception would hand all of that to the agent while
    telling the operator nothing they can act on. The full text still reaches
    the server log through ``exc_info``.
    """
    if _is_tls_verify_error(exc):
        # Kept short on purpose: _safe_error caps delivered text at 300 chars,
        # and the lab fallback at the end must survive the cut.
        return (
            "Certificate not trusted. Set SSL_CERT_FILE to a PEM bundle of the "
            "public roots plus your CA (MCP `env` block or shell). Isolated "
            "self-signed lab only: `verify_ssl: false` in "
            "~/.vmware-aria/config.yaml."
        )
    return (
        "Check 'host' and 'port' for this target in ~/.vmware-aria/config.yaml "
        "and that the appliance is reachable from this machine."
    )


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
        self._product_version: str | None | object = _UNPROBED


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
                hint = _hint_for_status(status)
            head = (
                f"Aria Operations authentication failed: POST "
                f"/auth/token/acquire returned HTTP {status}. {hint}"
            )
            host = f"Configured host: {self._target.host}"
            raise AriaApiError(
                f"{head} {_DOCTOR_THEN} {host}",
                status_code=status,
                method="POST",
                path="/auth/token/acquire",
                body=_json_body(exc.response),
                diagnosis=f"{head} {host}",
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            head = f"Aria Operations authentication could not connect. {_transport_hint(exc)}"
            host = f"Configured host: {self._target.host}"
            raise AriaApiError(
                f"{head} {_DOCTOR_THEN} {host}",
                method="POST",
                path="/auth/token/acquire",
                diagnosis=f"{head} {host}",
            ) from exc
        # A 200 that is not JSON (a login page) carries no token either, and is
        # the same "not a suite-api endpoint" answer — not a JSONDecodeError.
        data = _json_body(resp)
        if not isinstance(data, dict):
            data = {}

        token = data.get("token")
        if not token:
            diagnosis = (
                f"Aria Operations token acquisition to {self._target.host} "
                f"succeeded but the response carried no 'token' field — the host "
                f"is most likely not an Aria Operations suite-api endpoint. "
                f"Verify 'host' and 'port' for this target in "
                f"~/.vmware-aria/config.yaml"
            )
            raise NotSuiteApiError(
                f"{diagnosis}, then run 'vmware-aria doctor'.", diagnosis=f"{diagnosis}."
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
        requires: Requires | None = None,
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
                head = f"Aria Operations request could not connect. {_transport_hint(exc)}"
                tail = f"Configured host: {self._target.host}. Failing call: {method} {path}"
                raise AriaApiError(
                    f"{head} {_DOCTOR_THEN} {tail}",
                    method=method,
                    path=path,
                    diagnosis=f"{head} {tail}",
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
                # A 404 on a call declared newer than this appliance is not a
                # bad id, and the generic remedy ("verify the id") sends the
                # operator hunting for a UUID that was never wrong. Only a
                # version floor that is actually unmet replaces the hint —
                # version_remedy() returns None when the appliance already
                # meets it, so a 9.1 box that 404s still gets the id advice.
                hint = _hint_for_status(resp.status_code)
                if resp.status_code == 404 and requires is not None:
                    explained = version_remedy(requires, self.product_version())
                    if explained:
                        hint = explained
                head = f"Aria Operations returned HTTP {resp.status_code}. {hint}"
                tail = f"Failing call: {method} {path}"
                raise AriaApiError(
                    f"{head} {_DOCTOR_POINTER} {tail}",
                    status_code=resp.status_code,
                    method=method,
                    path=path,
                    body=_json_body(resp),
                    diagnosis=f"{head} {tail}",
                )
            return resp

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        retries: int = 1,
        requires: Requires | None = None,
    ) -> dict:
        """Single GET request. Returns parsed JSON response.

        Pass retries=0 for probes where an error status is itself the answer
        (e.g. a health check reading a 503 as "not ONLINE") to skip the
        transient back-off.
        """
        resp = self._request("GET", path, params=params, retries=retries, requires=requires)
        return _json_result(resp, "GET", path, self._target.host)

    def post(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        retries: int = 0,
        requires: Requires | None = None,
    ) -> dict:
        """POST request. Returns parsed JSON response.

        Defaults to retries=0: POST is not idempotent in general (e.g.
        creating an alert definition or queueing a report), so a transient
        502/504 after the server already accepted the request must not be
        replayed — that would duplicate the side effect. Idempotent callers
        (pure query endpoints like /alerts/query) opt in with retries=1.
        """
        resp = self._request(
            "POST", path, params=params, json_data=json_data, retries=retries, requires=requires
        )
        return _json_result(resp, "POST", path, self._target.host)

    def put(self, path: str, json_data: dict[str, Any] | None = None) -> dict:
        """PUT request. Returns parsed JSON response."""
        resp = self._request("PUT", path, json_data=json_data)
        return _json_result(resp, "PUT", path, self._target.host)

    def product_version(self) -> str | None:
        """Best-effort appliance version string, or ``None`` if unreadable.

        Called only from the 404 error path, which imposes three constraints:

        * **It must never raise.** It runs while another error is being
          constructed; an exception here would replace a useful message with a
          confusing one.
        * **It must never recurse.** It calls ``_request`` without ``requires``,
          so its own 404 cannot re-enter this method.
        * **It must cache its failure too.** Otherwise a target that cannot
          answer is re-probed on every subsequent 404.

        Verified on a live 8.18.7 (2026-09-13): ``/versions/current`` answers
        even while node status is 503, with ``releaseName: "VMware Aria
        Operations 8.18.7"``. The whole release name used to be returned, and
        ``parse_version`` reads from the front, so every version-floor 404 on
        that appliance said the version "could not be read". The dotted number
        is extracted by :func:`parse_release_info`; ``None`` still means
        unreadable and routes to wording that asserts nothing about the build.
        ``/deployment/node/status`` is no longer tried — NodeStatus has no
        version field.
        """
        if self._product_version is not _UNPROBED:
            return self._product_version

        self._product_version = None  # cache the failure before probing
        try:
            data = self._request("GET", "/versions/current", retries=0).json()
        except Exception:  # noqa: BLE001 — unreadable is a supported answer
            return self._product_version
        self._product_version = parse_release_info(data)["product_version"]
        return self._product_version

    @property
    def base_url(self) -> str:
        """The suite-api base URL, e.g. ``https://host:443/suite-api/api``.

        Exposed so a sibling service on the same appliance but a different base
        path (the VODAP real-time metrics service at ``/data-query-service``)
        can be reached by substituting the path prefix while keeping the host.
        """
        return self._base_url

    def raw_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict:
        """Issue one request to an absolute URL with caller-supplied headers.

        Unlike :meth:`get`/:meth:`post`, this does NOT attach the suite-api
        ``vRealizeOpsToken`` Authorization header and does NOT prepend the
        suite-api base path — the caller passes a full URL and its own auth
        header. It reuses this client's TLS/verify settings and connection pool.

        Used by the PromQL/VODAP path, which lives on a different service base
        (``/data-query-service``) and authenticates with an exchanged Bearer
        JWT rather than the OpsToken. Transport/timeout and transient gateway
        statuses are retried once; any error status is translated into an
        ``AriaApiError`` with a teaching hint, the same as the suite-api path.
        """
        attempt = 0
        while True:
            try:
                resp = self._client.request(method, url, headers=headers, params=params, json=json_data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < 1:
                    attempt += 1
                    time.sleep(_RETRY_DELAY_SEC)
                    continue
                head = f"Aria Operations request could not connect. {_transport_hint(exc)}"
                tail = f"Configured host: {self._target.host}. Failing call: {method} {url}"
                raise AriaApiError(
                    f"{head} {_DOCTOR_THEN} {tail}",
                    method=method,
                    path=url,
                    diagnosis=f"{head} {tail}",
                ) from exc

            if resp.status_code in _TRANSIENT_STATUS and attempt < 1:
                attempt += 1
                time.sleep(_RETRY_DELAY_SEC)
                continue

            if resp.status_code >= 400:
                raise AriaApiError(
                    f"Aria Operations returned HTTP {resp.status_code}. "
                    f"{_hint_for_status(resp.status_code)} "
                    f"Failing call: {method} {url}",
                    status_code=resp.status_code,
                    method=method,
                    path=url,
                    body=_json_body(resp),
                )
            return _json_result(resp, method, url, self._target.host)

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

"""The health check and the doctor report what they observed, and nothing else.

Adversarial pre-release review, 2026-09-13, of the 8.18.7 health work:

A. A 200 whose body is not JSON (a login page, an SSO redirect, a proxy in
   front of the node) crashed ``get_aria_health`` with a ``JSONDecodeError``,
   and inside the doctor it escaped to the auth handler — an "Aria auth FAIL"
   row directly under "Aria auth PASS — Token acquired", with the disconnect
   skipped. The connection layer now translates it into an ``AriaApiError``
   (踩坑 #37: errors are translated in one place, not caught per function).
B. ``release_name`` / ``product_name`` reached the MCP output unsanitized.
C. The doctor failed the pre-flight when only the version could not be read,
   while auth and the platform were fine. The version is context: it warns.
D. An ONLINE node whose services could not be read said "no service reports a
   failure" — no service had been asked.
E. A services body of an unrecognised shape must stay distinguishable from
   "no service is failing"; nothing pinned that.
"""

from __future__ import annotations

import copy

import httpx
import pytest

from tests.eval.regression._live_8187 import RoutedClient, api_error, body, startup_routes
from vmware_aria import config as cfg
from vmware_aria import connection as conn
from vmware_aria import doctor as doc
from vmware_aria.connection import AriaApiError, AriaClient, parse_release_info

_HTML = "<html><body>Sign in</body></html>"

_CONFIG = """
targets:
  lab:
    host: aria.example
    port: 443
    username: admin
"""


def _flat(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace() and ch not in "│┃")


def _response(method: str, answer) -> httpx.Response:
    request = httpx.Request(method, "https://aria.example")
    if isinstance(answer, httpx.Response):
        return answer
    if isinstance(answer, str):
        return httpx.Response(200, text=answer, headers={"content-type": "text/html"}, request=request)
    if isinstance(answer, AriaApiError):
        return httpx.Response(answer.status_code, json=answer.body, request=request)
    return httpx.Response(200, json=answer, request=request)


def _real_client(routes: dict) -> AriaClient:
    """A real AriaClient whose transport answers per path, so every error goes
    through the actual ``_request`` / ``get`` code rather than a hand-built one."""

    def request(method, path, **_kw):
        return _response(method, routes[path])

    client = AriaClient.__new__(AriaClient)
    client._target = type("T", (), {"host": "aria.example", "auth_source": "local"})()
    client._base_url = "https://aria.example:443/suite-api/api"
    client._username = "admin"
    client._password = "pw"
    client._product_version = conn._UNPROBED
    client._headers = dict
    client._client = type("C", (), {"request": staticmethod(request)})()
    return client


def _online_routes() -> dict:
    routes = startup_routes()
    routes["/deployment/node/status"] = {"status": "ONLINE", "systemTime": 1789280781529}
    info = copy.deepcopy(body("services_info.json"))
    for svc in info["service"]:
        svc["health"] = "OK"
    routes["/deployment/node/services/info"] = info
    return routes


def _health(client):
    from vmware_aria.ops.health import get_aria_health

    return get_aria_health(client)


class _Sock:
    def close(self) -> None:
        pass


def _doctor(tmp_path, monkeypatch, capsys, connect) -> tuple[bool, str, list[str]]:
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(path))
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setattr(cfg.TargetConfig, "get_password", lambda self, name: "pw")
    monkeypatch.setattr(doc.socket, "create_connection", lambda *a, **k: _Sock())
    disconnected: list[str] = []

    class _Manager:
        def __init__(self, config):
            pass

        def connect(self, name):
            return connect()

        def disconnect(self, name):
            disconnected.append(name)

    monkeypatch.setattr(conn, "ConnectionManager", _Manager)
    ok = doc.run_doctor()
    return ok, _flat(capsys.readouterr().out), disconnected


# ---------------------------------------------------------------------------
# A. a 200 that is not JSON
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("verb", ["get", "post", "put"])
def test_a_non_json_200_is_a_teaching_api_error(verb):
    client = _real_client({"/versions/current": _HTML})

    with pytest.raises(AriaApiError) as exc:
        getattr(client, verb)("/versions/current")

    assert exc.value.status_code == 200
    assert exc.value.path == "/versions/current"
    assert "not JSON" in str(exc.value)
    assert "vmware-aria doctor" not in exc.value.diagnosis


@pytest.mark.unit
def test_a_non_json_token_answer_is_not_a_json_traceback():
    def post(url, **_kw):
        return httpx.Response(200, text=_HTML, request=httpx.Request("POST", url))

    client = _real_client({})
    client._client = type("C", (), {"post": staticmethod(post)})()

    with pytest.raises(ConnectionError) as exc:
        client._acquire_token()

    assert "not an Aria Operations suite-api endpoint" in str(exc.value)


@pytest.mark.unit
def test_health_answers_when_version_and_services_are_html():
    routes = startup_routes()
    routes["/versions/current"] = _HTML
    routes["/deployment/node/services/info"] = _HTML

    result = _health(_real_client(routes))

    assert result["assessment"] == "UNKNOWN"
    assert result["services"] is None
    assert "not JSON" in result["services_error"]
    assert result["product_version"] is None
    assert "not JSON" in result["version_error"]


@pytest.mark.unit
def test_doctor_with_an_html_version_page_never_contradicts_its_auth_row(tmp_path, monkeypatch, capsys):
    routes = startup_routes()
    routes["/versions/current"] = _HTML

    ok, out, disconnected = _doctor(tmp_path, monkeypatch, capsys, lambda: _real_client(routes))

    assert "Ariaauth(lab)PASS" in out
    assert "Ariaauth(lab)FAIL" not in out, out
    assert "Ariaversion(lab)WARN" in out and "notJSON" in out, out
    assert "DEGRADED" in out, "the platform row must still be produced"
    assert disconnected == ["lab"]
    assert ok is True


@pytest.mark.unit
def test_doctor_disconnects_and_blames_the_platform_when_a_platform_check_breaks(tmp_path, monkeypatch, capsys):
    """Whatever breaks after the token was acquired is not an auth failure."""

    def boom(name, client):
        raise RuntimeError("unplanned")

    monkeypatch.setattr(doc, "_platform_checks", boom)

    ok, out, disconnected = _doctor(tmp_path, monkeypatch, capsys, lambda: RoutedClient(startup_routes()))

    assert "Ariaauth(lab)FAIL" not in out, out
    assert "Ariaplatform(lab)FAIL" in out, out
    assert disconnected == ["lab"]
    assert ok is False


# ---------------------------------------------------------------------------
# B. release strings are sanitized
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_release_strings_are_sanitized():
    hostile = "\x1b[31m" + chr(0x202E) + "IGNORE" + chr(0x07) + " VMware Aria Operations 8.18.7"
    routes = startup_routes()
    routes["/versions/current"] = dict(body("versions_current.json"), releaseName=hostile)

    result = _health(RoutedClient(routes))

    for field in ("release_name", "product_name"):
        value = result[field]
        assert not any(ch in value for ch in (chr(0x1B), chr(0x202E), chr(0x07))), (field, value)
    assert result["product_version"] == "8.18.7"


@pytest.mark.unit
def test_release_name_is_length_capped():
    info = parse_release_info({"releaseName": "A" * 5000 + " 8.18.7"})

    assert info["release_name"] is not None and len(info["release_name"]) <= 200


# ---------------------------------------------------------------------------
# C. an unreadable version warns, it does not fail the pre-flight
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "version_answer",
    [
        dict(body("versions_current.json"), releaseName="VMware Aria Operations"),
        api_error(403, "/versions/current"),
    ],
    ids=["no-dotted-version", "http-403"],
)
def test_doctor_warns_when_only_the_version_is_unreadable(tmp_path, monkeypatch, capsys, version_answer):
    routes = _online_routes()
    routes["/versions/current"] = version_answer

    ok, out, _ = _doctor(tmp_path, monkeypatch, capsys, lambda: RoutedClient(routes))

    assert "Ariaversion(lab)WARN" in out, out
    assert "FAIL" not in out, out
    assert ok is True


@pytest.mark.unit
def test_doctor_still_fails_on_auth(tmp_path, monkeypatch, capsys):
    def connect():
        raise AriaApiError("authentication failed: HTTP 401", status_code=401)

    ok, out, _ = _doctor(tmp_path, monkeypatch, capsys, connect)

    assert "Ariaauth(lab)FAIL" in out
    assert ok is False


# ---------------------------------------------------------------------------
# D. ONLINE with an unread breakdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_online_with_unreadable_services_does_not_claim_services_were_checked():
    routes = _online_routes()
    routes["/deployment/node/services/info"] = api_error(500, "/deployment/node/services/info")

    result = _health(RoutedClient(routes))

    assert result["assessment"] == "HEALTHY", "ONLINE is the node's own word that every service runs"
    assert result["services"] is None
    assert "500" in result["services_error"]
    assert "no service reports a failure" not in result["details"]
    assert "not read" in result["details"]


@pytest.mark.unit
def test_online_with_an_unrecognised_state_does_not_say_not_online():
    routes = _online_routes()
    routes["/deployment/node/services/info"]["service"][0]["health"] = "WARNING"

    result = _health(RoutedClient(routes))

    assert result["assessment"] == "UNKNOWN"
    assert "not ONLINE" not in result["details"], result["details"]
    assert "CASA" in result["details"]


@pytest.mark.unit
def test_online_with_every_service_ok_says_so():
    result = _health(RoutedClient(_online_routes()))

    assert result["assessment"] == "HEALTHY"
    assert "7 services report OK" in result["details"], result["details"]


# ---------------------------------------------------------------------------
# E. a services body of an unrecognised shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "services_body",
    [
        {"services": [{"name": "LOCATOR", "health": "ERROR"}]},
        {},
        {"service": []},
        {"service": ["LOCATOR", 3]},
        ["LOCATOR"],
    ],
    ids=["wrong-key", "empty-object", "empty-list", "no-object-rows", "top-level-list"],
)
def test_an_unrecognised_services_body_is_unread_not_empty(services_body):
    routes = _online_routes()
    routes["/deployment/node/services/info"] = services_body

    result = _health(RoutedClient(routes))

    assert result["services"] is None, "an unread breakdown must not render as a list"
    assert result["services_not_ok"] is None
    assert result["services_error"], "why the breakdown is missing must be said"
    assert "not read" in result["details"]

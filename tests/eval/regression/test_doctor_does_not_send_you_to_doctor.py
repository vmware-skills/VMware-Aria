"""The doctor must diagnose, not refer the operator back to itself.

Two findings on a real Aria Operations 8.18.7, 2026-09-13:

1. Every ``AriaApiError`` ended with "Run 'vmware-aria doctor' if every call to
   this target fails", and the doctor printed those errors verbatim — so the
   doctor's own report told the operator to run the doctor (the same loop the
   2026-08-29 round found: a remedy that sends you to a check that has nothing
   more to say).
2. The doctor read the version from ``/deployment/node/status``, which answers
   503 during startup — and on this appliance for hours with one stuck
   service — so it printed "Aria node info FAIL" while ``/versions/current``
   answered fine. ``nodeType`` is not a NodeStatus field either.

Other callers keep the pointer: for an agent in the middle of a tool call,
"run doctor" is a real next step. Only the doctor's own report drops it.
"""

from __future__ import annotations

import httpx
import pytest

from tests.eval.regression._live_8187 import RoutedClient, startup_routes
from vmware_aria import config as cfg
from vmware_aria import connection as conn
from vmware_aria import doctor as doc
from vmware_aria.connection import AriaClient

_CONFIG = """
targets:
  lab:
    host: aria.example
    port: 443
    username: admin
"""


def _flat(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace() and ch not in "│┃")


class _Sock:
    def close(self) -> None:
        pass


def _harness(tmp_path, monkeypatch, connect):
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(path))
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setattr(cfg.TargetConfig, "get_password", lambda self, name: "pw")
    monkeypatch.setattr(doc.socket, "create_connection", lambda *a, **k: _Sock())

    class _Manager:
        def __init__(self, config):
            pass

        def connect(self, name):
            return connect()

        def disconnect(self, name):
            pass

    monkeypatch.setattr(conn, "ConnectionManager", _Manager)


def _bare_client(post=None, request=None) -> AriaClient:
    client = AriaClient.__new__(AriaClient)
    client._target = type("T", (), {"host": "aria.example", "auth_source": "local"})()
    client._base_url = "https://aria.example:443/suite-api/api"
    client._username = "admin"
    client._password = "pw"
    client._product_version = conn._UNPROBED
    client._headers = lambda: {}
    client._client = type("C", (), {"post": staticmethod(post), "request": staticmethod(request)})()
    return client


def _raised(fn) -> Exception:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — capturing what real code raises
        return exc
    raise AssertionError("expected the real code path to raise")


def _auth_401() -> Exception:
    def post(url, **_kw):
        return httpx.Response(401, request=httpx.Request("POST", url))

    return _raised(_bare_client(post=post)._acquire_token)


def _auth_unreachable() -> Exception:
    def post(url, **_kw):
        raise httpx.ConnectError("connection refused")

    return _raised(_bare_client(post=post)._acquire_token)


def _not_suite_api() -> Exception:
    def post(url, **_kw):
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    return _raised(_bare_client(post=post)._acquire_token)


def _request_500() -> Exception:
    def request(method, path, **_kw):
        return httpx.Response(500, request=httpx.Request(method, "https://x"))

    return _raised(lambda: _bare_client(request=request).get("/versions/current", retries=0))


@pytest.mark.unit
@pytest.mark.parametrize("make", [_auth_401, _auth_unreachable, _not_suite_api, _request_500])
def test_real_error_messages_still_point_other_callers_at_doctor(make):
    """The control: the pointer exists in the message the tools pass on, so the
    doctor test below is removing something real, not something absent."""
    assert "vmware-aria doctor" in str(make())


@pytest.mark.unit
@pytest.mark.parametrize("make", [_auth_401, _auth_unreachable, _not_suite_api])
def test_doctor_report_never_says_run_doctor_when_connect_fails(tmp_path, monkeypatch, capsys, make):
    exc = make()

    def connect():
        raise exc

    _harness(tmp_path, monkeypatch, connect)
    ok = doc.run_doctor()
    out = _flat(capsys.readouterr().out)

    assert ok is False
    assert "vmware-ariadoctor" not in out, out
    assert "FAIL" in out


@pytest.mark.unit
def test_doctor_report_never_says_run_doctor_when_every_call_fails(tmp_path, monkeypatch, capsys):
    exc = _request_500()
    routes = {p: exc for p in startup_routes()}
    _harness(tmp_path, monkeypatch, lambda: RoutedClient(routes))

    doc.run_doctor()
    out = _flat(capsys.readouterr().out)

    assert "HTTP500" in out, "the failure itself must still be reported"
    assert "vmware-ariadoctor" not in out, out


@pytest.mark.unit
def test_doctor_reads_the_version_while_node_status_is_503(tmp_path, monkeypatch, capsys):
    _harness(tmp_path, monkeypatch, lambda: RoutedClient(startup_routes()))

    ok = doc.run_doctor()
    out = _flat(capsys.readouterr().out)

    assert "Arianodeinfo" not in out, "the old 503-probe row is back"
    assert "FAIL" not in out, out
    assert "VMwareAriaOperations8.18.7" in out
    assert "8.x" in out
    assert "DEGRADED" in out and "LOCATOR" in out
    assert "WARN" in out, "a degraded platform is worth a warning, not a silent PASS"
    assert ok is True, "a degraded-but-answering platform is not a failed pre-flight"

"""Aria-specific regression evals — 2026-06-08 external user report.

A user ran the MCP against a real Aria Operations instance: half the API
returned 404. Verification against the official suite-api spec confirmed
12 of their 14 claims plus 6 more invented endpoints in anomaly/capacity.
Each test below pins one fixed bug by exercising the real ops code with a
mocked AriaClient and asserting the request/parse shape the spec requires.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


def _client() -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.get.return_value = {}
    client.post.return_value = {}
    client.put.return_value = {}
    return client


# ── #1/#3: StatQuery statKey strings + intervalQuantifier ──────────────


def test_metrics_query_body_shape() -> None:
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    get_resource_metrics(client, "res-1", ["cpu|usage_average", "mem|usage_average"])

    path, = client.post.call_args.args
    body = client.post.call_args.kwargs["json_data"]
    assert path == "/resources/res-1/stats/query"
    assert body["statKey"] == ["cpu|usage_average", "mem|usage_average"], (
        "statKey must be an array of plain strings, not [{key: ...}] objects"
    )
    assert "intervalQuantifier" in body and "intervalQuantity" not in body


# ── #2: response parsing must traverse values[].stat-list.stat[] ───────


def test_metrics_response_parses_stat_list_nesting() -> None:
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    client.post.return_value = {
        "values": [
            {
                "resourceId": "res-1",
                "stat-list": {
                    "stat": [
                        {
                            "statKey": {"key": "cpu|usage_average"},
                            "timestamps": [1000, 2000],
                            "data": [1.5, 2.5],
                        }
                    ]
                },
            }
        ]
    }
    result = get_resource_metrics(client, "res-1", ["cpu|usage_average"])
    assert result["cpu|usage_average"] == [
        {"timestamp_ms": 1000, "value": 1.5},
        {"timestamp_ms": 2000, "value": 2.5},
    ]


# ── #4: topn must use GET /resources/stats/topn ────────────────────────


def test_top_consumers_uses_get_stats_topn() -> None:
    from vmware_aria.ops.resources import get_top_consumers

    client = _client()
    client.get.side_effect = [
        {"resourceList": [{"identifier": "vm-1", "resourceKey": {"name": "web-01"}}]},
        {
            "resourceStatGroups": [
                {
                    "groupKey": "vm-1",
                    # spec shape: data nests under `stat`
                    "resourceStats": [
                        {
                            "resourceId": "vm-1",
                            "stat": {"statKey": {"key": "cpu|usage_average"}, "data": [42.0]},
                        }
                    ],
                }
            ]
        },
    ]
    results = get_top_consumers(client, metric_key="cpu|usage_average", top_n=5)["items"]

    topn_call = client.get.call_args_list[1]
    assert topn_call.args[0] == "/resources/stats/topn"
    assert topn_call.kwargs["params"]["resourceId"] == ["vm-1"]
    assert "intervalQuantifier" in topn_call.kwargs["params"]
    client.post.assert_not_called()
    assert results[0] == {"id": "vm-1", "name": "web-01", "metric_key": "cpu|usage_average", "value": 42.0}


# ── #5: alert filtering must go through POST /alerts/query ─────────────


def test_list_alerts_uses_alert_query() -> None:
    from vmware_aria.ops.alerts import list_alerts

    client = _client()
    client.post.return_value = {"alerts": []}
    list_alerts(client, active_only=True, criticality="critical", resource_id="res-9")

    path, = client.post.call_args.args
    body = client.post.call_args.kwargs["json_data"]
    assert path == "/alerts/query"
    assert body["activeOnly"] is True
    assert body["alertCriticality"] == ["CRITICAL"]
    assert body["resource-query"] == {"resourceId": ["res-9"]}
    client.get.assert_not_called()


# ── #6/#7: alert actions via POST /alerts?action=... + uuids body ──────


def test_acknowledge_alert_uses_takeownership_action() -> None:
    from vmware_aria.ops.alerts import acknowledge_alert

    client = _client()
    client.get.return_value = {}
    result = acknowledge_alert(client, "alert-1")

    path, = client.post.call_args.args
    assert path == "/alerts"
    assert client.post.call_args.kwargs["params"] == {"action": "takeownership"}
    assert client.post.call_args.kwargs["json_data"] == {"uuids": ["alert-1"]}
    assert result["action"] == "takeownership"


def test_cancel_alert_uses_cancel_action() -> None:
    from vmware_aria.ops.alerts import cancel_alert

    client = _client()
    client.get.return_value = {}
    cancel_alert(client, "alert-2")

    path, = client.post.call_args.args
    assert path == "/alerts"
    assert client.post.call_args.kwargs["params"] == {"action": "cancel"}
    assert client.post.call_args.kwargs["json_data"] == {"uuids": ["alert-2"]}
    client.delete.assert_not_called()


# ── #8: alertdefinition enable/disable must be PUT ─────────────────────


def test_set_alert_definition_state_uses_put() -> None:
    from vmware_aria.ops.alerts import set_alert_definition_state

    client = _client()
    set_alert_definition_state(client, "def-1", enabled=True)
    assert client.put.call_args.args[0] == "/alertdefinitions/def-1/enable"

    set_alert_definition_state(client, "def-1", enabled=False)
    assert client.put.call_args.args[0] == "/alertdefinitions/def-1/disable"
    client.post.assert_not_called()


# ── #9 (refuted — pin the CORRECT wire key) + relation fix ─────────────


def test_create_alert_definition_keeps_base_symptom_set() -> None:
    from vmware_aria.ops.alerts import create_alert_definition

    client = _client()
    client.post.return_value = {"id": "new-def"}
    create_alert_definition(
        client, "n", "d", "VirtualMachine", symptom_definition_ids=["s1"]
    )
    body = client.post.call_args.kwargs["json_data"]
    sset = body["states"][0]["base-symptom-set"]  # wire key, NOT "symptoms"
    assert sset["relation"] == "SELF"  # "ANY" is not a valid relation value
    assert sset["symptomDefinitionIds"] == ["s1"]


# ── #10/#11: report generation body + list filter ──────────────────────


def test_generate_report_body_shape_and_requires_resource() -> None:
    from vmware_aria.ops.reports import generate_report

    client = _client()
    client.post.return_value = {"id": "rep-1", "status": "PENDING"}
    generate_report(client, "rdef-1", resource_ids=["res-1"])

    body = client.post.call_args.kwargs["json_data"]
    assert body["reportDefinitionId"] == "rdef-1"
    assert body["resourceId"] == "res-1"
    assert "reportDefinition" not in body  # old invented nesting

    with pytest.raises(ValueError, match="resource_ids"):
        generate_report(client, "rdef-1", resource_ids=None)


def test_list_reports_filters_definition_client_side() -> None:
    from vmware_aria.ops.reports import list_reports

    client = _client()
    client.get.return_value = {
        "reports": [
            {"id": "r1", "reportDefinitionId": "want"},
            {"id": "r2", "reportDefinitionId": "other"},
        ]
    }
    results = list_reports(client, definition_id="want")["items"]

    params = client.get.call_args.kwargs.get("params") or {}
    assert "reportDefinitionId" not in params  # not a valid API param
    assert [r["id"] for r in results] == ["r1"]


# ── #13: node status parsing ───────────────────────────────────────────


def test_aria_health_reads_status_field() -> None:
    from vmware_aria.ops.health import get_aria_health

    client = _client()
    client.get.return_value = {"status": "ONLINE", "systemTime": 1234}
    result = get_aria_health(client)
    assert result["overall_status"] == "ONLINE"
    assert result["healthy"] is True
    assert result["system_time_ms"] == 1234


# ── #14: token validity is an epoch-ms timestamp, not a duration ───────


def test_token_validity_treated_as_epoch_timestamp(monkeypatch) -> None:
    from vmware_aria.config import TargetConfig
    from vmware_aria.connection import AriaClient

    expiry_epoch_ms = int((time.time() + 6 * 3600) * 1000)

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "tok", "validity": expiry_epoch_ms}

    monkeypatch.setattr("httpx.Client.post", lambda self, *a, **k: FakeResponse())
    target = TargetConfig(host="h", username="u")
    client = AriaClient(target, "pw")

    # Old bug: now + validity/1000 put expiry ~56 years out. Correct: ~6h.
    seconds_until_expiry = client._token_expires_at - time.time()
    assert 0 < seconds_until_expiry < 7 * 3600, (
        f"token expiry {seconds_until_expiry:.0f}s out — validity must be "
        "parsed as an epoch-ms timestamp, not a duration"
    )


# ── invented capacity/anomaly endpoints replaced by stats calls ────────


def test_capacity_uses_stats_latest_not_invented_endpoints() -> None:
    from vmware_aria.ops.capacity import get_remaining_capacity, get_time_remaining

    client = _client()
    client.get.return_value = {"values": []}
    get_remaining_capacity(client, "cl-1")
    get_time_remaining(client, "cl-1")

    for call in client.get.call_args_list:
        path = call.args[0]
        assert path == "/resources/cl-1/stats/latest", f"unexpected path {path}"
        for key in call.kwargs["params"]["statKey"]:
            assert key.startswith("OnlineCapacityAnalytics|")


def test_anomaly_uses_bulk_stats_query_not_invented_endpoints() -> None:
    from vmware_aria.ops.anomaly import get_resource_riskbadge, list_anomalies

    client = _client()
    client.get.return_value = {
        "badges": [{"type": "RISK", "score": 25, "color": "GREEN"}]
    }
    result = get_resource_riskbadge(client, "res-1")
    assert client.get.call_args.args[0] == "/resources/res-1"  # no /badge/risk
    assert result["risk_score"] == 25

    # list_anomalies now issues ONE bulk POST /resources/stats/query with a
    # resourceId array (was a per-VM GET /resources/{id}/stats/latest N+1).
    client.post.reset_mock()
    client.post.return_value = {"values": []}
    list_anomalies(client, resource_id="res-1")
    path, = client.post.call_args.args
    assert path == "/resources/stats/query"
    body = client.post.call_args.kwargs["json_data"]
    assert body["resourceId"] == ["res-1"]
    assert body["statKey"] == ["System Attributes|total_alarms"]


# ── #6 follow-up: raw HTTP errors become teaching AriaApiError, not tracebacks
#
# The connection layer translates every non-2xx status (and transport
# failures) into AriaApiError with a status_code + remediation hint, and
# retries transient gateway statuses (502/503/504) exactly once. 4xx client
# errors (e.g. a bad UUID) are surfaced immediately, not retried.


def _conn(monkeypatch):
    from vmware_aria.config import TargetConfig
    from vmware_aria.connection import AriaClient

    expiry_epoch_ms = int((time.time() + 6 * 3600) * 1000)

    class TokenResp:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "tok", "validity": expiry_epoch_ms}

    # token acquisition on __init__ goes through httpx.Client.post
    monkeypatch.setattr("httpx.Client.post", lambda self, *a, **k: TokenResp())
    return AriaClient(TargetConfig(host="h", username="u"), "pw")


def test_404_becomes_teaching_error_not_traceback(monkeypatch) -> None:
    import httpx
    import pytest

    from vmware_aria.connection import AriaApiError

    client = _conn(monkeypatch)
    monkeypatch.setattr(
        "httpx.Client.request",
        lambda self, method, url, **k: httpx.Response(404, request=httpx.Request(method, url)),
    )

    with pytest.raises(AriaApiError) as exc_info:
        client.get("/resources/bad-uuid")

    err = exc_info.value
    assert err.status_code == 404
    assert "/resources/bad-uuid" in str(err)
    assert "404" in str(err)
    # the message must teach the fix, not just report the error
    assert "list" in str(err).lower()


def test_transient_503_retried_once_then_raises(monkeypatch) -> None:
    import httpx
    import pytest

    from vmware_aria.connection import AriaApiError

    client = _conn(monkeypatch)
    calls: list = []

    def fake_request(self, method, url, **k):
        calls.append(url)
        return httpx.Response(503, request=httpx.Request(method, url))

    monkeypatch.setattr("httpx.Client.request", fake_request)
    monkeypatch.setattr("vmware_aria.connection._RETRY_DELAY_SEC", 0)

    with pytest.raises(AriaApiError) as exc_info:
        client.get("/resources")

    assert exc_info.value.status_code == 503
    assert len(calls) == 2, "a transient 503 is retried exactly once (2 attempts total)"


def test_client_error_404_not_retried(monkeypatch) -> None:
    import httpx
    import pytest

    from vmware_aria.connection import AriaApiError

    client = _conn(monkeypatch)
    calls: list = []

    def fake_request(self, method, url, **k):
        calls.append(url)
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr("httpx.Client.request", fake_request)

    with pytest.raises(AriaApiError):
        client.get("/resources/x")

    assert len(calls) == 1, "client errors (4xx) must not be retried"


def test_is_alive_true_when_node_returns_503(monkeypatch) -> None:
    # A booting node (503) is reachable with a valid token — is_alive must not
    # report it dead, or connect() would needlessly re-authenticate every call.
    import httpx

    client = _conn(monkeypatch)
    monkeypatch.setattr(
        "httpx.Client.request",
        lambda self, method, url, **k: httpx.Response(503, request=httpx.Request(method, url)),
    )
    assert client.is_alive() is True


def test_is_alive_false_when_auth_fails(monkeypatch) -> None:
    import httpx

    client = _conn(monkeypatch)
    # Persistent 401 even after re-auth → cached client is stale.
    monkeypatch.setattr(
        "httpx.Client.request",
        lambda self, method, url, **k: httpx.Response(401, request=httpx.Request(method, url)),
    )
    assert client.is_alive() is False


# ── #8: liveness TTL cache — connect() runs on every MCP tool call; without a
# short TTL it fires a full is_alive() HTTP probe each time. Two back-to-back
# connect() calls within the TTL must probe only once; after expiry, re-probe.


def test_is_alive_cached_skips_probe_within_ttl(monkeypatch) -> None:
    import httpx

    client = _conn(monkeypatch)
    calls: list = []

    def fake_request(self, method, url, **k):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr("httpx.Client.request", fake_request)

    # First call probes and caches; the second within the TTL reuses the cache.
    assert client.is_alive_cached(ttl=30.0) is True
    assert client.is_alive_cached(ttl=30.0) is True
    assert len(calls) == 1, "a second liveness check within the TTL must not re-probe"


def test_is_alive_cached_reprobes_after_ttl(monkeypatch) -> None:
    import httpx

    client = _conn(monkeypatch)
    calls: list = []

    def fake_request(self, method, url, **k):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr("httpx.Client.request", fake_request)

    # Tiny TTL so the second call lands after expiry without a real sleep.
    assert client.is_alive_cached(ttl=0.0) is True
    assert client.is_alive_cached(ttl=0.0) is True
    assert len(calls) == 2, "once the TTL has elapsed the probe must run again"


def test_is_alive_cached_reprobes_after_probe_failure(monkeypatch) -> None:
    # A failed probe must not be cached: the next call has to re-probe so a
    # recovered/replaced session is detected promptly.
    import httpx

    client = _conn(monkeypatch)
    monkeypatch.setattr(
        "httpx.Client.request",
        lambda self, method, url, **k: httpx.Response(401, request=httpx.Request(method, url)),
    )
    assert client.is_alive_cached(ttl=30.0) is False
    assert client._liveness_checked_at == 0.0, "a failed probe must not refresh the TTL"


def test_connect_reuses_cached_client_without_reprobing(monkeypatch) -> None:
    # End-to-end: two back-to-back ConnectionManager.connect() calls within the
    # TTL hit /deployment/node/status only once.
    import httpx

    from vmware_aria.config import AppConfig, TargetConfig
    from vmware_aria.connection import ConnectionManager

    expiry_epoch_ms = int((time.time() + 6 * 3600) * 1000)

    class TokenResp:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "tok", "validity": expiry_epoch_ms}

    monkeypatch.setattr("httpx.Client.post", lambda self, *a, **k: TokenResp())

    probes: list = []

    def fake_request(self, method, url, **k):
        probes.append(url)
        return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr("httpx.Client.request", fake_request)

    cfg = AppConfig(
        targets={"prod": TargetConfig(host="h", username="u")},
        default_target="prod",
    )
    monkeypatch.setattr(TargetConfig, "get_password", lambda self, name: "pw")

    mgr = ConnectionManager(cfg)
    first = mgr.connect("prod")
    second = mgr.connect("prod")

    assert first is second, "the cached client must be reused"
    # connect() #1 creates the client (no liveness probe), connect() #2 probes
    # once; a third within the TTL would add no probe.
    assert len(probes) == 1, "back-to-back connect() within the TTL probes only once"
    third = mgr.connect("prod")
    assert third is first
    assert len(probes) == 1, "still cached — no extra probe"


def test_transport_error_after_reauth_is_wrapped(monkeypatch) -> None:
    # Regression for the post-reauth leak: a 401 forces a token re-acquire,
    # then the re-issued request hits a dropped connection. That transport
    # error must be wrapped as AriaApiError (status_code None), never leak raw.
    import httpx
    import pytest

    from vmware_aria.connection import AriaApiError

    client = _conn(monkeypatch)
    state = {"n": 0}

    def fake_request(self, method, url, **k):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401, request=httpx.Request(method, url))
        raise httpx.ConnectError("connection dropped")

    monkeypatch.setattr("httpx.Client.request", fake_request)
    monkeypatch.setattr("vmware_aria.connection._RETRY_DELAY_SEC", 0)

    with pytest.raises(AriaApiError) as exc_info:
        client.get("/resources")

    assert exc_info.value.status_code is None, "transport failure has no HTTP status"
    assert "could not connect" in str(exc_info.value).lower()


# ── auth errors translated at the source (connect + mid-request refresh) ─
#
# _acquire_token() used a bare raise_for_status(): a wrong password (401),
# bad authSource (400), or booting node (503) at connect time leaked a raw
# httpx traceback, and a refresh inside _request()'s loop escaped the
# translation layer entirely.


def test_auth_failure_at_connect_becomes_teaching_error(monkeypatch) -> None:
    import httpx
    import pytest

    from vmware_aria.config import TargetConfig
    from vmware_aria.connection import AriaApiError, AriaClient

    monkeypatch.setattr(
        "httpx.Client.post",
        lambda self, url, **k: httpx.Response(401, request=httpx.Request("POST", url)),
    )

    with pytest.raises(AriaApiError) as exc_info:
        AriaClient(TargetConfig(host="h", username="u"), "wrong-pw")

    err = exc_info.value
    assert err.status_code == 401
    assert err.path == "/auth/token/acquire"
    # the message must point at the credentials, not just report 401
    assert "username/password/authSource" in str(err)
    assert ".env" in str(err)


def test_auth_transport_error_becomes_aria_api_error(monkeypatch) -> None:
    import httpx
    import pytest

    from vmware_aria.config import TargetConfig
    from vmware_aria.connection import AriaApiError, AriaClient

    def fake_post(self, url, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.Client.post", fake_post)

    with pytest.raises(AriaApiError) as exc_info:
        AriaClient(TargetConfig(host="h", username="u"), "pw")

    assert exc_info.value.status_code is None
    assert "could not connect" in str(exc_info.value).lower()


def test_token_refresh_failure_mid_request_is_translated(monkeypatch) -> None:
    import httpx
    import pytest

    from vmware_aria.connection import AriaApiError

    client = _conn(monkeypatch)
    client._token_expires_at = 0  # force a refresh on the next call

    monkeypatch.setattr(
        "httpx.Client.post",
        lambda self, url, **k: httpx.Response(503, request=httpx.Request("POST", url)),
    )

    with pytest.raises(AriaApiError) as exc_info:
        client.get("/resources")

    assert exc_info.value.status_code == 503
    assert exc_info.value.path == "/auth/token/acquire"


# ── POST is not retried by default (non-idempotent creates) ────────────


def test_post_not_retried_on_transient_504_by_default(monkeypatch) -> None:
    # A 504 after the server already accepted a POST (create report, create
    # alert definition) must not be replayed — that duplicates the side
    # effect. Only idempotent query callers opt in with retries=1.
    import httpx
    import pytest

    from vmware_aria.connection import AriaApiError

    client = _conn(monkeypatch)
    calls: list = []

    def fake_request(self, method, url, **k):
        calls.append((method, url))
        return httpx.Response(504, request=httpx.Request(method, url))

    monkeypatch.setattr("httpx.Client.request", fake_request)
    monkeypatch.setattr("vmware_aria.connection._RETRY_DELAY_SEC", 0)

    with pytest.raises(AriaApiError) as exc_info:
        client.post("/reports", json_data={"reportDefinitionId": "d-1"})

    assert exc_info.value.status_code == 504
    assert len(calls) == 1, "non-idempotent POST must not be retried by default"


def test_idempotent_query_posts_opt_into_retry() -> None:
    from vmware_aria.ops.alerts import list_alerts
    from vmware_aria.ops.resources import get_resource_metrics

    client = _client()
    client.post.return_value = {"alerts": []}
    list_alerts(client)
    assert client.post.call_args.kwargs.get("retries") == 1

    client.post.reset_mock()
    client.post.return_value = {"values": []}
    get_resource_metrics(client, "res-1", ["cpu|usage_average"])
    assert client.post.call_args.kwargs.get("retries") == 1


def test_create_posts_do_not_pass_retries() -> None:
    from vmware_aria.ops.reports import generate_report

    client = _client()
    client.post.return_value = {"id": "r-1", "status": "QUEUED"}
    generate_report(client, definition_id="d-1", resource_ids=["res-1"])
    assert "retries" not in client.post.call_args.kwargs, (
        "report creation is not idempotent — it must use the no-retry default"
    )


# ── stale clients are closed before replacement ────────────────────────


def test_stale_client_closed_before_replacement(monkeypatch) -> None:
    # ConnectionManager.connect() used to drop a dead client on the floor and
    # build a new one, leaking the old HTTP connection pool and auth token.
    from vmware_aria.config import AppConfig, TargetConfig
    from vmware_aria.connection import ConnectionManager

    cfg = AppConfig(
        targets={"t1": TargetConfig(host="h", username="u")},
        default_target="t1",
    )
    mgr = ConnectionManager(cfg)

    stale = MagicMock(name="stale-client")
    stale.is_alive_cached.return_value = False
    mgr._clients["t1"] = stale

    fresh = MagicMock(name="fresh-client")
    monkeypatch.setenv("VMWARE_ARIA_T1_PASSWORD", "pw")
    monkeypatch.setattr("vmware_aria.connection.AriaClient", lambda t, p, u=None: fresh)

    result = mgr.connect("t1")

    stale.close.assert_called_once()
    assert result is fresh


# ── CLI surfaces operational errors as one line, not a traceback ────────


def test_cli_aria_api_error_is_one_red_line_not_traceback(monkeypatch) -> None:
    # A bad UUID (404) at the CLI used to dump a full traceback; the
    # _friendly_errors wrapper must print the teaching message and exit 1.
    from typer.testing import CliRunner

    from vmware_aria.cli import app
    from vmware_aria.connection import AriaApiError, _hint_for_status

    def boom(target, config_path=None):
        raise AriaApiError(
            "Aria Operations GET /resources/bad-uuid returned HTTP 404. "
            + _hint_for_status(404, "/resources/bad-uuid"),
            status_code=404,
            method="GET",
            path="/resources/bad-uuid",
        )

    monkeypatch.setattr("vmware_aria.cli._get_connection", boom)
    result = CliRunner().invoke(app, ["resource", "get", "bad-uuid"])

    assert result.exit_code == 1
    assert "404" in result.output
    assert "list the parent" in result.output, "teaching hint must reach the user"
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_missing_config_is_one_line_not_traceback(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from vmware_aria.cli import app

    missing = tmp_path / "nope" / "config.yaml"
    result = CliRunner().invoke(app, ["alert", "list", "--config", str(missing)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "Error" in result.output

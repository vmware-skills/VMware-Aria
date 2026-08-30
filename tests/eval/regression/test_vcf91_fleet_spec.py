"""VCF Operations 9.1 fleet/findings/PromQL — spec conformance + behaviour.

Guards the read tools added for VCF Operations 9.1 (vmware_aria/ops/fleet.py,
vmware_aria/ops/promql.py) against 踩坑 #36 (a prior skill shipped hallucinated
endpoints, half of which 404'd):

* the endpoint spec is seeded at tests/eval/spec/vcf91_fleet_operations.json;
* every HTTP path the two new ops modules touch must resolve to a spec entry
  (AST scan of the two files + a check that each named path constant is listed);
* behaviour is pinned against a fake client so a shape drift degrades to empty
  rows rather than crashing (踩坑 形态 #1), and the PromQL 2-hop token exchange
  is exercised end to end.

The whole-tree phantom guard (test_aria_spec_conformance.py) also validates
these modules; this file adds the module-focused path checks and the behaviour
regressions the whole-tree scan cannot express.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VCF_SPEC_PATH = REPO_ROOT / "tests" / "eval" / "spec" / "vcf91_fleet_operations.json"
NEW_OPS_FILES = [
    REPO_ROOT / "vmware_aria" / "ops" / "fleet.py",
    REPO_ROOT / "vmware_aria" / "ops" / "promql.py",
]

_HTTP_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE"}


# ---------------------------------------------------------------------------
# Spec is present and well-formed
# ---------------------------------------------------------------------------


def _spec() -> dict:
    return json.loads(VCF_SPEC_PATH.read_text())


def test_vcf_spec_loads_and_lists_the_expected_endpoints() -> None:
    spec = _spec()
    assert spec["operation_count"] == len(spec["operations"])
    pairs = {(op["method"], op["path"]) for op in spec["operations"]}
    for expected in [
        ("POST", "/api/fleet-management/certificate-management/certificates/query"),
        ("POST", "/api/fleet-management/password-management/accounts/query"),
        ("GET", "/api/integrations/vcf/{integrationId}/domains"),
        ("POST", "/api/diagnostics/findings/query"),
        ("GET", "/api/integrations/services"),
        ("POST", "/api/auth/token/exchange"),
        ("GET", "/api/v1/query"),
    ]:
        assert expected in pairs, f"{expected} missing from the VCF 9.1 spec index"


def test_data_query_endpoint_is_marked_inferred() -> None:
    """The only INFERRED base must be the data-query PromQL path; suite-api VERIFIED."""
    for op in _spec()["operations"]:
        if op["base"] == "/data-query-service":
            assert op["verification"] == "INFERRED"
        else:
            assert op["verification"] == "VERIFIED"


# ---------------------------------------------------------------------------
# Named path constants are all spec-listed (mechanical doc<->code link)
# ---------------------------------------------------------------------------


def _spec_suffixes() -> set[tuple[str, str]]:
    """Spec (method, path-without-/api-prefix) pairs, param names normalised."""
    out = set()
    for op in _spec()["operations"]:
        path = op["path"]
        # ops call the client with paths relative to base_url (/suite-api/api),
        # so drop the leading /api for suite-api ops; data-query keeps /api/v1.
        rel = path[len("/api"):] if op["base"] == "/suite-api" and path.startswith("/api") else path
        rel = re.sub(r"\{[^}]+\}", "{param}", rel)
        out.add((op["method"], rel))
    return out


def test_ops_path_constants_are_all_in_the_spec() -> None:
    """Every endpoint constant in the new modules must resolve to a spec entry."""
    from vmware_aria.ops import fleet, promql

    spec = _spec_suffixes()

    # fleet.py suite-api constants
    assert ("POST", fleet.CERT_QUERY_PATH) in spec
    assert ("POST", fleet.PASSWORD_ACCOUNT_QUERY_PATH) in spec
    assert ("POST", fleet.FINDINGS_QUERY_PATH) in spec
    assert ("GET", re.sub(r"\{[^}]+\}", "{param}", fleet._domains_path("{id}"))) in spec

    # promql.py suite-api constants
    assert ("GET", promql.INTEGRATIONS_SERVICES_PATH) in spec
    assert ("POST", promql.TOKEN_EXCHANGE_PATH) in spec
    # data-query-service query path (INFERRED base)
    assert ("GET", promql.PROMQL_QUERY_PATH) in spec


# ---------------------------------------------------------------------------
# AST scan: the two new files touch only spec-listed suite-api paths
# ---------------------------------------------------------------------------


def _literal_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            parts.append(str(v.value) if isinstance(v, ast.Constant) else "{param}")
        return "".join(parts)
    return None


def _collect_calls(py: Path) -> list[tuple[int, str, str]]:
    """(lineno, METHOD, path) for every client.<get|post|put|delete>() in a file."""
    tree = ast.parse(py.read_text())
    assigned: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            val = _literal_path(node.value)
            if isinstance(tgt, ast.Name) and val is not None:
                assigned[tgt.id] = val
    calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = _HTTP_METHODS.get(node.func.attr)
        if method is None or not node.args:
            continue
        arg = node.args[0]
        path = _literal_path(arg)
        if path is None and isinstance(arg, ast.Name):
            path = assigned.get(arg.id)
        if path is None or not path.startswith("/"):
            continue
        calls.append((node.lineno, method, path))
    return calls


def test_new_ops_touch_only_spec_listed_paths() -> None:
    spec = _spec_suffixes()
    violations = []
    collected = 0
    for py in NEW_OPS_FILES:
        for lineno, method, path in _collect_calls(py):
            collected += 1
            norm = re.sub(r"\{param\}", "{param}", path)
            if (method, norm) not in spec:
                violations.append(f"{py.name}:{lineno}: {method} {path}")
    assert collected, "AST scan collected no client calls — scan broken?"
    assert not violations, (
        "new VCF ops call endpoints not in the VCF 9.1 spec "
        "(invented endpoints WILL 404 in production):\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Behaviour: fleet reads return the envelope and degrade defensively
# ---------------------------------------------------------------------------


def _client() -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.get.return_value = {}
    client.post.return_value = {}
    return client


ENVELOPE_KEYS = ("items", "returned", "limit", "total", "truncated", "hint")


def test_certificate_list_returns_envelope_and_summarizes() -> None:
    from vmware_aria.ops.fleet import list_fleet_certificates

    c = _client()
    c.post.return_value = {
        "certificates": [
            {"commonName": "vcenter.example", "issuer": "CA", "validTo": "2027-01-01", "status": "OK"}
        ]
    }
    result = list_fleet_certificates(c)
    for key in ENVELOPE_KEYS:
        assert key in result
    assert result["returned"] == 1
    assert result["items"][0]["subject"] == "vcenter.example"
    assert result["items"][0]["status"] == "OK"
    # verified path used
    assert c.post.call_args[0][0] == "/fleet-management/certificate-management/certificates/query"


def test_password_account_list_defends_against_unknown_shape() -> None:
    """An unexpected container/shape degrades to empty rows, never crashes (形态 #1)."""
    from vmware_aria.ops.fleet import list_fleet_password_accounts

    c = _client()
    c.post.return_value = {"somethingElse": "not a list"}
    result = list_fleet_password_accounts(c)
    assert result["items"] == []
    assert result["returned"] == 0
    assert result["truncated"] is False


def test_unrecognized_shape_flags_empty_result_as_unconfirmed() -> None:
    """A non-empty but unrecognised response must NOT read as a clean 'none'.

    Only the query paths are VERIFIED against the VCF 9.1 spec, not the response
    schemas. A drifted 9.1 shape returning items:[] with no note would let an
    agent report "no expiring certificates" — a dangerous false all-clear
    (形态 #1). The envelope carries a note when the shape was unrecognised.

    The body was ``{"unexpectedContainer": [{"commonName": "x"}]}`` until
    2026-08-30, when a real 9.1 fleet of 32 certificates came back empty
    because an unguessed container name was treated as unreadable. A lone list
    of records is now read by shape, so demonstrating "unrecognised" needs a
    body that carries no records at all — the note, and the reason for it, are
    unchanged.
    """
    from vmware_aria.ops.fleet import list_fleet_certificates

    c = _client()
    c.post.return_value = {"queryId": "q-7", "elapsedMs": 12}
    result = list_fleet_certificates(c)
    assert result["items"] == []
    assert result["total"] == 0
    assert "note" in result, "unrecognised shape must carry an unconfirmed-empty note"
    assert "unconfirmed" in result["note"]


def test_recognized_empty_result_carries_no_note() -> None:
    """A recognised empty container is a genuine 'none' — no unconfirmed note."""
    from vmware_aria.ops.fleet import list_findings

    c = _client()
    c.post.return_value = {"findings": []}
    result = list_findings(c)
    assert result["items"] == []
    assert result.get("note") is None


def test_domain_list_uses_integration_id_in_path() -> None:
    from vmware_aria.ops.fleet import list_fleet_domains

    c = _client()
    c.get.return_value = {"domains": [{"id": "d1", "name": "wld-01", "type": "WORKLOAD"}]}
    result = list_fleet_domains(c, integration_id="int-123")
    assert c.get.call_args[0][0] == "/integrations/vcf/int-123/domains"
    assert result["items"][0]["name"] == "wld-01"


def test_findings_list_builds_filter_body_only_for_supplied_filters() -> None:
    from vmware_aria.ops.fleet import list_findings

    c = _client()
    c.post.return_value = {"findings": [{"ruleUuid": "r1", "severity": "CRITICAL"}]}
    result = list_findings(c, severities=["CRITICAL"], categories=None)
    body = c.post.call_args.kwargs["json_data"]
    assert body == {"severities": ["CRITICAL"]}, "only supplied filters go in the body"
    assert result["items"][0]["severity"] == "CRITICAL"
    assert result["items"][0]["rule_uuid"] == "r1"


def test_findings_empty_query_sends_empty_body() -> None:
    from vmware_aria.ops.fleet import list_findings

    c = _client()
    c.post.return_value = {"findings": []}
    list_findings(c)
    assert c.post.call_args.kwargs["json_data"] == {}


# ---------------------------------------------------------------------------
# Behaviour: PromQL 2-hop token exchange
# ---------------------------------------------------------------------------


def _promql_client(*, has_vodap: bool = True, token: str = "jwt-abc") -> MagicMock:
    client = MagicMock(name="AriaClient")
    client.base_url = "https://ops.example:443/suite-api/api"

    services = (
        {"services": [{"type": "VCF_VODAP", "serviceKeys": ["k1"]}]} if has_vodap else {"services": []}
    )

    def _get(path, params=None):
        if path == "/integrations/services":
            return services
        return {}

    def _post(path, json_data=None, params=None, retries=0):
        if path == "/auth/token/exchange":
            return {"token": token} if token else {}
        return {}

    client.get.side_effect = _get
    client.post.side_effect = _post
    client.raw_request.return_value = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"__name__": "cpu"}, "value": [1700000000, "42.5"]}],
        },
    }
    return client


def test_promql_query_runs_two_hop_exchange_and_uses_bearer() -> None:
    from vmware_aria.ops.promql import run_promql_query

    c = _promql_client()
    result = run_promql_query(c, query="cpu_usage_average{}")

    # hop 2: token exchange posted the serviceKeys
    exchange = [call for call in c.post.call_args_list if call[0][0] == "/auth/token/exchange"]
    assert exchange, "token exchange was not attempted"
    assert exchange[0].kwargs["json_data"] == {"serviceKeys": ["k1"]}

    # data-query call carried the Bearer JWT to the /data-query-service base
    args, kwargs = c.raw_request.call_args
    assert args[0] == "GET"
    assert args[1] == "https://ops.example:443/data-query-service/api/v1/query"
    assert kwargs["headers"]["Authorization"] == "Bearer jwt-abc"
    assert kwargs["params"]["query"] == "cpu_usage_average{}"

    # envelope + Prometheus projection + INFERRED caveat surfaced
    assert result["result_type"] == "vector"
    assert result["status"] == "success"
    assert result["base_path_confirmed"] is False
    assert result["items"][0]["value"] == "42.5"
    assert result["items"][0]["labels"]["__name__"] == "cpu"


def test_promql_query_rejects_empty_expression() -> None:
    from vmware_aria.ops.promql import run_promql_query

    c = _promql_client()
    with pytest.raises(ValueError, match="query"):
        run_promql_query(c, query="   ")
    c.raw_request.assert_not_called()


def test_promql_missing_vodap_service_is_a_teaching_error() -> None:
    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.promql import run_promql_query

    c = _promql_client(has_vodap=False)
    with pytest.raises(AriaApiError, match="VODAP"):
        run_promql_query(c, query="up")
    c.raw_request.assert_not_called()


def test_promql_exchange_without_token_is_a_teaching_error() -> None:
    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.promql import run_promql_query

    c = _promql_client(token="")
    with pytest.raises(AriaApiError, match="token"):
        run_promql_query(c, query="up")


def test_promql_unrecognized_services_shape_does_not_claim_vodap_absent() -> None:
    """An unparseable /integrations/services response must not confidently say
    'VODAP not registered — enable it'; it may in fact be registered (形态 #1)."""
    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.promql import run_promql_query

    c = _promql_client()
    # Non-empty but unrecognised container shape for the services list.
    c.get.side_effect = lambda path, params=None: (
        {"payload": {"data": [{"type": "VCF_VODAP"}]}}
        if path == "/integrations/services"
        else {}
    )
    with pytest.raises(AriaApiError) as ei:
        run_promql_query(c, query="up")
    msg = str(ei.value)
    assert "unrecognised shape" in msg
    # The property, not one phrasing of it: the message must leave open that
    # VODAP *is* registered. Asserting the exact sentence made this test fail on
    # a reword that preserved the meaning — and the reword was itself required,
    # because the original ran to 436 characters and `sanitize(str(exc), 300)`
    # cut the remedy off before the agent saw it.
    assert any(
        phrase in msg for phrase in ("may in fact be registered", "it may well be")
    ), f"message no longer allows that VODAP is registered: {msg}"
    # Must NOT emit the confident "Enable the ... integration" instruction.
    assert "Enable the real-time metrics (VODAP) integration" not in msg
    # And the remedy has to survive the cap that made the reword necessary.
    from vmware_policy import sanitize

    assert sanitize(msg, 300) == msg, f"{len(msg)} chars — the remedy is cut"
    c.raw_request.assert_not_called()


def test_promql_vodap_without_service_keys_is_a_teaching_error() -> None:
    """A VODAP service exposing no serviceKeys raises the authored hint instead
    of posting {"serviceKeys": null} and eating a generic 400 (LOW-1)."""
    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.promql import run_promql_query

    c = _promql_client()
    c.get.side_effect = lambda path, params=None: (
        {"services": [{"type": "VCF_VODAP"}]}  # registered, but no serviceKeys
        if path == "/integrations/services"
        else {}
    )
    with pytest.raises(AriaApiError, match="serviceKeys"):
        run_promql_query(c, query="up")
    # The token exchange must not be attempted with a null serviceKeys body.
    exchange = [call for call in c.post.call_args_list if call[0][0] == "/auth/token/exchange"]
    assert not exchange, "must not POST the exchange with empty serviceKeys"
    c.raw_request.assert_not_called()


def test_promql_data_query_401_rewords_toward_service_token_not_password() -> None:
    """A 401 from the data-query service is the exchanged JWT being rejected, not
    a suite-api password problem; the teaching text must not point at the
    VMWARE_ARIA_<TARGET>_PASSWORD env var (LOW-2)."""
    from vmware_aria.connection import AriaApiError
    from vmware_aria.ops.promql import run_promql_query

    c = _promql_client()
    c.raw_request.side_effect = AriaApiError(
        "Aria Operations returned HTTP 401. Authentication/authorization "
        "failed — check username/auth_source ... VMWARE_ARIA_<TARGET>_PASSWORD ...",
        status_code=401,
        method="GET",
        path="https://ops.example:443/data-query-service/api/v1/query",
    )
    with pytest.raises(AriaApiError) as ei:
        run_promql_query(c, query="up")
    msg = str(ei.value)
    assert "VMWARE_ARIA" not in msg, "must not blame the suite-api password env var"
    assert "VODAP" in msg
    assert "service token" in msg


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


def test_new_fleet_tools_register_on_the_mcp_server() -> None:
    from vmware_aria.mcp_server.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    for tool in (
        "fleet_certificate_list",
        "fleet_password_account_list",
        "fleet_domain_list",
        "findings_list",
        "promql_query",
    ):
        assert tool in names, f"{tool} not registered on the MCP server"


def test_new_fleet_tools_are_read_only() -> None:
    from vmware_aria.mcp_server.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in (
        "fleet_certificate_list",
        "fleet_password_account_list",
        "fleet_domain_list",
        "findings_list",
        "promql_query",
    ):
        ann = tools[name].annotations
        assert ann and ann.readOnlyHint is True, f"{name} must be readOnlyHint=True"

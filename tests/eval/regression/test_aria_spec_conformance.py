"""Spec-conformance regression eval — every API call must exist in suite-api.

2026-06-08 external user report: half of the Aria MCP API returned 404 —
multiple endpoints (anomalies, badge/*, remainingcapacity, timeremaining,
recommendations/rightsizing, resources/query/topn, alerts/{id}/acknowledge,
DELETE alerts/{id}) were never part of the vROps/Aria Operations suite-api.
Root cause: the API layer was written from memory and never validated
against the official specification.

This test AST-parses every ``client.<get|post|put|delete>("<path>")`` call
in vmware_aria/ (ops + connection) and asserts the (method, path) pair
exists in the official vROps 8.6 operation index stored at
tests/eval/spec/vrops86_operations.json (315 operations, parsed from the
official Swagger UI dump).

Any future invented endpoint fails here instead of 404-ing in production.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_DIR = REPO_ROOT / "tests" / "eval" / "spec"
SPEC_PATH = SPEC_DIR / "vrops86_operations.json"
# VCF Operations 9.1 suite-api endpoints added for the fleet/findings/PromQL
# read tools (vmware_aria/ops/fleet.py, promql.py). Merged into the matcher so
# the whole-tree phantom-endpoint guard recognises them instead of flagging
# them as invented (踩坑 #36). Only the suite-api-based operations are merged;
# the data-query-service PromQL call is on a separate base + Bearer JWT and is
# issued via client.raw_request(), which this AST scan does not collect.
VCF_SPEC_PATH = SPEC_DIR / "vcf91_fleet_operations.json"
SCAN_DIRS = [REPO_ROOT / "vmware_aria"]

_HTTP_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE"}


def _spec_matchers() -> list[tuple[str, re.Pattern]]:
    """Spec operations as (method, compiled path regex with {param} wildcards)."""
    operations = list(json.loads(SPEC_PATH.read_text(encoding="utf-8"))["operations"])
    # Add the VCF 9.1 suite-api operations (base "/suite-api"). The lone
    # data-query-service op (base "/data-query-service") is excluded — it is
    # not reachable by the suite-api-relative calls this scan validates.
    for op in json.loads(VCF_SPEC_PATH.read_text(encoding="utf-8"))["operations"]:
        if op.get("base", "/suite-api") == "/suite-api":
            operations.append(op)
    matchers = []
    for op in operations:
        # /api/resources/{id}/stats -> ^/api/resources/[^/]+/stats$
        pattern = re.sub(r"\{[^}]+\}", r"[^/]+", op["path"])
        matchers.append((op["method"], re.compile(f"^{re.escape('')}{pattern}$")))
    return matchers


def _literal_path(node: ast.AST) -> str | None:
    """Extract a path template from a str constant or f-string argument."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                parts.append("{param}")
        return "".join(parts)
    return None


def _collect_api_calls() -> list[tuple[str, str, str]]:
    """All (location, METHOD, path) client/self._client HTTP calls under vmware_aria/."""
    calls = []
    for scan_dir in SCAN_DIRS:
        for py in sorted(scan_dir.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            # simple `url = "<literal-or-fstring>"` assignments, so calls like
            # `self._client.post(url, ...)` (token acquire) resolve too
            assigned: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    tgt = node.targets[0]
                    val = _literal_path(node.value)
                    if isinstance(tgt, ast.Name) and val is not None:
                        assigned[tgt.id] = val
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
                if path is None:
                    continue
                # full URLs (token acquire uses an f-string with base_url) —
                # strip the leading interpolated host segment and keep the
                # path tail so auth endpoints are validated, not skipped.
                if path.startswith("{param}/"):
                    path = path[len("{param}"):]
                if not path.startswith("/"):
                    continue
                calls.append((f"{py.relative_to(REPO_ROOT).as_posix()}:{node.lineno}", method, path))
    return calls


def test_spec_index_is_loaded() -> None:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert spec["operation_count"] >= 300, "spec index missing or truncated"


def test_auth_endpoints_are_scanned_not_skipped() -> None:
    """Regression: f-string URLs like f"{self._base_url}/auth/token/acquire"
    rendered as "{param}/..." and were silently dropped by the AST scan, so
    the auth endpoints were never validated against the spec."""
    calls = _collect_api_calls()
    paths = {(method, path) for _, method, path in calls}
    assert ("POST", "/auth/token/acquire") in paths, (
        "auth token acquire call must be collected by the AST scan "
        f"(got {sorted(p for m, p in paths if 'auth' in p)})"
    )


def test_raw_request_confined_to_connection_and_promql() -> None:
    """``client.raw_request(...)`` bypasses the (method, path) phantom scanner.

    The AST guard above only collects get/post/put/delete calls, so a NEW call
    site reaching an unverified URL via ``raw_request`` would ship a 踩坑 #36
    phantom with every scanner still green. Pin the surface: the name may appear
    ONLY in connection.py (its definition) and ops/promql.py (the one gated,
    base_path_confirmed=False call site). Any other file must fail loudly and be
    added to the phantom-endpoint audit before it ships.
    """
    allowed = {"vmware_aria/connection.py", "vmware_aria/ops/promql.py"}
    offenders: list[str] = []
    found: set[str] = set()
    for scan_dir in SCAN_DIRS:
        for py in sorted(scan_dir.rglob("*.py")):
            # as_posix(), not str(): on Windows str() yields
            # "vmware_aria\\connection.py" and the allowlist below is written
            # with forward slashes, so every file looked like a new offender
            # and the guard's own "did it scan anything" assertion fired. The
            # guard was right; the comparison was platform-dependent.
            rel = py.relative_to(REPO_ROOT).as_posix()
            if "raw_request" in py.read_text(encoding="utf-8"):
                found.add(rel)
                if rel not in allowed:
                    offenders.append(rel)
    # 防「空结果读作没问题」(形态 #1): the scan must actually see both known
    # sites, or a bad glob would pass this test while checking nothing.
    assert allowed <= found, (
        "raw_request guard scanned no known sites — glob/paths broken? "
        f"expected {sorted(allowed)}, found {sorted(found)}"
    )
    assert not offenders, (
        "raw_request() used outside connection.py/ops.promql.py — this bypasses "
        "the (method, path) phantom-endpoint scanner (踩坑 #36). Add the new "
        "call site to the spec audit, or route it through get/post/put/delete:\n  "
        + "\n  ".join(offenders)
    )


def test_every_api_call_exists_in_suite_api_spec() -> None:
    matchers = _spec_matchers()
    calls = _collect_api_calls()
    assert calls, "no API calls collected — AST scan broken?"

    violations = []
    for loc, method, path in calls:
        # ops paths omit the /api prefix (client base_url is /suite-api/api)
        candidate = path if path.startswith("/api/") else f"/api{path}"
        # normalize f-string params to a single segment placeholder
        candidate = re.sub(r"\{param\}", "PARAM", candidate)
        candidate = re.sub(r"PARAM[^/]*", "PARAM", candidate)
        if not any(m == method and rx.match(candidate) for m, rx in matchers):
            violations.append(f"{loc}: {method} {path}")

    assert not violations, (
        "API calls not present in the vROps 8.6 suite-api spec "
        "(invented endpoints WILL 404 in production):\n  " + "\n  ".join(violations)
    )

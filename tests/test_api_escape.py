"""HLD §17 D-18: the read-only API escape hatch (search_api_operations, get_api_path).

A GET is issued only for a path matching an indexed GET operation; every other
request is refused before the client is touched. The operation index ships in
the package (one copy, also read by the spec-conformance tests).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from vmware_aria.ops import api_escape
from vmware_aria.ops.api_escape import MAX_RESULT_CHARS, get_api_path, search_api_operations

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, body: Any = None, version: str | None = "8.18.7") -> None:
        self.body = {"ok": True} if body is None else body
        self.version = version
        self.calls: list[tuple[str, Any]] = []

    def product_version(self) -> str | None:
        return self.version

    def get(self, path: str, params: Any = None) -> Any:
        self.calls.append((path, params))
        return self.body


def _only_in(label: str) -> str:
    """A concrete path whose GET operation is documented only in index ``label``."""
    for op in api_escape.operations():
        if op.method == "GET" and op.indexes == (label,) and "{" in op.path:
            return op.path.replace("{", "x").replace("}", "")
    raise AssertionError(f"no GET operation only in the {label} index")


# ── allowed ───────────────────────────────────────────────────────────────


def test_indexed_get_path_reaches_the_client_with_params() -> None:
    client = FakeClient()
    out = get_api_path(client, "/api/resources/abc-123/properties", {"pageSize": "20"})
    assert client.calls == [("/resources/abc-123/properties", {"pageSize": "20"})]
    assert out["operation"] == "/api/resources/{id}/properties"
    assert out["truncated"] is False and out["body"] == {"ok": True}


def test_path_without_api_prefix_is_read_as_suite_api_path() -> None:
    client = FakeClient()
    get_api_path(client, "/resources/abc/properties")
    assert client.calls == [("/resources/abc/properties", None)]


def test_body_strings_are_sanitized() -> None:
    client = FakeClient(body={"name​": "vm\x00-01‮", "n": 3, "items": ["a" * 900]})
    body = get_api_path(client, "/api/resources/abc")["body"]
    assert body["name"] == "vm-01"
    assert body["n"] == 3
    assert len(body["items"][0]) == 500


def test_9x_appliance_is_held_to_the_9_1_index() -> None:
    path = _only_in("8.6")
    client = FakeClient(version="9.0.1")
    with pytest.raises(ValueError, match="documented only in the 8.6"):
        get_api_path(client, path)
    assert client.calls == []
    # On 8.x (or unknown) the same path is accepted.
    get_api_path(FakeClient(version=None), path)


# ── refused, before any request ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "params", "message"),
    [
        ("/api/actions/abc", None, "only reads"),  # POST-only operation
        ("/api/nothing/here", None, "not a GET operation"),
        ("/api/resources/../auth/users", None, r"'\.\.'"),
        ("/api/resources//abc", None, "'//'"),
        ("/api/resources?pageSize=1", None, "params"),
        ("/api/resources#x", None, "params"),
        ("https://evil.example/api/resources", None, "not a URL"),
        ("api/resources", None, "not a URL"),
        ("/api/resources/%2e%2e/properties", None, "character"),
        ("/api/resources/{id}/properties", None, "character"),
        ("/api/resources", {"pageSize": 20}, "strings"),
        ("/api/resources", ["pageSize=20"], "map"),
        ("", None, "path must be"),
    ],
)
def test_refusals_never_reach_the_client(path: str, params: Any, message: str) -> None:
    client = FakeClient()
    with pytest.raises(ValueError, match=message):
        get_api_path(client, path, params)
    assert client.calls == []


def test_mcp_tool_refuses_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_aria.mcp_server import server

    def no_connect(target: Any = None) -> Any:
        raise AssertionError("a refused path must not log in")

    monkeypatch.setattr(server, "_get_connection", no_connect)
    out = server.get_api_path("/api/alerts/abc/../x")
    assert "error" in out and "'..'" in out["error"]


def test_mcp_tool_passes_an_allowed_get_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_aria.mcp_server import server

    client = FakeClient()
    monkeypatch.setattr(server, "_get_connection", lambda target=None: client)
    out = server.get_api_path("/api/alerts", {"pageSize": "5"}, target="prod")
    assert client.calls == [("/alerts", {"pageSize": "5"})]
    assert out["body"] == {"ok": True}


# ── size cap ──────────────────────────────────────────────────────────────


def test_large_answer_is_capped_and_says_so() -> None:
    body = {"resourceList": [{"name": f"vm-{i:05d}", "id": "x" * 36} for i in range(2000)]}
    out = get_api_path(FakeClient(body=body), "/api/resources")
    full = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    assert out["truncated"] is True
    assert out["original_size"] == len(full) > MAX_RESULT_CHARS
    assert out["body_text"] == full[:MAX_RESULT_CHARS]
    assert "body" not in out and "pageSize" in out["hint"]


# ── search ────────────────────────────────────────────────────────────────


def test_search_marks_get_operations_and_ranks_path_matches() -> None:
    out = search_api_operations("resource properties", limit=100)
    rows = {(op["method"], op["path"]): op for op in out["operations"]}
    assert rows[("GET", "/api/resources/{id}/properties")]["callable_with_get_api_path"] is True
    assert rows[("POST", "/api/resources/properties")]["callable_with_get_api_path"] is False
    assert out["total_matches"] == len(out["operations"]) == out["returned"]


@pytest.mark.parametrize("limit", [0, 101, True, "5"])
def test_search_limit_is_validated(limit: Any) -> None:
    with pytest.raises(ValueError, match="1 to 100"):
        search_api_operations("alerts", limit=limit)


def test_search_rejects_an_empty_query() -> None:
    with pytest.raises(ValueError, match="words"):
        search_api_operations("   ")


def test_index_is_loaded_from_the_package_and_not_empty() -> None:
    ops = api_escape.operations()
    assert api_escape.SPEC_DIR == REPO_ROOT / "vmware_aria" / "api_spec"
    assert sum(op.method == "GET" for op in ops) > 200
    assert {"8.6", "9.1"} <= {label for op in ops for label in op.indexes}


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_built_wheel_ships_the_index(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=180,
    )
    (wheel,) = tmp_path.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for name in ("vrops86_operations.json", "vcfops91_operations.json", "vcf91_fleet_operations.json"):
        assert f"vmware_aria/api_spec/{name}" in names


@pytest.mark.parametrize("tail", ["\n", "\r\n", "\n/../x"])
def test_a_trailing_newline_is_refused_before_any_request(tail):
    from vmware_aria.ops.api_escape import check_request

    with pytest.raises(ValueError):
        check_request("/api/resources" + tail, None)

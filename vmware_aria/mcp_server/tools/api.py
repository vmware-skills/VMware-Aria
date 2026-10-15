"""RAW API tools (2, read-only): the D-18 escape hatch (HLD §17).

search_api_operations finds a suite-api operation in the operation index this
package ships; get_api_path issues one GET of an indexed GET path through the
target's AriaClient. For questions no dedicated tool answers.
"""

from typing import Optional

from vmware_policy import vmware_tool

from vmware_aria.mcp_server._shared import mcp

_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
_LOCAL_READ = {**_READ, "openWorldHint": False}
_DOCTOR_HINT = "Run 'vmware-aria doctor' to verify connectivity."


@mcp.tool(annotations=_LOCAL_READ)
@vmware_tool(risk_level="low")
def search_api_operations(query: str, limit: int = 20) -> dict:
    """[READ] Search the Aria Operations suite-api operation index (vROps 8.6 + VCF Operations 9.1) shipped with this skill, before get_api_path. Use only when no dedicated tool answers the question.

    Returns method, path template, summary and which index documents it; callable_with_get_api_path is true only for GET. Words must all appear in method, path or summary; path matches rank first. Local: contacts no appliance.

    Args:
        query: Words to find, e.g. "resource properties" or "alert notes".
        limit: Operations to return, 1-100 (default 20).
    """
    from vmware_aria.mcp_server import server

    try:
        from vmware_aria.ops.api_escape import search_api_operations as _search

        return _search(query, limit=limit)
    except Exception as e:
        return {"error": server._safe_error(e, "search_api_operations")}


@mcp.tool(annotations=_READ)
@vmware_tool(risk_level="low")
def get_api_path(
    path: str,
    params: Optional[dict[str, str]] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] Raw GET of one suite-api path found with search_api_operations, for reads no dedicated tool covers. Prefer the dedicated tools: their results are shaped and paginated.

    path must match a GET operation of the index, with each {name} replaced by a value: /api/resources/<uuid>/properties. On a 9.x appliance only the 9.1 index applies; on 8.x, or if the version cannot be read, a path from either index is accepted (a path the appliance lacks returns a 404 error). Refused before any request: other methods, unindexed paths, '..' or '//', ? or # in path, URLs, non-string params.

    Returns {path, operation, index, truncated, body}; strings are sanitized and cut to 500 characters. Over 30,000 characters of JSON returns truncated: true, original_size and body_text (cut, not valid JSON) — page with params such as pageSize.

    Args:
        path: Suite-api path starting /api/, e.g. /api/resources/<uuid>/properties.
        params: Query parameters as strings, e.g. {"pageSize": "20", "page": "0"}.
        target: Aria target name from config; default when omitted.
    """
    from vmware_aria.mcp_server import server

    from vmware_aria.ops.api_escape import get_api_path as _get
    from vmware_aria.ops.api_escape import check_request

    try:
        check_request(path, params)  # refuse before connecting: a bad path costs no login
    except ValueError as e:
        return {"error": server._safe_error(e, "get_api_path")}
    try:
        return _get(server._get_connection(target), path, params)
    except Exception as e:
        return {"error": server._safe_error(e, "get_api_path"), "hint": _DOCTOR_HINT}

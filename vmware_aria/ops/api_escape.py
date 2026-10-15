"""Read-only API escape hatch (HLD §17 D-18).

Two operations over the suite-api operation indexes this package ships in
``vmware_aria/api_spec/`` — the same files the spec-conformance tests check every
built-in call against, so there is one copy:

* :func:`search_api_operations` — find operations by words in method, path and
  summary. Local; touches no appliance.
* :func:`get_api_path` — one raw ``GET`` through the target's ``AriaClient``
  (its token, retries and error translation), allowed only when the path matches
  a **GET** operation of an index. Everything else is refused before any request:
  other methods, paths outside the index, ``..`` / ``//``, query strings or
  fragments in the path, absolute URLs, non-string parameters.

Which index applies: a 9.x appliance (read from ``/versions/current`` once per
client) is held to the VCF Operations 9.1 index. On 8.x, or when the version
cannot be read, a path in either index is accepted — the 8.6 index is the only
8.x specification held and 8.18 answers paths it lacks, so refusing them would
block real reads; a path the appliance does not have comes back as a 404 error.

The body is sanitized string by string (``vmware_policy.sanitize``: control and
format characters stripped, each string cut to 500 characters), then capped at
:data:`MAX_RESULT_CHARS` of JSON text.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vmware_policy import sanitize

SPEC_DIR = Path(__file__).resolve().parent.parent / "api_spec"

#: Index label -> files. The fleet file is a verified 9.1 subset; only its
#: suite-api operations belong to this base URL.
_INDEX_FILES: dict[str, tuple[str, ...]] = {
    "8.6": ("vrops86_operations.json",),
    "9.1": ("vcfops91_operations.json", "vcf91_fleet_operations.json"),
}

#: JSON text cap for one get_api_path answer. About 8k tokens: large enough for
#: one resource, one page of a list (pass pageSize) or a stats series, small
#: enough that a single unpaged list — suite-api pages default to 1000 rows and
#: can run to megabytes — cannot flood the agent's context.
MAX_RESULT_CHARS = 30_000

SEARCH_LIMIT_MAX = 100
_QUERY_MAX = 200
_PATH_MAX = 500
_PARAMS_MAX = 50
_PARAM_KEY_MAX = 100
_PARAM_VALUE_MAX = 2000
_STRING_MAX = 500

#: What one ``{id}`` segment may hold: ids, keys, names. No ``%`` (an encoded
#: ``..`` or ``/``), no ``/``, ``?``, ``#`` or backslash.
_SEGMENT = r"[A-Za-z0-9._~:@+,= -]+"
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~:@+,=/ -]*$")

_SEARCH_HINT = "Call search_api_operations with words from what you want (e.g. 'resource properties') to find the path."


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    summary: str
    indexes: tuple[str, ...]
    pattern: re.Pattern[str]


def _compile(path: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}/]+\})", path)
    body = "".join(_SEGMENT if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile(f"^{body}$")


@functools.lru_cache(maxsize=1)
def operations() -> tuple[Operation, ...]:
    """Every indexed suite-api operation, merged by (method, path)."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for label, files in _INDEX_FILES.items():
        for name in files:
            data = json.loads((SPEC_DIR / name).read_text(encoding="utf-8"))
            for op in data["operations"]:
                if op.get("base", "/suite-api") != "/suite-api":
                    continue
                key = (op["method"].upper(), op["path"])
                entry = merged.setdefault(key, {"summary": op.get("description", ""), "indexes": []})
                if label not in entry["indexes"]:
                    entry["indexes"].append(label)
    if not merged:  # 形态 #1: an empty index must not read as "no such path"
        raise RuntimeError(f"No API operations found under {SPEC_DIR}; the package is incomplete.")
    return tuple(
        Operation(method, path, entry["summary"], tuple(entry["indexes"]), _compile(path))
        for (method, path), entry in sorted(merged.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    )


def _check_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_LIMIT_MAX:
        raise ValueError(f"limit must be a whole number from 1 to {SEARCH_LIMIT_MAX} (got {limit!r}).")
    return limit


def search_api_operations(query: str, limit: int = 20) -> dict[str, Any]:
    """Operations whose method, path or summary contain every word of ``query``."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be one or more words, e.g. 'resource properties' or 'alerts'.")
    if len(query) > _QUERY_MAX:
        raise ValueError(f"query must be at most {_QUERY_MAX} characters (got {len(query)}).")
    limit = _check_limit(limit)
    terms = query.lower().split()
    hits = [op for op in operations() if all(t in f"{op.method} {op.path} {op.summary}".lower() for t in terms)]
    # Words found in the path rank first, then GET (callable) before the rest.
    hits.sort(key=lambda op: (-sum(t in op.path.lower() for t in terms), op.method != "GET", op.path))
    shown = hits[:limit]
    return {
        "query": sanitize(query, _QUERY_MAX),
        "total_matches": len(hits),
        "returned": len(shown),
        "operations": [
            {
                "method": op.method,
                "path": op.path,
                "summary": sanitize(op.summary, 200),
                "callable_with_get_api_path": op.method == "GET",
                "indexes": list(op.indexes),
            }
            for op in shown
        ],
        "note": (
            "Only operations with callable_with_get_api_path true can be called (GET). Replace each "
            "{name} segment with a real value; put query parameters in params, not in the path. "
            "indexes says which API versions document the path (8.6 = vROps 8.x, 9.1 = VCF Operations 9.x)."
        ),
    }


def _check_path(path: Any) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError(f"path must be an API path such as /api/resources/<id>. {_SEARCH_HINT}")
    if len(path) > _PATH_MAX:
        raise ValueError(f"path must be at most {_PATH_MAX} characters (got {len(path)}).")
    if "://" in path or not path.startswith("/"):
        raise ValueError(
            "path must be a suite-api path starting with /api/, not a URL; the target's host comes "
            "from its config. " + _SEARCH_HINT
        )
    if "?" in path or "#" in path:
        raise ValueError("Put query parameters in params (a {name: value} map), not in path; path takes no ? or #.")
    if "//" in path or any(seg in (".", "..") for seg in path.split("/")):
        raise ValueError("path must not contain '//', '.' or '..' segments.")
    if not _SAFE_PATH.match(path):
        raise ValueError(
            "path holds a character an API path segment cannot (allowed: letters, digits, space and "
            "- . _ ~ : @ + , =). Replace {name} placeholders with real values; do not percent-encode."
        )
    return path if path.startswith("/api/") else f"/api{path}"


def _check_params(params: Any) -> dict[str, str] | None:
    if params is None:
        return None
    if not isinstance(params, dict):
        raise ValueError('params must be a map of parameter name to string value, e.g. {"pageSize": "20"}.')
    if len(params) > _PARAMS_MAX:
        raise ValueError(f"params may hold at most {_PARAMS_MAX} entries (got {len(params)}).")
    for key, value in params.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"params names and values must be strings (got {type(key).__name__}: {type(value).__name__} "
                f'for {sanitize(str(key), 50)!r}); write numbers and booleans as text, e.g. "20", "true".'
            )
        if not key or len(key) > _PARAM_KEY_MAX or len(value) > _PARAM_VALUE_MAX:
            raise ValueError(
                f"params names must be 1-{_PARAM_KEY_MAX} characters and values at most {_PARAM_VALUE_MAX}."
            )
    return dict(params)


def _indexes_for(client: Any) -> tuple[tuple[str, ...], str | None]:
    """Index labels that apply to this client's appliance, and the version read."""
    try:
        version = client.product_version()
    except Exception:  # noqa: BLE001 — product_version never raises; a fake might
        version = None
    major = re.match(r"^(\d+)\.", version) if isinstance(version, str) else None
    if major and int(major.group(1)) >= 9:
        return ("9.1",), version
    return ("8.6", "9.1"), version if isinstance(version, str) else None


def _match(api_path: str, labels: tuple[str, ...]) -> Operation:
    candidates = [op for op in operations() if op.pattern.match(api_path)]
    for op in candidates:
        if op.method == "GET" and set(op.indexes) & set(labels):
            return op
    documented = sorted({label for op in candidates if op.method == "GET" for label in op.indexes})
    if documented:
        raise ValueError(
            f"GET {api_path} is documented only in the {', '.join(documented)} API index, not in the one "
            f"for this appliance's version ({', '.join(labels)}). {_SEARCH_HINT}"
        )
    if candidates:
        methods = ", ".join(sorted({op.method for op in candidates}))
        raise ValueError(
            f"{api_path} is a {methods} operation; get_api_path only reads (GET). Use the dedicated tool, "
            f"or {_SEARCH_HINT[0].lower()}{_SEARCH_HINT[1:]}"
        )
    raise ValueError(f"{api_path} is not a GET operation in the API index. {_SEARCH_HINT}")


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize(value, _STRING_MAX)
    if isinstance(value, dict):
        return {sanitize(str(k), 200): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value  # numbers, booleans, None


def check_request(path: Any, params: Any) -> tuple[str, dict[str, str] | None]:
    """Validate path and params without touching the appliance (raises ValueError).

    Also refuses a path that is a GET operation in no index, so a bad path never
    costs a login. Which index applies is decided later, from the appliance.
    """
    api_path = _check_path(path)
    clean_params = _check_params(params)
    _match(api_path, tuple(_INDEX_FILES))
    return api_path, clean_params


def get_api_path(client: Any, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """One read-only GET of an indexed suite-api path; sanitized and size-capped."""
    api_path, clean_params = check_request(path, params)
    labels, version = _indexes_for(client)
    op = _match(api_path, labels)
    # The client's base URL already ends in /suite-api/api.
    body = _clean(client.get(api_path[len("/api") :], params=clean_params))
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    result: dict[str, Any] = {
        "path": api_path,
        "operation": op.path,
        "summary": sanitize(op.summary, 200),
        "index": list(labels),
        "product_version": version,
    }
    if len(text) <= MAX_RESULT_CHARS:
        return {**result, "truncated": False, "body": body}
    return {
        **result,
        "truncated": True,
        "original_size": len(text),
        "body_text": text[:MAX_RESULT_CHARS],
        "hint": (
            f"The answer was {len(text)} characters of JSON; the first {MAX_RESULT_CHARS} are in body_text "
            "(cut mid-document, not valid JSON). Narrow it: for list operations pass params such as "
            '{"pageSize": "20", "page": "0"}, or read one item by id.'
        ),
    }

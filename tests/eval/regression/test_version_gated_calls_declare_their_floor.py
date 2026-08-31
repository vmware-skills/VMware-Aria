"""Every call absent from the 8.6 spec must declare the version it needs.

The sibling test ``test_aria_spec_conformance`` merges the vROps 8.6 and the
VCF Operations 9.1 operation indexes into one matcher, because its question is
"was this endpoint invented?" (踩坑 #36). Merging is right for that question and
useless for this one: once merged, a 9.1-only path is indistinguishable from one
that has existed since 8.6.

This test keeps them apart and asks the other question. A path the code calls
that appears only in the 9.1 index does not exist on an 8.x appliance, so the
call returns 404 there — and the generic 404 remedy is "verify the id, list the
parent collection and copy an exact UUID". That advice is actively wrong here:
the id was fine, the endpoint is not there, and the operator goes looking for a
UUID that was never the problem.

So a 9.x-only call site must pass ``requires=`` (a ``vmware_policy.compat.Requires``)
and the connection layer turns its 404 into a version explanation instead.

What this test deliberately does NOT do
---------------------------------------
It does not ask for ``requires=`` on the ~20 call sites that are in the 8.6
index. Those work identically on 7.x, 8.x and 9.x, and a version branch that
never differs is just somewhere for a future reader to introduce a difference by
accident. The floor is declared only where the two versions actually diverge.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_DIR = REPO_ROOT / "tests" / "eval" / "spec"
OLDEST_SPEC = SPEC_DIR / "vrops86_operations.json"
PACKAGE = REPO_ROOT / "vmware_aria"

_HTTP = {"get", "post", "put", "delete"}


def _shape(path: str) -> str:
    """``/resources/{id}/stats`` and an f-string's ``/resources/{}/stats`` agree."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _oldest_shapes() -> set[str]:
    spec = json.loads(OLDEST_SPEC.read_text(encoding="utf-8"))
    shapes = set()
    for op in spec["operations"]:
        path = op["path"]
        if path.startswith("/api/"):  # spec is absolute; code is relative to /suite-api/api
            path = path[len("/api") :]
        shapes.add(_shape(path))
    assert shapes, f"no operations parsed from {OLDEST_SPEC} — check broken"
    return shapes


def _literal(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Resolve a call's first argument to a path string, or None.

    Module-level constants and f-strings both resolve here. Only resolving bare
    literals is exactly how the first version of this scan reported "0 problems":
    the 9.1 paths were the ones hoisted into named constants, so a literal-only
    scan skipped precisely the population it existed to check (形态 #1).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return consts.get(f"{node.func.id}()")
    return None


def _call_sites() -> list[tuple[str, str, int, bool]]:
    """(path, file, lineno, declares_requires) for every client.<verb>(...) call."""
    sites: list[tuple[str, str, int, bool]] = []
    for file in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(file.read_bytes().decode("utf-8"))
        consts: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            consts[t.id] = node.value.value
            # A helper whose whole body is `return f"/some/{x}/path"` is a path
            # builder; record it under "name()" so calls through it resolve too.
            if isinstance(node, ast.FunctionDef) and len(node.body) == 1:
                stmt = node.body[0]
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    built = _literal(stmt.value, {})
                    if built and built.startswith("/"):
                        consts[f"{node.name}()"] = built
            if isinstance(node, ast.FunctionDef):
                docless = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
                if len(docless) == 1 and isinstance(docless[0], ast.Return) and docless[0].value is not None:
                    built = _literal(docless[0].value, {})
                    if built and built.startswith("/"):
                        consts[f"{node.name}()"] = built

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _HTTP or not node.args:
                continue
            path = _literal(node.args[0], consts)
            if not path or not path.startswith("/"):
                continue
            declares = any(kw.arg == "requires" for kw in node.keywords)
            sites.append((path, str(file.relative_to(REPO_ROOT)), node.lineno, declares))
    assert sites, f"no client call sites found under {PACKAGE} — check broken"
    return sites


def test_calls_missing_from_the_8_6_spec_declare_a_version_floor() -> None:
    old = _oldest_shapes()
    undeclared = [
        (path, file, line)
        for path, file, line, declares in _call_sites()
        if _shape(path) not in old and not declares
    ]
    assert not undeclared, (
        "These calls do not exist in the vROps 8.6 operation index, so they 404 on "
        "an 8.x appliance — and without requires= that 404 is explained as a bad "
        "id, sending the operator after a UUID that was never wrong.\n"
        "Pass requires=<a vmware_policy.compat.Requires> at each site:\n"
        + "\n".join(f"  {p}\n      {f}:{ln}" for p, f, ln in undeclared)
    )


def test_the_scan_can_actually_see_the_version_gated_calls() -> None:
    """Guards the guard: prove the 9.x-only population is non-empty.

    If a refactor renames the path constants or moves the spec, every check
    above passes by scanning nothing. This asserts the scan still finds the
    calls it exists to police, so the suite fails loudly rather than going
    quietly green (形态 #1, and 形态 #2 — a check that only ever confirms an
    answer it already knows).
    """
    old = _oldest_shapes()
    gated = [(p, f, ln) for p, f, ln, _ in _call_sites() if _shape(p) not in old]
    assert len(gated) >= 6, (
        f"expected at least 6 VCF-9.x-only call sites (4 fleet + 2 VODAP), found "
        f"{len(gated)}: {gated}"
    )

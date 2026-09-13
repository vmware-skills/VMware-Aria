"""Every metric key the skill tells an agent to use must be one Aria defines.

2026-09-13 review: SKILL.md's "Key Metric Names" table and the investigation
protocol were rewritten to keys verified on a live 8.18.7 appliance, but
nothing in the repo held those keys — so the next edit could name a key Aria
does not define and every test would stay green. An agent handed such a key
gets ``not_collected_for_resource`` on every resource and no hint that the
document, not the estate, was wrong.

The fixtures are the product's own definitions
(``GET /adapterkinds/VMWARE/resourcekinds/{kind}/statkeys``), not a list of
keys some VM happened to report: a key can be defined and still not collected
on a given resource, which is a different question from this one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.eval.regression._live_8187 import body

REPO = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO / "skills" / "vmware-aria"

#: Which resource kind each document's keys are read against. SKILL.md's table
#: and workflows are about VMs; the investigation protocol's keys are queried
#: on hosts. A document that starts naming keys for another kind needs its own
#: entry here — the test fails loudly rather than guessing.
DOCUMENTS = {
    SKILL_DIR / "SKILL.md": "VirtualMachine",
    SKILL_DIR / "references" / "investigation-protocol.md": "HostSystem",
}

_DEFINITIONS = {
    "VirtualMachine": "vm_statkey_definitions.json",
    "HostSystem": "host_statkey_definitions.json",
}

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
# A statKey is ``group|name``. Group and name contain no whitespace here; the
# lookarounds stop a shell pipe (``a | b``) or a longer token from matching.
_STAT_KEY = re.compile(r"(?<![\w|])([A-Za-z][\w-]*)\|([A-Za-z_][\w.:-]*)(?![\w|])")


def defined_keys(kind: str) -> set[str]:
    rows = body(_DEFINITIONS[kind])["resourceTypeAttributes"]
    return {r["key"] for r in rows}


def documented_keys(text: str) -> list[str]:
    """Every ``group|name`` inside a code span, with table ``\\|`` escapes undone."""
    keys: list[str] = []
    for span in _CODE_SPAN.findall(text):
        unescaped = span.replace("\\|", "|")
        keys.extend(f"{g}|{n}" for g, n in _STAT_KEY.findall(unescaped))
    return list(dict.fromkeys(keys))


def test_the_parser_finds_the_keys_it_is_meant_to_find() -> None:
    """POSITIVE CONTROL: a parser that finds nothing would pass every document."""
    sample = (
        "| CPU Ready | `cpu\\|readyPct` | % |\n"
        "run `vmware-aria resource metrics <id> --metrics 'cpu|usage_average,mem|usage_average'`\n"
        "not a key: `a | b`, `https://x/y`, plain cpu|outside_code"
    )
    assert documented_keys(sample) == ["cpu|readyPct", "cpu|usage_average", "mem|usage_average"]


def test_the_fixtures_define_the_keys_verified_live() -> None:
    """CONTROL: the definitions are the real lists, not an empty file that fails open."""
    vm, host = defined_keys("VirtualMachine"), defined_keys("HostSystem")
    assert len(vm) > 500 and len(host) > 500
    assert {"cpu|readyPct", "mem|balloonPct", "virtualDisk|peak_vDisk_readLatency"} <= vm
    assert "cpu|max_cpu_ready" in host and "cpu|max_cpu_ready" not in vm
    assert "cpu|readyPct" not in host


@pytest.mark.parametrize("doc", list(DOCUMENTS), ids=lambda p: p.name)
def test_every_documented_metric_key_is_defined(doc: Path) -> None:
    kind = DOCUMENTS[doc]
    keys = documented_keys(doc.read_text(encoding="utf-8"))
    assert keys, f"no metric keys found in {doc.name} — the parser or the document changed"

    undefined = sorted(k for k in keys if k not in defined_keys(kind))
    assert not undefined, (
        f"{doc.name} names metric keys Aria Operations 8.18.7 does not define for "
        f"{kind}: {undefined}. Pick the defined key from "
        f"tests/eval/fixtures/aria_8187/{_DEFINITIONS[kind]} (search by 'name'), "
        f"or, if the key belongs to another resource kind, say so in the document "
        f"and map it in DOCUMENTS."
    )

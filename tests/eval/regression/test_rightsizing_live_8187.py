"""Rightsizing rows must say what their numbers mean — pinned on a real 8.18.7.

2026-09-13, live VMware Aria Operations 8.18.7 (fixture captured read-only,
``fixtures/aria_8187_rightsizing.json``; the tests replay the appliance's own
responses rather than a hand-written shape — 形态 #3). Three defects:

1. **No units, no direction.** ``OnlineCapacityAnalytics|cpu|recommendedSize``
   is MHz (= recommended cores x the host's MHz per core), ``mem`` is KB,
   ``diskspace`` is GB. The tool passed the bare numbers through, and every
   row read ``recommendation`` — a caller could not tell over- from
   under-sized from right-sized.
2. **Powered-off VMs and templates were presented like running workloads.**
   6 of the 10 rows were powered off or a template.
3. **Reductions below a vendor appliance's floor went unflagged** — vcsa and
   the Aria appliance itself were both recommended 1 vCPU.

Every property / stat key the code reads was first seen answered on the
appliance (踩坑 #36); the fake client refuses any key the fixture did not
capture, so a key typed from memory fails here instead of reading as None.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "aria_8187_rightsizing.json").read_text(encoding="utf-8")
)
STATS_PATH = "/resources/stats/query"
PROPS_PATH = "/resources/properties/latest/query"


class _ReplayClient:
    """Serves the captured 8.18.7 responses, filtered to what was asked for."""

    def __init__(self, fixture: dict) -> None:
        self._f = fixture
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []

    def get(self, path: str, params: dict | None = None, **_kw) -> dict:
        self.get_calls.append(path)
        if path == "/resources":
            return self._f["resources"]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, json_data: dict | None = None, **_kw) -> dict:
        self.post_calls.append(path)
        body = json_data or {}
        if path == STATS_PATH:
            asked = set(body["statKey"])
            unknown = asked - set(self._f["stat_keys_captured"])
            assert not unknown, f"stat keys never seen answered on 8.18.7: {unknown}"
            ids = set(body["resourceId"])
            return {
                "values": [
                    {
                        **row,
                        "stat-list": {
                            "stat": [
                                s for s in row["stat-list"]["stat"] if s["statKey"]["key"] in asked
                            ]
                        },
                    }
                    for row in self._f["stats_query"]["values"]
                    if row["resourceId"] in ids
                ]
            }
        if path == PROPS_PATH:
            asked = set(body["propertyKeys"])
            unknown = asked - set(self._f["property_keys_captured"])
            assert not unknown, f"property keys never seen answered on 8.18.7: {unknown}"
            ids = set(body["resourceIds"])
            return {
                "values": [
                    {
                        "resourceId": row["resourceId"],
                        "property-contents": {
                            "property-content": [
                                p
                                for p in row["property-contents"]["property-content"]
                                if p["statKey"] in asked
                            ]
                        },
                    }
                    for row in self._f["properties_query"]["values"]
                    if row["resourceId"] in ids
                ]
            }
        raise AssertionError(f"unexpected POST {path}")


def _rows() -> tuple[dict[str, dict], _ReplayClient]:
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = _ReplayClient(FIXTURE)
    items = list_rightsizing_recommendations(client, limit=50)["items"]
    return {r["name"]: r for r in items}, client


ROWS, CLIENT = _rows()
POWERED_ON = {"Open-test", "test-llm", "vcsa", "vRealize-Operations"}


# ── 1. units and direction ────────────────────────────────────────────────


def test_every_row_states_the_units_of_the_raw_recommendation() -> None:
    for name, row in ROWS.items():
        assert row["recommended_units"] == {"cpu": "MHz", "memory": "KB", "diskspace": "GB"}, name


def test_raw_recommendation_fields_are_unchanged() -> None:
    """Backward compatibility: recommended_* still carry the engine's number."""
    assert ROWS["Open-test"]["recommended_cpu"] == pytest.approx(2803.193359375)
    assert ROWS["Open-test"]["recommended_memory"] == 16777216.0
    assert ROWS["Open-test"]["sizing_status"] == "recommendation"


@pytest.mark.parametrize(
    ("name", "current", "recommended", "mhz_per_vcpu"),
    [
        ("Open-test", 2, 1, 2803.19),  # host .15: 2803 MHz/core (vCenter hz)
        ("Hermers - TT", 4, 2, 2803.19),
        ("test-llm", 2, 2, 2803.19),
        ("vRealize-Operations", 2, 1, 2611.20),  # host .56: 2611 MHz/core
    ],
)
def test_recommended_mhz_is_converted_to_vcpus_with_the_vms_own_core_speed(
    name: str, current: int, recommended: int, mhz_per_vcpu: float
) -> None:
    row = ROWS[name]
    assert row["current_vcpus"] == current
    assert row["recommended_vcpus"] == recommended
    assert row["cpu_mhz_per_vcpu"] == pytest.approx(mhz_per_vcpu, abs=0.01)


@pytest.mark.parametrize(
    ("name", "cpu", "memory"),
    [
        ("Open-test", "oversized", "oversized"),  # 2 -> 1 vCPU, 32 -> 16 GiB
        ("test-llm", "right_sized", "oversized"),  # 2 -> 2 vCPU, 16 -> 8 GiB
        # 8 GiB -> 8391702 KB is +0.04%: inside MEMORY_DIRECTION_TOLERANCE, not a resize.
        ("vRealize-Operations", "oversized", "right_sized"),
        ("vcsa", "oversized", "oversized"),  # 14 GiB -> 13.0 GiB (-7.4%) is a real one
        ("Hermers - TT", "oversized", "oversized"),
    ],
)
def test_direction_is_stated_against_the_current_configuration(name: str, cpu: str, memory: str) -> None:
    row = ROWS[name]
    assert row["cpu_direction"] == cpu
    assert row["memory_direction"] == memory


def test_current_memory_is_reported_in_the_same_unit_as_the_recommendation() -> None:
    assert ROWS["Open-test"]["current_memory_kb"] == 33554432.0
    assert ROWS["test-llm"]["current_memory_kb"] == 16777216.0


# ── 2. powered-off VMs and templates ──────────────────────────────────────


def test_power_state_and_template_flag_come_from_the_appliance() -> None:
    assert {n for n, r in ROWS.items() if r["power_state"] == "Powered On"} == POWERED_ON
    assert {n for n, r in ROWS.items() if r["is_template"] is True} == {"linux-hermers"}
    assert all(r["is_template"] is False for n, r in ROWS.items() if n != "linux-hermers")


def test_only_powered_on_non_template_vms_with_a_change_are_actionable() -> None:
    actionable = {n for n, r in ROWS.items() if r["actionable"]}
    # Every powered-on VM here has at least one dimension off its recommendation.
    assert actionable == POWERED_ON


def test_powered_off_rows_say_why_they_are_not_actionable() -> None:
    for name, row in ROWS.items():
        if name in POWERED_ON:
            continue
        assert row["actionable"] is False, name
        text = " ".join(row["caveats"]).lower()
        assert "powered off" in text or "template" in text, name
    assert any("template" in c.lower() for c in ROWS["linux-hermers"]["caveats"])


# ── 3. appliances and the engine's own verdict ────────────────────────────


def test_the_engines_own_oversized_undersized_verdict_is_surfaced() -> None:
    verdict = ROWS["Hermers - TT"]["aria_verdict"]
    assert verdict["oversized"] is True
    assert verdict["oversized_vcpus"] == 2.0
    assert verdict["oversized_memory_kb"] == 2097152.0
    assert ROWS["vRealize-Operations"]["aria_verdict"]["undersized_memory_kb"] == 1048576.0


def test_disagreement_between_recommended_size_and_engine_verdict_is_flagged() -> None:
    """vcsa: recommendedSize implies 2 -> 1 vCPU, summary|oversized|vcpus says 0."""
    vcsa = " ".join(ROWS["vcsa"]["caveats"])
    assert "summary|oversized|vcpus" in vcsa
    # Hermers - TT: 4 -> 2 vCPU and the engine agrees (2 reclaimable) — no noise.
    assert not any("summary|oversized|vcpus" in c for c in ROWS["Hermers - TT"]["caveats"])


def test_product_name_is_reported_when_the_appliance_publishes_one() -> None:
    assert ROWS["vRealize-Operations"]["product_name"] == "vRealize Operations Appliance"
    # vcsa carries no vApp product in vCenter either; nothing is guessed from its name.
    assert ROWS["vcsa"]["product_name"] is None


def test_every_reduction_warns_to_check_the_vendor_minimum() -> None:
    reductions = [
        n for n, r in ROWS.items() if "oversized" in (r["cpu_direction"], r["memory_direction"])
    ]
    assert {"vcsa", "vRealize-Operations"} <= set(reductions)
    for name in reductions:
        assert any("vendor minimum" in c for c in ROWS[name]["caveats"]), name


# ── plumbing ──────────────────────────────────────────────────────────────


def test_configuration_is_one_bulk_properties_query_not_per_vm() -> None:
    assert CLIENT.post_calls.count(PROPS_PATH) == 1
    assert CLIENT.post_calls.count(STATS_PATH) == 1
    assert not [p for p in CLIENT.get_calls if p.endswith("/properties")]


def test_unpublished_configuration_is_unknown_not_actionable() -> None:
    """No properties back: direction is None and the row says so — never a guess."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    fixture = {**FIXTURE, "properties_query": {"values": []}}
    rows = list_rightsizing_recommendations(_ReplayClient(fixture), limit=50)["items"]
    for row in rows:
        assert row["power_state"] is None
        assert row["is_template"] is None
        assert row["cpu_direction"] is None
        assert row["memory_direction"] is None
        assert row["actionable"] is False
        assert any("not published" in c for c in row["caveats"]), row["name"]


def test_reclaimable_row_has_no_direction() -> None:
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    client = MagicMock()
    client.post.side_effect = lambda path, json_data=None, **_: (
        {
            "values": [
                {
                    "resourceId": "vm-1",
                    "stat-list": {
                        "stat": [
                            {"statKey": {"key": "OnlineCapacityAnalytics|cpu|recommendedSize"}, "data": [0.0]},
                        ]
                    },
                }
            ]
        }
        if path == STATS_PATH
        else FIXTURE["properties_query"]
    )
    row = list_rightsizing_recommendations(client, resource_id="vm-1")["items"][0]
    assert row["sizing_status"] == "reclaimable"
    assert row["cpu_direction"] is None
    assert row["actionable"] is False


def test_cli_table_shows_power_direction_and_caveats(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI renders the converted, directional numbers — not bare MHz/KB."""
    from typer.testing import CliRunner

    from vmware_aria import cli

    monkeypatch.setattr(cli, "_get_connection", lambda *_a, **_k: (_ReplayClient(FIXTURE), None))
    monkeypatch.setenv("COLUMNS", "250")
    result = CliRunner().invoke(cli.app, ["capacity", "rightsizing", "--limit", "50"])

    assert result.exit_code == 0, result.output
    assert "2→1 ↓" in result.output  # Open-test vCPU, oversized
    assert "32.0→16.0 ↓" in result.output  # Open-test memory GiB, oversized
    assert "8.0→8.0 =" in result.output  # Aria appliance memory: +3 MB is right-sized
    assert "↓ oversized" in result.output  # the legend
    assert "template" in result.output
    assert "Off" in result.output
    assert "vendor minimum" in result.output
    assert "MHz" not in result.output.split("Rightsizing (OnlineCapacityAnalytics recommendedSize)")[1].split("↓ oversized")[0]


# ── tolerance: noise must not become a direction (found live, 2026-09-13) ──


def _single_vm_row(recommended: dict[str, float], props: dict[str, object]) -> dict:
    """One VM through the real code path, with appliance-shaped stats/properties replies."""
    from vmware_aria.ops.capacity import list_rightsizing_recommendations

    def reply(path: str, json_data: dict | None = None, **_kw) -> dict:
        if path == STATS_PATH:
            stats = [
                {"statKey": {"key": f"OnlineCapacityAnalytics|{dim}|recommendedSize"}, "data": [v]}
                for dim, v in recommended.items()
            ]
            return {"values": [{"resourceId": "vm-1", "stat-list": {"stat": stats}}]}
        content = [
            {"statKey": k, "data": [v]} if isinstance(v, float) else {"statKey": k, "values": [v]}
            for k, v in props.items()
        ]
        return {"values": [{"resourceId": "vm-1", "property-contents": {"property-content": content}}]}

    client = MagicMock()
    client.post.side_effect = reply
    return list_rightsizing_recommendations(client, resource_id="vm-1")["items"][0]


def _props(num_cpu: float, mhz_per_vcpu: float, memory_kb: float) -> dict[str, object]:
    return {
        "config|hardware|numCpu": num_cpu,
        "cpu|speed": num_cpu * mhz_per_vcpu * 1_000_000,
        "config|hardware|memoryKB": memory_kb,
        "summary|runtime|powerState": "Powered On",
        "summary|config|isTemplate": "false",
    }


def test_a_memory_difference_of_a_few_megabytes_is_right_sized_and_not_actionable() -> None:
    """Live 8.18.7: the Aria appliance, 8388608 KB configured, 8391656 KB recommended (+0.04%)."""
    row = _single_vm_row(
        {"cpu": 2 * 2803.193359375, "mem": 8391656.0}, _props(2.0, 2803.193359375, 8388608.0)
    )
    assert row["cpu_direction"] == "right_sized"
    assert row["memory_direction"] == "right_sized"
    assert row["actionable"] is False


@pytest.mark.parametrize(
    ("recommended_mhz", "mhz_per_vcpu"),
    [
        (2803.1934, 2803.1933),  # the coordinator's example (ratio 1 + 3.6e-8)
        (2803.20, 2803.19),  # a bigger rounding gap (ratio 1 + 3.6e-6)
        (2803.19, 2803.20),  # and the other side of it
    ],
)
def test_float_noise_in_the_mhz_ratio_does_not_add_a_vcpu(recommended_mhz: float, mhz_per_vcpu: float) -> None:
    row = _single_vm_row({"cpu": recommended_mhz, "mem": 16777216.0}, _props(2.0, mhz_per_vcpu, 16777216.0))
    assert row["recommended_vcpus"] == 1
    assert row["cpu_direction"] == "oversized"
    assert row["memory_direction"] == "right_sized"
    assert row["actionable"] is True


def test_a_real_fraction_of_a_core_still_rounds_up() -> None:
    """The epsilon absorbs noise, never demand: 1.5 cores of MHz is 2 vCPUs."""
    row = _single_vm_row({"cpu": 1.5 * 2803.19, "mem": 16777216.0}, _props(2.0, 2803.19, 16777216.0))
    assert row["recommended_vcpus"] == 2
    assert row["cpu_direction"] == "right_sized"


def test_a_real_memory_change_just_past_the_tolerance_still_has_a_direction() -> None:
    from vmware_aria.ops.capacity import MEMORY_DIRECTION_TOLERANCE

    current = 16777216.0
    over = current * (1 + MEMORY_DIRECTION_TOLERANCE * 1.5)
    row = _single_vm_row({"cpu": 2 * 2803.19, "mem": over}, _props(2.0, 2803.19, current))
    assert row["memory_direction"] == "undersized"
    assert row["actionable"] is True

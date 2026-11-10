"""Interpret Aria rightsizing facts and their evidence limits."""

from __future__ import annotations


import math

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass


_VERDICT_KEYS = {
    "oversized": "summary|oversized",
    "oversized_vcpus": "summary|oversized|vcpus",
    "oversized_memory_kb": "summary|oversized|memory",
    "undersized": "summary|undersized",
    "undersized_vcpus": "summary|undersized|vcpus",
    "undersized_memory_kb": "summary|undersized|memory",
}


_PROP_NUM_CPU = "config|hardware|numCpu"


_PROP_CPU_SPEED_HZ = "cpu|speed"  # total Hz across the VM's vCPUs (== vCenter host hz x numCpu)


CPU_VCPU_ROUNDING_EPSILON = 0.01


_POWERED_ON = "Powered On"


_POWERED_OFF = "Powered Off"


def _as_float(value: object) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_bool(value: object) -> bool | None:
    text = str(value).strip().lower() if value is not None else ""
    return {"true": True, "false": False}.get(text)


def _direction(current: float | None, recommended: float | None, tolerance: float = 0.0) -> str | None:
    """oversized / undersized / right_sized; within ``tolerance`` (relative) is right_sized."""
    if current is None or recommended is None:
        return None
    if current > 0 and abs(recommended - current) <= tolerance * current:
        return "right_sized"
    if recommended < current:
        return "oversized"
    if recommended > current:
        return "undersized"
    return "right_sized"


def _cpu_sizing(props: dict, recommended_mhz: float | None) -> dict:
    """Current vCPUs, MHz per vCPU and the MHz recommendation in vCPUs."""
    num_cpu = _as_float(props.get(_PROP_NUM_CPU))
    speed_hz = _as_float(props.get(_PROP_CPU_SPEED_HZ))
    mhz_per_vcpu = speed_hz / num_cpu / 1_000_000 if num_cpu and speed_hz else None
    recommended_vcpus = None
    if recommended_mhz is not None and mhz_per_vcpu:
        # Round up — a real fraction of a core is demand — but first absorb the
        # float noise between the engine's MHz and cpu|speed (see the constant).
        recommended_vcpus = max(1, math.ceil(recommended_mhz / mhz_per_vcpu - CPU_VCPU_ROUNDING_EPSILON))
    current_vcpus = int(num_cpu) if num_cpu is not None else None
    return {
        "current_vcpus": current_vcpus,
        "cpu_mhz_per_vcpu": round(mhz_per_vcpu, 2) if mhz_per_vcpu else None,
        "recommended_vcpus": recommended_vcpus,
        "cpu_direction": _direction(current_vcpus, recommended_vcpus),
    }


def _aria_verdict(stats: dict) -> dict | None:
    raw = {field: _as_float(stats.get(key)) for field, key in _VERDICT_KEYS.items()}
    if all(v is None for v in raw.values()):
        return None
    return {
        field: (None if v is None else v > 0) if field in ("oversized", "undersized") else v for field, v in raw.items()
    }


def _engine_direction(verdict: dict | None, unit: str) -> str | None:
    if verdict is None:
        return None
    over, under = verdict.get(f"oversized_{unit}"), verdict.get(f"undersized_{unit}")
    if over is None or under is None:
        return None
    return "oversized" if over > 0 else "undersized" if under > 0 else "right_sized"


def _rightsizing_caveats(row: dict, props_published: bool, props_failed: bool = False) -> list[str]:
    if props_failed:
        # Every property-derived field is None, so none of the checks below has
        # anything to say — and their "not published" wording would be false.
        return [
            "VM properties could not be read (the bulk property query failed — see "
            "properties_note): power state, template flag and current size are "
            "unknown, so this row is not actionable"
        ]
    caveats: list[str] = []
    power, template = row["power_state"], row["is_template"]
    if power is None or template is None:
        caveats.append(
            "power state / template flag not published for this VM — cannot tell whether it is a running workload"
        )
    elif template:
        caveats.append("template: not a running workload — size the VMs deployed from it instead")
    elif power == _POWERED_OFF:
        caveats.append(
            "powered off: the recommendation reflects past (or no) demand, not a "
            "running workload — decide whether the VM is needed before sizing it"
        )
    elif power != _POWERED_ON:
        caveats.append(f"power state is '{power}', not {_POWERED_ON}")
    if row["current_vcpus"] is None or row["current_memory_kb"] is None:
        if props_published or row["sizing_status"] == "recommendation":
            caveats.append(
                "current configuration (config|hardware|numCpu / memoryKB / cpu|speed) "
                "not published — direction cannot be stated"
            )
    for dim, unit, key in (("cpu", "vcpus", "vcpus"), ("memory", "memory_kb", "memory")):
        ours, engine = row[f"{dim}_direction"], _engine_direction(row["aria_verdict"], unit)
        if ours and engine and ours != engine:
            caveats.append(
                f"recommendedSize reads {dim} as {ours} but the engine's own "
                f"summary|oversized|{key} / summary|undersized|{key} read {engine} — "
                "confirm in the Aria UI before acting"
            )
    if "oversized" in (row["cpu_direction"], row["memory_direction"]):
        product = f" Aria reports product '{row['product_name']}'." if row["product_name"] else ""
        caveats.append(
            "before reducing, check the vendor minimum size for this guest — vendor "
            "appliances publish floors, and appliances cannot be reliably identified "
            f"from the API.{product}"
        )
    return caveats

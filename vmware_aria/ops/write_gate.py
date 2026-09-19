"""Shared pieces of the MCP confirmation gate (HLD §7, revised 2026-09-16).

Every gated MCP write tool in this skill takes ``confirm: bool = False``:

* L2 — a bare call previews: it returns the blast radius and writes nothing.
* L1 — preview and acting response both carry ``blast_radius``: what the call
  changes, with the object's identity, ``blockers`` and ``unmeasured``.
* L3 — ``confirm=True`` is refused, with a teaching error, when a blocker is
  present or a field the blast radius depends on could not be read.

The legacy spelling ``confirmed`` stays for one minor cycle as a deprecated
alias. When both are passed the conservative reading wins: the call acts only
if something said "act" and nothing said "hold".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

#: Identifiers listed individually in a blast radius; counts cover the rest.
MAX_LISTED = 16

PREVIEW_HINT = (
    "Nothing was changed. Show blast_radius to the user and get their explicit "
    "decision; to apply, call again with confirm=True."
)

_DEPRECATED = "confirmed is deprecated; use confirm. Removed in the next minor release."


class GateRefusedError(ValueError):
    """A confirmed write refused before anything was sent (blocker or unmeasured).

    A ``ValueError``, so ``_safe_error`` would pass its teaching text through;
    the MCP layer catches it first to keep the full text and the blast radius
    that was measured.
    """

    def __init__(self, message: str, blast_radius: dict | None = None) -> None:
        super().__init__(message)
        self.blast_radius = blast_radius


class Decision(NamedTuple):
    """Whether this call acts, and the deprecation note when the alias was used."""

    act: bool
    deprecated: str | None


def resolve_confirm(confirm: Any, confirmed: bool | None = None) -> Decision:
    """Read ``confirm`` and the deprecated ``confirmed`` conservatively.

    Args:
        confirm: The new parameter; only ``True`` acts.
        confirmed: Legacy alias; ``None`` means it was not passed. The old
            contract acted on ``confirmed=True`` alone.

    Returns:
        ``Decision(act, deprecated)``: acts iff ``confirm is True`` or
        ``confirmed is True``, and ``confirmed`` was not explicitly False.
    """
    act = (confirm is True or confirmed is True) and confirmed is not False
    return Decision(act=act, deprecated=_DEPRECATED if confirmed is not None else None)


def refusal(tool: str, subject: str, radius: dict, next_step: str) -> str | None:
    """The L3 reason ``confirm=True`` must not act on ``radius``, or None."""
    if radius.get("blockers"):
        return f"{tool} refused for {subject}: " + " ".join(radius["blockers"])
    if radius.get("unmeasured"):
        return (
            f"{tool} refused for {subject}: could not read "
            f"{', '.join(radius['unmeasured'])}, so what the call would change is "
            f"unknown. Nothing was changed. {next_step}"
        )
    return None


def gate(
    tool: str,
    subject: str,
    radius: dict,
    *,
    act: bool,
    apply: Callable[[], dict],
    next_step: str,
    noop: str | None = None,
) -> dict:
    """Preview, noop, refuse, or run ``apply`` — the one decision every gated tool makes.

    Args:
        tool: Tool name, for the refusal message.
        subject: What was measured, for the refusal message.
        radius: The blast radius just measured.
        act: Whether the caller asked to act (see ``resolve_confirm``).
        apply: The existing ops write; its result is returned with the radius.
        next_step: What to do when a field could not be read.
        noop: When set, the object is already in the requested state: this
            text is returned as the hint and nothing is sent.
    """
    if noop and not radius.get("blockers") and not radius.get("unmeasured"):
        return {"action": "noop", "blast_radius": radius, "hint": noop}
    if not act:
        return {"action": "preview", "blast_radius": radius, "hint": PREVIEW_HINT}
    reason = refusal(tool, subject, radius, next_step)
    if reason:
        raise GateRefusedError(reason, radius)
    return {**apply(), "blast_radius": radius}

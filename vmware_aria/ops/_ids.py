"""Alert and resource ids are UUIDs: refuse anything else before it reaches Aria.

Until 2026-09-15 the id checks only asked for a non-empty string, so a pasted
table cell such as ``GREEN(100.0)``, or two UUIDs joined by a space, went to
the appliance and came back HTTP 400 — and a maintenance dry-run reported
``ok`` for the joined pair. Every alert and resource id Aria Operations issues
is an 8-4-4-4-12 hexadecimal UUID, so the shape is checked locally, and the
error says what to paste instead.
"""

from __future__ import annotations

import re
from typing import Any

from vmware_policy import sanitize

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

#: Characters of a rejected value echoed back in the error.
_SHOWN_MAX = 80


def require_uuid(value: Any, field: str, noun: str, hint: str) -> str:
    """Return ``value`` stripped and lowercased when it is exactly one UUID, else raise.

    Lowercased because Aria issues lowercase ids and keys its answers by them:
    an uppercase id reached the appliance fine but found nothing when used as a
    lookup key, so ``resource health BA13F0B7-…`` reported no availability
    point for a service whose lowercase id read 0.0 (2026-09-15 review).

    Args:
        value: The id as the caller passed it.
        field: Parameter name to name in the error, e.g. ``resource_id``.
        noun: What the id identifies, e.g. ``resource`` or ``alert``.
        hint: Sentence telling the caller where to copy a real id from.

    Raises:
        ValueError: Empty, not a string, or not exactly one UUID.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty Aria {noun} UUID. {hint}")
    text = value.strip()
    if _UUID.fullmatch(text):
        return text.lower()
    shown = sanitize(text, max_len=_SHOWN_MAX)
    found = _UUID.findall(text)
    if len(found) > 1:
        raise ValueError(
            f"{field} must be one Aria {noun} UUID, but {shown!r} holds {len(found)} UUIDs — "
            f"pass one per call. {hint}"
        )
    raise ValueError(
        f"{field} must be an Aria {noun} UUID (8-4-4-4-12 hexadecimal digits); got {shown!r}. {hint}"
    )

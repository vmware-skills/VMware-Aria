"""Alert notes: list (READ) and add (WRITE).

Notes are how the suite-api records who is handling an alert and what was
done (``GET`` / ``POST /alerts/{id}/notes``; the body field is ``content``,
the stored field is ``note``). Both operations exist in the 8.6 and 9.1
indexes.

Note text is written by people and read back by agents, so every string that
leaves here passes through ``sanitize()``.

``add_alert_note`` has been exercised only against recording clients; no note
was created on a real appliance while writing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vmware_policy import paginated

from vmware_aria.ops._collection import int_or_none, read_window, text_or_none
from vmware_aria.ops._paging import next_offset, validate_page_args

if TYPE_CHECKING:
    from vmware_aria.connection import AriaClient
    from vmware_aria.notify.audit import AuditLogger

#: Characters of note text returned per note.
NOTE_MAX_LEN = 2000

_ALERT_HINT = (
    "Run list_alerts (or `vmware-aria alert list`) and copy an exact 'id' — the alert UUID, "
    "not the affected resource UUID."
)


def require_alert_id(alert_id: Any) -> str:
    """The stripped alert id, or a teaching ``ValueError``."""
    if not isinstance(alert_id, str) or not alert_id.strip():
        raise ValueError(f"alert_id must be a non-empty Aria alert UUID. {_ALERT_HINT}")
    return alert_id.strip()


def _note_row(raw: dict) -> dict:
    return {
        "id": text_or_none(raw.get("id")),
        "note": text_or_none(raw.get("note"), max_len=NOTE_MAX_LEN),
        "type": text_or_none(raw.get("type")),
        "user_name": text_or_none(raw.get("userName"), max_len=200),
        "user_id": text_or_none(raw.get("userId")),
        "created_time_ms": int_or_none(raw.get("creationTimeUTC")),
    }


def list_alert_notes(client: AriaClient, alert_id: str, limit: int = 100, offset: int = 0) -> dict:
    """List the notes on one alert (``GET /alerts/{id}/notes``).

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: Alert UUID.
        limit: Page size, 1-500; out of range is rejected.
        offset: Rows to skip; pass the previous ``next_offset``.

    Returns:
        Paginated envelope of note rows (id, note, type USER/SYSTEM, user_name,
        user_id, created_time_ms) plus ``notes_note``: ``None`` when the answer
        was read, otherwise why ``items`` is unknown rather than empty. An
        unknown alert raises (HTTP 404), it does not answer empty.
    """
    aid = require_alert_id(alert_id)
    validate_page_args(limit, offset)
    window = read_window(client, f"/alerts/{aid}/notes", "alertNotes", limit=limit, offset=offset)
    rows = [_note_row(r) for r in window.rows]
    if not window.recognised:
        return paginated(
            rows,
            limit=limit,
            total=None,
            next_offset=None,
            notes_note=(
                "Aria answered without a readable 'alertNotes' list, so whether this alert has notes is "
                "unknown — this is not 'no notes'. Retry, or run 'vmware-aria doctor'."
            ),
        )
    return paginated(
        rows,
        limit=limit,
        total=window.total,
        next_offset=next_offset(len(rows), limit, offset, window.total),
        notes_note=None,
    )


def add_alert_note(
    client: AriaClient,
    alert_id: str,
    note: str,
    audit_logger: AuditLogger | None = None,
    target_name: str = "default",
) -> dict:
    """Add a note to an alert (``POST /alerts/{id}/notes``).

    Args:
        client: Authenticated Aria Operations API client.
        alert_id: Alert UUID.
        note: Note text; surrounding whitespace is stripped, empty is refused.
        audit_logger: Optional audit logger; the operation is logged if given.
        target_name: Target name for the audit record.

    Returns:
        ``alert_id``, ``action``, ``created`` (the stored note as Aria returned
        it, or ``None`` when its answer carried no note id) and
        ``confirmation_note`` (why ``created`` is ``None``).
    """
    aid = require_alert_id(alert_id)
    if not isinstance(note, str) or not note.strip():
        raise ValueError(
            "note must be non-empty text — say who is handling the alert or what was done, "
            "e.g. 'Taking this: rebooting esx-03 after the memory upgrade'."
        )
    content = note.strip()

    data = client.post(f"/alerts/{aid}/notes", json_data={"content": content})

    created = _note_row(data) if isinstance(data, dict) and data.get("id") else None
    confirmation_note = None if created else (
        "Aria accepted the request, but its answer carried no note id, so the note is not confirmed. "
        "Run list_alert_notes for this alert before adding it again."
    )
    if audit_logger:
        audit_logger.log(
            target=target_name,
            operation="add_alert_note",
            resource=f"alert/{aid}",
            skill="aria",
            parameters={"alert_id": aid, "note": content},
            after_state=created or {},
            result="ok",
        )
    return {"alert_id": aid, "action": "add_note", "created": created, "confirmation_note": confirmation_note}

"""Shared helpers for the core-service repositories: opaque keyset cursors
and lenient date parsing for SPA-supplied payloads."""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime
from uuid import UUID


def parse_dob(value: str | None) -> date | None:
    """Coerce an SPA-supplied date-of-birth into a ``date``.

    The form sends ``""`` for an empty native ``<input type=date>`` and a
    ``YYYY-MM-DD`` string otherwise. Anything unparseable becomes ``None``
    rather than a 422 — DOB is optional metadata, not a gate.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except (ValueError, TypeError):
        return None


def encode_cursor(sort_key: datetime, row_id: UUID) -> str:
    """Opaque base64 keyset cursor over ``(sort_key, id)``."""
    raw = f"{sort_key.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID] | None:
    """Inverse of :func:`encode_cursor`; ``None`` on any malformed input."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None

"""PII / secret-safe logging filter.

Two policies, applied to log-record extras and to nested dict / list /
structlog event-dict payloads:

* **Drop list** — fields whose key matches (case-insensitive) are deleted
  outright. Used for high-sensitivity values (passwords, tokens, raw audio,
  transcripts, full request/response bodies).

* **Mask list** — fields whose key matches are replaced with the string
  ``<redacted>``. Used for PII whose presence/absence is informative but
  whose value is not (email, phone, MRN, name, …).

The filter does **not** parse free-form ``message`` strings for content;
mask your message templates explicitly. Best-effort regex masking on JSON
fragments embedded inside string messages is provided as a safety net.

Design notes:

* The matcher is exact-or-known-suffix on field names (e.g. ``access_token``,
  ``refresh_token``, ``client_secret``). Substring matching would create
  false positives — ``token_count`` in a metric should not be dropped.
* Recursion depth is capped at 10 to prevent pathological nested logs from
  blowing the stack.
* The ``patient_*`` family is implemented as a prefix rule, since clinical
  systems naturally namespace patient fields.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# ──────────────────────────────────────────────────────────────────────
# Drop list — values are removed entirely.
# ──────────────────────────────────────────────────────────────────────
_DROP_NAMES: frozenset[str] = frozenset(
    {
        # Authentication / authorization
        "password",
        "passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "cookie",
        "set-cookie",
        "session",
        "session_id",
        "session_token",
        "csrf_token",
        # MFA / recovery
        "mfa_secret",
        "totp_secret",
        "recovery_code",
        "recovery_codes",
        "backup_codes",
        # Cryptographic material
        "private_key",
        "privatekey",
        "encryption_key",
        "kek",
        "dek",
        # Clinical content (raw)
        "audio",
        "audio_data",
        "audio_content",
        "audio_bytes",
        "pcm",
        "transcript",
        "transcript_text",
        "transcription",
        "note",
        "note_body",
        # Generic body / payload
        "body",
        "payload",
        "request_body",
        "response_body",
    }
)

# ──────────────────────────────────────────────────────────────────────
# Mask list — values replaced with <redacted>.
# ──────────────────────────────────────────────────────────────────────
_MASK_NAMES: frozenset[str] = frozenset(
    {
        # Generic PII
        "email",
        "email_address",
        "phone",
        "phone_number",
        "msisdn",
        "mrn",
        "medical_record_number",
        "dob",
        "date_of_birth",
        "first_name",
        "last_name",
        "full_name",
        "address",
        "street_address",
        "ssn",
        "ipn",  # Ukrainian individual tax ID
        "drfo",  # Ukrainian state register identifier
        "passport_number",
    }
)

# Field-name prefixes that always mask. The patient_* family is the canonical case.
_MASK_PREFIXES: tuple[str, ...] = (
    "patient_",
)

# Regex to redact JSON-style fragments inside string messages.
_JSON_FIELD_NAMES = sorted(_DROP_NAMES | _MASK_NAMES)
_JSON_PATTERN: re.Pattern[str] = re.compile(
    r'("(?:' + "|".join(re.escape(n) for n in _JSON_FIELD_NAMES) + r')"\s*:\s*)"(?:[^"\\]|\\.)*"',
    re.IGNORECASE,
)

_MASK_VALUE = "<redacted>"
_MAX_DEPTH = 10


def _classify(name: str) -> str:
    """Return ``'drop'``, ``'mask'`` or ``'keep'`` for ``name``."""
    lname = name.lower()
    if lname in _DROP_NAMES:
        return "drop"
    if lname in _MASK_NAMES:
        return "mask"
    if any(lname.startswith(p) for p in _MASK_PREFIXES):
        return "mask"
    return "keep"


def scrub(value: Any, depth: int = 0) -> Any:
    """Return a copy of ``value`` with PII / secrets removed or masked.

    Pure function — used both by the logging filter and directly by structlog
    processors (see ``logging.py``).
    """
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                out[k] = scrub(v, depth + 1)
                continue
            verdict = _classify(k)
            if verdict == "drop":
                continue
            if verdict == "mask":
                out[k] = _MASK_VALUE
                continue
            out[k] = scrub(v, depth + 1)
        return out
    if isinstance(value, list):
        return [scrub(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub(v, depth + 1) for v in value)
    return value


def scrub_event_dict(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor adapter for ``scrub``."""
    return scrub(event_dict)  # type: ignore[no-any-return]


class PIISafeFilter(logging.Filter):
    """stdlib-logging filter that scrubs the log record before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        for attr, value in list(vars(record).items()):
            if attr.startswith("_") or attr in _STDLIB_RECORD_ATTRS:
                continue
            if not isinstance(attr, str):
                continue
            verdict = _classify(attr)
            if verdict == "drop":
                delattr(record, attr)
                continue
            if verdict == "mask":
                setattr(record, attr, _MASK_VALUE)
                continue
            if isinstance(value, (dict, list, tuple)):
                setattr(record, attr, scrub(value))

        if isinstance(record.args, dict):
            record.args = scrub(record.args)

        if isinstance(record.msg, str):
            record.msg = _JSON_PATTERN.sub(r'\1"<redacted>"', record.msg)

        return True


_STDLIB_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }
)

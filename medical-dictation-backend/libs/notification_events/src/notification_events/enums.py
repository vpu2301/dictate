"""Closed vocabularies shared across the notification wire.

Every one of these is a ``StrEnum`` so it serialises as its literal value
in JSON and compares equal to the plain string a DB CHECK constraint
stores — the enum and the SQL constraint must be kept in lockstep.
"""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    """The v1 notification categories.

    Adding a member here is not enough to make it work: every category
    MUST also have an entry in ``notification_service.domain.catalog``.
    A test enforces the 1:1 mapping, so an un-catalogued category is a
    build failure rather than a silent no-op at runtime — the same
    "unknown intent is a bug" contract as nlp-service's operations map.
    """

    REPORT_FINALIZED = "report.finalized"
    REPORT_SIGNED = "report.signed"
    REPORT_SIGNING_FAILED = "report.signing_failed"
    REPORT_AMENDED = "report.amended"
    REPORT_CHAIN_FAILURE = "report.chain_failure"
    REPORT_SHARED_WITH_YOU = "report.shared_with_you"
    DICTATION_COMPLETED = "dictation.completed"
    TRANSCRIPTION_COMPLETED = "transcription.completed"
    TRANSCRIPTION_FAILED = "transcription.failed"
    SYSTEM_DIGEST = "system.digest"


class Channel(StrEnum):
    """Delivery channels.

    ``apns``/``fcm`` are deliberately absent — sprint 18 adds them as new
    members plus a ``PushProvider``; the outbox schema already models one
    row per (notification, channel) so no migration is needed then.
    """

    IN_APP = "in_app"
    EMAIL = "email"


class EmailMode(StrEnum):
    """Per-category email delivery mode."""

    IMMEDIATE = "immediate"
    DIGEST = "digest"
    OFF = "off"


class Severity(StrEnum):
    """Drives client-side presentation, not routing."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

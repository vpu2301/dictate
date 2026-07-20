"""Notification audit event kinds. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

# Materialisation
NOTIFICATION_MATERIALIZED: Final = "notification.materialized"
NOTIFICATION_COALESCED: Final = "notification.coalesced"  # severity=warn (storm, E1)

# Delivery
NOTIFICATION_DELIVERED: Final = "notification.delivered"
NOTIFICATION_SUPPRESSED: Final = "notification.suppressed"
NOTIFICATION_DELIVERY_FAILED: Final = "notification.delivery_failed"  # severity=warn
NOTIFICATION_DEAD_LETTERED: Final = "notification.dead_lettered"  # severity=error

# User actions
NOTIFICATION_READ: Final = "notification.read"
PREFERENCES_UPDATED: Final = "notification.preferences_updated"

# Jobs
DIGEST_SENT: Final = "notification.digest_sent"

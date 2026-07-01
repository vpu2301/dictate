"""Canonical audit event-kind strings.

Centralised so misspellings fail at import time and the catalogue in
docs/audit/event-kinds.md (Day 10) stays in sync.
"""

from __future__ import annotations

from typing import Final

# ── Authentication ────────────────────────────────────────────────────
AUTH_LOGIN: Final[str] = "auth.login"
AUTH_LOGIN_FAILED: Final[str] = "auth.login_failed"
AUTH_REFRESH: Final[str] = "auth.refresh"
AUTH_REFRESH_REPLAY_DETECTED: Final[str] = "auth.refresh_replay_detected"
AUTH_LOGOUT: Final[str] = "auth.logout"
AUTH_ACCOUNT_LOCKED: Final[str] = "auth.account_locked"

# ── User lifecycle ────────────────────────────────────────────────────
USER_INVITED: Final[str] = "user.invited"
USER_DEACTIVATED: Final[str] = "user.deactivated"
USER_REACTIVATED: Final[str] = "user.reactivated"
USER_ROLE_CHANGED: Final[str] = "user.role_changed"

# ── Tenant (clinic) lifecycle + membership (Sprint 12) ─────────────────
TENANT_CREATED: Final[str] = "tenant.created"
TENANT_UPDATED: Final[str] = "tenant.updated"
TENANT_LOGO_UPDATED: Final[str] = "tenant.logo_updated"
TENANT_MEMBER_ADDED: Final[str] = "tenant.member_added"
TENANT_MEMBER_ROLE_CHANGED: Final[str] = "tenant.member_role_changed"
TENANT_MEMBER_REMOVED: Final[str] = "tenant.member_removed"
TENANT_SWITCHED: Final[str] = "tenant.switched"

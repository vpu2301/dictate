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

# ── Step-up re-authentication (S14 break-glass) ───────────────────────
# A password re-entry by an already-authenticated user, proving presence
# before a high-risk act. Both outcomes are `sec` severity: the failure
# is a wrong password typed by someone holding a live session, which is
# exactly the shape of a hijacked tab.
AUTH_REAUTH_SUCCEEDED: Final[str] = "auth.reauth_succeeded"
AUTH_REAUTH_FAILED: Final[str] = "auth.reauth_failed"

# ── MFA (sprint 16) ───────────────────────────────────────────────────
AUTH_MFA_ENROLLED: Final[str] = "auth.mfa.enrolled"
# Admin-assisted reset. The kind was pre-ledgered in sprint 02
# (docs/audit/event-kinds.md, permissions.csv) as `user.reset_mfa`; the
# sprint-16 spec's `auth.mfa.reset` name lost to the existing catalogue.
USER_RESET_MFA: Final[str] = "user.reset_mfa"
# S21 — an access review asked a user to enrol. `sec` severity, because
# the interesting question a year later is not "was anyone reminded" but
# "how long did this account sit unprotected after we noticed", and that
# answer needs the ask and the enrolment on the same trail.
USER_MFA_REMINDED: Final[str] = "user.mfa_reminded"

# ── Session revocation (sprint 16) ────────────────────────────────────
AUTH_SESSION_REVOKED: Final[str] = "auth.session.revoked"

# ── Password recovery ─────────────────────────────────────────────────
# All `sec` severity. The *request* is recorded as well as the outcome
# because a burst of requests against one account, none of them
# completed, is the visible half of an attempt to take it over — and
# without the request event the trail starts only once the attacker
# succeeds.
#
# Note what is deliberately NOT recorded: a request for an address with
# no account. Writing one would turn the audit log into the account
# enumeration oracle the endpoint's uniform 202 exists to prevent.
AUTH_PASSWORD_RESET_REQUESTED: Final[str] = "auth.password.reset_requested"
AUTH_PASSWORD_RESET_COMPLETED: Final[str] = "auth.password.reset_completed"
AUTH_PASSWORD_CHANGED: Final[str] = "auth.password.changed"
# The "this wasn't me" button. Its own kind rather than a flavour of
# session-revoked, because it is a user telling us an account was taken
# over — the single highest-signal event this service can emit.
AUTH_ACCOUNT_LOCKDOWN: Final[str] = "auth.account.lockdown"

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

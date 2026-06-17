"""Permission matrix: ``(role, action, target_kind) → allowed``.

The ``ALLOW`` dict is the runtime gate. The CSV at
``docs/auth/permissions.csv`` is the human-reviewable source of truth.
The exhaustive test ``libs/auth/tests/unit/test_perms.py`` fails CI if
the two ever diverge — adding a new permission means editing both, and
the test verifies they match.

Sprint 2 introduces the *mechanism*. The actions catalogue grows each
sprint: sprint 3 adds ``report.*`` actions, sprint 17 narrows things
further via scopes (the third argument here, wired but not yet used).
"""

from __future__ import annotations

from typing import Final

from .claims import Claims

# ── Domain literals ─────────────────────────────────────────────────────
# Kept as plain str so we can read them from the CSV without a Literal
# bridge. The exhaustive test guards against typos.

Role = str  # tenant_admin | clinician | nurse | auditor | service
Action = str  # e.g. 'user.invite', 'audit.read'
TargetKind = str  # e.g. 'user', 'audit', 'tenant'

KNOWN_ROLES: Final[frozenset[str]] = frozenset(
    {"tenant_admin", "clinician", "nurse", "auditor", "service"}
)

KNOWN_TARGET_KINDS: Final[frozenset[str]] = frozenset(
    {
        "tenant",
        "user",
        "audit",
        "asr_job",
        "dictation_session",
        "nlp_text",
        "abbreviation",
        "template",
        "report",
    }
)


# ── The matrix ──────────────────────────────────────────────────────────
# Only "True" entries are listed; ``can`` defaults to deny.
# Mirror at docs/auth/permissions.csv.

ALLOW: Final[dict[tuple[Role, Action, TargetKind], bool]] = {
    # tenant_admin: tenant-wide admin
    ("tenant_admin", "tenant.read", "tenant"): True,
    ("tenant_admin", "tenant.update", "tenant"): True,
    ("tenant_admin", "user.invite", "user"): True,
    ("tenant_admin", "user.deactivate", "user"): True,
    ("tenant_admin", "user.reset_mfa", "user"): True,
    ("tenant_admin", "audit.read", "audit"): True,
    ("tenant_admin", "audit.verify", "audit"): True,
    # clinician: routine clinical user (sprint 2 surface)
    ("clinician", "tenant.read", "tenant"): True,
    # nurse: like clinician but with less write capability (sprint 4+)
    ("nurse", "tenant.read", "tenant"): True,
    # auditor: read-only audit access + tenant context
    ("auditor", "tenant.read", "tenant"): True,
    ("auditor", "audit.read", "audit"): True,
    ("auditor", "audit.verify", "audit"): True,
    # service: machine-to-machine identity (no human-facing perms today)
    # ── Sprint 03: ASR ─────────────────────────────────────────────────
    ("tenant_admin", "asr.write", "asr_job"): True,
    ("tenant_admin", "asr.read", "asr_job"): True,
    ("tenant_admin", "asr.cancel", "asr_job"): True,
    ("clinician", "asr.write", "asr_job"): True,
    ("clinician", "asr.read", "asr_job"): True,
    ("clinician", "asr.cancel", "asr_job"): True,
    # Nurses can submit and read their own; cancel still goes through
    # clinician/admin in the pilot.
    ("nurse", "asr.write", "asr_job"): True,
    ("nurse", "asr.read", "asr_job"): True,
    # Service tokens (asr-worker → audit/storage) need read+cancel:
    ("service", "asr.read", "asr_job"): True,
    ("service", "asr.write", "asr_job"): True,
    # ── Sprint 04: streaming dictation ────────────────────────────────
    ("tenant_admin", "dictation.start", "dictation_session"): True,
    ("tenant_admin", "dictation.read", "dictation_session"): True,
    ("tenant_admin", "dictation.finalize", "dictation_session"): True,
    ("clinician", "dictation.start", "dictation_session"): True,
    ("clinician", "dictation.read", "dictation_session"): True,
    ("clinician", "dictation.finalize", "dictation_session"): True,
    ("nurse", "dictation.start", "dictation_session"): True,
    ("nurse", "dictation.read", "dictation_session"): True,
    ("nurse", "dictation.finalize", "dictation_session"): True,
    # Service tokens (S2S between dictation-service and NLP in sprint 05):
    ("service", "dictation.read", "dictation_session"): True,
    # ── Sprint 05: NLP post-processing ───────────────────────────────
    ("tenant_admin", "nlp.process", "nlp_text"): True,
    ("clinician", "nlp.process", "nlp_text"): True,
    ("nurse", "nlp.process", "nlp_text"): True,
    ("service", "nlp.process", "nlp_text"): True,
    ("tenant_admin", "nlp.read.abbreviations", "abbreviation"): True,
    ("tenant_admin", "nlp.write.abbreviations", "abbreviation"): True,
    ("clinician", "nlp.read.abbreviations", "abbreviation"): True,
    ("nurse", "nlp.read.abbreviations", "abbreviation"): True,
    ("auditor", "nlp.read.abbreviations", "abbreviation"): True,
    ("service", "nlp.read.abbreviations", "abbreviation"): True,
    # ── Sprint 06: templates ─────────────────────────────────────────
    ("tenant_admin", "template.read", "template"): True,
    ("tenant_admin", "template.clone", "template"): True,
    ("tenant_admin", "template.update", "template"): True,
    ("tenant_admin", "template.deprecate", "template"): True,
    ("clinician", "template.read", "template"): True,
    ("nurse", "template.read", "template"): True,
    ("auditor", "template.read", "template"): True,
    # Service tokens read templates to load them for dictation/nlp:
    ("service", "template.read", "template"): True,
    # ── Sprint 08: reports (versioning, diff, search) ────────────────
    # Clinical document — mirrors dictation_session: authors (admin,
    # clinician, nurse) read+write; auditors denied content; service
    # tokens read-only (signing-service S2S reads a report to sign it).
    ("tenant_admin", "report.write", "report"): True,
    ("tenant_admin", "report.read", "report"): True,
    ("clinician", "report.write", "report"): True,
    ("clinician", "report.read", "report"): True,
    ("nurse", "report.write", "report"): True,
    ("nurse", "report.read", "report"): True,
    ("service", "report.read", "report"): True,
}


class AuthzDeniedError(Exception):
    """Raised by ``requires()``-shaped deps when a role check fails.

    Distinct from ``HTTPException`` so callers can choose to emit an audit
    event before mapping to 403. The auth-service does exactly that — see
    ``services/auth-service/src/auth_service/deps.py``.
    """

    def __init__(
        self,
        *,
        action: Action,
        target_kind: TargetKind,
        claims: Claims,
        reason: str = "role_denied",
        required_scope: str | None = None,
    ) -> None:
        super().__init__(
            f"deny: roles={list(claims.roles)} cannot {action!r} on {target_kind!r} "
            f"(reason={reason})"
        )
        self.action = action
        self.target_kind = target_kind
        self.claims = claims
        self.reason = reason
        self.required_scope = required_scope


def can(role: Role, action: Action, target_kind: TargetKind) -> bool:
    """Return ``True`` iff the matrix has an explicit allow for the tuple."""
    return ALLOW.get((role, action, target_kind), False)


def check(
    claims: Claims,
    *,
    action: Action,
    target_kind: TargetKind,
    scope: str | None = None,
) -> None:
    """Raise :class:`AuthzDeniedError` if none of the caller's roles allow
    the action, or if ``scope`` is required but missing from ``claims.scope``.

    Pure / framework-free — both libs/auth tests and the auth-service dep
    call this same function.
    """
    if not any(can(role, action, target_kind) for role in claims.roles):
        raise AuthzDeniedError(
            action=action,
            target_kind=target_kind,
            claims=claims,
            reason="role_denied",
        )
    if scope is not None:
        token_scopes = claims.scope.split() if claims.scope else []
        if scope not in token_scopes:
            raise AuthzDeniedError(
                action=action,
                target_kind=target_kind,
                claims=claims,
                reason="scope_missing",
                required_scope=scope,
            )

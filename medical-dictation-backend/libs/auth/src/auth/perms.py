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
        "patient",
        "note",
        "notification",
        "phrase",
        "phi_access_request",
        "synonym",
    }
)


# ── The matrix ──────────────────────────────────────────────────────────
# Only "True" entries are listed; ``can`` defaults to deny.
# Mirror at docs/auth/permissions.csv.

ALLOW: Final[dict[tuple[Role, Action, TargetKind], bool]] = {
    # tenant_admin: tenant-wide admin
    ("tenant_admin", "tenant.read", "tenant"): True,
    ("tenant_admin", "tenant.update", "tenant"): True,
    # Tenant (clinic) lifecycle + membership management (Sprint 12).
    ("tenant_admin", "tenant.create", "tenant"): True,
    ("tenant_admin", "tenant.manage_members", "tenant"): True,
    ("tenant_admin", "user.read", "user"): True,
    ("tenant_admin", "user.invite", "user"): True,
    ("tenant_admin", "user.manage_roles", "user"): True,
    ("tenant_admin", "user.deactivate", "user"): True,
    ("tenant_admin", "user.reactivate", "user"): True,
    ("tenant_admin", "user.reset_mfa", "user"): True,
    ("tenant_admin", "audit.read", "audit"): True,
    ("tenant_admin", "audit.verify", "audit"): True,
    # clinician: routine clinical user (sprint 2 surface)
    ("clinician", "tenant.read", "tenant"): True,
    # nurse: like clinician but with less write capability (sprint 4+)
    ("nurse", "tenant.read", "tenant"): True,
    # auditor: read-only audit access + tenant context. user.read gives the
    # auditor read-only visibility of the tenant's user roster (CRUD task).
    ("auditor", "tenant.read", "tenant"): True,
    ("auditor", "user.read", "user"): True,
    ("auditor", "audit.read", "audit"): True,
    ("auditor", "audit.verify", "audit"): True,
    # service: machine-to-machine identity (no human-facing perms today)
    # ── Sprint 03: ASR ─────────────────────────────────────────────────
    # tenant_admin is DELIBERATELY absent from asr.* — see the PHI
    # separation block at the bottom of this matrix.
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
    # tenant_admin is DELIBERATELY absent — see the PHI separation block.
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
    # Clinical document — authors (clinician, nurse) read+write; auditors
    # denied content; service tokens read-only (signing-service S2S reads
    # a report to sign it). tenant_admin is DELIBERATELY absent — see the
    # PHI separation block.
    ("clinician", "report.write", "report"): True,
    ("clinician", "report.read", "report"): True,
    ("nurse", "report.write", "report"): True,
    ("nurse", "report.read", "report"): True,
    ("service", "report.read", "report"): True,
    # ── Sprint 11: patients (clinical/EHR core-service) ──────────────
    # Patient roster + the per-patient record (encounters, consents,
    # anamnesis, privacy). Two tiers since S15:
    #
    #   patient.read       — the roster LIST. For tenant_admin the rows
    #                        come back REDACTED (name + id): enough to
    #                        find the record to break glass on, nothing
    #                        more. Clinical roles get full rows.
    #   patient.read_full  — one patient's demographics + timeline.
    #                        Clinical roles only; an admin reaches the
    #                        same endpoints through a live per-patient
    #                        break-glass grant (phi_access.request).
    #
    # Auditors denied PHI; service tokens have no S2S surface here today.
    ("tenant_admin", "patient.read", "patient"): True,
    ("tenant_admin", "patient.write", "patient"): True,
    ("clinician", "patient.read", "patient"): True,
    ("clinician", "patient.read_full", "patient"): True,
    ("clinician", "patient.write", "patient"): True,
    ("nurse", "patient.read", "patient"): True,
    ("nurse", "patient.read_full", "patient"): True,
    ("nurse", "patient.write", "patient"): True,
    # Erasure approval (S11 step 04): the SECOND person of the two-person
    # rule. tenant_admin only — requesting stays under patient.write, and
    # the service layer + DB CHECK forbid approving one's own request.
    ("tenant_admin", "privacy.approve", "patient"): True,
    # DSAR export (S11 step 06): produces the complete PHI package —
    # admin-only, like erasure approval.
    ("tenant_admin", "patient.dsar", "patient"): True,
    # Clinical notes (SOAP/APSO/DAP/free) bound to a patient.
    # tenant_admin is DELIBERATELY absent — see the PHI separation block.
    ("clinician", "note.read", "note"): True,
    ("clinician", "note.write", "note"): True,
    ("nurse", "note.read", "note"): True,
    ("nurse", "note.write", "note"): True,
    # ── Sprint 12: notifications ──────────────────────────────────────
    # Every role that can hold a session gets both, INCLUDING auditor:
    # these act only on the caller's OWN notification rows (the endpoints
    # take no user_id and the queries filter on recipient_user_id), so
    # this grants no visibility into clinical content. Withholding it
    # would leave an auditor unable to read or dismiss alerts addressed
    # to them.
    ("tenant_admin", "notification.read", "notification"): True,
    ("tenant_admin", "notification.write", "notification"): True,
    ("clinician", "notification.read", "notification"): True,
    ("clinician", "notification.write", "notification"): True,
    ("nurse", "notification.read", "notification"): True,
    ("nurse", "notification.write", "notification"): True,
    ("auditor", "notification.read", "notification"): True,
    ("auditor", "notification.write", "notification"): True,
    # ── Admin ⟂ PHI separation ────────────────────────────────────────
    # A tenant_admin runs the clinic, not the clinical record. The four
    # blocks above deliberately drop tenant_admin from `asr.*`,
    # `dictation.*`, `report.read`/`report.write` and `note.*`: an
    # administrator has no standing clinical need for a patient's
    # dictations, notes or reports. Since S15 the patient record itself
    # is behind the same wall: the admin keeps the REDACTED roster
    # (`patient.read`, name + id) and registration (`patient.write`),
    # but opening one patient's demographics or timeline requires
    # `patient.read_full` — which admins do not hold — or a live
    # per-patient break-glass grant.
    #
    # Note this is a matrix over ROLES, not people: a practising doctor
    # who also administers the tenant holds BOTH `tenant_admin` and
    # `clinician`, and `check()` passes on any granting role — so their
    # clinical access is unchanged. It is the admin-ONLY account that
    # loses the clinical surfaces.
    #
    # Two escape hatches keep that from being a wall instead of a door:
    #
    #   stats.read  — PHI-free aggregate reads. Gates the list endpoints
    #                 the business dashboard aggregates (report search,
    #                 dictation sessions, ASR jobs) in a stripped mode:
    #                 no title, no snippet, no patient reference, no
    #                 transcript, no result URL. Counts and timings only.
    #   phi_access.* — the break-glass path below.
    ("tenant_admin", "stats.read", "tenant"): True,
    # ── Break-glass access to a single report or patient ──────────────
    # An admin who genuinely needs one report or one patient record (a
    # complaint, a legal request, a billing dispute) requests it: a
    # reason from a closed vocabulary plus a password re-entry mints a
    # time-limited, single-resource grant. Every step is audited at
    # `sec` severity and — for reports — the authors are notified.
    # `phi_access.read` is the oversight surface — who broke glass, on
    # what, and why.
    ("tenant_admin", "phi_access.request", "phi_access_request"): True,
    ("tenant_admin", "phi_access.read", "phi_access_request"): True,
    ("auditor", "phi_access.read", "phi_access_request"): True,
    # ── Autocomplete phrases (decoupled from report.*) ────────────────
    # Sprint 10 reused `report.read`/`report.write` to gate the phrase
    # library because no role needed the distinction. Dropping
    # tenant_admin from `report.*` above makes one: curating the tenant
    # phrase library is administration, not clinical authorship. These
    # are that distinction, and autocomplete-service now gates on them.
    ("tenant_admin", "autocomplete.read", "phrase"): True,
    ("tenant_admin", "autocomplete.write", "phrase"): True,
    ("clinician", "autocomplete.read", "phrase"): True,
    ("clinician", "autocomplete.write", "phrase"): True,
    ("nurse", "autocomplete.read", "phrase"): True,
    ("nurse", "autocomplete.write", "phrase"): True,
    ("service", "autocomplete.read", "phrase"): True,
    # ── Sprint 15: medical synonyms (search query expansion, ADR-0038) ──
    # The dictionary is search metadata, not PHI: reading it rides along
    # with searching (clinical roles + service + admin); writing tenant
    # entries is an admin curation act — tenant_admin holding write does
    # NOT breach the PHI separation because synonym rows carry dictionary
    # terms, never patient data.
    ("clinician", "synonym.read", "synonym"): True,
    ("nurse", "synonym.read", "synonym"): True,
    ("service", "synonym.read", "synonym"): True,
    ("tenant_admin", "synonym.read", "synonym"): True,
    ("auditor", "synonym.read", "synonym"): False,
    ("tenant_admin", "synonym.write", "synonym"): True,
    ("clinician", "synonym.write", "synonym"): False,
    ("nurse", "synonym.write", "synonym"): False,
    ("auditor", "synonym.write", "synonym"): False,
    ("service", "synonym.write", "synonym"): False,
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


def can_claims(claims: Claims, action: Action, target_kind: TargetKind) -> bool:
    """``True`` iff any of the caller's roles grants the tuple.

    The predicate form of :func:`check` — for handlers that must *branch*
    on a permission rather than refuse without it (a report search that
    answers in stripped, PHI-free form when the caller only holds
    ``stats.read``).
    """
    return any(can(role, action, target_kind) for role in claims.roles)


def check_any(
    claims: Claims,
    *,
    options: tuple[tuple[Action, TargetKind], ...],
) -> None:
    """Pass if ANY of the ``(action, target_kind)`` pairs is granted.

    For endpoints reachable by two different standings — a clinician's
    full clinical read, or an admin's PHI-free aggregate read. The denial
    is reported against the FIRST option, which is by convention the
    primary/most-privileged one, so the audit row and the 403 name the
    permission the caller was most likely reaching for.
    """
    if not options:
        raise ValueError("check_any requires at least one option")
    for action, target_kind in options:
        if can_claims(claims, action, target_kind):
            return
    action, target_kind = options[0]
    raise AuthzDeniedError(
        action=action,
        target_kind=target_kind,
        claims=claims,
        reason="role_denied",
    )


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

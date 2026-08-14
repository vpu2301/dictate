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
    {"tenant_admin", "clinician", "nurse", "auditor", "service", "knowledge_admin"}
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
        "consent",
        "note",
        "notification",
        "phrase",
        "phi_access_request",
        "synonym",
        "evidence",
        "evidence_corpus",
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
    # S21: ask a user to enrol a second factor. Distinct from reset_mfa,
    # which CLEARS one — this touches nothing about the account, which is
    # why the auditor holds it too (see the auditor block below).
    ("tenant_admin", "user.remind_mfa", "user"): True,
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
    # The auditor's ONLY write in the whole matrix, and it is deliberate:
    # an access review that can see an account without a second factor but
    # cannot ask for one produces a finding nobody acts on. The act changes
    # nothing about the account — no role, no status, no credential — it
    # records a request that only the SUBJECT can close, by enrolling. That
    # asymmetry is what keeps the read-only role read-only.
    ("auditor", "user.remind_mfa", "user"): True,
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
    # ── Signing authority — CLINICIAN ONLY (hotfix) ───────────────────
    # Applying a qualified electronic signature to a clinical document is
    # a clinician's personal legal act under Law 2155-VIII. It is NOT an
    # aspect of "being able to write the document", which is what
    # `report.write` means and which nurses legitimately hold: a nurse
    # prepares and finalizes, a clinician signs.
    #
    # Before this hotfix every signing surface — the report sign route,
    # the signing-service session initiation, the local-KEP upload, the
    # certificate enumeration, the inline signer, and the consent sign
    # route — gated on `report.write`/`patient.write`. Both are held by
    # `nurse`; `patient.write` is additionally held by `tenant_admin`.
    # Signing authority therefore had no representation in this matrix at
    # all. These three actions give it one.
    #
    #   report.sign    — finalized → signed. The signature itself.
    #   report.amend   — signed → amended. Amending a signed clinical
    #                    report re-signs it, so it is the same act.
    #   consent.sign   — КЕП on a patient consent. A nurse may still
    #                    RECORD that a consent exists (`patient.write`
    #                    on POST /consents); attesting to it with a
    #                    qualified signature is the clinical act.
    #
    # `report.finalize` deliberately does NOT appear here. Finalize is
    # the structural draft → finalized transition — it validates required
    # sections and ICD-10 codes and freezes the version for signature. It
    # applies no signature and advances nothing towards one, so it stays
    # under `report.write` and nurses keep it.
    #
    # If a site employs physician-assistant-style staff who legally sign,
    # that is a NEW role with `report.sign` granted — never `nurse`
    # widened, which would silently re-open this hole for every nurse in
    # every tenant.
    ("clinician", "report.sign", "report"): True,
    ("clinician", "report.amend", "report"): True,
    ("clinician", "consent.sign", "consent"): True,
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
    #
    # Admin-only, and back to being so (2026-08-09). The
    # treatment-relationship hotfix had briefly made a clinical role's
    # standing read relationship-scoped, which sent the covering doctor
    # and the corridor consult through this door too — so the capability
    # was extended to them. That gate is gone: `patient.read_full` and
    # `report.read` are once again the whole answer for a clinical role,
    # with the relationship recorded in the audit event rather than
    # required (see the guards in core-service / report-service).
    #
    # Which makes this a capability a clinician can no longer spend. A
    # permission with no reachable use is not harmless — it is a live
    # grant-minting power sitting on the role that least needs it, and a
    # modal the UI would have to keep alive for a door nobody walks
    # through. So it goes back to the administrator, who genuinely holds
    # no clinical read and for whom break-glass is the ONLY way in.
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
    # ── Sprint 21: corpus review (ADR-0043/0044) ──────────────────────
    # Gates the /admin/corpus review surface + the review-recording path.
    # The reviewer is a clinician (tier-3 decisions are clinical safety
    # judgements, human-mandatory); tenant_admin holds it for the admin
    # console. Nurses/auditors don't review corpus candidates.
    ("tenant_admin", "corpus.review", "phrase"): True,
    ("clinician", "corpus.review", "phrase"): True,
    ("nurse", "corpus.review", "phrase"): False,
    ("auditor", "corpus.review", "phrase"): False,
    ("service", "corpus.review", "phrase"): False,
    ("knowledge_admin", "corpus.review", "phrase"): False,
    # ── Corpus contribution + promotion (authored ingest over HTTP) ────
    # `corpus.contribute` gates POST /corpus/candidates: a typed or
    # dictated phrase from the console fill worksheet becomes a GLOBAL
    # 'candidate' row awaiting review — never anything pre-accepted, so
    # contributing is safe for the same roles that author clinical text.
    # `corpus.promote` gates POST /corpus/promote (accepted → serving
    # corpus): a curation/release act, admin-only — the reviewer who
    # accepts must not be able to single-handedly publish to all tenants.
    ("tenant_admin", "corpus.contribute", "phrase"): True,
    ("clinician", "corpus.contribute", "phrase"): True,
    ("nurse", "corpus.contribute", "phrase"): False,
    ("auditor", "corpus.contribute", "phrase"): False,
    ("service", "corpus.contribute", "phrase"): False,
    ("knowledge_admin", "corpus.contribute", "phrase"): False,
    ("tenant_admin", "corpus.promote", "phrase"): True,
    ("clinician", "corpus.promote", "phrase"): False,
    ("nurse", "corpus.promote", "phrase"): False,
    ("auditor", "corpus.promote", "phrase"): False,
    ("service", "corpus.promote", "phrase"): False,
    ("knowledge_admin", "corpus.promote", "phrase"): False,
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
    # ── EVA-S01: evidence module (Evidence AI extension) ──────────────
    # New role `knowledge_admin` curates the tenant corpus and holds NO
    # clinical or user-management permission (explicit false rows in the
    # CSV for every pre-existing action). Patient-context reads follow the
    # admin ⟂ PHI separation: tenant_admin may ask generic questions but
    # never reads patient context (evidence.context.read is clinical-only).
    # nurse context reads and clinician drug predictions are additionally
    # gated by per-site feature flags in the service layer (S05/S10); the
    # matrix records the ceiling, the flag lowers it at runtime.
    ("clinician", "evidence.ask", "evidence"): True,
    ("nurse", "evidence.ask", "evidence"): True,
    ("tenant_admin", "evidence.ask", "evidence"): True,
    ("clinician", "evidence.context.read", "evidence"): True,
    ("nurse", "evidence.context.read", "evidence"): True,
    ("tenant_admin", "evidence.context.read", "evidence"): False,
    ("clinician", "evidence.acts.manage", "evidence"): True,
    ("tenant_admin", "evidence.acts.manage", "evidence"): True,
    ("clinician", "evidence.deeptrace.run", "evidence"): True,
    ("tenant_admin", "evidence.deeptrace.run", "evidence"): True,
    ("clinician", "evidence.drugs.read", "evidence"): True,
    ("nurse", "evidence.drugs.read", "evidence"): True,
    ("tenant_admin", "evidence.drugs.read", "evidence"): True,
    ("clinician", "evidence.drugs.predict", "evidence"): True,
    ("knowledge_admin", "evidence.corpus.manage", "evidence_corpus"): True,
    ("tenant_admin", "evidence.corpus.manage", "evidence_corpus"): True,
    ("knowledge_admin", "evidence.domains.manage", "evidence_corpus"): True,
    ("auditor", "evidence.ops.read", "evidence"): True,
    ("tenant_admin", "evidence.ops.read", "evidence"): True,
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

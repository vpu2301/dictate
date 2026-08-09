"""Permission-matrix exhaustive test.

The CSV at ``docs/auth/permissions.csv`` is the human-reviewable source of
truth. ``libs/auth.perms.ALLOW`` is the runtime gate. This file fails CI
if the two diverge — adding a permission means editing both.

Strategy:

1. Parse the CSV.
2. For every row, assert ``can(role, action, target) == row.allowed``.
3. Reject duplicate keys in the CSV.
4. Reject any (role, action, target) referenced by ``ALLOW`` that the CSV
   doesn't list — the CSV must be the *complete* allowlist documentation.
"""

import csv
from pathlib import Path

import pytest

from auth.claims import Claims
from auth.perms import (
    ALLOW,
    KNOWN_ROLES,
    KNOWN_TARGET_KINDS,
    AuthzDeniedError,
    can,
    check,
)

CSV_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "docs"
    / "auth"
    / "permissions.csv"
)


def _csv_rows() -> list[dict[str, str]]:
    assert CSV_PATH.exists(), f"missing CSV at {CSV_PATH}"
    with CSV_PATH.open() as f:
        return list(csv.DictReader(f))


def _make_claims(roles: list[str], scope: str = "") -> Claims:
    """A minimally-valid Claims object — only the fields perms checks reads."""
    from uuid import UUID

    return Claims(
        sub=UUID("11111111-1111-1111-1111-111111111111"),
        tid=UUID("00000000-0000-0000-0000-00000000000a"),
        roles=roles,
        scope=scope,
        mfa=False,
        sid="s",
        iss="i",
        aud="a",
        iat=0,
        exp=9999999999,
    )


# ── CSV ↔ code sync ─────────────────────────────────────────────────────


def test_csv_is_non_empty():
    rows = _csv_rows()
    assert len(rows) > 0


def test_csv_columns():
    rows = _csv_rows()
    assert set(rows[0].keys()) >= {"role", "action", "target_kind", "allowed"}


def test_csv_has_no_duplicate_keys():
    rows = _csv_rows()
    keys = [(r["role"], r["action"], r["target_kind"]) for r in rows]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, f"duplicate rows in CSV: {duplicates}"


def test_csv_uses_only_known_roles_and_targets():
    rows = _csv_rows()
    for r in rows:
        assert r["role"] in KNOWN_ROLES, f"unknown role {r['role']!r} in CSV"
        assert r["target_kind"] in KNOWN_TARGET_KINDS, f"unknown target {r['target_kind']!r} in CSV"


def test_every_csv_row_matches_can():
    """For every (role, action, target) in the CSV, can() returns CSV's allowed."""
    rows = _csv_rows()
    failures = []
    for r in rows:
        expected = r["allowed"].strip().lower() == "true"
        got = can(r["role"], r["action"], r["target_kind"])
        if got != expected:
            failures.append(
                f"{r['role']}/{r['action']}/{r['target_kind']}: CSV={expected} can()={got}"
            )
    assert not failures, "CSV ↔ code drift:\n" + "\n".join(failures)


def test_every_allow_entry_is_in_csv():
    """The CSV must be the *complete* documentation. Any (role, action,
    target) granted in code must appear (with allowed=true) in the CSV."""
    rows = _csv_rows()
    csv_allows = {
        (r["role"], r["action"], r["target_kind"])
        for r in rows
        if r["allowed"].strip().lower() == "true"
    }
    code_allows = {key for key, v in ALLOW.items() if v}
    missing = code_allows - csv_allows
    assert not missing, f"code grants not in CSV: {missing}"
    extra = csv_allows - code_allows
    assert not extra, f"CSV grants not in code: {extra}"


def test_matrix_is_complete_every_role_has_explicit_row():
    """Exhaustiveness: for every (action, target_kind) the CSV mentions,
    there is an explicit row for *every* known role — true or false.

    This makes deny a decision, never an accident. A new action that omits
    even one role fails CI here, forcing the author to make the call.
    """
    rows = _csv_rows()
    pairs = {(r["action"], r["target_kind"]) for r in rows}
    seen = {(r["role"], r["action"], r["target_kind"]) for r in rows}
    missing: list[str] = []
    for action, target in sorted(pairs):
        for role in sorted(KNOWN_ROLES):
            if (role, action, target) not in seen:
                missing.append(f"{role}/{action}/{target}")
    assert not missing, (
        "matrix incomplete — every (role × action × target_kind) needs an "
        "explicit true/false row:\n" + "\n".join(missing)
    )


# ── can() ────────────────────────────────────────────────────────────────


def test_can_default_is_deny():
    assert can("clinician", "audit.read", "audit") is False
    assert can("nurse", "user.invite", "user") is False


def test_negative_space_explicit_denies():
    """C4: things that MUST be denied across the CRUD surface. Each of these
    is an explicit ``false`` row in the CSV; this pins the intent in code."""
    # Only tenant_admin manages roles.
    assert can("nurse", "user.manage_roles", "user") is False
    assert can("clinician", "user.manage_roles", "user") is False
    assert can("auditor", "user.manage_roles", "user") is False
    assert can("service", "user.manage_roles", "user") is False
    # Auditor is read-only — no template writes.
    assert can("auditor", "template.update", "template") is False
    assert can("auditor", "template.clone", "template") is False
    assert can("auditor", "template.deprecate", "template") is False
    # Service tokens are not human dictation actors.
    assert can("service", "dictation.start", "dictation_session") is False
    assert can("service", "dictation.finalize", "dictation_session") is False
    # Reactivate is admin-only.
    assert can("clinician", "user.reactivate", "user") is False
    assert can("auditor", "user.reactivate", "user") is False
    # user.read is admin + auditor only.
    assert can("clinician", "user.read", "user") is False
    assert can("nurse", "user.read", "user") is False
    assert can("service", "user.read", "user") is False


def test_new_user_crud_grants():
    """The Part-A grants are present exactly where intended."""
    assert can("tenant_admin", "user.read", "user") is True
    assert can("auditor", "user.read", "user") is True
    assert can("tenant_admin", "user.manage_roles", "user") is True
    assert can("tenant_admin", "user.reactivate", "user") is True


def test_can_grants_match_csv():
    assert can("tenant_admin", "user.invite", "user") is True
    assert can("auditor", "audit.verify", "audit") is True
    assert can("clinician", "tenant.read", "tenant") is True


def test_can_unknown_role_or_action_is_deny():
    assert can("super_admin", "audit.read", "audit") is False
    assert can("tenant_admin", "report.summon_lawyer", "user") is False


# ── check(): role-based + scope-based ──────────────────────────────────


def test_check_passes_when_role_allows():
    claims = _make_claims(roles=["auditor"])
    # No exception.
    check(claims, action="audit.read", target_kind="audit")


def test_check_passes_with_any_matching_role():
    claims = _make_claims(roles=["clinician", "auditor"])
    check(claims, action="audit.read", target_kind="audit")  # ok via auditor


def test_check_raises_when_no_role_allows():
    claims = _make_claims(roles=["clinician"])
    with pytest.raises(AuthzDeniedError) as exc:
        check(claims, action="audit.read", target_kind="audit")
    assert exc.value.reason == "role_denied"
    assert exc.value.action == "audit.read"


def test_check_role_pass_but_scope_missing_raises():
    claims = _make_claims(roles=["auditor"], scope="openid email")
    with pytest.raises(AuthzDeniedError) as exc:
        check(claims, action="audit.read", target_kind="audit", scope="audit:read")
    assert exc.value.reason == "scope_missing"
    assert exc.value.required_scope == "audit:read"


def test_check_role_pass_and_scope_present_succeeds():
    claims = _make_claims(roles=["auditor"], scope="openid audit:read")
    check(claims, action="audit.read", target_kind="audit", scope="audit:read")


def test_check_empty_roles_is_deny():
    claims = _make_claims(roles=[])
    with pytest.raises(AuthzDeniedError):
        check(claims, action="tenant.read", target_kind="tenant")


# ── HOTFIX: signing authority is clinician-only ─────────────────────────
#
# The defect: every signing surface in the system gated on `report.write`
# (report sign route, signing-service session initiation, local-KEP
# upload, certificate enumeration, inline signer) or `patient.write`
# (consent sign route). `nurse` holds both; `tenant_admin` holds
# `patient.write`. Signing authority had no representation in the matrix
# at all, so a nurse could apply a qualified electronic signature to a
# clinical report and a nurse or an operational admin could apply one to
# a patient consent.
#
# These tests are the matrix half of the closure. They are written as an
# enumeration over KNOWN_ROLES rather than a list of asserts so that a
# role added later cannot quietly default into signing authority: a new
# role is denied here until someone deliberately adds it to _MAY_SIGN.

_SIGNING_ACTIONS: tuple[tuple[str, str], ...] = (
    ("report.sign", "report"),
    ("report.amend", "report"),
    ("consent.sign", "consent"),
)

_MAY_SIGN: frozenset[str] = frozenset({"clinician"})


def test_signing_actions_are_clinician_only():
    """No role but `clinician` may sign, amend, or sign a consent."""
    granted: dict[tuple[str, str], set[str]] = {}
    for action, target in _SIGNING_ACTIONS:
        granted[(action, target)] = {
            role for role in KNOWN_ROLES if can(role, action, target)
        }
    unexpected = {
        key: sorted(roles - _MAY_SIGN)
        for key, roles in granted.items()
        if roles - _MAY_SIGN
    }
    assert not unexpected, f"non-clinician roles hold signing authority: {unexpected}"
    missing = {
        key: sorted(_MAY_SIGN - roles)
        for key, roles in granted.items()
        if _MAY_SIGN - roles
    }
    assert not missing, f"clinician is missing signing authority: {missing}"


def test_signing_actions_deny_each_non_clinician_role_explicitly():
    """The same fact, spelled out per role — this is the readable slice a
    compliance reviewer is pointed at."""
    for action, target in _SIGNING_ACTIONS:
        assert can("clinician", action, target) is True
        for role in ("tenant_admin", "nurse", "auditor", "service", "knowledge_admin"):
            assert can(role, action, target) is False, (
                f"{role} must not hold {action} on {target}"
            )


def test_signing_authority_is_not_implied_by_write():
    """The precise shape of the defect: holding the write permission on a
    resource must not carry the authority to sign it."""
    # A nurse authors, edits and finalizes reports…
    assert can("nurse", "report.write", "report") is True
    # …and still cannot sign one.
    assert can("nurse", "report.sign", "report") is False
    assert can("nurse", "report.amend", "report") is False
    # A nurse and an admin both record that a consent exists…
    assert can("nurse", "patient.write", "patient") is True
    assert can("tenant_admin", "patient.write", "patient") is True
    # …and neither may attest to it with a qualified signature.
    assert can("nurse", "consent.sign", "consent") is False
    assert can("tenant_admin", "consent.sign", "consent") is False


def test_finalize_is_not_a_signing_act():
    """`report.finalize` is deliberately absent from the signing actions.

    Finalize is the structural draft → finalized transition: it validates
    required sections and ICD-10 codes and freezes the version so that it
    *can* be signed. It applies no signature, so it stays under
    `report.write` and nurses keep it. If a future change makes finalize
    itself sign, this test is the reminder that it must move.
    """
    assert ("report.finalize", "report") not in _SIGNING_ACTIONS
    assert can("nurse", "report.write", "report") is True


def test_check_rejects_non_clinician_signing():
    """The runtime gate, not just the table."""
    for role in ("tenant_admin", "nurse", "auditor", "service"):
        claims = _make_claims(roles=[role])
        for action, target in _SIGNING_ACTIONS:
            with pytest.raises(AuthzDeniedError):
                check(claims, action=action, target_kind=target)
    clinician = _make_claims(roles=["clinician"])
    for action, target in _SIGNING_ACTIONS:
        check(clinician, action=action, target_kind=target)  # no raise


def test_dual_role_clinician_admin_may_still_sign():
    """The matrix is over ROLES, not people. A practising doctor who also
    administers the tenant holds both roles, and `check()` passes on any
    granting role — the hotfix must not strip their clinical authority."""
    both = _make_claims(roles=["tenant_admin", "clinician"])
    for action, target in _SIGNING_ACTIONS:
        check(both, action=action, target_kind=target)  # no raise


# ── break-glass is the ADMINISTRATOR's door ────────────────────────────


def test_break_glass_is_admin_only():
    """The one role with no clinical read at all is the one that needs it.

    Briefly (the treatment-relationship hotfix) this was extended to the
    clinical roles, because their standing read had been narrowed to
    patients they already had a relationship with. That narrowing was
    reverted on 2026-08-09 — `patient.read_full` / `report.read` are once
    again the whole answer for a clinician or a nurse, with the
    relationship recorded in the audit event instead of required — so the
    clinical break-glass door leads nowhere and the grant-minting power
    that came with it is withdrawn.

    Service tokens never get one either: break-glass is a human act with a
    human justification.
    """
    assert can("tenant_admin", "phi_access.request", "phi_access_request") is True
    assert can("clinician", "phi_access.request", "phi_access_request") is False
    assert can("nurse", "phi_access.request", "phi_access_request") is False
    assert can("service", "phi_access.request", "phi_access_request") is False
    assert can("auditor", "phi_access.request", "phi_access_request") is False


def test_clinical_roles_keep_standing_patient_access():
    """The other half of the same revert, stated positively: a clinical
    role opens a chart on its standing permission, full stop. If this ever
    goes back to requiring a relationship, the covering doctor and the
    corridor consult are blocked again — which is what made the control
    fire on the normal case."""
    for role in ("clinician", "nurse"):
        assert can(role, "patient.read_full", "patient") is True, role
        assert can(role, "report.read", "report") is True, role
    # …and the boundary that must NOT move: an admin holds neither.
    assert can("tenant_admin", "patient.read_full", "patient") is False
    assert can("tenant_admin", "report.read", "report") is False


def test_break_glass_oversight_is_admin_and_auditor_only():
    """Reading the grant log is oversight, not clinical work."""
    assert can("tenant_admin", "phi_access.read", "phi_access_request") is True
    assert can("auditor", "phi_access.read", "phi_access_request") is True
    assert can("clinician", "phi_access.read", "phi_access_request") is False
    assert can("nurse", "phi_access.read", "phi_access_request") is False

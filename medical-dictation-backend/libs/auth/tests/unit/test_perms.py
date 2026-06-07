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


# ── can() ────────────────────────────────────────────────────────────────


def test_can_default_is_deny():
    assert can("clinician", "audit.read", "audit") is False
    assert can("nurse", "user.invite", "user") is False


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

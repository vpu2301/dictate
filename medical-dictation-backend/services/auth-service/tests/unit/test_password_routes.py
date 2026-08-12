"""The password-recovery HTTP surface.

Written around the security properties rather than the happy path,
because the happy path is the part that would be noticed if it broke:

  * ``/forgot`` must be an enumeration dead end — identical response for
    a real address, an unknown one, a deactivated one, and a throttled
    one.
  * A reset token must be single-use, and must die along with every
    other outstanding token once a password changes.
  * Every password change must end every live session.
  * A password change must always queue the security notification.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims

TENANT = uuid4()
SUB = uuid4()
EMAIL = "olena@clinic.example"
GOOD_PASSWORD = "correct horse battery staple"


class FakeKeycloak:
    def __init__(self) -> None:
        self.passwords: dict[UUID, str] = {SUB: "old-password-here"}
        self.logged_out: list[UUID] = []
        self.set_password_calls: list[tuple[UUID, str]] = []
        self.grant_ok = True

    async def password_grant(self, *, username: str, password: str) -> Any:
        from auth_service.keycloak_client import KeycloakError

        if not self.grant_ok or self.passwords.get(SUB) != password:
            raise KeycloakError(status=401, body={"error": "invalid_grant"})
        return SimpleNamespace(access_token="a", refresh_token="r")

    async def set_password(
        self, sub: UUID, *, new_password: str, temporary: bool = False
    ) -> None:
        self.set_password_calls.append((sub, new_password))
        self.passwords[sub] = new_password

    async def logout_user(self, sub: UUID) -> None:
        self.logged_out.append(sub)

    async def list_sessions(self, sub: UUID) -> list[dict[str, Any]]:
        return [
            {
                "id": "sess-1",
                "ipAddress": "203.0.113.7",
                "start": 1_754_000_000_000,
                "lastAccess": 1_754_000_100_000,
            }
        ]


class FakeDenylist:
    def __init__(self) -> None:
        self.revoked: list[str] = []

    async def revoke_sub(self, sub: str, *, ttl_seconds: int) -> None:
        self.revoked.append(sub)


class FakeStore:
    """Stands in for the tables migration 0076 creates."""

    def __init__(self) -> None:
        self.account: dict[str, Any] | None = {
            "tenant_id": TENANT,
            "subject_sub": SUB,
            "email": EMAIL,
            "display_name": "Olena",
            "status": "active",
        }
        self.tokens: dict[bytes, dict[str, Any]] = {}
        self.mail: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.spend_all_calls: list[UUID] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from auth_service import deps
    from auth_service.config import settings
    from auth_service.main import create_app
    from auth_service.routers import password as pw

    monkeypatch.setattr(settings, "password_reset_enabled", True)

    store = FakeStore()
    kc = FakeKeycloak()
    denylist = FakeDenylist()
    audit_calls: list[dict[str, Any]] = []
    limiter_allows = {"value": True}

    async def _write_event(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    class _Limiter:
        async def check(self, *, ip: str, email: str) -> bool:
            return limiter_allows["value"]

    @contextlib.asynccontextmanager
    async def _acquire():
        yield SimpleNamespace()

    state = SimpleNamespace(
        keycloak=kc,
        denylist=denylist,
        # Present so `current_user` can run far enough to reject a
        # request with no Authorization header, which is what the
        # unauthenticated-access test asserts.
        jwks_cache=object(),
        audit_writer=SimpleNamespace(write_event=_write_event),
        # The tenant-blind lookups acquire an unscoped connection off
        # this pool; the tenant-scoped ones go through the patched
        # `tenant_connection`, which ignores the pool object entirely.
        app_pool=SimpleNamespace(acquire=_acquire),
        tenant_writer_pool=SimpleNamespace(acquire=_acquire),
        password_rate_limiter=_Limiter(),
    )
    deps.install_state(state)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_conn(pool: Any, tenant_id: Any):
        async def _fetchrow(query: str, *a: Any) -> Any:
            if "FROM users" in query:
                return {"email": EMAIL, "display_name": "Olena"}
            return None

        async def _execute(*a: Any, **k: Any) -> None:
            return None

        yield SimpleNamespace(fetchrow=_fetchrow, execute=_execute)

    monkeypatch.setattr(pw, "tenant_connection", _fake_conn)

    # ── Repository fakes ─────────────────────────────────────────────
    async def _resolve(conn: Any, *, email: str) -> Any:
        if store.account and email.lower() == EMAIL.lower():
            return store.account
        return None

    async def _insert_token(
        conn: Any,
        *,
        tenant_id: UUID,
        subject_sub: UUID,
        token_hash: bytes,
        purpose: str,
        expires_at: Any,
        requested_ip_hash: str,
    ) -> UUID:
        store.tokens[token_hash] = {
            "tenant_id": tenant_id,
            "subject_sub": subject_sub,
            "purpose": purpose,
            "consumed": False,
        }
        return uuid4()

    async def _peek(conn: Any, *, token_hash: bytes, purpose: str) -> Any:
        row = store.tokens.get(token_hash)
        if row is None or row["consumed"] or row["purpose"] != purpose:
            return None
        return {"tenant_id": row["tenant_id"], "subject_sub": row["subject_sub"]}

    async def _consume(conn: Any, *, token_hash: bytes, purpose: str) -> Any:
        row = store.tokens.get(token_hash)
        if row is None or row["consumed"] or row["purpose"] != purpose:
            return None
        row["consumed"] = True
        return {"tenant_id": row["tenant_id"], "subject_sub": row["subject_sub"]}

    async def _spend_all(conn: Any, *, subject_sub: UUID) -> int:
        store.spend_all_calls.append(subject_sub)
        n = 0
        for row in store.tokens.values():
            if row["subject_sub"] == subject_sub and not row["consumed"]:
                row["consumed"] = True
                n += 1
        return n

    async def _enqueue(conn: Any, **kwargs: Any) -> UUID:
        store.mail.append(kwargs)
        return uuid4()

    async def _record(conn: Any, **kwargs: Any) -> None:
        store.events.append(kwargs)

    async def _sweep(conn: Any) -> None:
        return None

    async def _recent(conn: Any, *, subject_sub: UUID, limit: int = 20) -> list[Any]:
        return []

    for name, fn in [
        ("resolve_account_by_email", _resolve),
        ("insert_token", _insert_token),
        ("peek_token", _peek),
        ("consume_token", _consume),
        ("spend_all_tokens", _spend_all),
        ("enqueue_mail", _enqueue),
        ("record_password_event", _record),
        ("sweep_dead_tokens", _sweep),
        ("recent_password_events", _recent),
    ]:
        monkeypatch.setattr(pw.repo, name, fn)

    def _client(claims: Claims | None = None) -> TestClient:
        app = create_app()
        if claims is not None:
            app.dependency_overrides[deps.current_user] = lambda: claims
        return TestClient(app)

    return SimpleNamespace(
        client=_client,
        store=store,
        kc=kc,
        denylist=denylist,
        audit=audit_calls,
        limiter_allows=limiter_allows,
        pw=pw,
        settings=settings,
    )


def _claims() -> Claims:
    return Claims(
        sub=SUB,
        tid=TENANT,
        sid="sess-1",
        roles=["clinician"],
        email=EMAIL,
        preferred_username=EMAIL,
        iss="https://kc/realms/medical-dictation",
        aud="mdx-api",
        exp=9_999_999_999,
        iat=0,
        nbf=0,
        mfa=False,
    )


def _token_for(env: Any, purpose: str) -> str:
    """Reach into the fake store for the plaintext of the only token."""
    import hashlib
    import secrets

    # Mint through the router's own helper so the hashing matches.
    token = secrets.token_urlsafe(32)
    env.store.tokens[hashlib.sha256(token.encode()).digest()] = {
        "tenant_id": TENANT,
        "subject_sub": SUB,
        "purpose": purpose,
        "consumed": False,
    }
    return token


# ── /forgot: the enumeration dead end ────────────────────────────────


def test_forgot_queues_mail_for_a_real_account(env: Any) -> None:
    r = env.client().post("/auth/password/forgot", json={"email": EMAIL})
    assert r.status_code == 202
    assert r.json() == {"status": "accepted"}
    assert len(env.store.mail) == 1
    assert env.store.mail[0]["kind"] == "password_reset"
    assert env.store.mail[0]["to_address"] == EMAIL
    # The token-bearing half is separate from the retained half.
    assert "reset_url" in env.store.mail[0]["secret_fields"]
    assert "reset_url" not in env.store.mail[0]["render_fields"]


def test_forgot_is_identical_for_an_unknown_address(env: Any) -> None:
    known = env.client().post("/auth/password/forgot", json={"email": EMAIL})
    env.store.mail.clear()
    unknown = env.client().post(
        "/auth/password/forgot", json={"email": "nobody@nowhere.example"}
    )
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert env.store.mail == []


def test_forgot_is_identical_for_a_deactivated_account(env: Any) -> None:
    env.store.account["status"] = "deactivated"
    r = env.client().post("/auth/password/forgot", json={"email": EMAIL})
    assert r.status_code == 202
    assert r.json() == {"status": "accepted"}
    assert env.store.mail == []


def test_forgot_is_identical_when_rate_limited(env: Any) -> None:
    """A 429 would confirm the sweep is being counted — free intel."""
    env.limiter_allows["value"] = False
    r = env.client().post("/auth/password/forgot", json={"email": EMAIL})
    assert r.status_code == 202
    assert r.json() == {"status": "accepted"}
    assert env.store.mail == []


def test_forgot_never_audits_an_unknown_address(env: Any) -> None:
    """An audit row per probe would rebuild the enumeration oracle the
    uniform 202 exists to remove."""
    env.client().post(
        "/auth/password/forgot", json={"email": "nobody@nowhere.example"}
    )
    assert env.audit == []


def test_forgot_rejects_unknown_fields(env: Any) -> None:
    r = env.client().post(
        "/auth/password/forgot", json={"email": EMAIL, "admin": True}
    )
    assert r.status_code == 422


def test_endpoints_are_404_when_the_feature_is_off(env: Any) -> None:
    """A disabled feature must not be discoverable."""
    env.settings.password_reset_enabled = False
    try:
        r = env.client().post("/auth/password/forgot", json={"email": EMAIL})
        assert r.status_code == 404
    finally:
        env.settings.password_reset_enabled = True


# ── /reset ───────────────────────────────────────────────────────────


def test_reset_sets_the_password_and_kills_sessions(env: Any) -> None:
    token = _token_for(env, "password_reset")
    r = env.client().post(
        "/auth/password/reset", json={"token": token, "new_password": GOOD_PASSWORD}
    )
    assert r.status_code == 204
    assert env.kc.set_password_calls == [(SUB, GOOD_PASSWORD)]
    assert SUB in env.kc.logged_out
    assert str(SUB) in env.denylist.revoked


def test_reset_token_is_single_use(env: Any) -> None:
    token = _token_for(env, "password_reset")
    first = env.client().post(
        "/auth/password/reset", json={"token": token, "new_password": GOOD_PASSWORD}
    )
    second = env.client().post(
        "/auth/password/reset",
        json={"token": token, "new_password": "another good passphrase"},
    )
    assert first.status_code == 204
    assert second.status_code == 400
    assert second.json()["code"] == "invalid_reset_token"


def test_reset_spends_every_other_outstanding_token(env: Any) -> None:
    """A second live link would be a spare key to an account the user
    believes they have just secured."""
    stale = _token_for(env, "password_reset")
    fresh = _token_for(env, "password_reset")
    assert (
        env.client()
        .post(
            "/auth/password/reset",
            json={"token": fresh, "new_password": GOOD_PASSWORD},
        )
        .status_code
        == 204
    )
    replay = env.client().post(
        "/auth/password/reset",
        json={"token": stale, "new_password": "yet another passphrase"},
    )
    assert replay.status_code == 400


def test_reset_queues_the_security_notification(env: Any) -> None:
    token = _token_for(env, "password_reset")
    env.client().post(
        "/auth/password/reset", json={"token": token, "new_password": GOOD_PASSWORD}
    )
    kinds = [m["kind"] for m in env.store.mail]
    assert "password_changed" in kinds
    notification = next(m for m in env.store.mail if m["kind"] == "password_changed")
    assert "lockdown_url" in notification["secret_fields"]


def test_reset_refuses_a_weak_password_with_machine_readable_reasons(
    env: Any,
) -> None:
    token = _token_for(env, "password_reset")
    r = env.client().post(
        "/auth/password/reset", json={"token": token, "new_password": "password1234"}
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "weak_password"
    assert "common" in body["reasons"]
    assert body["min_length"] >= 12


def test_a_rejected_password_does_not_burn_the_link(env: Any) -> None:
    """Peek, judge, THEN consume.

    Consuming first would mean one typo costs a locked-out user their
    only link and a trip back to their inbox — and it stops no attacker,
    who would simply submit a strong password first time.
    """
    token = _token_for(env, "password_reset")
    weak = env.client().post(
        "/auth/password/reset", json={"token": token, "new_password": "password1234"}
    )
    assert weak.status_code == 422
    # The very next attempt with a good password must still work.
    good = env.client().post(
        "/auth/password/reset", json={"token": token, "new_password": GOOD_PASSWORD}
    )
    assert good.status_code == 204


def test_reset_with_a_lockdown_token_is_refused(env: Any) -> None:
    """Purpose binding: a token minted for one act must not redeem another."""
    token = _token_for(env, "account_lockdown")
    r = env.client().post(
        "/auth/password/reset", json={"token": token, "new_password": GOOD_PASSWORD}
    )
    assert r.status_code == 400


def test_reset_with_an_unknown_token_is_refused(env: Any) -> None:
    r = env.client().post(
        "/auth/password/reset",
        json={"token": "not-a-real-token-at-all", "new_password": GOOD_PASSWORD},
    )
    assert r.status_code == 400


# ── /change ──────────────────────────────────────────────────────────


def test_change_requires_the_current_password(env: Any) -> None:
    r = env.client(_claims()).post(
        "/auth/password/change",
        json={"current_password": "wrong", "new_password": GOOD_PASSWORD},
    )
    assert r.status_code == 401
    assert env.kc.set_password_calls == []


def test_change_succeeds_and_revokes_sessions(env: Any) -> None:
    r = env.client(_claims()).post(
        "/auth/password/change",
        json={
            "current_password": "old-password-here",
            "new_password": GOOD_PASSWORD,
        },
    )
    assert r.status_code == 204
    assert env.kc.set_password_calls == [(SUB, GOOD_PASSWORD)]
    assert SUB in env.kc.logged_out
    assert str(SUB) in env.denylist.revoked


def test_change_queues_the_security_notification(env: Any) -> None:
    env.client(_claims()).post(
        "/auth/password/change",
        json={
            "current_password": "old-password-here",
            "new_password": GOOD_PASSWORD,
        },
    )
    assert [m["kind"] for m in env.store.mail] == ["password_changed"]


def test_change_refuses_reusing_the_current_password(env: Any) -> None:
    reused = "old-password-here-that-is-long"
    env.kc.passwords[SUB] = reused
    r = env.client(_claims()).post(
        "/auth/password/change",
        json={"current_password": reused, "new_password": reused},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "password_unchanged"


def test_change_refuses_a_weak_password(env: Any) -> None:
    r = env.client(_claims()).post(
        "/auth/password/change",
        json={
            "current_password": "old-password-here",
            "new_password": "qwerty123456",
        },
    )
    assert r.status_code == 422
    assert env.kc.set_password_calls == []


def test_change_requires_authentication(env: Any) -> None:
    r = env.client().post(
        "/auth/password/change",
        json={"current_password": "x", "new_password": GOOD_PASSWORD},
    )
    assert r.status_code in (401, 403)


# ── /security/lockdown ───────────────────────────────────────────────


def test_lockdown_revokes_everything_and_returns_a_reset_token(env: Any) -> None:
    token = _token_for(env, "account_lockdown")
    r = env.client().post("/auth/security/lockdown", json={"token": token})
    assert r.status_code == 200
    body = r.json()
    assert body["reset_token"]
    assert body["sessions_revoked"] is True
    assert SUB in env.kc.logged_out
    assert str(SUB) in env.denylist.revoked


def test_lockdown_spends_outstanding_tokens(env: Any) -> None:
    """The attacker's own reset link must die with the session."""
    attacker_link = _token_for(env, "password_reset")
    token = _token_for(env, "account_lockdown")
    env.client().post("/auth/security/lockdown", json={"token": token})
    r = env.client().post(
        "/auth/password/reset",
        json={"token": attacker_link, "new_password": GOOD_PASSWORD},
    )
    assert r.status_code == 400


def test_lockdown_token_is_single_use(env: Any) -> None:
    token = _token_for(env, "account_lockdown")
    assert (
        env.client().post("/auth/security/lockdown", json={"token": token}).status_code
        == 200
    )
    assert (
        env.client().post("/auth/security/lockdown", json={"token": token}).status_code
        == 400
    )


def test_lockdown_returned_token_actually_resets(env: Any) -> None:
    """The handed-back token is the user's way straight into setting a
    new password without waiting for a second email."""
    token = _token_for(env, "account_lockdown")
    reset_token = env.client().post(
        "/auth/security/lockdown", json={"token": token}
    ).json()["reset_token"]
    r = env.client().post(
        "/auth/password/reset",
        json={"token": reset_token, "new_password": GOOD_PASSWORD},
    )
    assert r.status_code == 204


def test_lockdown_writes_a_dedicated_audit_kind(env: Any) -> None:
    from auth_service import audit_kinds

    token = _token_for(env, "account_lockdown")
    env.client().post("/auth/security/lockdown", json={"token": token})
    kinds = [c["kind"] for c in env.audit]
    assert audit_kinds.AUTH_ACCOUNT_LOCKDOWN in kinds


def test_lockdown_reports_partial_revocation_honestly(env: Any) -> None:
    """If Keycloak refuses the logout, the response must not claim the
    account was secured."""
    from auth_service.keycloak_client import KeycloakError

    async def _boom(sub: UUID) -> None:
        raise KeycloakError(status=503, body={})

    env.kc.logout_user = _boom
    token = _token_for(env, "account_lockdown")
    r = env.client().post("/auth/security/lockdown", json={"token": token})
    assert r.status_code == 200
    assert r.json()["sessions_revoked"] is False


# ── sessions ─────────────────────────────────────────────────────────


def test_list_sessions_marks_the_current_device(env: Any) -> None:
    r = env.client(_claims()).get("/auth/sessions")
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["id"] == "sess-1"
    assert rows[0]["current"] is True


def test_revoke_all_sessions(env: Any) -> None:
    r = env.client(_claims()).post("/auth/sessions/revoke-all")
    assert r.status_code == 204
    assert SUB in env.kc.logged_out
    assert str(SUB) in env.denylist.revoked


def test_policy_endpoint_reports_the_enforced_minimum(env: Any) -> None:
    r = env.client().get("/auth/password/policy")
    assert r.status_code == 200
    assert r.json()["min_length"] >= 12

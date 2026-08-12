# Runbook — password recovery

Forgot-password, reset, self-service change, and the "this wasn't me"
account-lockdown door. All of it lives in **auth-service** (`:8000`),
behind `MDX_PASSWORD_RESET_ENABLED`.

- Code: `services/auth-service/src/auth_service/routers/password.py`
- Schema: `infra/postgres/migrations/0076_password_recovery.sql`
- SPA: `~/Desktop/dictat` — `src/pages/{ForgotPassword,ResetPassword,AccountRecovery}Page.jsx`,
  `src/components/AccountSecuritySection.jsx`, `src/api/password.js`
- Config: `services/auth-service/.env.example`

---

## The flow

```
 SPA #/forgot-password ──{email}──▶ POST /auth/password/forgot ──▶ 202 (always)
                                          │
                                          ├─ mint token (sha256 stored)
                                          └─ enqueue mail ──▶ outbox worker ──▶ SMTP
                                                                   │
 inbox ◀── "Reset your Klarnote password" ◀─────────────────────────┘
   │
   └─▶ SPA #/reset-password?token=… ──▶ POST /auth/password/reset
                                              ├─ Keycloak reset-password
                                              ├─ revoke ALL sessions
                                              ├─ spend all other tokens
                                              └─ enqueue security notification
                                                        │
 inbox ◀── "Your Klarnote password was changed" ◀────────┘
   │
   └─▶ [This wasn't me] ──▶ SPA #/account-recovery?token=…
                                  └─▶ POST /auth/security/lockdown
                                         ├─ revoke ALL sessions
                                         ├─ spend all tokens
                                         └─ return a fresh reset_token
                                                → straight into #/reset-password
```

`POST /auth/password/change` (authenticated, needs the current password)
runs the same tail: Keycloak set-password → revoke all sessions → spend
tokens → queue the security notification.

## Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET`  | `/auth/password/policy`      | none | `{min_length, max_length}` for the SPA meter |
| `POST` | `/auth/password/forgot`      | none | **Always 202** `{"status":"accepted"}` |
| `POST` | `/auth/password/reset`       | none (token) | 204 · 400 `invalid_reset_token` · 422 `weak_password` |
| `POST` | `/auth/password/change`      | bearer | 204 · 401 wrong current · 422 `weak_password`/`password_unchanged` |
| `POST` | `/auth/security/lockdown`    | none (token) | 200 `{reset_token, expires_in, sessions_revoked}` |
| `GET`  | `/auth/password/events`      | bearer | Recent security activity |
| `GET`  | `/auth/sessions`             | bearer | Live Keycloak sessions |
| `POST` | `/auth/sessions/revoke-all`  | bearer | Sign out everywhere |

All endpoints return **404 when `MDX_PASSWORD_RESET_ENABLED=false`** — a
disabled feature must not be discoverable.

---

## Bring it up locally

```bash
cd medical-dictation-backend
make dev-up && make migrate-up && make seed
docker compose up -d auth-service          # or: make run-auth-service
open http://localhost:8025                 # Mailpit — every dev mail lands here
```

Then in the SPA (`~/Desktop/dictat`, `npm run dev`): `#/forgot-password`,
enter `clinician@tenant-a.example`, and read the mail in Mailpit.

Both mails render in **en / de / uk**. The reset mail takes its language
from the SPA's `lang` field; the security notification takes it from
`Accept-Language` on the request that triggered the change.

---

## Things that will bite

**Reset mail never arrives.** In order of likelihood:

1. `MDX_PASSWORD_RESET_ENABLED` is false → the endpoint 404s and the SPA
   shows its confirmation screen anyway (it cannot tell, by design).
2. The outbox worker is not running. Startup logs
   `auth.password.reset_enabled_but_no_mail_worker` as an **error** when
   the feature is on but no worker was started — tokens are minted and
   queued and nothing ever sends them. Check `MDX_BACKGROUND_JOBS`.
3. Rows are dead-lettering. `SELECT kind, status, attempt_count, last_error
   FROM auth_mail_outbox ORDER BY created_at DESC LIMIT 20;` (needs
   `app.tenant_id` set — see the query below).
4. Google Workspace refusing the credential: `535-5.7.8 Username and
   Password not accepted` means `MDX_AUTH_SMTP_PASSWORD` is the account
   password, not a **16-character App Password**. The account needs
   2-Step Verification on to generate one.

**Every send takes exactly 30 seconds.** That is `socket.getfqdn()`
blocking on reverse DNS. It is already fixed here — `SmtpProvider`
passes an explicit `local_hostname` derived from the sending domain
(`adapters/email.py:ehlo_hostname`). If you see it again, something has
dropped that argument.

**"The reset link 404s."** `MDX_APP_BASE_URL` does not match the origin
the SPA is actually served from. Every mailed link is built from it.

**Users are not signed out after a reset.** `MDX_SESSION_REVOCATION_ENABLED`
is off, so only Keycloak's half runs and access tokens already issued
stay valid for their remaining lifetime (15 min). Turn it on.

**The lockdown response says `sessions_revoked: false`.** Keycloak's
logout or the denylist push failed. The user's password reset still
went through, but an attacker's session may be alive for up to the
access-token lifetime. Investigate immediately — this is the one field
worth alerting on.

---

## Useful queries

The three tables are RLS-scoped, so set the tenant first:

```sql
-- as app_role
BEGIN;
SELECT set_config('app.tenant_id', '<tenant-uuid>', true);

-- outstanding links for one user
SELECT purpose, issued_at, expires_at, consumed_at
  FROM auth_password_reset_tokens
 WHERE subject_sub = '<sub>' ORDER BY issued_at DESC;

-- mail that is stuck or dead
SELECT kind, lang, status, attempt_count, next_attempt_at, last_error
  FROM auth_mail_outbox
 WHERE status <> 'sent' ORDER BY created_at DESC LIMIT 20;

-- what happened to this account
SELECT kind, via, client_label, created_at
  FROM auth_password_events
 WHERE subject_sub = '<sub>' ORDER BY created_at DESC LIMIT 20;
COMMIT;
```

Token redemption is deliberately **tenant-blind** — a browser following
a mailed link has no session and no tenant — so it goes through two
`SECURITY DEFINER` functions rather than a widened grant:
`public.consume_password_reset_token(bytea, text)` and
`public.resolve_account_for_password_reset(text)`.

---

## Security properties worth not breaking

1. **No account enumeration.** `/forgot` returns an identical 202 for a
   real address, an unknown one, a deactivated one, and a rate-limited
   one; no audit row is written for an address with no account. The SPA
   shows one confirmation screen for all of them. An e2e test asserts
   the two screens are byte-identical
   (`e2e/password-recovery.spec.js`). Residual, accepted: response
   *timing* still differs slightly between a real and unknown address.

2. **Tokens are never stored in plaintext.** Only `sha256` reaches
   `auth_password_reset_tokens`. The mailed URL sits in
   `auth_mail_outbox.secret_fields` between enqueue and send, and a
   `CHECK` constraint makes a row that is not `pending` with a non-null
   `secret_fields` **impossible** — clearing is a database invariant,
   not a habit the worker has to remember.

3. **Single use, purpose-bound.** Redemption is an atomic
   compare-and-swap; a reset token cannot redeem a lockdown and vice
   versa. Verified live, and in `tests/unit/test_password_routes.py`.

4. **A password change ends every session** — Keycloak logout *and* the
   sprint-16 denylist. Neither alone closes the window: Keycloak cannot
   reach already-issued access tokens, and the denylist is fail-open.

5. **The lockdown link does not fire on page load.** Corporate mail
   gateways, link previewers and antivirus crawlers follow links in
   email. Auto-firing would sign the real user out of everything from a
   routine notification. It is a POST behind a deliberate click.

6. **The security notification cannot be unsubscribed from.** No
   `List-Unsubscribe` header — an attacker holding the mailbox must not
   be able to silence the one warning that exposes them.

---

## Rollback

```bash
uv run python scripts/db/migrate.py down     # drops 0076
```

Or, without touching the schema, set `MDX_PASSWORD_RESET_ENABLED=false`
and restart: every endpoint returns 404 and the SPA's
`#/forgot-password` still renders but its submit fails. Prefer the flag.

# ADR-0039: MFA/TOTP — enforced in the auth-service proxy, secret stored envelope-encrypted in Keycloak attributes

Date: 2026-08-08
Status: Accepted
Sprint: 16

## Context

The sprint-02 threat model deferred MFA with "Re-enable: set
`MDX_REQUIRE_MFA=true`, build TOTP enrolment endpoints (sprint 16)".
The `requires_mfa()` dependency, the `mfa` claim slot, the
`user.reset_mfa` permission and the `mfa_enrolled_at` user attribute
have been waiting since then.

EXPLORE findings that shaped the design:

1. **Keycloak's admin REST API cannot register an OTP credential for an
   existing user.** The credentials surface is GET/DELETE plus
   `reset-password`; OTP credentials are only importable at user
   creation / realm import. The supported enrolment path is Keycloak's
   own browser flow (`CONFIGURE_TOTP`) —
2. — but **this architecture has no Keycloak browser flow.** Since
   sprint A3 the SPA posts credentials to auth-service, which proxies
   the direct grant; Keycloak is not publicly exposed in the production
   topology. There is nowhere to render Keycloak's enrolment UI.
3. Every token-issuing path already runs through auth-service.

## Decision

- **auth-service owns the TOTP lifecycle.** `POST /auth/mfa/enrol`
  generates the RFC-6238 secret and returns the `otpauth://` URI;
  `POST /auth/mfa/verify` completes enrolment; `DELETE /auth/mfa/{sub}`
  is the admin reset (`user.reset_mfa`, audited `sec`, revokes live
  sessions).
- **The secret is stored in Keycloak user attributes,
  envelope-encrypted** by libs/crypto (AAD = tenant ‖ sub ‖ purpose
  label). Keycloak remains the identity/credential store — the spec's
  intent — while no plaintext secret exists at rest anywhere. The spec
  line "via Keycloak's own TOTP credential API" is unimplementable per
  finding 1; this is the recorded deviation.
- **Enforcement lives in the login proxy.** An enrolled user's password
  grant is not released without a valid TOTP code (`otp` field; machine
  codes `otp_required` / `otp_invalid`; fail-closed when the secret
  store is unreachable). Because auth-service is the only token path,
  proxy-side enforcement is enforcement.
- **Claims**: protocol mappers project the `mfa_enrolled` attribute into
  `mfa` and `mfa_enrolled` claims. On tokens this platform issues,
  `mfa ⇔ mfa_enrolled` (the proxy guarantees it); two claims exist so a
  future flow-based step-up (acr/amr) can decouple "has MFA" from "used
  MFA this session" without a schema change.
- **Grace flow** (`MDX_REQUIRE_MFA=true`): gated routes return
  **403 `mfa_enrolment_required`** for unenrolled users (FE routes to
  enrolment) and **401 + `WWW-Authenticate: MFA`** for enrolled users
  holding a pre-enrolment token. Gated set: role management,
  user (de/re)activation, MFA reset, tenant CRUD (auth-service);
  erasure approve/reject + DSAR trigger (core-service). Trust-store
  admin has no HTTP surface (PR-gated script) — nothing to gate.
- **Switches** (dev unchanged, prod flips): `MDX_MFA_ENROLMENT_ENABLED`
  (surface), `MDX_REQUIRE_MFA` (gating; same env name in auth-service
  and core-service).

## Consequences

- The `mdx-dev-cli` client could reach Keycloak's token endpoint
  directly inside the compose network, bypassing the proxy check. It is
  a dev-only confidential client; the realm export is the dev seed. The
  production realm must not ship it (threat-model note added).
- TOTP codes are not single-use-tracked (a code could be replayed
  within its 30 s step against a second login). Accepted for the pilot;
  the fix (last-used-counter in Redis) rides the existing denylist
  wiring if SOC asks for it.
- Audit: `auth.mfa.enrolled` (info), `user.reset_mfa` (sec — the
  sprint-02-ledgered kind wins over the spec's `auth.mfa.reset` name).

## Revisit when

- Keycloak grows an admin API for OTP credential registration → move
  storage + validation into Keycloak and delete `totp.py`.
- A browser-flow deployment appears → adopt `CONFIGURE_TOTP` + acr.

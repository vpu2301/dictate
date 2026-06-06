# ADR-0006 — Keycloak as Identity Provider

**Date:** 2026-05-11
**Status:** Accepted
**Deciders:** Backend tech lead, Security lead, SRE/DevOps

---

## Context

Sprint 02 introduces identity. Users log in; a JWT proves who they are; that
JWT carries the tenant id (`tid`) that downstream RLS policies enforce. We
need an OIDC provider that:

1. Issues RS256-signed JWTs with a stable `iss` and a JWKS endpoint.
2. Supports refresh-token rotation with replay detection.
3. Lets us inject custom claims (we need `tid` from a user attribute).
4. Hosts a UI/API for user lifecycle (invite, deactivate, reset MFA).
5. Can run on-prem in a Ukrainian datacentre (privacy / data-residency).
6. Is FOSS so legal/procurement is a non-issue.
7. Lets us scope service-account permissions on the Admin API.

The pilot deployment is in a single region; multi-region HA is a sprint-16
concern.

## Decision

Use **Keycloak 24** as the IdP. One realm (`medical-dictation`) with five
realm roles (`tenant_admin`, `clinician`, `nurse`, `auditor`, `service`) and
five clients:

- `mdx-api` (bearer-only) — the audience marker every access token carries.
- `mdx-frontend` (public, PKCE-only) — the SPA's standard-flow client.
- `mdx-backend` (confidential, password-grant enabled) — server-side login
  proxy + S2S. The secret gates abuse of the password grant.
- `mdx-admin` (confidential, client-credentials) — auth-service's
  Admin-API identity, with scoped `realm-management` roles.
- `mdx-dev-cli` (public, password grant) — dev-only smoke client. Disabled
  in non-dev realms.

Realm settings:

- Access tokens: 900 s; refresh tokens 43 200 s (12 h); rotation on.
- Brute-force detection: 5 failures / 60 s lockout window.
- Custom protocol mappers per client: `tid` (from user attribute), flat
  realm-`roles` array, custom audience `mdx-api`.

The realm export at `infra/keycloak/realm-export.json` is the source of
truth; `make keycloak-export` re-extracts after manual changes.

## Consequences

**Positive**

- We don't write our own OIDC server. Token validation, signing-key
  rotation, brute-force lockout, refresh rotation, password-policy
  enforcement, MFA infrastructure — all standard.
- The realm export is reviewable diff-by-diff. Misconfigured changes show
  up in PRs rather than being applied by hand.
- `mdx-admin`'s service account has only the realm-management roles we
  scoped — no global `realm-admin`. Reduces blast radius of a leaked
  secret.
- Keycloak's JWKS endpoint + standard kid rotation lets `libs/auth`'s
  JwksCache handle key rotation without a service restart.

**Negative**

- A JVM dependency in the dev stack (~600 MB image, ~30 s start).
- Keycloak's realm-import is fussy: column-length limits, key ordering of
  `protocolMappers`, the fact that `directAccessGrants` is a per-client
  flag and not realm-level. We hit two of these the first day.
- Realm-import only runs on the first start of a fresh `keycloak` DB —
  changing the export requires `docker compose stop keycloak` + drop the
  DB. Acceptable for dev; production reapplies via Keycloak admin client
  or kc.sh import-realm.

## Alternatives considered

- **Auth0 / Okta** — managed; same OIDC surface. Rejected: data-residency,
  per-MAU pricing, vendor lock-in on the user store.
- **Ory Kratos + Hydra + Keto** — clean separation of identity / OIDC /
  authz. Rejected: three components instead of one; smaller community;
  the admin UI maturity isn't there for the pilot.
- **Custom JWT issuer** — we'd reinvent JWKS rotation, brute-force
  protection, refresh handling. Rejected on cost and risk.
- **AWS Cognito / GCP Identity Platform** — cloud lock-in, conflicts with
  Ukrainian data-residency requirements.

## Trigger conditions for revisiting

- Pricing/license changes that affect FOSS use of Keycloak.
- A clear multi-region active-active requirement (Keycloak's clustering
  is fine for HA inside one region; cross-region active-active is the
  rough edge).
- Customer demand for a hosted IdP (Azure AD / Google Workspace
  federation is doable via Keycloak's identity-provider federation but
  was not configured in sprint 02).

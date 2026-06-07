# Batch A — Exit Review (Sprint A3 Integration Checkpoint) — Backend

**Status:** Backend integration COMPLETE and verified; awaiting the live SPA walkthrough
(frontend repo `~/Desktop/dictat`) + sign-offs to formally open Batch B.
**Scope of this note:** the **backend** side of A3 — making the real backend ready for and
verifiable against the real SPA, and locking the SPA↔backend contract. The browser-side
acceptance items (route guard, silent-refresh UX, the witnessed human walkthrough) are the
frontend's to demonstrate; this note proves the backend half each depends on.
**Date:** 2026-06-07
**Verifier:** Sprint A3 (backend)

The SPA was **not yet runnable against the backend** when A3 started: there was no auth-service
listening, no CORS, and the login + replay contracts the SPA was coded against did not match
what the backend actually accepted/returned. All fixed below; verified by emulating the SPA's
cross-origin browser calls with `curl` (Origin header, preflight, `credentials`-style cookie).

---

## The SPA ↔ backend contract (now locked)

| Aspect | Contract |
|--------|----------|
| SPA origin (dev) | `http://localhost:5173` (Vite); preview `:4173` (both 127.0.0.1 + localhost allow-listed) |
| auth-service URL | `http://localhost:8000` (`VITE_AUTH_SERVICE_URL`); run via `make run-auth-service` |
| Credentials | every call uses `credentials:"include"`; refresh cookie `mdx_rt` is HttpOnly, `Path=/auth`, `SameSite=lax` (dev), `Secure` off in dev |
| `POST /auth/login` | accepts **either** `application/x-www-form-urlencoded` **or** `application/json`; identifier under **`email` or `username`**; returns `{access_token, expires_in, token_type}` + sets `mdx_rt` |
| Access token | in-memory only (never localStorage); sent as `Authorization: Bearer …`; audience `mdx-api`, RS256 |
| `POST /auth/refresh` | cookie-driven; rotates cookie; **replay → 401 `application/problem+json` with `code:"auth_refresh_replay"`** |
| `GET /auth/me` | verified claims + `db_user` (role, display_name, tenant_id) |
| Admin/audit | `POST /admin/users/invite`, `POST /admin/users/{sub}/deactivate`, `GET /audit/events`, `GET /audit/verify` — all tenant-derived-from-token; **client never sends a tenant id** |
| Errors | RFC 9457 `application/problem+json` with `instance: urn:uuid:…` (+ optional `code`) |

**Run the backend for the SPA (from clean):**
```
make dev-up && make migrate-up && make seed && make run-auth-service
```
Then start the SPA (`~/Desktop/dictat`): `npm run dev` → http://localhost:5173.

---

## Backend defects discovered & fixed (A3)

| ID | Sev | Summary | Fix |
|----|-----|---------|-----|
| DEF-A3-01 | **blocker** | No CORS on auth-service → every SPA cross-origin call browser-blocked. | Added `CORSMiddleware` (config `CORS_ALLOWED_ORIGINS`, `allow_credentials=true`, explicit origins — no wildcard). Preflight + credentialed responses verified. |
| DEF-A3-02 | **blocker** | auth-service ran **nowhere** — root compose is infra-only and the `infra/compose/dev.yml` overlay builds asr/dictation/nlp but **not** auth-service. The SPA's `:8000` had nothing to talk to. | Added `make run-auth-service` (uvicorn on `:8000` with OTEL). Documented in the run recipe. |
| DEF-A3-03 | **major** | Login contract mismatch: SPA sends `x-www-form-urlencoded` with `email`; backend required JSON `username` → every SPA login was a 422. | `login` now parses JSON **or** form and accepts `email`\|`username`. JSON+`username` (A1 tests) still works. |
| DEF-A3-04 | **major** | Replay 401 carried no machine-readable code, so the SPA could not distinguish a replay (force clean re-login, no retry) from an ordinary expired-token 401 — AC-A3-5 unmet. | Replay now returns problem+json `code:"auth_refresh_replay"` via a new generic `problem_extras` hook in `libs/observability`. |
| DEF-A3-05 | minor | Refresh cookie was `SameSite=Strict` (works same-site localhost↔localhost but fragile for SPA flows / future cross-site). | Made `AUTH_COOKIE_SAMESITE` configurable; default `lax` for the first-party SPA. |
| DEF-A3-07 | major | Day-4 needs two `tenant_admin`s, but tenant B had only a clinician — cross-tenant-admin isolation was untestable. | Added `dev-admin-b` (`tenant_admin`, tenant B) to the realm-export (pinned id) and `seed.sql`. |
| OBS-A3-06 | note | There is **no `GET /admin/users` list endpoint**; the SPA admin screen is invite/deactivate only. Cross-tenant "cannot see the other tenant's users" is therefore observed through the tenant-scoped **`/audit/events`** screen, not a user list. | Documented for the frontend/Batch B — confirm the admin screen sources its list from audit events (or add a list endpoint in Batch B). |

---

## Acceptance criteria — backend evidence

All verified by emulating the SPA's cross-origin browser calls against `http://localhost:8000`
with `Origin: http://localhost:5173`.

| AC | Backend evidence | Result |
|----|------------------|--------|
| AC-A3-1 | Login (form + `email`) → 200, `access_token` + `mdx_rt` (HttpOnly, SameSite=lax, Path=/auth); `GET /auth/me` → `roles=['clinician']`, `tid`, `db_user.display_name="Dev Clinician A"` | ✅ (backend half; SPA renders it) |
| AC-A3-2 | tenant_admin invite → 201; DB `users` row under tenant A; exactly one `user.invited` audit row | ✅ |
| AC-A3-3 | One invite call → exactly one `user.invited` row (distinct_targets == rows); user created once | ✅ |
| AC-A3-4 | clinician → `POST /admin/users/invite` **403**, `GET /audit/events` **403**; 2 `authz.denied` audit rows written | ✅ |
| AC-A3-5 | refresh → 200 rotated; replay old cookie → 401 with `code:"auth_refresh_replay"` (SPA force-relogin trigger) + `auth.refresh_replay_detected` (sec) audit | ✅ |
| AC-A3-6 | admin-A `/audit/events` = 15 (tenant A only); admin-B = 2 (tenant B only); matches DB per-tenant counts exactly — zero cross-tenant rows | ✅ |
| AC-A3-7 | Admin/audit endpoints derive tenant from the **verified token** (`claims.tid`); SPA payloads carry no tenant id (inspected `endpoints.js`) | ✅ (server-derived by construction) |
| AC-A3-8 | This note + the contract above; CORS preflight echoes `Access-Control-Allow-Origin: http://localhost:5173`, `Allow-Credentials: true` | ⚠️ pending sign-offs + live SPA demo |

CORS preflight evidence:
```
OPTIONS /auth/login  →  200
  access-control-allow-origin: http://localhost:5173
  access-control-allow-credentials: true
  access-control-allow-methods: GET, POST, OPTIONS
  access-control-allow-headers: …, Authorization, Content-Type
```

---

## Carry-over tickets

- **OBS-A3-06** — confirm/define how the SPA admin screen lists users (no backend list endpoint today).
- **DEF-A1-23** (from A1) — logs still not shipped to Loki (stdout only); unchanged here.
- **Productionisation** — `make run-auth-service` is a dev convenience; Batch F should containerise auth-service (add a Dockerfile + compose service on `:8000`) and set `AUTH_COOKIE_SECURE=true`, `AUTH_COOKIE_SAMESITE=none`, and prod `CORS_ALLOWED_ORIGINS`.
- **OpenAPI snapshot** — `docs/api/auth-service-openapi.json` was regenerated (login body schema changed); commit it so `openapi-check` stays green in CI.

## Sign-off (opens Batch B)

| Role | Name | Decision | Date | Signature |
|------|------|----------|------|-----------|
| Tech lead | | | | |
| Frontend lead | | | | |
| Security lead | | | | |
| DPO | | | | |
| SRE / DevOps | | | | |

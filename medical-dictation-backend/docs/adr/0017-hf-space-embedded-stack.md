# ADR-0017 — HF Space embeds Postgres, Redis, and Keycloak in a single container

- Status: accepted
- Date: 2026-05-10
- Sprint: 07
- Deciders: SRE lead, security lead, DPO

## Context

Sprint-07 publishes a public-facing demo on Hugging Face Spaces. HF
Spaces run a single OCI container, no sidecars, no managed Postgres,
no managed Redis. Our backend has hard dependencies on each:

- Postgres 16 with `pgcrypto` and RLS (sprint-02).
- Redis 7 for the WS resume-token cache (sprint-04) and the demo rate
  limiter (sprint-07).
- Keycloak 24 for OIDC (sprint-02). The user override on day-1 was
  explicit: no anonymous demo auth — use the real Keycloak flow with a
  seeded clinician account, gated by the HF API key on the Space
  itself.

## Decision

Bake Postgres + Redis + Keycloak into the HF Space image and supervise
them with `supervisord`. Postgres and the audio scratch directory live
on **tmpfs** (`/run/pgdata`, `/run/dictation`); Keycloak's H2-style
state is held in the same tmpfs Postgres. The container restarts daily
(06:00 UTC cron-driven `kill 1`) and rotates all transient state.

Realm definition is staged at boot time from `realm-export.json`
(seeded `clinician@demo.test` / `demo-please-change`, tenant_id
`00000000-0000-0000-0000-0000000000d0`). The HF Space is configured
**private** and gated by the team's HF API key — users who can reach
the Space are already trusted enough to receive a clinician seat.

## Consequences

Positive:
- Reuses every existing sprint-02..06 module unmodified — no
  demo-specific auth or storage code paths to maintain.
- Demo and production share the same WS protocol bytes, the same NLP
  pipeline version, the same audit kinds (plus the small demo extension
  set in `libs/demo`).
- Hard guarantee of no persistence: tmpfs + daily restart + privacy
  test means every release is provably non-retentive.

Negative / accepted:
- Single-container failure domain — if Postgres OOMs, the whole demo
  dies until next restart. Mitigation: tmpfs sizes capped; supervisord
  brings processes back; SRE dashboard alerts on Space health.
- Bigger image (~6 GB with Java + Keycloak + model weights). HF Spaces
  free tier accepts this.
- Keycloak boot adds ~25s to cold-start. Acceptable for a demo;
  documented in the runbook.

## Alternatives considered

- **Build a separate `services/auth-service/src/auth_service/demo/`
  module that mints anonymous JWTs.** Rejected by user directive on
  day-1 of sprint-07: "but not demo auth, we csan provide real HF
  key" — they prefer to keep one auth surface in the codebase.
- **Externalise Postgres + Redis to a sidecar HF Space.** HF doesn't
  support inter-Space networking, so this would have meant exposing
  the DB on the public internet. Rejected on security grounds.

## Links

- Sprint-07 spec §2 (HF Space architecture).
- ADR-0001 (RLS-first multi-tenancy) — preserved verbatim in demo.
- ADR-0011 (envelope encryption) — DEKs still derived, even though
  audio never reaches durable storage.
- Runbook: `docs/runbooks/hf-space.md`.

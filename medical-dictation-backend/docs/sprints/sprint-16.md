# Sprint 16 — Production Hardening (KMS, MFA, Revocation, Schedulers)

Date: 2026-08-08. Every prior sprint's deliberate "sprint-16" IOU,
reconciled against the repo ledger (`grep -rn "sprint.16"`) and either
**paid** here or **re-ledgered** with a reason. Everything ships
settings-gated with dev-identical defaults — no deployment work was in
scope; production enables features by flipping the flags listed below.

## Paid

| IOU (origin) | Delivered | Switch (dev default) |
|---|---|---|
| KMS master key — ADR-0011 "1-line swap" | `KmsMasterKeyProvider` (Vault Transit) + `CompositeMasterKeyProvider` + `build_master_key_provider` at all 7 sites; `scripts/kms/rewrap-tenant-keks.py` (transactional, resumable, dry-run, verify-before-commit, audited); fail-closed startup; ADR-0011 amendment; `docs/runbooks/kms.md` | `MDX_MASTER_KEY_PROVIDER` (`file`) |
| Signing HMAC placeholders → KMS (signing runbook) | `crypto.fetch_kv_secrets` + signing-service lifespan sourcing, fail-closed | `MDX_HMAC_KEYS_FROM_VAULT` (off) |
| MFA/TOTP — threat model | Enrol/verify/reset endpoints, proxy-side OTP enforcement at login, `mfa`/`mfa_enrolled` claims + realm mappers, grace flow, gates on role mgmt / user lifecycle / tenant CRUD / erasure approval / DSAR; ADR-0039 | `MDX_MFA_ENROLMENT_ENABLED`, `MDX_REQUIRE_MFA` (both off) |
| Session-revocation listener — threat model | `libs/auth.revocation` Redis denylist (sid+sub, TTL-bounded, fail-open) checked by `current_user` in all 10 services; pushes on logout / replay / deactivate; clear on reactivate; ADR-0040 | `MDX_SESSION_REVOCATION_ENABLED` (off) |
| Idle-draft cleanup scheduler — sprint 08 | `run_for_all_tenants` + in-process loop + CLI; migration 0071 `active_tenant_ids()`; ADR-0041 | `MDX_BACKGROUND_JOBS` (off), `MDX_IDLE_DRAFT_DAYS` (30) |
| Telemetry cold archive — sprint 10 | Archive-before-drop (gzip JSONL via `EncryptedObjectStore`, global-tenant envelope, `mdx-telemetry-archive`); archive failure blocks the drop; per-run audit | `MDX_TELEMETRY_COLD_ARCHIVE_ENABLED` (off) |
| Erasure backup-completion notices — sprint 11 | `core_service.jobs.backup_horizon` stamps `backups_purged_confirmed_at` + note, audits `erasure.backup_horizon_reached` (sec); in-process + CLI | `MDX_BACKGROUND_JOBS` (off) |
| Whisper/diarizer pre-warm — sprint-03 retro | Dictation: background thread load, `/healthz` immediate, `/readyz` 503-until-loaded; generation: 1-token warm probe gating readiness. (S14 had already warmed both models eagerly; the gap was liveness during >60 s loads.) | `MDX_WARM_IN_BACKGROUND`, `MDX_PREWARM_ENABLED` (both off) |
| Demo-envvars CI gate — sprint 07 | `check-no-demo-envvars-in-prod` (MD_OBJECT_STORE_DISABLED / MDX_DEMO_MODE / DEMO_* / AUTH_BYPASS_DEV) in `make ci` + workflow; sprint-09 dev-signing gate added to the workflow sweep (it was in `make ci` only) | — (always on) |

New audit kinds: `auth.mfa.enrolled`, `user.reset_mfa` (activated),
`auth.session.revoked`, `scheduler.job.completed/failed`,
`kms.rewrap.completed`, `erasure.backup_horizon_reached`.
Migration: `0071_active_tenant_ids_fn.sql`. New bucket:
`mdx-telemetry-archive`.

Deviations (argued in the ADRs): TOTP secret is envelope-encrypted in
Keycloak *attributes* (KC admin API cannot register OTP credentials;
no browser flow exists in this architecture) — ADR-0039. Revocation
feed is auth-service-push, not a Keycloak webhook/SPI (deployment
work out of scope; all session ends already flow through auth-service)
— ADR-0040. `auth.mfa.reset` from the spec kept its sprint-02-ledgered
name `user.reset_mfa`. One scheduler *process* is impossible under the
import contracts; the "one pattern" is the shared runner — ADR-0041.

## Re-ledgered (found by the grep, deliberately NOT built — sprint 17+)

Infra/deployment scope (excluded from this sprint by decision):
Tempo trace backend (ADR-0005), production HPA wired to the dictation
weight gauge, Wolfi/base-image re-evaluation (ADR-0002), pgbouncer
transaction pooling (ADR-0004 — libs/db is already compatible),
audit-events streaming to immutable S3 (threat model), restore tooling
drills beyond `restore.sh`, multi-region Keycloak HA (ADR-0006),
Cloudflare in front of `/verify` (signing runbook), Terraform bucket
retention. Frontend scope: CSP. Protocol scope: WS-over-HTTP/2
fallback (ADR-0012). Process scope: uv quarterly re-eval (ADR-0001),
sprint-16 capacity-ADR revision hooks (asr-worker runbook), Argon2id
recovery codes (glossary) — recovery codes were not in this sprint's
IOU table. nlp-service unix-socket co-location note
(dictation `nlp_client.py`) — an optimisation idea, not a commitment.
Autocomplete admin surface for `suggest.disabled` tenants
(`main_deps.py` note) — needs a product decision.

## Deployment half (same sprint, k3d fallback posture)

The infra IOUs, paid against a local k3d staging cluster per the spec's
"hosting decision pending" clause — gaps named in
`docs/deploy/hosting-gap.md`:

| IOU | Outcome |
|---|---|
| HPA on active sessions (dictation runbook) + mode-weighted units (S14) | KEDA ScaledObject on `capacity_weight_used/max` (threshold 0.75) — **proven live**: 4 sessions → utilisation 1.0 → 1→2 replicas |
| Drain-safe scale-in | `/internal/drain` (loopback-only) + preStop hold, grace 1830 s — **proven live**: pod deleted mid-session, session finalized `normal`, new sessions refused during drain |
| Per-pod 2 GiB tmpfs (§tmpfs-pressure) | `emptyDir medium: Memory sizeLimit: 2Gi` per pod (kubelet-enforced), verified mounted |
| WAF in front of /verify | edge nginx allowlist + per-IP zones in-cluster — **proven live**: 429 at the edge before the app limiter; non-public paths 404; HSTS end-to-end; provider WAF = hosting hand-off |
| Regenerate dev client secrets | `gen-prod-secrets.py` (→Vault) + `gen-prod-realm.py` (0 dev-secrets, 0 dev users, no mdx-dev-cli — grep-verified) + `check-k8s-rendered` CI gate on the rendered prod manifests |
| KMS deployment counterpart | ExternalSecrets→Vault templates + `MDX_MASTER_KEY_PROVIDER=vault` in values-prod |
| HTTP/2 POST fallback decision | **closed with data** — zero `ws_upgrade_rejections` across the pilot with the pipeline proven live; ADR-0042 |

Chart: `infra/k8s/mdx` (53 staging / 47 prod objects — every compose
service mapped, `docs/deploy/inventory.md`; kafka dropped as unused).
Full staging E2E smoke green: login → patient → WS dictate (Opus,
finalize, encrypted audio in MinIO) → report draft → sign(mock,
callback) → public `/verify` through the TLS edge. Host crons became
CronJobs; migrate/seed became Helm hooks with a self-healing init.sql
initContainer.

**Latent defects found by the staging bring-up** (each fixed): signing
callback RLS hole — 0020's promised definer fn never built, callback
role saw zero rows, PLUS the `signing_sessions_tenant_update` policy
existed only as a hand-applied dev-DB fix (both → migration 0073);
mock-provider header lookup case-sensitive vs Starlette's lowercasing
(libs/kep fix); k3d/k3s netpol allow-rules not enforced (named gap);
pgvector image requirement; Sprig `default`-swallows-false trap;
fsGroup-vs-0400-master-key catch-22 (root initContainer copy).

## Verification

See the sprint sign-off transcript: unit suites green
(`crypto 70, auth 60, auth-service 55, dictation, generation, report,
autocomplete, core`), live Vault round-trip + re-wrap acid test, MFA
flow, revocation flow, scheduler idempotence, prewarm probe, CI gate
red/green, `make ci` / `ci-with-db`.

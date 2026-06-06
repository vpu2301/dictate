# ADR-0018 — Demo privacy contract: tmpfs-only, object-store-disabled, daily release gate

- Status: accepted
- Date: 2026-05-11
- Sprint: 07
- Deciders: DPO, security lead, SRE lead

## Context

The HF Space demo accepts real (test-account) dictations from
clinicians evaluating the product. We need a contractual,
auditable guarantee that no audio or transcripts survive past a
session — both because we promise it in the marketing copy and
because the Space sits in a different sovereignty boundary than the
production deployment.

## Decision

Demo containers run with the following envelope:

1. **Object-store disabled.** `MD_OBJECT_STORE_DISABLED=true` at the
   image-build level. `libs/storage/object_store.put()` raises
   `ObjectStoreDisabledError`; sprint-04 finalize gracefully skips the
   `audio_files` row insertion. CI gate
   `scripts/ci/check-no-direct-object-storage.py` ensures no service
   bypasses the wrapper.
2. **Tmpfs-only state.** Postgres, Redis, Keycloak, and the audio
   scratch buffer live in `/run/...`. Container restart wipes all of
   them.
3. **Purge on finalize.** `DEMO_AUDIO_PURGE_ON_FINALIZE=true` causes
   sprint-04 finalize to delete the in-flight tmpfs buffer immediately
   after the WS `finalize` server message is sent.
4. **Daily restart.** `kill 1` cron at 06:00 UTC triggers an HF Space
   container restart; the cron is part of supervisord so the moment
   the cron dies for any reason, the next runbook check catches it.
5. **Daily release gate.** GitHub Actions
   `daily-privacy-test.yml` (02:00 UTC) logs in via real Keycloak,
   dictates synthetic audio, ends the session, and verifies tmpfs is
   clean at 5s + 60s checkpoints. On failure: page DPO + security via
   `#privacy-alerts`, mark the Prom gauge red, and gate the next
   release.

## Consequences

Positive:
- Verifiable promise: every release is gated on an end-to-end test
  that runs against the actual deployed Space.
- Single point of code change to flip the demo back to "regular" mode
  (un-set the two envvars + the daily test stops being meaningful).

Negative:
- Demo users cannot re-load a transcript across container restarts —
  acknowledged in the demo intro screen.
- The daily test consumes a small amount of WER eval rig budget — not
  significant.

## Out of scope

Production deployments still write to durable storage via the same
codepath, with the envvars set false. The demo envelope is opt-in via
envvar; it doesn't change the default product shape.

## Links

- Sprint-07 spec §3 (Privacy contract).
- `scripts/eval/run_daily_privacy_test.py`.
- `.github/workflows/daily-privacy-test.yml`.
- ADR-0017 (HF Space embedded stack — privacy depends on the tmpfs
  decision made there).

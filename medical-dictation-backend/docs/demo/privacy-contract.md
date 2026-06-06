# Demo Privacy Contract

## The promise (user-facing)

> Audio you record and transcripts you create in the demo are kept in
> memory only and are deleted the moment your session ends or the
> container restarts (at least once per day). Nothing is written to
> durable storage. We verify this with a daily automated test.

## How it's enforced (engineering-facing)

ADR-0018 + ADR-0017 are the binding decisions. Operationally:

| guarantee                                       | mechanism                              |
| ----------------------------------------------- | -------------------------------------- |
| No durable disk in the container                | tmpfs-only mounts                      |
| No object-store writes possible                 | `MD_OBJECT_STORE_DISABLED=true` raises `ObjectStoreDisabledError` |
| Audio buffer purged at finalize                 | sprint-04 finalize when `DEMO_AUDIO_PURGE_ON_FINALIZE=true` |
| All state rotates daily                         | supervisord `daily-restart-cron` 06:00 UTC |
| Promise is tested                                | `.github/workflows/daily-privacy-test.yml` runs at 02:00 UTC |

## What "session" means here

A session = one WebSocket connection from open to `finalize`. The
audio scratch buffer is keyed to the session id and deleted when
finalize is acknowledged. If the WS dies before finalize, the
sprint-04 reconnect logic preserves the buffer for up to 60s; after
that the supervisor task cleans it up.

## What can still leak in principle (and how it's mitigated)

- **Keycloak audit log entries.** Keycloak emits per-login events. We
  redact the email locally before persistence to Postgres; rotated by
  daily restart.
- **Postgres WAL.** Tmpfs WAL is wiped with the rest of the data on
  container restart. We do **not** ship WAL anywhere.
- **HF infra logs.** stdout/stderr from supervisord captures error
  text. Service code at logging level INFO redacts `[redacted-pii]`
  on any field that traces back to a tenant id (sprint-02
  privacy_logger). DPO has read access to verify samples.

## DPO verification routine

Quarterly, the DPO runs:

1. Trigger an HF Space restart.
2. Log in as `clinician@demo.test`, dictate "пацієнт Іванов Іван
   1980 року народження інфаркт міокарда".
3. End the session.
4. Open the Grafana `sprint-07-demo-health` dashboard. Confirm
   `mdx_demo_audio_residual_after_finalize` is 0.
5. Run `scripts/eval/run_daily_privacy_test.py --self-test` against
   the live Space. Expect PASS.
6. Sign off in `docs/demo/dpo-verification-log.md`.

## When this contract changes

Any change to the envelope (e.g. enabling persistence for an enterprise
trial) requires a new ADR, an updated user-facing promise, and a
re-run of the verification routine before the demo is re-opened.

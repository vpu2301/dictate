# Demo Architecture

Public demo at HF Space. Single OCI container; all dependencies
embedded. See ADR-0017 for the decision context.

## Process tree (supervisord priorities)

| pri | process              | port  | purpose                              |
| --- | -------------------- | ----- | ------------------------------------ |
| 10  | postgres             | 5432  | data + audit (tmpfs)                 |
| 10  | redis-server         | 6379  | rate-limit + resume cache (tmpfs)    |
| 15  | keycloak             | 8088  | OIDC realm `medical-dictation`       |
| 20  | auth-service         | 8000  | sprint-02                            |
| 21  | report-service       | 8004  | sprint-06                            |
| 22  | nlp-service          | 8005  | sprint-05                            |
| 23  | asr-service          | 8001  | sprint-03 batch + WS broker          |
| 24  | asr-worker           | —     | sprint-03 inference                  |
| 25  | dictation-service    | 8002  | sprint-04 WS terminator              |
| 50  | nginx                | 7860  | reverse proxy                        |
| 99  | daily-restart-cron   | —     | `kill 1` at 06:00 UTC                |

## State

All state is **tmpfs**. There is no durable filesystem mount.
Container restart (cron-driven or HF infra) wipes everything.

The dictation buffer that sprint-04 created
(`MDX_TMPFS_ROOT=/run/dictation/<session_id>.audio`) is deleted at WS
`finalize`. The seeded Keycloak realm is staged from
`realm-export.json` at every boot.

## Authentication

Two layers of gating:

1. **Space-level (HF API key).** The Space is private; only people
   with an HF API key for the team account can reach the URL at all.
   This is the broad gate.
2. **App-level (real Keycloak).** Inside the Space, the standard
   sprint-02 Keycloak OIDC flow runs. Seeded user:

   - email: `clinician@demo.test`
   - password: `demo-please-change` (rotate before public flip)
   - tenant_id: `00000000-0000-0000-0000-0000000000d0`
   - roles: `clinician`

   No anonymous auth path exists — this was an explicit user decision
   on sprint-07 day-1.

## Privacy envelope

- `MD_OBJECT_STORE_DISABLED=true` — sprint-04 finalize skips
  `audio_files` insert.
- `DEMO_AUDIO_PURGE_ON_FINALIZE=true` — tmpfs buffer deleted on
  finalize.
- Daily release gate (`scripts/eval/run_daily_privacy_test.py`)
  asserts both at 5s and 60s after finalize.

See ADR-0018 for the contract.

## Rate limits (sprint-07 day-7)

Three-axis limiter from `libs/demo/src/demo/rate_limit.py`:

| axis              | default | enforced where                      |
| ----------------- | ------- | ----------------------------------- |
| ip_concurrent     | 3       | auth-service middleware             |
| ip_minutes_per_h  | 30      | dictation-service per-30s tick      |
| user_minutes_per_d| 60      | dictation-service per-30s tick      |
| session_max_min   | 15      | dictation-service supervisor task   |
| cooldown_after    | 5 hits  | → 15 min cooldown                   |

429 responses include `Retry-After` and audit a
`demo.rate_limit_hit`.

## Observability

Two Grafana dashboards:

- `sprint-07-demo-health` — Space up, active sessions, rate-limit
  rates, audio residual after finalize, daily privacy test status.
- `sprint-07-wer-trend` — WER overall, RTF p95, per-specialty WER,
  number-norm accuracy.

Alerts in `monitoring/prometheus/sprint-07-alerts.yml` page the right
team (SRE / DPO / ML-MLOps).

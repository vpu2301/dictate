# Runbook — HF Space Demo

## What this is

Public demo at `huggingface.co/spaces/<team>/medical-dictation-demo`.
Single-container deployment of the full sprint-02..06 stack with
embedded Postgres 16, Redis 7, and Keycloak 24 (ADR-0017). Privacy
envelope enforced by tmpfs + envvars + daily release gate (ADR-0018).

## Architecture diagram (mental model)

```
                 ┌─── HF API key gate ──────────────────────────────┐
                 │  (Space is private; only team-members can open)  │
                 └───────┬──────────────────────────────────────────┘
                         │
                         ▼
                  ┌─ nginx :7860 ─┐
                  │               │
       /auth/realms/* → Keycloak :8088
       /auth/*        → auth-service :8000
       /asr/*         → asr-service :8001
       /templates/*   → template-service :8006
       /nlp/*         → nlp-service :8005
       /dictate/* + /ws/dictate → dictation-service :8002

                  Postgres :5432 (tmpfs /run/pgdata)
                  Redis :6379 (tmpfs)
                  daily-restart-cron @ 06:00 UTC
```

## How to access

- The Space is **private**. To open it, set your HF API key on
  huggingface.co under "Settings → Access Tokens" and visit the Space.
- Inside the Space, log in via the regular OIDC button. The seeded
  user is **`clinician@demo.test`** / **`demo-please-change`** (rotate
  before any public flip — see "Rotate demo credentials" below).

## Day-to-day operations

### Status checks

- Space health: `curl https://<space-url>/_health` → 200 + JSON with
  `{"keycloak": "up", "postgres": "up", "redis": "up", ...}`.
- Grafana: `sprint-07-demo-health` dashboard.
- Daily privacy test: Grafana `mdx_demo_privacy_test_passed` should be 1.
- Daily WER: Grafana `sprint-07-wer-trend` dashboard.

### Deploys

`main` branch is the source of truth. To ship to the Space:

```bash
git push origin main:demo
```

Triggers `.github/workflows/hf-deploy.yml`. The workflow uses
`HF_TOKEN` (org secret) to push the folder to the Space, waits up to
5 min for `_health` to return 200, and surfaces the run link.

### Restarts

The Space auto-restarts daily at 06:00 UTC via supervisord's
`daily-restart-cron` (literally `kill 1`).

To restart manually:
```bash
# from a maintainer's box
huggingface-cli space restart <team>/medical-dictation-demo
```

### Rotate demo credentials

Before any public-link flip:

1. Open the Keycloak admin console: `https://<space-url>/auth/admin/`.
   Admin password: rotate via `KEYCLOAK_ADMIN_PASSWORD` secret →
   re-deploy.
2. Rotate the `clinician@demo.test` password. Pull a new one from
   1Password "Demo credentials".
3. Update the realm-export file in `infra/hf-space/realm-export.json`
   so it survives the next restart.
4. Push to `demo` branch.

## Incident playbook

### "Space is down"

1. Check HF Space page — read the build log if the container failed
   to come up.
2. If running but `/_health` returns 5xx: SSH into the Space via HF
   debug shell, run `supervisorctl status`. Restart the failing
   process.
3. If Postgres OOMed: the tmpfs sizes in `start.sh` are conservative —
   investigate what triggered the spike. Likely a stuck WS session;
   check `dictation_sessions` for rows older than 15 min.
4. If Keycloak failed to boot: most common cause is the realm-export
   schema being out of sync with the Keycloak version. Diff against
   `infra/hf-space/realm-export.json`.

### "Daily privacy test failed"

This is a P1 — pages DPO + security automatically via
`#privacy-alerts`. Follow this exactly:

1. Pull the artifact: `eval/reports/privacy-test-<date>.json` from the
   failed workflow run.
2. Look for the `residual_files` array — these are the files that
   survived finalize.
3. **Do not** SSH in and `rm` them. Snapshot the tmpfs first
   (`tar -czf /tmp/forensic.tgz /run/dictation`) and pull it to the
   security lead's investigation rig.
4. Disable the Space (HF dashboard → settings → "make private + take
   offline") until root cause is identified.
5. File an incident in the security tracker.

### "WER regressed"

1. Open the Grafana `sprint-07-wer-trend` dashboard and identify which
   language + specialty changed.
2. Pull the JSON report from
   `eval/reports/<run_id>.json` (workflow artifact). The
   per-utterance breakdown shows exactly which utterances drove the
   regression.
3. `git log --since="3 days ago" -- services/asr-service
   services/nlp-service libs/asr libs/nlp` to find candidate commits.
4. If the change was intentional (e.g. NLP improvement that trades
   off cardiology a bit for general medicine): bake a new baseline
   after 3 consecutive runs at the new level. Otherwise revert.

### "Rate-limit flood"

Watch the `mdx_demo_rate_limit_hits_total` rate. If sustained > 1/s:

1. The cooldown logic should already be slowing them — verify in
   audit `demo.ip_blocked` rows.
2. If an IP is hostile, add it to nginx's deny list in `nginx.conf`
   and re-deploy.

## Tmpfs sizes (start.sh)

| mount             | size   | purpose                       |
| ----------------- | ------ | ----------------------------- |
| `/run/pgdata`     | 2 GiB  | Postgres datadir              |
| `/run/dictation`  | 1 GiB  | per-session audio buffers     |
| `/dev/shm`        | 512 MB | Redis snapshots               |

Each is wiped on container restart.

## Secrets

Stored as HF Space secrets (set via the Space settings UI):

- `HF_TOKEN` — never exposed inside the Space; only used by deploy.
- `KEYCLOAK_ADMIN_PASSWORD` — rotated quarterly + before public flips.
- `MASTER_KEK` — the base64 KEK_master (ADR-0011). Generated fresh
  per Space deploy; demo encryption is essentially nominal because
  state never persists.
- `EVAL_DB_DSN` — points at a separate prod-like Postgres used by the
  nightly-wer workflow (not the in-Space Postgres). Held in GitHub
  Actions secrets, not in the Space.

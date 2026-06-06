# Runbook — dictation-service (streaming ASR)

Operational guide for the sprint-04 streaming surface.

## Key paths

| Concern               | Path / command                                                  |
| --------------------- | --------------------------------------------------------------- |
| Service code          | `services/dictation-service/`                                   |
| WS endpoint           | `ws://…/ws/dictate` (subprotocol `medical-dictation.v1`)        |
| Companion HTTP        | `GET/POST /dictate/sessions/...`                                |
| tmpfs root            | `/run/dictation/<session_id>/audio.bin` (mode 0700, 0600 file)  |
| Master key            | `/etc/mdx/master.key` (mode 0400)                               |
| Worker liveness key   | Redis: `mdx:dict:worker:<worker_id>:hb` (TTL ≈ 30 s)            |
| Dashboard             | Grafana → "Sprint 04 — Streaming Dictation"                     |
| Alerts                | `infra/prometheus/rules/sprint-04-streaming-dictation.yml`      |
| Protocol spec         | `docs/api/dictation-ws-v1.md`                                   |

## Failure modes

### § high-partial-latency

Symptom: `mdx_dictation_partial_latency_ms` p95 > 1500 ms for 5 min.

1. Check GPU utilization on the worker. If pinned, look for noisy
   neighbors (other CUDA processes).
2. Check inference-queue depth (logs: `inference.deadline_missed`).
3. If 3+ consecutive deadline misses, the queue auto-emits
   `Warning{worker_overloaded}` — reduce `MDX_PER_WORKER_MAX_SESSIONS`
   from 4 to 3 and bounce one replica.
4. If a recent model bump: roll back; re-run `scripts/eval/run_streaming_latency.py`.

### § stuck-reconnecting

Symptom: session sits in `reconnecting` long after client gave up.

1. SQL: `SELECT id, user_id, worker_id, last_active_at FROM dictation_sessions WHERE status='reconnecting' ORDER BY last_active_at;`.
2. Check worker liveness for `worker_id`. If TTL expired, the worker is dead — `POST /dictate/sessions/{id}/finalize` to commit what was captured.
3. Otherwise wait: the 30-minute abandon timer will fire automatically.

### § worker-crash

Symptom: `dictation-service` pod restart; `mdx_dictation_active_sessions` for that worker_id drops to 0.

1. `nvidia-smi` on the host — GPU healthy?
2. Logs: `kubectl logs ... -p` to read the prior instance's last messages.
3. Sessions bound to that worker move to `failed` (next reconnect
   attempt). Frontend offers "recover from local buffer" (sprint-03
   batch path).

### § master-key-missing

Same as sprint-03 asr-worker §master-key-missing — service refuses to
start; security incident in prod; in dev, run:

```sh
openssl rand 32 > infra/dev/master.key
chmod 0400 infra/dev/master.key
```

### § scale-out-trigger

Symptom: `mdx_dictation_active_sessions{worker_id} == 4` sustained 5 min.

Sprint 16 wires HPA to this. Until then: manually scale via
`docker compose up -d --scale dictation-service=N` (each replica is a
separate worker_id).

### § mass-reconnects

Symptom: `mdx_dictation_reconnects_total` rate > 10% of sessions over 1 h.

Likely causes: load-balancer reload, corporate-proxy update, network
event. Check LB logs; check `mdx_dictation_ws_upgrade_rejections_total{reason}`
for upstream patterns.

### § audio-truncated

Symptom: `dictation.audio.truncated` audit events appearing.

A session ran past the 30-min ring-buffer head; audio file is truncated
to the last 30 min. Transcript for the lost range was already committed
(if it had been finalized). Investigate per user: abusive client?
runaway retransmit storm?

### § token-expiring-storms

Symptom: many sessions receiving `token_expiring` simultaneously.

Likely a Keycloak issue or a synchronised cohort whose tokens minted
together. Check JWKS cache hit ratio (sprint-02 dashboard). If JWKS
fetch is slow, refresh latency grows and sessions hit `token_expired`
before refresh completes.

### § tmpfs-pressure

Symptom: `OSError: No space left on device` in `audio_buffer.tmpfs_write_failed`.

Each session reserves ~115 MB on tmpfs. 4 concurrent = 460 MB. Sprint
16 mounts a dedicated per-pod tmpfs of 2 GB; until then, ensure host
`/run` has > 1 GB free.

## Pre-flight after deploy

- All replicas report `mdx_dictation_model_loaded == 1`.
- A synthetic latency probe completes under target.
- `make wer-eval-streaming` (or the nightly cron) shows targets met.
- WS subprotocol negotiation: client offering `medical-dictation.v0`
  receives 400 (verify via dev-tools).

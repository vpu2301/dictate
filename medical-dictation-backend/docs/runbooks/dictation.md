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

### § transcript-is-nonsense-words

Symptom: the transcript is fluent-looking but the words do not exist in
the language — e.g. «Добро губня, лікарию, в житті жди ні бомсь» where the
speaker said «Доброго дня, лікарю, вже тиждень болі». Reported from the
conversation/scribe surface, because that is the only surface whose text
comes from this service (Studio dictation uses the browser's Web Speech
API and batch uploads go through asr-worker).

This is a MODEL problem, not a wire or diarization problem. Check first:

```bash
curl -s localhost:8002/healthz | jq .model      # or: docker compose exec dictation-service env | grep MD_ASR
```

1. **`whisper-tiny` (or `small`) is fatal for Ukrainian.** It does not
   degrade gracefully — it invents word-shapes. Measured on
   `eval/conversations/v1/uk-consult-001`: tiny/int8 → «в житті жди ні
   бомсь»; small/int8 → «Вжити ж Денибон»; large-v3/int8 → «вже тиждень
   болі». Only large-v3 is acceptable. Anything else, repoint
   `MD_ASR_MODEL` and bounce.
2. If the model is right, check `MD_ASR_STREAMING_VAD_FILTER` is not
   `false`. With the VAD off, silence-only windows are decoded and Whisper
   emits its training attractors ("Дякую за перегляд!" / "Thanks for
   watching!") which then commit as if they were speech.
3. A nonsense run that also shows `initial_prompt` repetition is worth a
   look at the specialty prompt: a prompt repeated inside `initial_prompt`
   drives Whisper into repetition loops.

### § transcript-stops-short-of-what-was-said

Symptom: the persisted transcript is correct but ends early — the closing
exchange of the consultation is missing.

Two independent tails, both funnelled through `_finalize_normal`:

1. **Un-windowed audio.** `next_slice` only yields a window once
   `MDX_WINDOW_MIN_FOR_PARTIAL_SECONDS` of fresh audio exists, so the
   remainder below that never reaches the model without the forced final
   window. Confirm `windower.final_drain_failed` is absent from the logs.
2. **Un-committed words.** Words inside the commit horizon
   (`MDX_WINDOW_OVERLAP_SECONDS`) are held pending a silence boundary that
   end-of-session will never produce. Confirm
   `finalize.provisional_flushed` appears for the session.

Both losses scale with those two settings, so a worker retuned for
throughput (large window/hop) loses proportionally more if either
mechanism is broken.

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

### § dev-stack-oom (Docker Desktop)

Symptom: the SPA's conversation mode shows `connect_failed` and the
WebSocket never opens, while every other service looks healthy.
`docker ps` shows dictation-service perpetually `health: starting` with
a climbing `RestartCount`; its log repeats "Started server process" /
"Waiting for application startup" and never reaches "Application
startup complete".

Cause: the dev stack runs TWO whisper large-v3 CPU instances —
asr-worker (2.7 GiB) and dictation-service (3.0 GiB, model + ECAPA
diarizer) — on top of ~2 GiB of infra. Docker Desktop's default engine
memory is 8 GiB, so dictation-service is killed by the kernel partway
through loading the diarizer and `restart: unless-stopped` puts it
straight back into the same wall. Nothing is logged inside the
container: it never gets to run a shutdown handler.

Confirm from OUTSIDE the container — this is the only place the reason
is visible:

```sh
docker events --filter container=medical-dictation-dictation-service-1 \
  --since 3m --until 0s
# → `oom` immediately followed by `die exit=137` on every cycle
docker info --format '{{.MemTotal}}'   # what the engine actually has
```

Fix: Docker Desktop → Settings → Resources → Memory ≥ 12 GiB, apply,
and let the stack come back up. Verify with
`docker stats --no-stream` that dictation-service settles at ~3 GiB and
`RestartCount` stays 0. Where the host cannot spare it, stop asr-worker
(`docker compose stop asr-worker`) for the duration — batch
transcription goes away, streaming and conversation keep working.

### § master-key-missing

Same as sprint-03 asr-worker §master-key-missing — service refuses to
start; security incident in prod; in dev, run:

```sh
openssl rand 32 > infra/dev/master.key
chmod 0400 infra/dev/master.key
```

### § scale-out-trigger

Symptom: `DictationWorkerWeightSaturated` — `mdx_dictation_capacity_weight_used`
has reached `mdx_dictation_capacity_weight_max` for 5 min.

**Watch weight, not headcount.** Since sprint 14 a conversation session
costs `MDX_CONVERSATION_SESSION_WEIGHT` (default 2) because it runs two
models; dictation costs 1. A worker carrying 2 conversation sessions is
FULL while `active_sessions` reads "2" — the old headcount rule
(`active_sessions >= 4`) could not fire in that state at all.

Sprint 16 wires HPA to the weight gauge. Until then: manually scale via
`docker compose up -d --scale dictation-service=N` (each replica is a
separate worker_id).

### § conversation-capacity-unavailable

Symptom: `DictationConversationCapacityUnavailable` — a worker serves
dictation (`/readyz` 200, whisper loaded) but `conversation_ready == 0`.

This is deliberate: a failed diarizer does NOT take the worker out of
rotation, because it is still a perfectly good dictation worker. But it
must not receive conversation traffic, and nothing else would notice.

```sh
curl -s localhost:8002/readyz | jq '{conversation_ready, diarizer_loaded, diarizer_error}'
```

`diarizer_error` names the cause. The two that matter:

- `ModelIntegrityError` — the startup checksum assertion refused the
  baked weights (fail-closed, docs/models/PINS.md). **Do not "fix" this
  by clearing `MDX_DIAR_MODEL_SHA256`.** The image is wrong: rebuild it,
  and treat a mismatch on a previously-good image as a supply-chain
  incident.
- missing `/opt/models/ecapa` — the image predates the sprint-14 bake.
  Rebuild from `services/dictation-service/Dockerfile{,.cpu}`.

### § device-memory-pressure

Symptom: `DictationDeviceMemoryHigh` (>90%) or `...Critical` (>97%).

Conversation mode puts a second model on the same device. On CUDA the
gauge reads whole-device used/total, so a co-tenant process counts too.

1. `curl -s localhost:8002/readyz | jq '{capacity_used, capacity_max}'`
   on each replica — is the fleet simply oversubscribed?
2. Drain conversation sessions first: they hold ~2× the resources of a
   dictation session. Setting `MDX_CONVERSATION_ENABLED=false` on a
   worker makes it advertise zero conversation capacity at the next
   restart without touching dictation.
3. If memory is high with FEW sessions, suspect a leak rather than
   load — session teardown should return the gauge to the two-model
   floor (measured: Whisper ≈ 362 MB + diarizer ≈ 164 MB RSS on CPU).

### § verify the baked models

Both models are baked, checksum-pinned, and re-verified at startup. To
confirm what a running image actually contains:

```sh
docker inspect <image> --format '{{json .Config.Labels}}' | jq   # repo/revision/sha256 for both models
docker run --rm --network none <image> \
  python -c "import os;from dictation_service.diarization.integrity import verify_model_dir;\
verify_model_dir(os.environ['MDX_DIAR_MODEL_DIR'],pins={'embedding_model.ckpt':os.environ['MDX_DIAR_MODEL_SHA256'],\
'mean_var_norm_emb.ckpt':os.environ['MDX_DIAR_MEANVAR_SHA256']})"
```

`--network none` is the point: it proves the weights load with no egress.

The build-time HF token is a BuildKit `--secret` and must never reach a
layer. To re-prove that after touching a Dockerfile:

```sh
docker save <image> | grep -a -c '<the token>'   # must print 0
docker history --no-trunc <image> | grep -c '<the token>'  # must print 0
```

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

### § sessions-stuck-active (capacity leak)

Symptom: `per_tenant_max_active_sessions` is hit while nobody is
dictating; `GET /dictate/sessions` lists sessions that no client owns;
clinicians cannot end a visit because core-service reports a live
recording on it.

Cause: the abandon timer lives inside the worker process that owns the
session, so a worker that dies (OOM, node drain, crash) takes its timers
with it and leaves every session it held in `active` / `paused` /
`reconnecting` **forever**. Those rows keep counting against the tenant
cap.

The **reaper** (`session/reaper.py`) is the standing fix: every
`MDX_SESSION_REAPER_INTERVAL_S` (default 300 s) it sweeps sessions
untouched for `MDX_SESSION_REAPER_GRACE_S` (default 300 s) and abandons
the ones whose worker's Redis heartbeat has expired. Its only interlock
is that heartbeat — a session paused for an hour on a *healthy* worker
is a candidate every sweep and is never collected.

Triage:

```bash
# What is stranded, and who owned it?
psql -c "SELECT worker_id, status, count(*), min(last_active_at)
         FROM dictation_sessions
         WHERE status IN ('creating','active','paused','reconnecting')
         GROUP BY 1,2 ORDER BY 3 DESC;"
# Is that worker still alive?  (TTL > 0 = alive)
redis-cli TTL mdx:dict:worker:<worker_id>:hb
```

- Reaper collections log `session.reaped` (WARN) and write
  `dictation.session.abandoned` with `reason=reaped_dead_worker`. A
  steady trickle means workers are dying — chase that, not the reaper.
- Nothing being collected while rows pile up: check the sweep is
  running at all (`session.reaper_swept`) and that
  `MDX_SESSION_REAPER_ENABLED` is not false. Cross-tenant enumeration
  goes through the `dictation_tenants_with_stale_sessions` SECURITY
  DEFINER function (0059) — without it RLS silently returns zero rows
  and the reaper "succeeds" having done nothing (the 0051 failure mode).
- A clinician blocked from ending a visit by a session that is really
  dead does not have to wait for the sweep: core-service only counts
  sessions active within `MDX_ENCOUNTER_LIVE_SESSION_WINDOW_SECONDS`
  (default 120 s), and `POST /encounters/{id}/complete` accepts
  `{"force": true}`.

## Pre-flight after deploy

- All replicas report `mdx_dictation_model_loaded{model="whisper"} == 1`.
- Replicas intended to serve conversation report
  `mdx_dictation_conversation_ready == 1` **and** log
  `diarization.model_verified` with the expected revision. A replica that
  logged `diarization.warmup_failed` is dictation-only — intentional, but
  it must be a known state, not a surprise.
- `mdx_dictation_capacity_weight_max` matches the intended budget on
  every replica (a mismatched `MDX_PER_WORKER_MAX_SESSIONS` silently
  changes fleet capacity).
- A synthetic latency probe completes under target.
- `make wer-eval-streaming` (or the nightly cron) shows targets met.
- WS subprotocol negotiation: client offering `medical-dictation.v0`
  receives 400 (verify via dev-tools).

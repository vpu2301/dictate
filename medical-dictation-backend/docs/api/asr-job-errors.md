# Batch ASR — rejection criteria and failure vocabulary

Everything that can go wrong with `POST /asr/jobs` and the job it creates,
why it can go wrong, and what the caller should do about it.

Two vocabularies, because they answer different questions and live in
different places:

| | Where | Wire location | Source of truth |
|---|---|---|---|
| **Rejection codes** | Submit time — no job exists | `code` in the RFC 9457 problem body | `asr_service.validators.result.ValidationCode` |
| **Failure kinds** | After a job exists | `error_kind` on the job view | `asr_models.errors.JobErrorKind` |

A submitted upload either becomes a job (`202`) or is rejected with a
code. Once it is a job, it ends `complete`, `cancelled`, or `failed` with
a kind. There is no third outcome — the reaper and the dead-letter path
below exist specifically to keep that promise when a worker dies.

---

## 1. Submit-time rejection criteria

Checked in this order; the first failure short-circuits, so a file that
is both oversized and mis-declared reports whichever is checked first.
Nothing is written — no ciphertext, no row — for anything rejected before
step 9.

| # | Criterion | Code | HTTP | Why it happens |
|---|---|---|---|---|
| 1 | Bearer + `asr.write` on `asr_job` | `scope_missing` | 401 / 403 | Token absent, expired, or a role without dictation upload |
| 2 | Declared MIME in the allow-list | `mime_not_allowed` | 400 | A container we cannot transcribe (video, zip, `application/octet-stream` from a client that did not set the type) |
| 3 | Not empty | `empty_upload` | 400 | The browser lost the recording before submit; a MediaRecorder stopped before its first flush |
| 4 | Size ≤ `MD_ASR_MAX_UPLOAD_MB` | `size_exceeded` | 400 | An hour of uncompressed WAV; an accidental whole-session upload |
| 5 | Magic bytes match the declared MIME | `mime_mismatch` | 400 | A polyglot or mis-declared file — MP3 bytes labelled `audio/wav`. Also a file under 12 bytes |
| 6 | ffprobe can read it | `unprobeable` | 400 | Truncated header, container we do not recognise, or ffprobe timed out |
| 7 | Duration ≥ `MD_ASR_MIN_DURATION_MS` | `duration_too_short` | 400 | A tapped record button. Whisper answers a fraction of a second of noise with a **hallucinated phrase** — rejecting is safer than charting it |
| 7 | Duration ≤ `MD_ASR_MAX_DURATION_SECONDS` | `duration_exceeded` | 400 | A recorder left running; a whole clinic session in one file |
| 8 | Codec in the allow-list | `codec_not_allowed` | 400 | An exotic codec inside an allowed container |
| 8 | Sample rate ≥ `MD_ASR_MIN_SAMPLE_RATE_HZ` | `sample_rate_too_low` | 400 | Telephony-grade capture below the floor where Ukrainian clinical terms survive |
| 8 | Channels ≤ `MD_ASR_MAX_CHANNELS` | `channels_exceeded` | 400 | A multi-track recorder |
| 9 | `encounter_id` exists in this tenant | `encounter_invalid` | 400 | A stale id, or another tenant's encounter — RLS makes the two indistinguishable, deliberately (no existence oracle) |
| 9 | Encounter not cancelled | `encounter_closed` | 400 | Dictation against a visit that was called off |
| 9 | `prompt_id` in the catalogue | `prompt_invalid` | 400 | A prompt id cached from a previous release. Checked here because the FK would otherwise surface as a 500 *after* the audio was uploaded |
| 9 | Tenant under the concurrent cap | `concurrency_exceeded` | 429 | `MD_ASR_PER_TENANT_CONCURRENT_JOBS` queued+running jobs already. Body carries `active` and `limit` |
| 10 | Tenant under the monthly quota | `quota_exceeded` | 429 | `MD_ASR_MONTHLY_QUOTA_BYTES` for the calendar month. A soft billing guard, not a security boundary — parallel uploads can overshoot by the in-flight bytes |
| 11 | Queue accepted the job | `enqueue_failed` | 503 | Redis unreachable at publish. The row is marked `failed` before the response — a `queued` job nobody will ever transcribe is worse than an error |

`language` outside `uk|en` and a malformed UUID are rejected by FastAPI's
own form validation as **422** with the framework's field-level body, not
a `code` from this table.

### Problem body shape

All of the above render as RFC 9457 `application/problem+json` with the
machine-readable members at the top level:

```json
{
  "type": "urn:mdx:asr:validation:duration_too_short",
  "title": "Bad Request",
  "status": 400,
  "detail": "audio is 120 ms; the minimum is 400 ms",
  "instance": "urn:uuid:…",
  "code": "duration_too_short",
  "reason": "audio rejected by validation"
}
```

Branch on `code`. `detail` is human-facing prose and its wording is not a
contract. `type` is stable; the concurrency rejection keeps its historical
`urn:mdx:asr:rate_limit:per_tenant_concurrent` URI rather than the
`urn:mdx:asr:validation:…` form, for clients that already match on it.

---

## 2. Failure kinds — after the job exists

`GET /asr/jobs/{id}` and `GET /asr/jobs` return, for a `failed` job:

| Field | Meaning |
|---|---|
| `error_kind` | The closed vocabulary below |
| `error_stage` | `queue` / `decode` / `inference` / `persist` / `lifecycle` |
| `error_retryable` | Whether re-running the same job could have succeeded (drives the worker's own retry decision) |
| `error_message` | PHI-free explanation, safe to show a clinician |
| `error_detail` | Free text from the underlying exception. **May quote the audio** — never surfaced to the notification feed or to `stats.read` callers (ADR-0031) |

`error_stage` / `error_retryable` / `error_message` are computed from
`error_kind`, so they cannot drift from it.

| Kind | Stage | Retried? | Resubmit helps? | Why it happens |
|---|---|---|---|---|
| `enqueue_failed` | queue | — | yes | asr-service could not publish to Redis. Written by the service, not the worker |
| `queue_lost` | queue | no | yes | The row committed and the publish returned, but no worker ever claimed it — a flushed Redis, a trimmed stream, a recreated consumer group. Written by the reaper |
| `bad_payload` | queue | no | yes | The worker cannot parse the queued job description — asr-service deployed ahead of asr-worker. Goes straight to the DLQ |
| `job_row_missing` | queue | no | yes | The message names a job id that is not in the database |
| `audio_missing` | decode | no | yes | The audio object is gone: retention TTL, the S11 erasure engine, or an upload whose row was written but whose object never landed |
| `decrypt_failed` | decode | no | **no** | Envelope failure — wrong AAD, a tenant KEK that will not unwrap, a truncated object. An operator's problem; re-uploading changes nothing |
| `storage_unavailable` | decode | **yes** | no | MinIO/S3 unreachable while fetching |
| `corrupt_audio` | decode | no | yes | ffmpeg refused the file that ffprobe accepted — truncated payload, a container whose declared codec the stream does not carry |
| `no_speech` | decode | no | yes | Decoded cleanly, contains no speech: silence, or a microphone that captured nothing. **Deliberately a failure, not an empty `complete`** — Whisper given silence produces a hallucinated phrase, and a hallucination stored as a transcript reaches the chart looking like dictation |
| `model_unavailable` | inference | **yes** | no | The Whisper model is not loaded on the worker that claimed the job |
| `gpu_oom` | inference | no | yes | CUDA out of memory. Not retried — hammering a full GPU costs the whole queue. The CUDA cache is released before the worker moves on |
| `timeout` | inference | no | yes | Inference exceeded `max(60s, audio_seconds × MD_ASR_MAX_INFERENCE_SECONDS_MULTIPLIER)` |
| `result_store_failed` | persist | **yes** | no | The transcript was produced but could not be encrypted/uploaded. Retried, which redoes inference — cheaper than a `complete` row pointing at an object that was never written |
| `db_unavailable` | persist | **yes** | no | The transcript is stored; recording the status failed |
| `retry_exhausted` | lifecycle | no | yes | A retryable kind failed on every delivery and the message was dead-lettered. The last kind is preserved in `error_detail` |
| `worker_lost` | lifecycle | no | yes | The worker died mid-inference and the reaper collected the job |
| `unhandled` | inference | **yes** | no | Unclassified. Every one of these is a gap in this table — see the triage note below |

An `error_kind` this build does not recognise (a job failed by a newer
worker) decodes as `unhandled` with `error_retryable: false`: a reader
that cannot name a failure is in no position to promise it is temporary.

### Retry, dead-letter, reap

Three mechanisms, and between them they guarantee every job reaches a
terminal row:

1. **Retryable kinds** are handed back to Redis Streams. The job row stays
   `running` — the job has not failed, this attempt has.
2. **`MD_ASR_JOBS_MAX_RETRIES` deliveries later** the message is
   dead-lettered, and the worker writes `retry_exhausted` to the row. (The
   queue giving up used to be silent on the database's side; the row sat
   in `running` indefinitely.)
3. **The reaper** in asr-service sweeps every
   `MD_ASR_JOB_REAPER_INTERVAL_S` for jobs the worker could not close out
   itself: `running` past `MD_ASR_JOB_REAPER_RUNNING_GRACE_S` →
   `worker_lost`; `queued` past `MD_ASR_JOB_REAPER_QUEUED_GRACE_S` →
   `queue_lost`. Its updates are conditional on the status it scanned, so
   a job that finished in between keeps its own outcome.

The grace windows are the reaper's **only** interlock — asr-worker
publishes no heartbeat. Keep the running window comfortably above the
worst case the worker allows itself (`MD_ASR_MAX_DURATION_SECONDS` ×
`MD_ASR_MAX_INFERENCE_SECONDS_MULTIPLIER`, ≈ 2.5 h at the defaults) plus
a redelivery. Reaping early is not catastrophic — the worker's idempotency
check sees the terminal row on redelivery and skips — but it costs the
clinician a transcript that was on its way.

Jobs closed out by the reaper or by the enqueue path do **not** emit a
notification; only the worker's own failures do (asr-service does not
carry the notification dependency). They surface on the job list and on
`GET /asr/jobs/{id}`.

---

## 3. Result-fetch errors

`GET /asr/jobs/{id}/result` decrypts and returns the transcript
(presigned URLs serve ciphertext — ADR-0011 forbids client-side decrypt).

| HTTP | `type` | Why |
|---|---|---|
| 404 | — | No such job in this tenant |
| 409 | `urn:mdx:asr:result:not-ready` | Not `complete`. The body carries `job_status` and, for a failed job, the whole failure vocabulary — so one response tells a poller both that the transcript is not coming and whether resubmitting helps |
| 410 | `urn:mdx:asr:result:erased` | The job is `complete` but the transcript object is gone (retention / erasure) |

NLP enrichment failures are **not** errors: the endpoint returns the raw
transcript with `nlp_applied: false`.

---

## Triage

- **`unhandled` appearing at all** is the signal to extend
  `asr_models.errors`. Every kind that shows up more than once deserves a
  name, a stage, and a retry policy — which is what makes the retry
  decision reviewable instead of incidental.
- **A spike in `corrupt_audio` or `unprobeable`** points at a client
  recorder change, not at the fleet.
- **`no_speech` in bulk** points at a microphone or permissions problem on
  the client, not at the model.
- **`worker_lost` in bulk** means workers are dying — check the GPU node
  before anything else (`docs/runbooks/asr-worker.md`).
- **Adding a kind:** add it to `JobErrorKind`, give it a spec (stage,
  retryable, resubmittable, PHI-free message), add a row here. The
  `test_every_kind_has_a_spec` test fails if you forget the spec; nothing
  but review catches a missing row here.

# ASR Architecture (Sprint 03)

This page is the durable mental model for the batch ASR path. Sprint 04
streaming reuses the engine + crypto + storage primitives; this doc is
its dependency.

## Service topology

```
        ┌──────────────┐    POST /asr/jobs    ┌───────────────┐
        │   browser /  │ ───────────────────▶ │  asr-service  │
        │  EHR client  │                       │ (CPU, multi)  │
        └──────────────┘                       └───────┬───────┘
                                                       │ 1. encrypt + put
                                                       ▼
                                               ┌───────────────┐
                                               │     MinIO     │
                                               │  mdx-audio    │
                                               └───────────────┘
                                                       │
                                                       │ 2. INSERT rows
                                                       ▼
                                               ┌───────────────┐
                                               │   Postgres    │
                                               │ audio_files / │
                                               │ transcription │
                                               │     _jobs     │
                                               └───────────────┘
                                                       │
                                                       │ 3. XADD asr:jobs
                                                       ▼
                                               ┌───────────────┐
                                               │     Redis     │
                                               │   Streams     │
                                               └───────┬───────┘
                                                       │
                                                       │ 4. XREADGROUP
                                                       ▼
                                               ┌───────────────┐
                                               │  asr-worker   │
                                               │  (GPU, N)     │
                                               └───────┬───────┘
                                                       │ 5. fetch audio
                                                       ▼  → ffmpeg → PCM
                                                       │  → VAD → Whisper
                                                       │
                                                       │ 6. encrypt + put
                                                       ▼
                                               ┌───────────────┐
                                               │     MinIO     │
                                               │ mdx-transcripts│
                                               └───────────────┘
```

## Envelope

```
┌─────────────────────────────────────────────────────────────────┐
│              KEK_master  (1 per environment)                     │
│              (file in dev, KMS in prod — ADR-0011)               │
│                          │                                       │
│                  wraps   ▼                                       │
│              KEK_tenant  (1 per tenant; tenant_keks)             │
│                          │                                       │
│                  wraps   ▼                                       │
│              DEK_object  (1 per audio file or transcript;        │
│                           ephemeral, never persisted plaintext)  │
│                          │                                       │
│                  encrypts ▼                                       │
│              ciphertext  (in MinIO, header || ciphertext)        │
└─────────────────────────────────────────────────────────────────┘
```

AAD on every operation: `tenant_id.bytes || caller_aad`. The caller_aad
is the row id (e.g., audio_id, job_id) so the ciphertext is bound to
its logical home.

## Queue

Redis Streams. One stream `asr:jobs`. One consumer group
`asr-workers`. Workers are members of the group with distinct
`consumer` names (one per replica).

```
Producer (asr-service):
    XADD asr:jobs * value <json> key <job_id> h-tenant_id <…> h-job_id <…>

Consumer (asr-worker):
    XREADGROUP GROUP asr-workers <consumer-name> COUNT 1 BLOCK 5000 STREAMS asr:jobs >
    -- on success:
    XACK asr:jobs asr-workers <message_id>
    -- on stuck (no ack within 60s):
    XAUTOCLAIM asr:jobs asr-workers <consumer-name> 60000 0-0 COUNT 10
    -- on 3 retries:
    XADD asr:jobs:dlq * (move to DLQ)
    XACK asr:jobs asr-workers <message_id>
```

## Failure-mode table

| Where                  | What                          | Recovery                                |
| ---------------------- | ----------------------------- | --------------------------------------- |
| API: validator         | Reject upload                 | RFC 9457 problem detail; client retries |
| API: storage           | MinIO put fails               | 5xx; no row inserted; client retries    |
| API: DB                | INSERT fails after MinIO put  | Orphan ciphertext → cleanup cron        |
| Queue: XADD            | Redis down                    | 5xx; orchestrator retries               |
| Worker: fetch          | Object missing                | Mark failed, `corrupt_audio`            |
| Worker: ffmpeg         | Decode fails                  | Mark failed, `corrupt_audio`            |
| Worker: Whisper        | OOM                           | Mark failed, `gpu_oom`; release cache   |
| Worker: Whisper        | Timeout                       | Mark failed, `timeout`                  |
| Worker: storage put    | MinIO put of transcript fails | Mark failed; XACK; alert                |
| Worker: ack            | Crashed before XACK           | XAUTOCLAIM reclaims → next consumer      |
| Worker: ack            | Reclaimed > 3 times           | Move to DLQ; ops investigates           |

## Cross-references

- **ADR-0009** — inference engine choice.
- **ADR-0010** — queue choice.
- **ADR-0011** — envelope structure.
- **`docs/runbooks/asr-worker.md`** — operational playbook.
- **`docs/audit/event-kinds.md`** — `asr.*` kinds emitted along the path.

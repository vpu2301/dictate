# Runbook — asr-worker

Single-page operations guide for the sprint-03 GPU worker.

## Key paths

| Concern               | Path / command                                     |
| --------------------- | -------------------------------------------------- |
| Service code          | `services/asr-worker/`                             |
| Master key (dev)      | `/etc/mdx/master.key` (mounted from `infra/dev/`)  |
| Queue                 | Redis stream `asr:jobs`, group `asr-workers`       |
| DLQ                   | Redis stream `asr:jobs:dlq`                        |
| Audio bucket          | MinIO `mdx-audio`                                  |
| Transcript bucket     | MinIO `mdx-transcripts`                            |
| Dashboard             | Grafana → "Sprint 03 — ASR Health"                 |
| Alerts                | `infra/prometheus/rules/sprint-03-asr.yml`         |
| WER nightly           | `scripts/eval/run_wer.py`                          |

## Failure modes

### § master-key-missing

The worker refuses to start because `/etc/mdx/master.key` is missing.

1. Confirm volume mount: `docker compose exec asr-worker ls -l /etc/mdx`.
2. If the file is absent in dev, re-create it:
   ```sh
   openssl rand 32 > infra/dev/master.key
   chmod 0400 infra/dev/master.key
   ```
3. In staging/prod a missing master key is a **security incident** —
   page security lead immediately; do not invent a new key.

### § master-key-permissions

Mode is more permissive than 0400. `chmod 0400` the file and restart.
In staging/prod treat as an incident — the file should never have been
group/other-readable.

### § gpu-oom

Symptoms: `mdx_asr_oom_total` increments; jobs land in `failed` with
`error_kind='gpu_oom'`.

1. Identify the offending job via traces (audio_seconds, segments).
2. If a single tenant is sending unusually long audio, reduce beam size:
   set `MD_ASR_BEAM_SIZE=3` and bounce one replica to confirm the
   reduction holds.
3. If the OOM persists with beam_size=3 on a 30-min file, reduce the
   max audio length cap (`MD_ASR_MAX_DURATION_SECONDS`) until we ship
   chunk-streaming inference (sprint 04).

### § model-corruption

Symptoms: `mdx_asr_model_loaded=0`; worker logs "checksum mismatch".

1. Delete the cached weights: `docker compose exec asr-worker rm -rf /root/.cache/huggingface`.
2. Restart the worker.

### § queue-backlog

Symptoms: `mdx_asr_queue_depth > 100` for > 5 m.

1. Scale workers: `docker compose -f base.yml -f dev.yml -f gpu.yml up -d --scale asr-worker=N`.
2. If the upstream rate is sustained, the capacity model is wrong;
   open a follow-up to revise the sprint-16 sizing ADR.

### § minio-outage

Symptoms: jobs queue but don't progress; worker log says
`storage.s3.head_bucket_failed`.

1. Confirm MinIO health: `mc admin info local`.
2. Recover MinIO; jobs resume automatically because they remain in the
   pending-entries list until reclaimed.

### § nvidia-driver-mismatch

Symptoms: container fails to start; `nvidia-container-cli` errors.

1. `nvidia-smi` on the host — should match the CUDA version in the
   `cuda:12.4.1-cudnn-runtime-ubuntu22.04` base image (driver ≥ 535).
2. Upgrade the host driver or downgrade the worker base image (rare).

### § audit-chain-divergence (touching asr.*)

Same procedure as sprint-02 auth audit divergence; ASR kinds are
`asr.audio_uploaded`, `asr.job_queued`, `asr.transcription_*`,
`asr.job_cancelled`, `asr.quota_exceeded`.

## Pre-flight after deployment

- Confirm `mdx_asr_model_loaded == 1` on every replica.
- Submit a known-good fixture via `scripts/dev/asr-smoke.sh` (1-line:
  POST a 5-second WAV; expect status=complete within 60 s).
- Confirm a non-zero `mdx_asr_warmup_seconds` exists per replica.

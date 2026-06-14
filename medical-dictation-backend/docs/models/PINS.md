# Model pins (Sprint B1, ADR-0021)

Single source of truth for every model the platform bakes at build time.
All models are **build-time-only** sources: fetched at a pinned, immutable
commit revision, checksum-verified (fail-closed), baked into the image, and
loaded **fully offline** at runtime (`HF_HUB_OFFLINE=1`). Hugging Face is
never a runtime dependency and never processes PHI.

Pins resolved from the Hugging Face API on **2026-06-10**.

| Service | Repo | Revision (commit) | Verified artifact | SHA-256 | Baked path |
|---|---|---|---|---|---|
| asr-worker, dictation-service (GPU) | `Systran/faster-whisper-large-v3` | `edaa852ec7e145841d8ffdb056a99866b5f0a478` | `model.bin` | `69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1` | `/opt/models/whisper-large-v3` |
| asr-worker, dictation-service (CPU dev) | `Systran/faster-whisper-tiny` | `d90ca5fe260221311c53c58e660288d3deb8d356` | `model.bin` | `dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1` | `/opt/models/whisper-tiny` |
| nlp-service | `oliverguhr/fullstop-punctuation-multilang-large` | `345e80adc07e761d3a35feafd20f2f44a151f453` | `model.safetensors` | `270f27d7398a5fdad43bdf9953ea532fbe62c5f5227ed5f5316e9bd64a9255e1` | `/opt/models/punctuation` |

## How the pin is enforced

Each service Dockerfile has a `model-fetch` build stage that:

1. `huggingface-cli download <repo> --revision <commit>` — immutable, never a
   moving tag.
2. `sha256sum -c` the verified artifact against the pinned digest — **a
   mismatch fails the build** (AC-B1-1).
3. The runtime stage `COPY --from=model-fetch` bakes the weights and stamps
   OCI labels `mdx.model.repo` / `mdx.model.revision` / `mdx.model.sha256`,
   so a deployed image is self-describing (`docker inspect`).

`HF_TOKEN` is consumed only as a BuildKit `--secret` (`--mount=type=secret,id=hf_token`)
and never lands in any layer, env, or log. The public Systran/oliverguhr
repos do not require it; a private in-perimeter mirror does.

## Re-pinning

Override at build time without editing the Dockerfile:

```sh
DOCKER_BUILDKIT=1 docker build \
  --build-arg MD_ASR_MODEL_REVISION=<new-commit> \
  --build-arg MD_ASR_MODEL_SHA256=<new-model.bin-sha256> \
  -f services/asr-worker/Dockerfile -t mdx-asr-worker:gpu .
```

Re-baselining the WER gate after a model change is gated by an ADR (ADR-0019).

## Verified on 2026-06-10 (CPU/tiny, fully offline)

Built `Dockerfile.cpu`, ran with `--network none`, and transcribed real
speech end-to-end — proving pin → verify → bake → offline-load → transcribe.
A deliberately corrupted `--build-arg MD_ASR_MODEL_SHA256` failed the build as
designed. The GPU/large-v3 path uses the identical mechanism.

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
| dictation-service (conversation mode, sprint 14, ADR-0034) | `speechbrain/spkrec-ecapa-voxceleb` | `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286` | `embedding_model.ckpt` | `0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2` | `/opt/models/ecapa` |
| generation-service (Layer C inline completion, sprint 15, ADR-0036) | `ollama.com/library/gemma3:1b` (Gemma 3 1B instruct, Q4_K_M GGUF) | tag digest `8648f39daa8f` | GGUF blob | `7cd4618c1faf8b7233c6c906dac1694b6a47684b37b8895d470ac688520b9c01` | dev: `~/.ollama/models/blobs/` (served by `llama-server`); prod bake pending GPU rig |

Assembly for the ECAPA row is scripted — `scripts/models/prepare_ecapa.py`
(also verifies `mean_var_norm_emb.ckpt`
`cd70225b05b37be64fc5a95e24395d804231d43f74b2e1e5a513db7b69b34c33` and copies
the repo-owned offline-patched `infra/models/ecapa/hyperparams.yaml`). Known
gap, recorded deliberately: **Silero VAD weights ship inside the `silero-vad`
PyPI wheel** (uv.lock-pinned, MIT) rather than through this table's
fetch+checksum flow — acceptable for the pilot because the wheel hash is
locked, but a future sprint should hoist the JIT file into a pinned artifact.

Layer C (sprint 15) row: the Gemma 3 1B GGUF is fetched via `ollama pull
gemma3:1b` (content-addressed — the blob file IS its sha256) and served in dev
by `llama-server` pointed at the blob path (ADR-0036 records why: a constant
~420 ms/request scheduler overhead in Ollama 0.32.5 with gemma3's SWA cache).
The production image bake (fetch at pin → `sha256sum -c` → bake, same flow as
the rows above) is deferred with the GPU rig; the digest above is the pin it
must verify against.

## How the pin is enforced

Each service Dockerfile has a `model-fetch` build stage that:

1. `huggingface-cli download <repo> --revision <commit>` — immutable, never a
   moving tag.
2. `sha256sum -c` the verified artifact against the pinned digest — **a
   mismatch fails the build** (AC-B1-1).
3. The runtime stage `COPY --from=model-fetch` bakes the weights and stamps
   OCI labels `mdx.model.repo` / `mdx.model.revision` / `mdx.model.sha256`,
   so a deployed image is self-describing (`docker inspect`). The ECAPA row
   adds `mdx.diar.model.*` from its own `ecapa-fetch` stage, which reuses
   `scripts/models/prepare_ecapa.py` so the image and a developer's
   `make prepare-ecapa` produce byte-identical dirs.

### Re-asserted at startup (sprint 14)

A build-time check only proves the image was correct **when it was built**.
Since sprint 14 the diarization digests are verified AGAIN when the process
starts (`dictation_service/diarization/integrity.py`), before the weights are
loaded, driven by the `MDX_DIAR_MODEL_SHA256` / `MDX_DIAR_MEANVAR_SHA256`
ENV the Dockerfile bakes. A mismatch, a missing artifact, or a missing
`hyperparams.yaml` **refuses to start the diarizer** — the worker degrades
to dictation-only and `/readyz` reports `conversation_ready: false` with the
reason. Diarizing with weights nobody can account for is not an option for a
medical product.

Whisper is not yet startup-verified — `MD_ASR_MODEL_SHA256` is logged as
provenance only. Extending the same assertion to the ASR weights is a
follow-up (todo.md).

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

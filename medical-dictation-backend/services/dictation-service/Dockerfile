# ── GPU build for dictation-service ───────────────────────────────────
# Shares the CUDA base with asr-worker. The WhisperEngine is imported
# in-process (ADR-0009 + sprint-04 day-1), so this image bakes the SAME
# pinned + checksum-verified Whisper weights and runs fully offline
# (ADR-0021, Sprint B1 Day 2).

# ── Stage 1: fetch + verify weights (build-time only) ──────────────────
FROM python:3.12-slim AS model-fetch

ARG MD_ASR_MODEL_REPO=Systran/faster-whisper-large-v3
ARG MD_ASR_MODEL_REVISION=edaa852ec7e145841d8ffdb056a99866b5f0a478
ARG MD_ASR_MODEL_SHA256=69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1

RUN pip install --no-cache-dir "huggingface_hub[cli]==0.25.2"

# Retry on a broken stream: hf_hub resumes from the .incomplete file in this
# same layer. If every attempt fails, sha256sum -c below still fails closed.
ENV HF_HUB_DOWNLOAD_TIMEOUT=60
RUN --mount=type=secret,id=hf_token \
    set -eu; \
    if [ -f /run/secrets/hf_token ]; then HF_TOKEN="$(cat /run/secrets/hf_token)"; export HF_TOKEN; fi; \
    for attempt in 1 2 3 4 5; do \
      huggingface-cli download "$MD_ASR_MODEL_REPO" \
        --revision "$MD_ASR_MODEL_REVISION" \
        --local-dir /opt/models/whisper \
        --local-dir-use-symlinks False \
        --max-workers 2 && break; \
      echo "whisper download attempt ${attempt} failed; resuming in 10s"; \
      sleep 10; \
    done; \
    echo "${MD_ASR_MODEL_SHA256}  /opt/models/whisper/model.bin" | sha256sum -c -; \
    echo "model.bin checksum verified against pinned digest"

# ── Stage 1b: fetch + verify the ECAPA diarization weights ─────────────
# Sprint 14 (ADR-0034): conversation mode runs a SECOND model on the same
# GPU. Same bake contract as Whisper — immutable revision, sha256
# fail-closed, offline at runtime. The assembly script is the single source
# of truth for the pins (docs/models/PINS.md), shared with the dev path
# (`make prepare-ecapa`) so the image and a laptop load identical weights.
FROM python:3.12-slim AS ecapa-fetch

ARG MDX_DIAR_MODEL_REPO=speechbrain/spkrec-ecapa-voxceleb
ARG MDX_DIAR_MODEL_REVISION=0f99f2d0ebe89ac095bcc5903c4dd8f72b367286
ARG MDX_DIAR_MODEL_SHA256=0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2
ARG MDX_DIAR_MEANVAR_SHA256=cd70225b05b37be64fc5a95e24395d804231d43f74b2e1e5a513db7b69b34c33

RUN pip install --no-cache-dir "huggingface_hub[cli]==0.25.2"

COPY scripts/models/prepare_ecapa.py /build/scripts/models/prepare_ecapa.py
COPY infra/models/ecapa/ /build/infra/models/ecapa/

# ECAPA is ungated (Apache-2.0) so the token is not needed for the public
# repo; the mount is kept for a private in-perimeter mirror. BuildKit
# secrets never land in a layer — verified in docs/runbooks/dictation.md.
# Retried like the Whisper fetch above; the script verifies both digests
# itself, so a run that completes has already failed closed on a mismatch.
ENV HF_HUB_DOWNLOAD_TIMEOUT=60
RUN --mount=type=secret,id=hf_token \
    set -eu; \
    if [ -f /run/secrets/hf_token ]; then HF_TOKEN="$(cat /run/secrets/hf_token)"; export HF_TOKEN; fi; \
    for attempt in 1 2 3 4 5; do \
      python /build/scripts/models/prepare_ecapa.py \
        --target /opt/models/ecapa \
        --revision "$MDX_DIAR_MODEL_REVISION" \
        --embedding-sha256 "$MDX_DIAR_MODEL_SHA256" \
        --meanvar-sha256 "$MDX_DIAR_MEANVAR_SHA256" && exit 0; \
      echo "ecapa download attempt ${attempt} failed; resuming in 10s"; \
      sleep 10; \
    done; \
    echo "ecapa download failed after 5 attempts" >&2; exit 1

# ── Stage 2: runtime image ─────────────────────────────────────────────
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS runtime

ARG MD_ASR_MODEL_REPO=Systran/faster-whisper-large-v3
ARG MD_ASR_MODEL_REVISION=edaa852ec7e145841d8ffdb056a99866b5f0a478
ARG MD_ASR_MODEL_SHA256=69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1
ARG MDX_DIAR_MODEL_REPO=speechbrain/spkrec-ecapa-voxceleb
ARG MDX_DIAR_MODEL_REVISION=0f99f2d0ebe89ac095bcc5903c4dd8f72b367286
ARG MDX_DIAR_MODEL_SHA256=0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2
ARG MDX_DIAR_MEANVAR_SHA256=cd70225b05b37be64fc5a95e24395d804231d43f74b2e1e5a513db7b69b34c33

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-venv \
        python3.12-dev \
        python3-pip \
        ffmpeg \
        libsndfile1 \
        libopus0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

RUN python3.12 -m pip install --no-cache-dir --upgrade pip uv==0.4.29

WORKDIR /build
COPY pyproject.toml uv.lock /build/
COPY libs/ /build/libs/
COPY services/asr-worker/ /build/services/asr-worker/
COPY services/dictation-service/ /build/services/dictation-service/

RUN python3.12 -m uv pip install --system --no-cache \
    --target /deps \
    /build/services/dictation-service \
    /build/services/asr-worker \
    /build/libs/observability \
    /build/libs/auth \
    /build/libs/audit \
    /build/libs/db \
    /build/libs/messaging \
    /build/libs/crypto \
    /build/libs/storage \
    /build/libs/asr_models \
    /build/libs/secret

# Bake the verified weights from the fetch stages.
COPY --from=model-fetch /opt/models/whisper /opt/models/whisper-large-v3
COPY --from=ecapa-fetch /opt/models/ecapa /opt/models/ecapa

LABEL mdx.model.repo="${MD_ASR_MODEL_REPO}" \
      mdx.model.revision="${MD_ASR_MODEL_REVISION}" \
      mdx.model.sha256="${MD_ASR_MODEL_SHA256}" \
      mdx.diar.model.repo="${MDX_DIAR_MODEL_REPO}" \
      mdx.diar.model.revision="${MDX_DIAR_MODEL_REVISION}" \
      mdx.diar.model.sha256="${MDX_DIAR_MODEL_SHA256}"

# Editable workspace libs install as .pth files, which Python only honors
# in a site dir — not via PYTHONPATH. sitecustomize is auto-imported because
# /deps is on PYTHONPATH; it re-registers /deps as a site dir so the editable
# .pth files (asr_models, audit, observability, …) actually activate.
RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}" \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    MD_ASR_MODEL=/opt/models/whisper-large-v3 \
    MD_ASR_MODEL_REPO="${MD_ASR_MODEL_REPO}" \
    MD_ASR_MODEL_REVISION="${MD_ASR_MODEL_REVISION}" \
    MD_ASR_MODEL_SHA256="${MD_ASR_MODEL_SHA256}" \
    MDX_DIAR_MODEL_DIR=/opt/models/ecapa \
    MDX_DIAR_MODEL_REPO="${MDX_DIAR_MODEL_REPO}" \
    MDX_DIAR_MODEL_REVISION="${MDX_DIAR_MODEL_REVISION}" \
    MDX_DIAR_MODEL_SHA256="${MDX_DIAR_MODEL_SHA256}" \
    MDX_DIAR_MEANVAR_SHA256="${MDX_DIAR_MEANVAR_SHA256}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python3.12", "-m", "uvicorn", "dictation_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

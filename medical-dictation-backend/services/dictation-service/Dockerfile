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

RUN --mount=type=secret,id=hf_token \
    set -eu; \
    if [ -f /run/secrets/hf_token ]; then HF_TOKEN="$(cat /run/secrets/hf_token)"; export HF_TOKEN; fi; \
    huggingface-cli download "$MD_ASR_MODEL_REPO" \
      --revision "$MD_ASR_MODEL_REVISION" \
      --local-dir /opt/models/whisper \
      --local-dir-use-symlinks False; \
    echo "${MD_ASR_MODEL_SHA256}  /opt/models/whisper/model.bin" | sha256sum -c -; \
    echo "model.bin checksum verified against pinned digest"

# ── Stage 2: runtime image ─────────────────────────────────────────────
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS runtime

ARG MD_ASR_MODEL_REPO=Systran/faster-whisper-large-v3
ARG MD_ASR_MODEL_REVISION=edaa852ec7e145841d8ffdb056a99866b5f0a478
ARG MD_ASR_MODEL_SHA256=69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1

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

# Bake the verified weights from the fetch stage.
COPY --from=model-fetch /opt/models/whisper /opt/models/whisper-large-v3

LABEL mdx.model.repo="${MD_ASR_MODEL_REPO}" \
      mdx.model.revision="${MD_ASR_MODEL_REVISION}" \
      mdx.model.sha256="${MD_ASR_MODEL_SHA256}"

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
    MD_ASR_MODEL_SHA256="${MD_ASR_MODEL_SHA256}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python3.12", "-m", "uvicorn", "dictation_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── GPU build for asr-worker (Whisper + CUDA) ──────────────────────────
# Uses nvidia/cuda runtime image because faster-whisper + cuDNN need both
# the CUDA runtime libs and a glibc base. Distroless is not viable here
# (driver/CUDA stack expects glibc). See ADR-0009 / ADR-0021.
#
# Build (HF token is a BuildKit secret — never baked into the image):
#   DOCKER_BUILDKIT=1 docker build \
#     --secret id=hf_token,env=HF_TOKEN \
#     -f services/asr-worker/Dockerfile -t mdx-asr-worker:gpu .
#
# The model is fetched at a PINNED commit revision and its weights are
# checksum-verified at build time; a SHA-256 mismatch FAILS THE BUILD
# (fail-closed). Runtime is fully offline (HF_HUB_OFFLINE=1) and loads the
# baked weights from /opt/models — no Hugging Face egress ever (ADR-0021).

# ── Stage 1: fetch + verify weights (build-time only) ──────────────────
FROM python:3.12-slim AS model-fetch

# Pinned source of truth. Override at build with --build-arg to re-pin.
# Values resolved from the HF API on 2026-06-10 (Sprint B1 Day 1).
ARG MD_ASR_MODEL_REPO=Systran/faster-whisper-large-v3
ARG MD_ASR_MODEL_REVISION=edaa852ec7e145841d8ffdb056a99866b5f0a478
ARG MD_ASR_MODEL_SHA256=69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1

RUN pip install --no-cache-dir "huggingface_hub[cli]==0.25.2"

# Download at the immutable revision, then verify model.bin's SHA-256
# against the pinned digest. HF_TOKEN is mounted as a build secret and
# never persists in any layer; the public Systran repo does not require it,
# but private mirrors do.
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

# Re-declare ARGs so they are visible to LABEL/ENV in this stage.
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
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

RUN python3.12 -m pip install --no-cache-dir --upgrade pip uv==0.4.29

WORKDIR /build
COPY pyproject.toml uv.lock /build/
COPY libs/ /build/libs/
COPY services/asr-worker/ /build/services/asr-worker/

RUN python3.12 -m uv pip install --system --no-cache \
    --target /deps \
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

# Self-describing image (AC-B1-1): a deployed image carries its provenance.
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
    # Cut the runtime cord to Hugging Face entirely (AC-B1-2).
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    # Load from the baked local dir instead of resolving a HF id at runtime.
    MD_ASR_MODEL=/opt/models/whisper-large-v3 \
    MD_ASR_MODEL_REPO="${MD_ASR_MODEL_REPO}" \
    MD_ASR_MODEL_REVISION="${MD_ASR_MODEL_REVISION}" \
    MD_ASR_MODEL_SHA256="${MD_ASR_MODEL_SHA256}"

USER nonroot
WORKDIR /app
CMD ["python3.12", "-m", "asr_worker.main"]

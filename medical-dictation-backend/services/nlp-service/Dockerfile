# ── nlp-service runtime image ──────────────────────────────────────
# CPU-only. The punctuation transformer is pinned to an immutable HF
# revision, checksum-verified, and BAKED at build time (Sprint B1 Day 2,
# ADR-0021). There is no first-request download and no Hugging Face egress
# at runtime (HF_HUB_OFFLINE=1) — the previous "download on first request"
# behaviour is removed.

# ── Stage 1: fetch + verify the punctuation weights (build-time only) ──
FROM python:3.12-slim AS model-fetch

# Pins resolved from the HF API on 2026-06-10. We verify model.safetensors
# (what transformers loads by preference) against its LFS SHA-256.
ARG MDX_NLP_PUNCTUATION_MODEL_REPO=oliverguhr/fullstop-punctuation-multilang-large
ARG MDX_NLP_PUNCTUATION_MODEL_REVISION=345e80adc07e761d3a35feafd20f2f44a151f453
ARG MDX_NLP_PUNCTUATION_MODEL_SHA256=270f27d7398a5fdad43bdf9953ea532fbe62c5f5227ed5f5316e9bd64a9255e1

RUN pip install --no-cache-dir "huggingface_hub[cli]==0.25.2"

# Fetch only what transformers needs (skip the duplicate onnx/, tf, and
# pytorch_model.bin payloads to keep the image lean), then fail-closed on a
# SHA-256 mismatch of the safetensors weights.
#
# The safetensors payload is ~2.2 GB and several images fetch models in
# parallel, so a mid-stream ChunkedEncodingError is a routine build failure
# on a home link. Retry: hf_hub resumes from the .incomplete file in this
# same layer, so each attempt continues where the last one broke. If every
# attempt fails the file is absent/short and sha256sum -c still fails closed.
ENV HF_HUB_DOWNLOAD_TIMEOUT=60
RUN --mount=type=secret,id=hf_token \
    set -eu; \
    if [ -f /run/secrets/hf_token ]; then HF_TOKEN="$(cat /run/secrets/hf_token)"; export HF_TOKEN; fi; \
    for attempt in 1 2 3 4 5; do \
      huggingface-cli download "$MDX_NLP_PUNCTUATION_MODEL_REPO" \
        --revision "$MDX_NLP_PUNCTUATION_MODEL_REVISION" \
        --local-dir /opt/models/punctuation \
        --local-dir-use-symlinks False \
        --max-workers 2 \
        config.json model.safetensors tokenizer.json tokenizer_config.json \
        sentencepiece.bpe.model special_tokens_map.json && break; \
      echo "punctuation download attempt ${attempt} failed; resuming in 10s"; \
      sleep 10; \
    done; \
    echo "${MDX_NLP_PUNCTUATION_MODEL_SHA256}  /opt/models/punctuation/model.safetensors" | sha256sum -c -; \
    echo "model.safetensors checksum verified against pinned digest"

# ── Stage 2: runtime image ─────────────────────────────────────────
FROM python:3.12-slim AS runtime

ARG MDX_NLP_PUNCTUATION_MODEL_REPO=oliverguhr/fullstop-punctuation-multilang-large
ARG MDX_NLP_PUNCTUATION_MODEL_REVISION=345e80adc07e761d3a35feafd20f2f44a151f453
ARG MDX_NLP_PUNCTUATION_MODEL_SHA256=270f27d7398a5fdad43bdf9953ea532fbe62c5f5227ed5f5316e9bd64a9255e1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

RUN pip install --no-cache-dir --upgrade pip uv==0.4.29

WORKDIR /build
COPY pyproject.toml uv.lock /build/
COPY libs/ /build/libs/
COPY services/nlp-service/ /build/services/nlp-service/

# Punctuation model is heavy; install transformers + torch via pip here
# rather than from the workspace pyproject (which keeps the workspace
# lockfile small).
# torch is a large wheel; bump uv's 30s default so slow links don't time out
ENV UV_HTTP_TIMEOUT=600
RUN uv pip install --system --no-cache \
    --target /deps \
    /build/services/nlp-service \
    /build/libs/observability \
    /build/libs/auth \
    /build/libs/audit \
    /build/libs/db \
    /build/libs/asr_models \
    /build/libs/secret

# This image is CPU-only (the punctuation model runs under torch.no_grad on
# CPU — nothing here ever touches a GPU), so torch comes from the PyTorch CPU
# index. The default PyPI torch drags in ~2.9 GB of nvidia-* CUDA wheels that
# are never loaded; besides bloating the image, that download is what broke
# builds mid-stream on a home link. unsafe-best-match lets everything except
# torch still resolve from PyPI.
#
# Retried like the model fetches above: uv has no internal retry for a
# connection dropped mid-body, and losing one here costs the whole build.
RUN for attempt in 1 2 3 4 5; do \
      uv pip install --system --no-cache \
        --target /deps \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        --index-strategy unsafe-best-match \
        "transformers>=4.40" \
        "torch>=2.2" && break; \
      echo "transformers/torch install attempt ${attempt} failed; retrying in 10s"; \
      sleep 10; \
      [ "${attempt}" = "5" ] && exit 1; \
    done

# Bake the verified punctuation weights from the fetch stage.
COPY --from=model-fetch /opt/models/punctuation /opt/models/punctuation

LABEL mdx.model.repo="${MDX_NLP_PUNCTUATION_MODEL_REPO}" \
      mdx.model.revision="${MDX_NLP_PUNCTUATION_MODEL_REVISION}" \
      mdx.model.sha256="${MDX_NLP_PUNCTUATION_MODEL_SHA256}"

# Editable workspace libs install as .pth files, which Python only honors
# in a site dir — not via PYTHONPATH. sitecustomize is auto-imported because
# /deps is on PYTHONPATH; it re-registers /deps as a site dir so the editable
# .pth files (asr_models, audit, observability, …) actually activate.
RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}" \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/hf-cache \
    MDX_NLP_PUNCTUATION_MODEL=/opt/models/punctuation \
    MDX_NLP_PUNCTUATION_MODEL_REPO="${MDX_NLP_PUNCTUATION_MODEL_REPO}" \
    MDX_NLP_PUNCTUATION_MODEL_REVISION="${MDX_NLP_PUNCTUATION_MODEL_REVISION}" \
    MDX_NLP_PUNCTUATION_MODEL_SHA256="${MDX_NLP_PUNCTUATION_MODEL_SHA256}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "nlp_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

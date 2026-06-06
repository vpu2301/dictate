# ── nlp-service runtime image ──────────────────────────────────────
# CPU-only. The punctuation transformer is large enough that we don't
# bundle weights in the image; the first request triggers download
# from HuggingFace cache (or readiness gates 503 while it loads).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/tmp/hf-cache \
    TRANSFORMERS_CACHE=/tmp/hf-cache

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
RUN uv pip install --system --no-cache --no-dev \
    --target /deps \
    /build/services/nlp-service \
    /build/libs/observability \
    /build/libs/auth \
    /build/libs/audit \
    /build/libs/db \
    /build/libs/asr_models \
    /build/libs/secret \
 && uv pip install --system --no-cache \
    --target /deps \
    "transformers>=4.40" \
    "torch>=2.2 ; platform_system != 'Darwin'"

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "nlp_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

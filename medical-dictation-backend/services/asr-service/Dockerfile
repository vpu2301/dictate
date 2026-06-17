# ── Stage 1: build deps into /deps ────────────────────────────────────
FROM python:3.12-slim AS build

WORKDIR /build
RUN pip install --no-cache-dir uv==0.4.29

COPY pyproject.toml uv.lock /build/
COPY libs/ /build/libs/
COPY services/asr-service/ /build/services/asr-service/

RUN uv pip install --system --no-cache \
    --target /deps \
    /build/services/asr-service \
    /build/libs/observability \
    /build/libs/auth \
    /build/libs/audit \
    /build/libs/db \
    /build/libs/messaging \
    /build/libs/crypto \
    /build/libs/storage \
    /build/libs/asr_models \
    /build/libs/secret

# ── Stage 2: distroless runtime ──────────────────────────────────────
# asr-service needs libmagic (for python-magic) + ffmpeg (for duration probe).
# Distroless doesn't ship those, so use python-slim as the final image and
# create a non-root user explicitly.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

WORKDIR /app

COPY --from=build /deps /deps
# The editable workspace libs install as redirect .pth files pointing at their
# source roots under /build (e.g. /build/libs/observability/src). Those roots
# must exist in the runtime image or the redirects dangle and imports fail with
# ModuleNotFoundError. Carry the source tree over (matches nlp-service).
COPY --from=build /build/libs /build/libs
COPY --from=build /build/services/asr-service /build/services/asr-service
# Editable workspace libs install as .pth files, which Python only honors
# in a site dir — not via PYTHONPATH. sitecustomize is auto-imported because
# /deps is on PYTHONPATH; it re-registers /deps as a site dir so the editable
# .pth files (asr_models, audit, observability, …) actually activate.
RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PYTHONUNBUFFERED=1 \
    PATH="/deps/bin:${PATH}"

USER nonroot
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "asr_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

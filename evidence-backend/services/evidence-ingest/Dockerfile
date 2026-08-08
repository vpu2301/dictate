# ── evidence-ingest runtime (EVA-S02) ──────────────────────────────
# Corpus ingestion: fetch -> scan -> parse -> chunk -> embed -> index.
# Serves BOTH the ops API and the queue worker; the worker overrides the
# command (see infra/compose/evidence.yaml).
#
# BUILD CONTEXT IS ~/Desktop/dictate (parent of both workspaces) — see the
# long explanation in services/evidence-answer/Dockerfile.
#
# `messaging` is in the install list here and nowhere else: ingest is the only
# evidence service that consumes a Redis stream.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# lxml wheels cover linux/arm64 and linux/amd64, so no libxml2 build deps are
# needed — but pypdf/python-docx parse untrusted uploads, which is why the
# ClamAV scan happens BEFORE the parse in the pipeline, not after.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

RUN pip install --no-cache-dir --upgrade pip uv==0.4.29

WORKDIR /build
COPY evidence-backend/pyproject.toml evidence-backend/uv.lock /build/evidence-backend/
COPY evidence-backend/libs/ /build/evidence-backend/libs/
COPY evidence-backend/services/evidence-ingest/ /build/evidence-backend/services/evidence-ingest/
COPY medical-dictation-backend/libs/ /build/medical-dictation-backend/libs/
# The platform's workspace root, required even though nothing here builds from
# it: libs/storage declares `crypto = {workspace = true}` (and crypto declares
# `secret`), and uv resolves `workspace = true` against the nearest
# tool.uv.workspace root. Without this file the build dies with
#   Failed to parse entry: `crypto`
#   Package is not included as workspace package in `tool.uv.workspace`
COPY medical-dictation-backend/pyproject.toml /build/medical-dictation-backend/

RUN uv pip install --system --no-cache \
    --target /deps \
    /build/evidence-backend/services/evidence-ingest \
    /build/evidence-backend/libs/evidence_models \
    /build/evidence-backend/libs/evidence_safety \
    /build/evidence-backend/libs/models \
    /build/medical-dictation-backend/libs/observability \
    /build/medical-dictation-backend/libs/auth \
    /build/medical-dictation-backend/libs/audit \
    /build/medical-dictation-backend/libs/db \
    /build/medical-dictation-backend/libs/storage \
    /build/medical-dictation-backend/libs/crypto \
    /build/medical-dictation-backend/libs/secret \
    /build/medical-dictation-backend/libs/messaging

RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "evidence_ingest.main:app", "--host", "0.0.0.0", "--port", "8000"]

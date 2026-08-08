# ── evidence-answer runtime (EVA-S04) ──────────────────────────────
# Quick Search question engine. Follows the platform's paved-road pattern
# (services/generation-service/Dockerfile there) with ONE structural
# difference forced by ADR-0001:
#
#   THE BUILD CONTEXT IS ~/Desktop/dictate — the parent of BOTH workspaces,
#   not evidence-backend.
#
# Evidence consumes the platform libs as path dependencies pointing at
# ../medical-dictation-backend/libs/* (root pyproject `tool.uv.sources`).
# Those live outside evidence-backend, so a context rooted here cannot see
# them and `uv pip install` silently falls back to resolving `db`, `auth`,
# `audit`… from PyPI — which is how you get an image that builds clean and
# imports the wrong `auth` at runtime. infra/compose/evidence.yaml sets the
# context accordingly; building by hand needs the same:
#
#   docker build -f evidence-backend/services/evidence-answer/Dockerfile \
#     -t evidence-answer ~/Desktop/dictate
#
# Every internal dependency is therefore listed explicitly below, including
# the transitive ones (storage -> crypto -> secret): anything omitted is not
# a build error, it is a PyPI fetch.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

RUN pip install --no-cache-dir --upgrade pip uv==0.4.29

WORKDIR /build
COPY evidence-backend/pyproject.toml evidence-backend/uv.lock /build/evidence-backend/
COPY evidence-backend/libs/ /build/evidence-backend/libs/
COPY evidence-backend/services/evidence-answer/ /build/evidence-backend/services/evidence-answer/
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
    /build/evidence-backend/services/evidence-answer \
    /build/evidence-backend/libs/evidence_models \
    /build/evidence-backend/libs/evidence_safety \
    /build/evidence-backend/libs/models \
    /build/medical-dictation-backend/libs/observability \
    /build/medical-dictation-backend/libs/auth \
    /build/medical-dictation-backend/libs/audit \
    /build/medical-dictation-backend/libs/db \
    /build/medical-dictation-backend/libs/storage \
    /build/medical-dictation-backend/libs/crypto \
    /build/medical-dictation-backend/libs/secret

# Editable path installs land as .pth files that only activate if site
# processes /deps as a site dir — PYTHONPATH alone does not run them, and the
# service dies with ModuleNotFoundError on a workspace lib (see the platform
# CLAUDE.md "New service Dockerfile" note).
RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "evidence_answer.main:app", "--host", "0.0.0.0", "--port", "8000"]

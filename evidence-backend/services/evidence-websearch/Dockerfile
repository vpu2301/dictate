# ── evidence-websearch runtime (EVA-S04) ───────────────────────────
# Web agent: metasearch -> allowlist -> fetch -> extract -> screen.
#
# BUILD CONTEXT IS ~/Desktop/dictate (parent of both workspaces) — see the
# long explanation in services/evidence-answer/Dockerfile. Every internal
# dependency is listed explicitly, transitive ones included.
#
# This service has no direct network path by design (ADR-0005): all egress
# goes through the squid proxy at EVA_EGRESS_PROXY_URL, and with that unset
# the HTTP client is pointed at a discard port so fetches fail closed. Which
# is exactly why running it in compose — where the proxy is reachable — is
# the only way to exercise fetching for real.
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
COPY evidence-backend/services/evidence-websearch/ /build/evidence-backend/services/evidence-websearch/
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
    /build/evidence-backend/services/evidence-websearch \
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

RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "evidence_websearch.main:app", "--host", "0.0.0.0", "--port", "8000"]

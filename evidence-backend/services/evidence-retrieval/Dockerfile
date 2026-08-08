# ── evidence-retrieval runtime (EVA-S03) ───────────────────────────
# Hybrid retrieval: dense + lexical -> RRF -> cross-encoder rerank.
#
# BUILD CONTEXT IS ~/Desktop/dictate (parent of both workspaces) — see the
# long explanation in services/evidence-answer/Dockerfile.
#
# Smallest dependency set of the five: /retrieve is an internal hop, so this
# service carries no auth/audit/storage — it never sees a user token and
# writes nothing. Reranking is served by the gateway, not loaded here.
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
COPY evidence-backend/services/evidence-retrieval/ /build/evidence-backend/services/evidence-retrieval/
COPY medical-dictation-backend/libs/ /build/medical-dictation-backend/libs/
# The platform's workspace root. This service's lib set does not strictly need
# it today — it takes neither storage nor crypto — but any platform lib that
# declares `{workspace = true}` on another (storage -> crypto -> secret) fails
# to parse without it, so it is copied everywhere rather than left as a trap
# for whoever adds the first such dependency here.
COPY medical-dictation-backend/pyproject.toml /build/medical-dictation-backend/

RUN uv pip install --system --no-cache \
    --target /deps \
    /build/evidence-backend/services/evidence-retrieval \
    /build/evidence-backend/libs/evidence_models \
    /build/evidence-backend/libs/models \
    /build/medical-dictation-backend/libs/observability \
    /build/medical-dictation-backend/libs/db

RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "evidence_retrieval.main:app", "--host", "0.0.0.0", "--port", "8000"]

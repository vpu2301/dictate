# ── evidence-model-gateway runtime (EVA-S00/S02/S03/S04) ───────────
# The single seam every model call goes through (rule E12): embed.dense,
# rerank, and the two generator.* roles.
#
# BUILD CONTEXT IS ~/Desktop/dictate (parent of both workspaces) — see the
# long explanation in services/evidence-answer/Dockerfile. This service's
# only internal dependency is `observability`; it deliberately touches no DB,
# no auth and no object store.
#
# TWO THINGS MAKE THIS IMAGE UNLIKE THE OTHER FOUR:
#
# 1. sentence-transformers pulls in torch. The default PyPI wheel bundles the
#    CUDA runtime (~2.5 GB of image for nothing on a CPU dev box), so the
#    CPU-only index is pinned first and torch is installed from it up front.
#    The rig image overrides this with a CUDA build.
# 2. Weights are NOT baked. First start downloads BGE-M3 (~2.3 GB) and only
#    serves /readyz once the model loads and passes the dimension check — so
#    a cold container looks hung for several minutes and is not. For an
#    offline image, bake the weights and point EVA_GATEWAY_EMBED_MODEL_DIR /
#    _RERANK_MODEL_DIR at them: a local dir beats the hub id precisely so
#    nothing resolves at runtime (rule BE6).
#
# The generator.* roles are separate processes and are NOT in this image
# (ADR-0006) — generation and embedding have different memory and batching
# profiles, and coupling their lifecycles makes either restart the other's
# outage. With generation enabled and no generator reachable, /readyz is
# honestly 503; set EVA_GATEWAY_GENERATION_ENABLED=false for an
# embed/rerank-only gateway.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

RUN pip install --no-cache-dir --upgrade pip uv==0.4.29

WORKDIR /build
COPY evidence-backend/pyproject.toml evidence-backend/uv.lock /build/evidence-backend/
COPY evidence-backend/services/evidence-model-gateway/ /build/evidence-backend/services/evidence-model-gateway/
COPY medical-dictation-backend/libs/ /build/medical-dictation-backend/libs/
# The platform's workspace root. Not strictly needed for `observability`
# alone, but any platform lib declaring `{workspace = true}` on another
# (storage -> crypto -> secret) fails to parse without it — copied everywhere
# rather than left as a trap for the next dependency added here.
COPY medical-dictation-backend/pyproject.toml /build/medical-dictation-backend/

RUN uv pip install --system --no-cache --target /deps \
      --index-url https://download.pytorch.org/whl/cpu \
      torch

RUN uv pip install --system --no-cache \
    --target /deps \
    /build/evidence-backend/services/evidence-model-gateway \
    /build/medical-dictation-backend/libs/observability

RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

# HF_HOME must be writable by nonroot or the first weight download fails with
# a permission error that reads like a network problem.
RUN mkdir -p /models && chown 65532:65532 /models

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "evidence_model_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]

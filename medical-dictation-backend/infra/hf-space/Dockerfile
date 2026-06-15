# ── HF Spaces "all-in-one" image ──────────────────────────────────────
# Sprint 07: bundles the slimmed stack into a single container for
# Hugging Face Spaces. ADR-0017 documents why this is acceptable for
# an ephemeral demo and forbidden in production.
#
# Auth model: real Keycloak (sprint-02 realm) baked into the container.
# Access to the Space itself is gated by HF (private repo + HF API
# token); inside the Space, login uses a seeded test clinician.
#
# Stages:
#   1. wheels     — build deps for every Python service.
#   2. runtime    — CUDA + Postgres 16 + Redis + Keycloak + nginx + supervisord.
#
# Privacy contract (ADR-0019):
#   - /run/dictation mounted as tmpfs (no audio on disk).
#   - MD_OBJECT_STORE_DISABLED=true (no MinIO; sprint-03 EncryptedObjectStore.put refuses).
#   - DEMO_AUDIO_PURGE_ON_FINALIZE=true (sprint-04 finalize cleans tmpfs immediately).
#   - Postgres data_directory is tmpfs (ephemeral).

# ── Stage 1: build deps ────────────────────────────────────────────────
FROM python:3.12-slim AS wheels

WORKDIR /build
COPY pyproject.toml uv.lock /build/
COPY libs/ /build/libs/
COPY services/auth-service/ /build/services/auth-service/
COPY services/asr-service/ /build/services/asr-service/
COPY services/asr-worker/ /build/services/asr-worker/
COPY services/dictation-service/ /build/services/dictation-service/
COPY services/nlp-service/ /build/services/nlp-service/
COPY services/report-service/ /build/services/report-service/

RUN pip install --no-cache-dir --upgrade pip uv==0.4.29 \
 && uv pip install --system --no-cache \
    --target /deps \
    /build/services/auth-service \
    /build/services/asr-service \
    /build/services/asr-worker \
    /build/services/dictation-service \
    /build/services/nlp-service \
    /build/services/report-service \
    /build/libs/observability \
    /build/libs/auth \
    /build/libs/audit \
    /build/libs/db \
    /build/libs/messaging \
    /build/libs/crypto \
    /build/libs/storage \
    /build/libs/asr_models \
    /build/libs/template_models \
    /build/libs/secret

# ── Stage 2: GPU runtime ──────────────────────────────────────────────
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/tmp/hf-cache \
    PGDATA=/run/pgdata \
    KEYCLOAK_VERSION=24.0.5

# System deps: Python + Postgres + Redis + Java (Keycloak) + nginx + supervisord.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3.12 python3-pip \
        postgresql-16 postgresql-client-16 \
        redis-server \
        openjdk-21-jre-headless \
        nginx \
        supervisor \
        ffmpeg libsndfile1 libopus0 \
        ca-certificates curl wget unzip \
 && rm -rf /var/lib/apt/lists/*

# Download + install Keycloak.
RUN wget -q "https://github.com/keycloak/keycloak/releases/download/${KEYCLOAK_VERSION}/keycloak-${KEYCLOAK_VERSION}.tar.gz" -O /tmp/keycloak.tar.gz \
 && tar -xzf /tmp/keycloak.tar.gz -C /opt/ \
 && mv /opt/keycloak-${KEYCLOAK_VERSION} /opt/keycloak \
 && rm /tmp/keycloak.tar.gz \
 # Pre-build Keycloak so first-run is fast.
 && /opt/keycloak/bin/kc.sh build --db=postgres

COPY --from=wheels /deps /deps
# Editable workspace libs install as .pth files, which Python only honors
# in a site dir — not via PYTHONPATH. sitecustomize is auto-imported because
# /deps is on PYTHONPATH; it re-registers /deps as a site dir so the editable
# .pth files (asr_models, audit, observability, …) actually activate.
RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps PATH="/deps/bin:${PATH}"

# Compose runtime config.
COPY infra/hf-space/supervisord.conf /etc/supervisor/conf.d/medical-dictation.conf
COPY infra/hf-space/nginx.conf /etc/nginx/sites-available/default
COPY infra/hf-space/start.sh /usr/local/bin/start.sh
COPY infra/hf-space/init-db.sh /usr/local/bin/init-db.sh
COPY infra/postgres/init.sql /opt/init.sql
COPY infra/postgres/migrations /opt/migrations
COPY infra/postgres/seed /opt/seed
COPY infra/seeds/templates /opt/templates
COPY infra/keycloak/realm-export.json /opt/keycloak-realm.json

RUN chmod +x /usr/local/bin/start.sh /usr/local/bin/init-db.sh \
 && mkdir -p /run/dictation /run/pgdata /var/lib/redis /var/log/medical-dictation \
 && chmod 0700 /run/dictation \
 && chmod -R 0775 /run/pgdata /var/lib/redis /var/log/medical-dictation

EXPOSE 7860

# Privacy + demo envvars locked at image build time so a runtime
# override cannot accidentally relax them.
ENV MD_OBJECT_STORE_DISABLED=true \
    DEMO_AUDIO_PURGE_ON_FINALIZE=true \
    MDX_REQUIRE_MFA=false \
    MDX_TMPFS_ROOT=/run/dictation \
    KC_DB=postgres \
    KC_DB_URL=jdbc:postgresql://127.0.0.1:5432/keycloak \
    KC_DB_USERNAME=keycloak \
    KC_DB_PASSWORD=keycloak \
    KC_HTTP_RELATIVE_PATH=/auth \
    KC_HOSTNAME_STRICT=false \
    KC_HOSTNAME_STRICT_HTTPS=false \
    KEYCLOAK_ADMIN=admin \
    KEYCLOAK_ADMIN_PASSWORD=admin

CMD ["/usr/local/bin/start.sh"]

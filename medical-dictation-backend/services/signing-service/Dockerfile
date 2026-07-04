# ── signing-service runtime ────────────────────────────────────────
# Mirrors the report-service paved-road pattern (see CLAUDE.md
# "New service Dockerfile"). Pulls in libs/kep (package `medical_kep`)
# for the КЕП/KEP providers + PAdES signing.
# ── UAPKI stage — DSTU file-key signing backend (ADR-0026) ─────────
# Pinned release + checksum; the four shared objects land in /opt/uapki.
# amd64-only prebuilt: on other arches the stage produces an empty dir
# and FileKeyProvider simply isn't wired (registry checks the lib dir).
FROM debian:bookworm-slim AS uapki
ARG UAPKI_VERSION=2.0.12
ARG UAPKI_SHA256=ac0185c121e38ab9d66789ebad4154359b9283d98c6eb1502083e56553981217
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/uapki \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         curl -fsSL -o /tmp/uapki.tar.gz \
           "https://github.com/specinfo-ua/UAPKI/releases/download/v${UAPKI_VERSION}/uapki-v${UAPKI_VERSION}-linux-amd64.tar.gz" \
         && echo "${UAPKI_SHA256}  /tmp/uapki.tar.gz" | sha256sum -c - \
         && tar xzf /tmp/uapki.tar.gz --strip-components=1 -C /opt/uapki \
         && rm /tmp/uapki.tar.gz; \
       else \
         echo "non-amd64 build: skipping UAPKI (file_key provider disabled)"; \
       fi

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libcurl4 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --shell /sbin/nologin --no-create-home nonroot

# UAPKI shared objects (libcurl4 above is their only runtime dep).
COPY --from=uapki /opt/uapki /opt/uapki
ENV UAPKI_LIB_DIR=/opt/uapki \
    LD_LIBRARY_PATH=/opt/uapki

RUN pip install --no-cache-dir --upgrade pip uv==0.4.29

WORKDIR /build
COPY pyproject.toml uv.lock /build/
COPY libs/ /build/libs/
COPY services/signing-service/ /build/services/signing-service/

RUN uv pip install --system --no-cache \
    --target /deps \
    /build/services/signing-service \
    /build/libs/observability \
    /build/libs/auth \
    /build/libs/audit \
    /build/libs/db \
    /build/libs/kep \
    /build/libs/secret

RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "signing_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

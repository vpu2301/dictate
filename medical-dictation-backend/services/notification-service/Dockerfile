# ── notification-service runtime ───────────────────────────────────
# Mirrors the autocomplete-service paved-road pattern (see CLAUDE.md
# "New service Dockerfile" for why sitecustomize re-adds /deps).
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
COPY pyproject.toml uv.lock /build/
COPY libs/ /build/libs/
COPY services/notification-service/ /build/services/notification-service/

# Every workspace lib is listed explicitly: they install as editable
# .pth entries, which only activate via the sitecustomize below.
RUN uv pip install --system --no-cache \
    --target /deps \
    /build/services/notification-service \
    /build/libs/observability \
    /build/libs/auth \
    /build/libs/audit \
    /build/libs/db \
    /build/libs/secret \
    /build/libs/messaging \
    /build/libs/notification_events

RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps \
    PATH="/deps/bin:${PATH}"

USER nonroot
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "notification_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

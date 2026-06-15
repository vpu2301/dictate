# ── Stage 1: build dependencies into a target directory ──────────────────────
FROM python:3.12-slim AS build

WORKDIR /build

RUN pip install --no-cache-dir uv==0.4.29

COPY pyproject.toml .
# Install production deps only into /deps so we can copy them cleanly
RUN uv pip install --system --no-cache \
    --target /deps \
    .

COPY src/ /app/

# ── Stage 2: distroless final image (non-root) ────────────────────────────────
FROM gcr.io/distroless/python3-debian12:nonroot

WORKDIR /app

# Copy installed packages and application source
COPY --from=build /deps /deps
COPY --from=build /app /app

# Editable workspace libs install as .pth files, which Python only honors
# in a site dir — not via PYTHONPATH. sitecustomize is auto-imported because
# /deps is on PYTHONPATH; it re-registers /deps as a site dir so the editable
# .pth files (asr_models, audit, observability, …) actually activate.
RUN printf 'import site\nsite.addsitedir("/deps")\n' > /deps/sitecustomize.py

ENV PYTHONPATH=/deps
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# distroless ENTRYPOINT is /usr/bin/python3; CMD provides arguments
CMD ["-m", "uvicorn", "template_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

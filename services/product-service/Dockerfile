# Dockerfile for product-service (generated from infra/docker/service.Dockerfile.template).
# Regenerate with: .\scripts\generate-dockerfiles.ps1

# Template for the Python services.
#
# Each service has its own Dockerfile generated from this so that it builds
# independently -- no shared base image to build first, which keeps CI simple
# and means one service's build cannot break another's. The cost is that the
# common layers are repeated; the benefit is that `docker build` on any single
# service just works.
#
# Placeholders: product-service (directory name), 8001 (listen port).

# ---------------------------------------------------------------------------
# Stage 1: build wheels.
#
# Compiling happens here, in a stage that is thrown away. The runtime image
# never contains a compiler, which removes a large amount of attack surface
# and a lot of megabytes.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependencies are copied and installed *before* the application code. Docker
# caches layers, so editing a source file does not invalidate the (slow)
# dependency install -- only the (fast) code copy below it.
COPY libs/retailpulse_common /build/libs/retailpulse_common
COPY services/product-service/requirements.txt /build/requirements.txt

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir -r /build/requirements.txt \
    && pip install --no-cache-dir /build/libs/retailpulse_common

# ---------------------------------------------------------------------------
# Stage 2: runtime.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is needed by the container healthcheck below. Nothing else is added.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    # A dedicated non-root user. Running as root means a container escape
    # starts with root on the host, and it is one line to avoid.
    && useradd --create-home --uid 10001 retailpulse

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=retailpulse:retailpulse services/product-service/app /app/app
COPY --chown=retailpulse:retailpulse services/product-service/alembic* /app/

USER retailpulse

EXPOSE 8001

# Liveness only -- it must not check dependencies. If it did, a Redis blip
# would make Docker restart every container, turning a degraded dependency
# into a full outage.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8001/health || exit 1

# Exec form, not shell form. Shell form runs the process under /bin/sh, which
# does not forward SIGTERM -- so the app never gets to shut down cleanly and
# Docker kills it after the grace period. That matters here because the
# consumers need SIGTERM to finish the message they are holding.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]

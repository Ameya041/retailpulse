# One-shot migration runner.
#
# Migrations deliberately do NOT run at application startup. With N replicas
# that means N processes racing to apply the same DDL: at best one wins and the
# rest error, at worst two partially apply and leave the schema in a state
# nobody designed. A job that must run exactly once should not live in a
# process that runs N times.
#
# This container applies every service's migrations in turn and exits. Compose
# and Kubernetes both wait for it to succeed before starting the services.

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY libs/retailpulse_common /build/libs/retailpulse_common

# Only what migrations need -- no web framework, no ML stack.
RUN pip install --no-cache-dir \
        "sqlalchemy>=2.0" \
        "alembic>=1.13" \
        "psycopg[binary]>=3.1" \
        "pydantic>=2.6" \
        "pydantic-settings>=2.2" \
        "pyjwt>=2.8" \
        "passlib[bcrypt]>=1.7.4" \
        "bcrypt<4.1" \
        "fastapi>=0.110" \
        "prometheus-client>=0.20" \
        "python-json-logger>=2.0" \
    && pip install --no-cache-dir /build/libs/retailpulse_common

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 retailpulse

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Every service that owns a database. The gateway and ml-service are absent
# because they own none.
COPY --chown=retailpulse:retailpulse services/product-service /app/product-service
COPY --chown=retailpulse:retailpulse services/inventory-service /app/inventory-service
COPY --chown=retailpulse:retailpulse services/order-service /app/order-service
COPY --chown=retailpulse:retailpulse services/user-service /app/user-service
COPY --chown=retailpulse:retailpulse services/payment-service /app/payment-service
COPY --chown=retailpulse:retailpulse services/fulfilment-service /app/fulfilment-service
COPY --chown=retailpulse:retailpulse services/analytics-service /app/analytics-service
COPY --chown=retailpulse:retailpulse infra/docker/run-migrations.sh /app/run-migrations.sh

USER retailpulse

CMD ["/app/run-migrations.sh"]

# syntax=docker/dockerfile:1.7

# --- 1. Builder: install dependencies into a uv-managed venv ---
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv is the canonical Python tool for this project (per CLAUDE.md).
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install the runtime dependencies into /opt/venv first so this layer caches
# until pyproject.toml / uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now copy the source and install the project itself.
COPY main.py ./
COPY src ./src
RUN uv sync --frozen --no-dev

# --- 2. Runtime: bring just the venv + source over ---
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root user.
RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

# librdkafka runtime for confluent-kafka. The wheel bundles it on most
# platforms but pulling the apt package guarantees it works on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends librdkafka1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --from=builder --chown=app:app /app/main.py /app/main.py
COPY --from=builder --chown=app:app /app/src /app/src
# scripts/ is needed by tests + data-init reuses some SQL paths; keep it.
COPY --chown=app:app scripts /app/scripts

USER app

EXPOSE 8000

# Compose-friendly defaults; overrideable.
ENV KAFKA_BOOTSTRAP_SERVERS=kafka:29092 \
    KAFKA_CONSUMER_GROUP=multiagent-service-group \
    KAFKA_SUBSCRIBE_TOPICS=chat.request,route.request,geocode.request \
    DATABASE_URL=postgresql+asyncpg://admin:secret@timescaledb:5432/traffic_data \
    REDIS_URL=redis://redis:6379

CMD ["python", "main.py"]

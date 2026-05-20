# syntax=docker/dockerfile:1.7
#
# One-shot tooling image. Brings up the toolchain (osm2pgsql + postgres client
# + uv-managed Python venv) needed to seed an empty TimescaleDB with the OSM
# road graph and Taipei VD/parking static data.
#
# Build context expected: repo root.
#   docker build -f infra/data-init/Dockerfile -t stm-data-init .

FROM debian:bookworm-slim AS data-init

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/multiagent-venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        osm2pgsql \
        postgresql-client \
        curl \
        ca-certificates \
        bash \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# uv for repeatable Python installs.
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

WORKDIR /app/multiagent

# Install multiagent runtime deps so seed_vd_static.py can `import src.*`.
COPY backend/multiagent-service/pyproject.toml backend/multiagent-service/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Bring in the source the script needs and install the project itself.
COPY backend/multiagent-service/main.py ./
COPY backend/multiagent-service/src ./src
RUN uv sync --frozen --no-dev

# Import SQL + style files + Python seeders live at repo /scripts.
COPY scripts /app/scripts

COPY infra/data-init/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Cached OSM downloads + intermediates land here. Compose mounts a volume.
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]

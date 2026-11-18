# One-shot tooling image. Brings up the toolchain (osm2pgsql + postgres client
# + a small uv-managed Python venv) needed to seed an empty TimescaleDB with
# the OSM road graph and Taipei VD static data.
#
# Build context expected: repo root.
#   docker build -f infra/data-init/Dockerfile -t stm-data-init .
#
# Kept deliberately small: the VD seeder (scripts/seed_vd_static.py) only needs
# the four packages below, so we no longer install the whole multiagent-service
# dependency tree (openai, scipy, mcp, ...) just to run one script.

FROM python:3.12-slim-bookworm AS data-init

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/seed-venv \
    PATH=/opt/seed-venv/bin:$PATH

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        osm2pgsql \
        osmium-tool \
        postgresql-client \
        curl \
        ca-certificates

COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /usr/local/bin/

# Versions pinned to backend/multiagent-service/uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/seed-venv --python /usr/local/bin/python3.12 \
    && uv pip install \
        httpx==0.28.1 \
        asyncpg==0.31.0 \
        sqlalchemy==2.0.48 \
        geoalchemy2==0.19.0

# Import SQL + style files + Python seeders live at repo /scripts.
COPY scripts /app/scripts

# Optional pre-built DB snapshot (see infra/data-init/export-snapshot.sh).
# When present, the entrypoint restores it in seconds instead of running the
# full OSM pipeline. The directory always exists (README.md keeps COPY happy).
COPY infra/data-init/seed /app/seed

COPY infra/data-init/entrypoint.sh /entrypoint.sh
# Strip CRLF in case the repo was checked out on Windows with autocrlf=true.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Cached OSM downloads + intermediates land here. Compose mounts a volume.
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]

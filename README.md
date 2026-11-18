# Smart Traffic System

End-to-end demo stack for a Taipei-area smart traffic navigation system.

## Components

- **`backend/main-service/`** — Spring Boot 4.0 (Kotlin). Exposes the public
  REST API and bridges to multiagent via Kafka.
- **`backend/multiagent-service/`** — FastAPI + custom Kafka consumer (Python /
  `uv`). Hosts the routing agent, chat agent, geocoding agent, and the
  TDX/VD/parking ingestion loops.
- **`frontend/`** — Vite + React 19 + TypeScript + Tailwind v3 + Leaflet
  single-page demo app.
- **`infra/`** — `docker-compose.yml` for TimescaleDB, Kafka, ZooKeeper, plus
  Dockerfiles for the three application services and a one-shot `data-init`
  container that seeds the Taipei road graph.

## Quick start (Docker — one command)

For running on a fresh machine. Builds all three application images, brings
up the infra, seeds the Taipei road graph, and serves the SPA on
<http://localhost:5173>.

`data-init` restores the committed snapshot
(`infra/data-init/seed/taipei-graph.dump`) in a few seconds. If the graph SQL,
`osm2pgsql.style` or bbox changed since the snapshot was exported (or you set
`FORCE_REBUILD=1`), it falls back to the full OSM pipeline: a ~320 MB
Geofabrik download on first run, then about a minute of processing. After
changing the graph SQL, refresh the snapshot with
`bash infra/data-init/export-snapshot.sh` and commit it.

```powershell
# 1. Copy env template and fill in your DeepSeek key (TDX is optional).
Copy-Item .env.example .env
notepad .env

# 2. Bring everything up.
docker compose -f infra/docker-compose.yml --env-file .env up -d --build
```

Wait for `data-init` to exit successfully — track it with:

```powershell
docker logs -f stm_data_init
```

Then open <http://localhost:5173>.

| Port | Service | Purpose |
|---|---|---|
| 5173 | frontend (nginx) | SPA + reverse-proxies `/api/*` to main-service |
| 8081 | main-service | REST API (also reachable through the SPA proxy) |
| 8090 | kafka-ui | Optional Kafka inspector |
| 5432 | timescaledb | DB (exposed for local tooling) |
| 6379 | redis | Cache |
| 9092 | kafka | External listener |

Tear down: `docker compose -f infra/docker-compose.yml down` (add `-v` to
also wipe the seeded DB and OSM cache).

## Local development

### 1. Infra

```powershell
docker compose -f infra/docker-compose.yml up -d timescaledb kafka zookeeper
```

### 2. main-service (Kotlin)

```powershell
Set-Location backend\main-service
.\gradlew.bat bootRun
# Listens on :8081 (8080 is reserved on Windows hosts that run EDB / PEM Apache)
```

### 3. multiagent-service (Python, via uv)

```powershell
Set-Location backend\multiagent-service
uv sync
uv run python main.py
# FastAPI on :8000; Kafka consumer threads start in the lifespan hook.
```

### 4. frontend (Vite dev server)

```powershell
Set-Location frontend
npm install            # first time only
npm run dev
# Vite dev server on :5173
# /api/* is proxied to http://localhost:8081 — no CORS config needed.
```

Open <http://localhost:5173>.

### Tests

| Service | Command |
|---|---|
| multiagent | `Set-Location backend\multiagent-service; uv run pytest` |
| main-service | `Set-Location backend\main-service; .\gradlew.bat test` |
| frontend | `Set-Location frontend; npm test` (Vitest, run-once) |

### Type / build checks (frontend)

```powershell
Set-Location frontend
npx tsc --noEmit       # type check only
npm run build          # full production build → dist/
```

## OpenSpec workflow

Active changes live under `openspec/changes/<change-name>/`. Use the
`/opsx:*` skills to propose, continue, apply, and archive changes — see
`openspec/AGENTS.md` for details.

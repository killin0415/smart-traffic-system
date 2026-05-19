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
- **`infra/`** — `docker-compose.yml` for TimescaleDB, Kafka, ZooKeeper.

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

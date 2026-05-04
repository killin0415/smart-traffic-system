"""
Multiagent Service - Main Entry Point
Runs FastAPI (HTTP) + Kafka consumer concurrently.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
import uvicorn

_env_path = Path(__file__).resolve().parents[2] / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from src.agents.routing import RoadGraph
from src.agents.traffic import run_periodic_refresh
from src.db import async_session, get_session
from src.db.seed import seed_road_network
from src.db.speed_camera import seed_speed_cameras
from src.db.vd_sensor import seed_vd_sensors
from src.kafka import runtime as kafka_runtime
from src.kafka.consumer import start_kafka_consumer


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler.
    Seeds data, loads the in-memory RoadGraph, and starts the Kafka consumer.
    """
    # --- Startup ---
    print("=" * 50)
    print("[Multiagent Service] Starting up...")
    print("=" * 50)

    # Seed road network + speed cameras if DB is empty
    async for session in get_session():
        await seed_road_network(session)
        await seed_speed_cameras(session)
        await seed_vd_sensors(session)

    # Load in-memory RoadGraph for A* routing
    async with async_session() as session:
        graph = await RoadGraph.from_db(session)

    # Share runtime with Kafka consumer thread (graph + event loop + session factory)
    kafka_runtime.set_runtime(
        graph=graph,
        loop=asyncio.get_running_loop(),
        session_factory=async_session,
    )

    # Start Kafka consumer as a background task
    kafka_task = asyncio.create_task(start_kafka_consumer())

    # Periodic TDX Live refresher (best-effort — missing creds degrades gracefully)
    traffic_task = asyncio.create_task(run_periodic_refresh(graph, async_session))

    print("[Multiagent Service] All components started successfully!")

    yield  # App is running

    # --- Shutdown ---
    print("[Multiagent Service] Shutting down...")
    for task in (kafka_task, traffic_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    print("[Multiagent Service] Shutdown complete.")


app = FastAPI(
    title="Smart Traffic Multiagent Service",
    description="Multi-Agent AI service for intelligent traffic navigation",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """HTTP health check endpoint."""
    return {"status": "healthy", "service": "multiagent-service"}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )

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

from src.agents.chat_agent import build_chat_agent_from_env
from src.agents.parking import run_periodic_parking_refresh
from src.agents.routing import RoadGraph
from src.agents.vd_traffic import run_periodic_vd_refresh
from src.agents.weight_provider import TaipeiWeightProvider
from src.db import async_session, get_session
from src.db.models import VDStatic
from src.db.seed_taipei import seed_parking_lots
from src.db.speed_camera import seed_speed_cameras
from src.kafka import runtime as kafka_runtime
from src.kafka.consumer import start_kafka_consumer
from sqlalchemy import func, select


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

    # Seed static data (road network is built offline via scripts/import_taipei_osm.sh
    # + scripts/build_graph_from_osm.sql; lifespan only checks + warns if missing).
    async for session in get_session():
        await seed_speed_cameras(session)
        vd_count = (
            await session.execute(select(func.count()).select_from(VDStatic))
        ).scalar_one()
        if vd_count == 0:
            print(
                "[Multiagent Service] WARNING: vd_static is empty. "
                "Run `uv run --script scripts/seed_vd_static.py` before "
                "expecting live VD weights to work."
            )
        await seed_parking_lots(session)

    # Load in-memory RoadGraph for A* routing
    async with async_session() as session:
        graph = await RoadGraph.from_db(session)

    # Build initial dynamic weights from latest VD readings.
    weight_provider = TaipeiWeightProvider()
    await weight_provider.rebuild(async_session)
    weight_provider.apply_to_graph(graph)

    # Build chat agent (graceful no-op when LLM API key missing).
    chat_agent = build_chat_agent_from_env()

    # Share runtime with Kafka consumer thread.
    kafka_runtime.set_runtime(
        graph=graph,
        loop=asyncio.get_running_loop(),
        session_factory=async_session,
        chat_agent=chat_agent,
    )
    kafka_runtime.set_weight_provider(weight_provider)

    # Start Kafka consumer as a background task
    kafka_task = asyncio.create_task(start_kafka_consumer())

    # Periodic refreshers
    vd_task = asyncio.create_task(
        run_periodic_vd_refresh(graph, weight_provider, async_session)
    )
    parking_task = asyncio.create_task(run_periodic_parking_refresh(async_session))

    print("[Multiagent Service] All components started successfully!")

    yield  # App is running

    # --- Shutdown ---
    print("[Multiagent Service] Shutting down...")
    for task in (kafka_task, vd_task, parking_task):
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

"""
Multiagent Service - Main Entry Point
Runs FastAPI (HTTP) + Kafka consumer concurrently.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from src.kafka.consumer import start_kafka_consumer


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler.
    Starts the Kafka consumer alongside the HTTP server.
    """
    # --- Startup ---
    print("=" * 50)
    print("[Multiagent Service] Starting up...")
    print("=" * 50)

    # Start Kafka consumer as a background task
    kafka_task = asyncio.create_task(start_kafka_consumer())

    print("[Multiagent Service] All components started successfully!")

    yield  # App is running

    # --- Shutdown ---
    print("[Multiagent Service] Shutting down...")
    kafka_task.cancel()
    try:
        await kafka_task
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

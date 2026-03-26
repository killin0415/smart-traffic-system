"""
Agent Service - Main Entry Point
Runs FastAPI (HTTP) + gRPC server + Kafka consumer concurrently.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from src.grpc_server.server import start_grpc_server
from src.kafka.consumer import start_kafka_consumer


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler.
    Starts the gRPC server and Kafka consumer alongside the HTTP server.
    """
    # --- Startup ---
    print("=" * 50)
    print("[Agent Service] Starting up...")
    print("=" * 50)

    # Start gRPC server
    grpc_server = await start_grpc_server()

    # Start Kafka consumer as a background task
    kafka_task = asyncio.create_task(start_kafka_consumer())

    print("[Agent Service] All components started successfully!")

    yield  # App is running

    # --- Shutdown ---
    print("[Agent Service] Shutting down...")
    kafka_task.cancel()
    try:
        await kafka_task
    except asyncio.CancelledError:
        pass
    await grpc_server.stop(grace=5)
    print("[Agent Service] Shutdown complete.")


app = FastAPI(
    title="Smart Traffic Agent Service",
    description="Multi-Agent AI service for intelligent traffic navigation",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """HTTP health check endpoint."""
    return {"status": "healthy", "service": "agent-service"}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )

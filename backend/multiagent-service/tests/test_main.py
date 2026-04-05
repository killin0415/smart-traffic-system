"""Unit tests for the FastAPI application endpoints."""
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from main import app


@pytest.fixture(autouse=True)
def _mock_lifespan_deps():
    """Mock external dependencies triggered by the app lifespan."""
    with (
        patch("main.seed_road_network", new_callable=AsyncMock),
        patch("main.start_kafka_consumer", new_callable=AsyncMock),
    ):
        yield


@pytest.mark.asyncio
async def test_health_endpoint_returns_healthy():
    """GET /health should return status healthy."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "multiagent-service"


@pytest.mark.asyncio
async def test_health_endpoint_returns_json():
    """GET /health should return JSON content type."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert "application/json" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_nonexistent_route_returns_404():
    """GET /nonexistent should return 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/nonexistent")

    assert response.status_code == 404

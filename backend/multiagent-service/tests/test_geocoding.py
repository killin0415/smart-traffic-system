"""Geocoding tests (Nominatim mocked via httpx.MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from src.agents import geocoding


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Avoid real sleeps between tests."""
    geocoding._last_request_at = 0.0
    geocoding.MIN_REQUEST_INTERVAL_SEC = 0.0
    yield
    geocoding.MIN_REQUEST_INTERVAL_SEC = 1.0


def _patch_httpx(monkeypatch, handler):
    """Replace httpx.AsyncClient with one backed by MockTransport running `handler`."""
    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(geocoding.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_geocode_success(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q")
        captured["user_agent"] = request.headers.get("User-Agent")
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "22.618",
                    "lon": "120.308",
                    "display_name": "夢時代購物中心, 高雄市",
                }
            ],
        )

    _patch_httpx(monkeypatch, handler)

    result = await geocoding.geocode_location("夢時代")
    assert result is not None
    assert result["latitude"] == 22.618
    assert result["longitude"] == 120.308
    assert "夢時代" in result["display_name"]
    assert "高雄" in captured["q"]  # query auto-appends 高雄
    assert captured["user_agent"].startswith("smart-traffic-system")


@pytest.mark.asyncio
async def test_geocode_empty_result(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=[]))
    assert await geocoding.geocode_location("asdfnotaplace") is None


@pytest.mark.asyncio
async def test_geocode_api_error_returns_none(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    assert await geocoding.geocode_location("夢時代") is None


@pytest.mark.asyncio
async def test_geocode_blank_query_returns_none():
    assert await geocoding.geocode_location("") is None
    assert await geocoding.geocode_location("   ") is None


@pytest.mark.asyncio
async def test_geocode_does_not_double_append_keyword(monkeypatch):
    """Query already containing 高雄 should NOT have it appended again."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q")
        return httpx.Response(200, json=[{"lat": "22.6", "lon": "120.3", "display_name": "高雄"}])

    _patch_httpx(monkeypatch, handler)
    await geocoding.geocode_location("高雄火車站")
    assert captured["q"] == "高雄火車站"

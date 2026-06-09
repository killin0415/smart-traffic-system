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
async def test_geocode_success_returns_list(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q")
        captured["limit"] = request.url.params.get("limit")
        captured["user_agent"] = request.headers.get("User-Agent")
        return httpx.Response(
            200,
            json=[
                {"lat": "25.0478", "lon": "121.5170", "display_name": "台北車站"},
                {"lat": "25.0413", "lon": "121.5645", "display_name": "台北市中心"},
            ],
        )

    _patch_httpx(monkeypatch, handler)

    result = await geocoding.geocode_location("台北車站", city_hint="台北", limit=5)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["latitude"] == 25.0478
    assert result[0]["longitude"] == 121.5170
    assert "台北車站" in result[0]["display_name"]
    assert captured["q"] == "台北車站 台北"
    assert captured["limit"] == "5"
    assert captured["user_agent"].startswith("smart-traffic-system")


@pytest.mark.asyncio
async def test_geocode_no_city_hint_does_not_append_anything(monkeypatch):
    """When city_hint is None / empty, the query is sent verbatim with no suffix."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q")
        return httpx.Response(200, json=[{"lat": "25.0", "lon": "121.5", "display_name": "x"}])

    _patch_httpx(monkeypatch, handler)
    await geocoding.geocode_location("中正紀念堂")
    assert captured["q"] == "中正紀念堂"

    captured.clear()
    await geocoding.geocode_location("中正紀念堂", city_hint=None)
    assert captured["q"] == "中正紀念堂"

    captured.clear()
    await geocoding.geocode_location("中正紀念堂", city_hint="")
    assert captured["q"] == "中正紀念堂"

    captured.clear()
    await geocoding.geocode_location("中正紀念堂", city_hint="   ")
    assert captured["q"] == "中正紀念堂"


@pytest.mark.asyncio
async def test_geocode_with_city_hint_appends_to_query(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q")
        return httpx.Response(200, json=[{"lat": "25.0", "lon": "121.5", "display_name": "x"}])

    _patch_httpx(monkeypatch, handler)
    await geocoding.geocode_location("中正紀念堂", city_hint="台北")
    assert captured["q"].endswith("台北")
    assert "中正紀念堂" in captured["q"]


@pytest.mark.asyncio
async def test_geocode_empty_result_returns_empty_list(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=[]))
    result = await geocoding.geocode_location("asdfnotaplace")
    assert result == []


@pytest.mark.asyncio
async def test_geocode_api_error_returns_empty_list(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    result = await geocoding.geocode_location("台北車站")
    assert result == []


@pytest.mark.asyncio
async def test_geocode_blank_query_returns_empty_list():
    assert await geocoding.geocode_location("") == []
    assert await geocoding.geocode_location("   ") == []


@pytest.mark.asyncio
async def test_geocode_limit_clamped_to_max(monkeypatch):
    """limit > MAX_LIMIT (10) is clamped to 10 before being sent to Nominatim."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json=[])

    _patch_httpx(monkeypatch, handler)
    await geocoding.geocode_location("台北車站", limit=50)
    assert captured["limit"] == "10"


@pytest.mark.asyncio
async def test_geocode_limit_floor(monkeypatch):
    """limit < 1 is clamped up to 1 (Nominatim rejects limit=0)."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json=[])

    _patch_httpx(monkeypatch, handler)
    await geocoding.geocode_location("台北車站", limit=0)
    assert captured["limit"] == "1"

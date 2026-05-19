"""
Geocoding via OpenStreetMap Nominatim.

City attachment is caller-driven via `city_hint` (no hardcoded city). Respects
Nominatim usage policy: custom User-Agent + >= 1 second between requests.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "smart-traffic-system/0.1 (capstone-project; contact: swordfire1000@gmail.com)"
MIN_REQUEST_INTERVAL_SEC = 1.0
REQUEST_TIMEOUT_SEC = 10.0
MAX_LIMIT = 10

_last_request_at: float = 0.0
_rate_lock = asyncio.Lock()


async def geocode_location(
    query: str,
    city_hint: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Resolve a location query to a list of `{latitude, longitude, display_name}`.

    - `city_hint`: when non-empty, appended to the query (e.g. `"中正紀念堂 台北"`).
      When None / empty / whitespace, no city suffix is added.
    - `limit`: clamped to 1..MAX_LIMIT (10).

    Returns at most `limit` entries; empty list on no match or upstream error.
    """
    if not query or not query.strip():
        return []

    effective_query = query.strip()
    if city_hint and city_hint.strip():
        effective_query = f"{effective_query} {city_hint.strip()}"

    effective_limit = max(1, min(limit, MAX_LIMIT))

    async with _rate_lock:
        await _wait_for_rate_limit()

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
                response = await client.get(
                    NOMINATIM_URL,
                    params={"q": effective_query, "format": "json", "limit": effective_limit},
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.8,en;q=0.5"},
                )
        except httpx.HTTPError as e:
            logger.error("Nominatim request failed for %r: %s", query, e)
            return []
        finally:
            _mark_request_time()

    if response.status_code != 200:
        logger.error("Nominatim returned HTTP %s for %r", response.status_code, query)
        return []

    try:
        raw_results = response.json()
    except ValueError:
        logger.error("Nominatim returned non-JSON body for %r", query)
        return []

    if not raw_results:
        return []

    out: list[dict] = []
    for item in raw_results[:effective_limit]:
        try:
            out.append({
                "latitude": float(item["lat"]),
                "longitude": float(item["lon"]),
                "display_name": item.get("display_name", ""),
            })
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Nominatim entry missing fields for %r: %s", query, e)
            continue
    return out


async def _wait_for_rate_limit() -> None:
    """Block until at least MIN_REQUEST_INTERVAL_SEC has elapsed since the previous request."""
    global _last_request_at
    now = time.monotonic()
    delta = now - _last_request_at
    if delta < MIN_REQUEST_INTERVAL_SEC:
        await asyncio.sleep(MIN_REQUEST_INTERVAL_SEC - delta)


def _mark_request_time() -> None:
    global _last_request_at
    _last_request_at = time.monotonic()

"""
Geocoding via OpenStreetMap Nominatim.

Auto-appends 「高雄」 to improve precision for capstone scenario queries.
Respects Nominatim usage policy: custom User-Agent + >= 1 second between requests.
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

_last_request_at: float = 0.0
_rate_lock = asyncio.Lock()


async def geocode_location(query: str) -> dict | None:
    """Resolve a Chinese location query (e.g. 「夢時代」) to lat/lng/display_name.

    Returns {"latitude": float, "longitude": float, "display_name": str} or None
    if there is no match or the API errored.
    """
    if not query or not query.strip():
        return None

    normalised = query.strip()
    if "高雄" not in normalised:
        normalised = f"{normalised} 高雄"

    async with _rate_lock:
        await _wait_for_rate_limit()

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
                response = await client.get(
                    NOMINATIM_URL,
                    params={"q": normalised, "format": "json", "limit": 1},
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.8,en;q=0.5"},
                )
        except httpx.HTTPError as e:
            logger.error("Nominatim request failed for %r: %s", query, e)
            return None
        finally:
            _mark_request_time()

    if response.status_code != 200:
        logger.error("Nominatim returned HTTP %s for %r", response.status_code, query)
        return None

    try:
        results = response.json()
    except ValueError:
        logger.error("Nominatim returned non-JSON body for %r", query)
        return None

    if not results:
        return None

    top = results[0]
    try:
        return {
            "latitude": float(top["lat"]),
            "longitude": float(top["lon"]),
            "display_name": top.get("display_name", ""),
        }
    except (KeyError, TypeError, ValueError) as e:
        logger.error("Nominatim response missing fields for %r: %s", query, e)
        return None


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

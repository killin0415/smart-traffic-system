"""TDX (Transport Data eXchange) OAuth client_credentials helper.

The TDX API requires a short-lived Bearer token obtained from the OIDC
client_credentials grant. We cache the token in a module-level global and
refresh it 60 seconds before expiry so concurrent callers don't stampede
the token endpoint.

Credentials are read from env vars `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET`.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

TDX_TOKEN_URL = (
    "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
)

_token: str | None = None
_token_expires_at: float = 0.0


def _credentials() -> tuple[str, str]:
    cid = os.getenv("TDX_CLIENT_ID")
    csec = os.getenv("TDX_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError(
            "TDX_CLIENT_ID / TDX_CLIENT_SECRET env vars are required for TDX API"
        )
    return cid, csec


def reset_token_cache() -> None:
    """Clear the cached token. Tests use this; production never needs it."""
    global _token, _token_expires_at
    _token = None
    _token_expires_at = 0.0


async def get_access_token(client: httpx.AsyncClient | None = None) -> str:
    """Return a valid TDX access token, refreshing from cache when needed."""
    global _token, _token_expires_at

    now = time.time()
    if _token and now < _token_expires_at - 60:
        return _token

    cid, csec = _credentials()
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30)
    try:
        response = await client.post(
            TDX_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": csec,
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            await client.aclose()

    _token = payload["access_token"]
    _token_expires_at = now + int(payload.get("expires_in", 3600))
    logger.info("TDX token refreshed; valid for %ds", int(payload.get("expires_in", 3600)))
    return _token

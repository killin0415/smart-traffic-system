"""Redis-backed per-session chat history storage."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis.exceptions

from src.cache import redis_client

logger = logging.getLogger(__name__)

KEY_PREFIX = "chat:history:"
DEFAULT_MAX_TURNS = 10
DEFAULT_TTL_SECONDS = 1800


def _key(session_id: str) -> str:
    return f"{KEY_PREFIX}{session_id}"


class ChatHistoryStore:
    """Store user/assistant chat turns in Redis LIST keys."""

    def __init__(self, redis_client: Any, *, max_turns: int, ttl_seconds: int) -> None:
        self._redis = redis_client
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds

    async def load(self, session_id: str) -> list[dict]:
        """Load chat history in oldest-to-newest order."""
        try:
            entries = await self._redis.lrange(_key(session_id), 0, -1)
            return [json.loads(entry) for entry in entries]
        except (redis.exceptions.RedisError, ConnectionError) as exc:
            logger.warning("failed to load chat history for session_id=%s: %s", session_id, exc)
            return []

    async def append_turn(self, session_id: str, user_content: str, assistant_content: str) -> None:
        """Append one user/assistant turn and retain only the latest max_turns."""
        key = _key(session_id)
        user_entry = json.dumps({"role": "user", "content": user_content}, ensure_ascii=False)
        assistant_entry = json.dumps({"role": "assistant", "content": assistant_content}, ensure_ascii=False)
        limit = self._max_turns * 2

        try:
            pipe = self._redis.pipeline()
            pipe.rpush(key, user_entry)
            pipe.rpush(key, assistant_entry)
            pipe.ltrim(key, -limit, -1)
            pipe.expire(key, self._ttl_seconds)
            await pipe.execute()
        except (redis.exceptions.RedisError, ConnectionError) as exc:
            logger.warning("failed to append chat history for session_id=%s: %s", session_id, exc)


def _parse_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default
    return value


def build_chat_history_store_from_env() -> ChatHistoryStore:
    """Build a ChatHistoryStore from environment configuration."""
    max_turns = _parse_env_int("CHAT_HISTORY_MAX_TURNS", DEFAULT_MAX_TURNS)
    ttl_seconds = _parse_env_int("CHAT_HISTORY_TTL_SECONDS", DEFAULT_TTL_SECONDS)
    return ChatHistoryStore(redis_client, max_turns=max_turns, ttl_seconds=ttl_seconds)

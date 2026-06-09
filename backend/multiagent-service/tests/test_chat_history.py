"""Unit tests for Redis-backed chat history storage."""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio
import redis.exceptions
from fakeredis.aioredis import FakeRedis

from src.agents.chat_history import ChatHistoryStore


@pytest_asyncio.fixture
async def redis_client():
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def history_store(redis_client):
    return ChatHistoryStore(redis_client, max_turns=2, ttl_seconds=60)


@pytest.mark.asyncio
async def test_load_empty_session_returns_empty_list(history_store):
    assert await history_store.load("never-seen") == []


@pytest.mark.asyncio
async def test_append_turn_then_load_preserves_write_order(history_store):
    await history_store.append_turn("s1", "first user", "first assistant")
    await history_store.append_turn("s1", "second user", "second assistant")

    assert await history_store.load("s1") == [
        {"role": "user", "content": "first user"},
        {"role": "assistant", "content": "first assistant"},
        {"role": "user", "content": "second user"},
        {"role": "assistant", "content": "second assistant"},
    ]


@pytest.mark.asyncio
async def test_append_turn_trims_to_latest_max_turns(redis_client):
    store = ChatHistoryStore(redis_client, max_turns=2, ttl_seconds=60)

    for idx in range(3):
        await store.append_turn("trimmed", f"user-{idx}", f"assistant-{idx}")

    assert await redis_client.llen("chat:history:trimmed") == 4
    assert await store.load("trimmed") == [
        {"role": "user", "content": "user-1"},
        {"role": "assistant", "content": "assistant-1"},
        {"role": "user", "content": "user-2"},
        {"role": "assistant", "content": "assistant-2"},
    ]


@pytest.mark.asyncio
async def test_append_turn_sets_ttl(redis_client):
    ttl_seconds = 60
    store = ChatHistoryStore(redis_client, max_turns=2, ttl_seconds=ttl_seconds)

    await store.append_turn("ttl-session", "hi", "hello")

    ttl = await redis_client.ttl("chat:history:ttl-session")
    assert 0 < ttl <= ttl_seconds


@pytest.mark.asyncio
async def test_session_ids_are_isolated(history_store):
    await history_store.append_turn("session-a", "a-user", "a-assistant")
    await history_store.append_turn("session-b", "b-user", "b-assistant")

    assert await history_store.load("session-a") == [
        {"role": "user", "content": "a-user"},
        {"role": "assistant", "content": "a-assistant"},
    ]
    assert await history_store.load("session-b") == [
        {"role": "user", "content": "b-user"},
        {"role": "assistant", "content": "b-assistant"},
    ]


@pytest.mark.asyncio
async def test_append_turn_logs_warning_and_swallows_pipeline_failure(redis_client, caplog, monkeypatch):
    store = ChatHistoryStore(redis_client, max_turns=2, ttl_seconds=60)

    class FailingPipeline:
        def rpush(self, *_args, **_kwargs):
            return self

        def ltrim(self, *_args, **_kwargs):
            return self

        def expire(self, *_args, **_kwargs):
            return self

        async def execute(self):
            raise redis.exceptions.RedisError("pipeline down")

    monkeypatch.setattr(redis_client, "pipeline", lambda: FailingPipeline())

    with caplog.at_level(logging.WARNING):
        await store.append_turn("s1", "hi", "hello")

    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_load_logs_warning_and_returns_empty_list_on_redis_failure(redis_client, caplog, monkeypatch):
    store = ChatHistoryStore(redis_client, max_turns=2, ttl_seconds=60)

    async def failing_lrange(*_args, **_kwargs):
        raise redis.exceptions.RedisError("lrange down")

    monkeypatch.setattr(redis_client, "lrange", failing_lrange)

    with caplog.at_level(logging.WARNING):
        assert await store.load("s1") == []

    assert any(record.levelno == logging.WARNING for record in caplog.records)

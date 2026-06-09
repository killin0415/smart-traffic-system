"""Tests for the DeepSeek-backed ChatAgent: tool-call path, chitchat, fallback, and timeout."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from src.agents.chat_agent import (
    FALLBACK_REPLY,
    GENERIC_ERROR_REPLY,
    SERVICE_NOT_READY_REPLY,
    ChatAgent,
)
from src.agents.chat_history import ChatHistoryStore
from src.kafka import runtime as kafka_runtime


@pytest.fixture
def _wired_runtime():
    """Wire kafka_runtime so ChatAgent thinks the service is ready."""

    class _FakeSessionCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_factory = lambda: _FakeSessionCtx()
    kafka_runtime.set_runtime(graph=MagicMock(), loop=MagicMock(), session_factory=fake_factory)
    yield
    kafka_runtime.set_runtime(graph=None, loop=None, session_factory=None)


def _make_response(*, content: str | None = None, tool_calls: list[tuple[str, str, str]] | None = None):
    """Construct a fake OpenAI ChatCompletion response object.

    `tool_calls` is a list of `(id, name, arguments_json)` tuples.
    """
    msg = SimpleNamespace(content=content, tool_calls=None)
    if tool_calls:
        msg.tool_calls = [
            SimpleNamespace(
                id=tcid,
                type="function",
                function=SimpleNamespace(name=name, arguments=args_json),
            )
            for tcid, name, args_json in tool_calls
        ]
    choice = SimpleNamespace(message=msg, index=0, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


@pytest_asyncio.fixture
async def redis_client():
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _make_agent_with_mock_client(create_side_effect, *, history_store=None):
    """Build a ChatAgent whose client.chat.completions.create is mocked."""
    agent = ChatAgent(api_key="dummy-key", history_store=history_store)
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=create_side_effect)
    agent._client = fake_client
    return agent, fake_client


# ---------- 1. Fallback when no API key ----------


@pytest.mark.asyncio
async def test_fallback_when_api_key_missing():
    """No API key → log warning, return stub reply, route_payload=None."""
    agent = ChatAgent(api_key=None)
    out = await agent.agenerate("我從台北車站想去信義誠品")
    assert out == {"reply": FALLBACK_REPLY, "route_payload": None}


# ---------- 2. Service not ready ----------


@pytest.mark.asyncio
async def test_service_not_ready_when_runtime_unwired():
    kafka_runtime.set_runtime(graph=None, loop=None, session_factory=None)
    agent = ChatAgent(api_key="dummy-key")
    agent._client = MagicMock()  # would otherwise fall back
    out = await agent.agenerate("hi")
    assert out == {"reply": SERVICE_NOT_READY_REPLY, "route_payload": None}


# ---------- 3. Chitchat (no tool call) ----------


@pytest.mark.asyncio
async def test_chitchat_returns_reply_only(_wired_runtime):
    async def fake_create(**kwargs):
        return _make_response(content="你好！有什麼可以幫忙的嗎？")

    agent, _ = _make_agent_with_mock_client(fake_create)
    out = await agent.agenerate("你好")
    assert out["reply"] == "你好！有什麼可以幫忙的嗎？"
    assert out["route_payload"] is None


# ---------- 4. Route intent triggers tool, payload captured ----------


@pytest.mark.asyncio
async def test_route_intent_invokes_tool_and_captures_payload(_wired_runtime):
    """Multi-turn loop: model asks for tool → we execute → model finishes."""
    fake_route_result = {
        "routes": [
            {
                "path": [1, 2, 3],
                "edges": [10, 20],
                "road_names": ["市民大道", "忠孝東路"],
                "estimated_time_min": 12.5,
                "distance_km": 4.2,
                "speed_cameras": [],
            }
        ],
        "error": None,
    }

    call_count = {"n": 0}

    async def fake_create(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First turn: model issues a tool call.
            return _make_response(
                content=None,
                tool_calls=[
                    (
                        "call_1",
                        "plan_route",
                        json.dumps(
                            {
                                "origin_lat": 25.0478,
                                "origin_lng": 121.5170,
                                "dest_lat": 25.0418,
                                "dest_lng": 121.5654,
                                "top_k": 3,
                            }
                        ),
                    )
                ],
            )
        # Second turn: model responds with final summary.
        return _make_response(content="已為您規劃 1 條路線，預估 12.5 分鐘。")

    with patch(
        "src.agents.chat_agent.plan_route", new=AsyncMock(return_value=fake_route_result)
    ) as mock_plan_route:
        agent, _ = _make_agent_with_mock_client(fake_create)
        out = await agent.agenerate("我從台北車站想去信義誠品")

    assert out["reply"] == "已為您規劃 1 條路線，預估 12.5 分鐘。"
    assert out["route_payload"] == fake_route_result
    mock_plan_route.assert_awaited_once()
    # Should make exactly two LLM calls (tool turn + reply turn).
    assert call_count["n"] == 2


# ---------- 5. Tool returns error → no payload captured ----------


@pytest.mark.asyncio
async def test_tool_error_result_not_captured(_wired_runtime):
    call_count = {"n": 0}

    async def fake_create(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_response(
                tool_calls=[
                    (
                        "call_1",
                        "plan_route",
                        json.dumps({"origin_lat": 25.0, "origin_lng": 121.5, "dest_lat": 25.1, "dest_lng": 121.6}),
                    )
                ],
            )
        return _make_response(content="抱歉，找不到路線。")

    with patch(
        "src.agents.chat_agent.plan_route",
        new=AsyncMock(return_value={"routes": [], "error": "no path found between origin and destination"}),
    ):
        agent, _ = _make_agent_with_mock_client(fake_create)
        out = await agent.agenerate("我想從一個怪地方去另一個怪地方")

    assert out["reply"] == "抱歉，找不到路線。"
    assert out["route_payload"] is None


# ---------- 6. Timeout ----------


@pytest.mark.asyncio
async def test_timeout_returns_generic_error(_wired_runtime):
    async def hanging_create(**kwargs):
        await asyncio.sleep(10)  # never returns within timeout
        return _make_response(content="should never get here")

    agent, _ = _make_agent_with_mock_client(hanging_create)
    agent._timeout = 0.05  # tiny timeout for fast test
    out = await agent.agenerate("你好")
    assert out == {"reply": GENERIC_ERROR_REPLY, "route_payload": None}


# ---------- 7. API error ----------


@pytest.mark.asyncio
async def test_api_error_returns_generic_error(_wired_runtime):
    async def boom(**kwargs):
        raise RuntimeError("API outage")

    agent, _ = _make_agent_with_mock_client(boom)
    out = await agent.agenerate("你好")
    assert out == {"reply": GENERIC_ERROR_REPLY, "route_payload": None}


# ---------- 8. Tool-loop limit guard ----------


@pytest.mark.asyncio
async def test_max_tool_loops_guard(_wired_runtime):
    """If the model keeps requesting tool calls, the agent caps total LLM rounds."""

    async def always_tool(**kwargs):
        return _make_response(
            tool_calls=[
                (
                    "call_x",
                    "plan_route",
                    json.dumps({"origin_lat": 25.0, "origin_lng": 121.5, "dest_lat": 25.1, "dest_lng": 121.6}),
                )
            ],
        )

    with patch(
        "src.agents.chat_agent.plan_route",
        new=AsyncMock(return_value={"routes": [{"path": [1], "edges": [], "road_names": [], "estimated_time_min": 0, "distance_km": 0, "speed_cameras": []}], "error": None}),
    ) as mock_plan:
        agent, fake_client = _make_agent_with_mock_client(always_tool)
        agent._max_tool_loops = 2
        out = await agent.agenerate("loop please")

    # Loop exhausted: agent returns its overflow message but still surfaces last captured payload.
    assert "上限" in out["reply"]
    assert out["route_payload"] is not None
    # Exactly max_tool_loops LLM calls were made.
    assert fake_client.chat.completions.create.await_count == 2
    # Tool was executed each round.
    assert mock_plan.await_count == 2


# ---------- 9. Chat history integration ----------


@pytest.mark.asyncio
async def test_session_history_is_prepended_between_system_and_current_user(_wired_runtime, redis_client):
    store = ChatHistoryStore(redis_client, max_turns=3, ttl_seconds=60)
    await store.append_turn("s-history", "上一輪 user", "上一輪 assistant")
    captured_messages = []

    async def fake_create(**kwargs):
        captured_messages.append([message.copy() for message in kwargs["messages"]])
        return _make_response(content="本輪回答")

    agent, _ = _make_agent_with_mock_client(fake_create, history_store=store)

    await agent.agenerate("本輪 user", session_id="s-history")

    messages = captured_messages[0]
    assert messages == [
        {"role": "system", "content": messages[0]["content"]},
        {"role": "user", "content": "上一輪 user"},
        {"role": "assistant", "content": "上一輪 assistant"},
        {"role": "user", "content": "本輪 user"},
    ]


@pytest.mark.asyncio
async def test_missing_session_id_keeps_stateless_messages_and_does_not_write_redis(_wired_runtime, redis_client):
    store = ChatHistoryStore(redis_client, max_turns=3, ttl_seconds=60)
    captured_messages = []

    async def fake_create(**kwargs):
        captured_messages.append([message.copy() for message in kwargs["messages"]])
        return _make_response(content="stateless reply")

    agent, _ = _make_agent_with_mock_client(fake_create, history_store=store)

    await agent.agenerate("你好")

    messages = captured_messages[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[-1]["content"] == "你好"
    assert await redis_client.keys("chat:history:*") == []


@pytest.mark.asyncio
async def test_history_store_none_with_session_id_keeps_stateless_messages(_wired_runtime):
    captured_messages = []

    async def fake_create(**kwargs):
        captured_messages.append([message.copy() for message in kwargs["messages"]])
        return _make_response(content="stateless reply")

    agent, _ = _make_agent_with_mock_client(fake_create, history_store=None)

    await agent.agenerate("你好", session_id="s-no-store")

    messages = captured_messages[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[-1]["content"] == "你好"


@pytest.mark.asyncio
async def test_successful_tool_call_turn_appends_only_user_and_final_assistant_text(_wired_runtime, redis_client):
    store = ChatHistoryStore(redis_client, max_turns=5, ttl_seconds=60)
    call_count = {"n": 0}

    async def fake_create(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_response(
                tool_calls=[
                    (
                        "call_1",
                        "plan_route",
                        json.dumps(
                            {
                                "origin_lat": 25.0478,
                                "origin_lng": 121.5170,
                                "dest_lat": 25.0418,
                                "dest_lng": 121.5654,
                            }
                        ),
                    )
                ],
            )
        return _make_response(content="已為您規劃路線。")

    with patch(
        "src.agents.chat_agent.plan_route",
        new=AsyncMock(return_value={"routes": [{"path": [1], "edges": []}], "error": None}),
    ):
        agent, _ = _make_agent_with_mock_client(fake_create, history_store=store)
        await agent.agenerate("幫我規劃路線", session_id="s-tool")

    entries = await store.load("s-tool")
    assert entries == [
        {"role": "user", "content": "幫我規劃路線"},
        {"role": "assistant", "content": "已為您規劃路線。"},
    ]
    assert all("tool_calls" not in entry for entry in entries)
    assert await redis_client.llen("chat:history:s-tool") == 2


@pytest.mark.asyncio
async def test_timeout_reply_does_not_append_history(_wired_runtime, redis_client):
    store = ChatHistoryStore(redis_client, max_turns=3, ttl_seconds=60)
    await store.append_turn("s-timeout", "before user", "before assistant")

    async def hanging_create(**kwargs):
        await asyncio.sleep(10)
        return _make_response(content="should never get here")

    agent, _ = _make_agent_with_mock_client(hanging_create, history_store=store)
    agent._timeout = 0.05

    out = await agent.agenerate("會 timeout", session_id="s-timeout")

    assert out == {"reply": GENERIC_ERROR_REPLY, "route_payload": None}
    assert await store.load("s-timeout") == [
        {"role": "user", "content": "before user"},
        {"role": "assistant", "content": "before assistant"},
    ]


@pytest.mark.asyncio
async def test_history_trims_oldest_turn_after_max_turns_plus_one(_wired_runtime, redis_client):
    max_turns = 2
    store = ChatHistoryStore(redis_client, max_turns=max_turns, ttl_seconds=60)

    async def fake_create(**kwargs):
        user_content = kwargs["messages"][-1]["content"]
        return _make_response(content=f"reply to {user_content}")

    agent, _ = _make_agent_with_mock_client(fake_create, history_store=store)

    for idx in range(max_turns + 1):
        await agent.agenerate(f"user-{idx}", session_id="s-trim")

    assert await redis_client.llen("chat:history:s-trim") == max_turns * 2
    assert await store.load("s-trim") == [
        {"role": "user", "content": "user-1"},
        {"role": "assistant", "content": "reply to user-1"},
        {"role": "user", "content": "user-2"},
        {"role": "assistant", "content": "reply to user-2"},
    ]

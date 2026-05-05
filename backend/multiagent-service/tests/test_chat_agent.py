"""Tests for the DeepSeek-backed ChatAgent: tool-call path, chitchat, fallback, and timeout."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.chat_agent import (
    FALLBACK_REPLY,
    GENERIC_ERROR_REPLY,
    SERVICE_NOT_READY_REPLY,
    ChatAgent,
)
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


def _make_agent_with_mock_client(create_side_effect):
    """Build a ChatAgent whose client.chat.completions.create is mocked."""
    agent = ChatAgent(api_key="dummy-key")
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

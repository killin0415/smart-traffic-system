"""
DeepSeek chat agent for multiagent-service.

Uses the OpenAI-compatible DeepSeek API so `chat.request` messages can be
answered naturally and, when the user expresses a routing intent, the
routing tool (`plan_route`) is invoked automatically. The agent returns the
captured `plan_route` payload alongside its natural-language reply so the
caller can deliver both via `chat.response`.

Falls back to a stub reply when `DEEPSEEK_API_KEY` is missing — the service
must still start up cleanly without an API key. Costs: at `max_tokens=500`
on `deepseek-v4-flash`, a typical demo turn is ≪ US$0.001; we additionally
cap each turn at `max_tool_loops` LLM round-trips to keep wild loops from
running up the bill.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from openai import AsyncOpenAI

from src.mcp_servers.routing_tool import plan_route

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "30"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "500"))
DEFAULT_MAX_TOOL_LOOPS = int(os.getenv("DEEPSEEK_MAX_TOOL_LOOPS", "3"))


# NOTE: This intentionally asks the LLM to estimate WGS84 coordinates for place
# names rather than calling a separate geocoding service — capstone demo scope
# does not include geocoding. If a real geocoder is added later, drop the
# "guess coordinates" sentence and have the agent call the geocoder first.
SYSTEM_INSTRUCTION = (
    "你是一個台北智慧交通助手。當使用者表達路線規劃意圖（例如「從 A 到 B」、"
    "「怎麼去 X」），你必須呼叫 `plan_route` 工具，並傳入 origin_lat/origin_lng/"
    "dest_lat/dest_lng（WGS84 經緯度）。如果使用者直接給了座標就用那組座標，"
    "否則自行推估台北市區內地點的座標。拿到工具回傳後，用一兩句話總結（路線數、"
    "第一條的距離與預估時間）。如果只是閒聊就直接回應，不要呼叫工具。"
)

GENERIC_ERROR_REPLY = "目前服務忙線，請稍後再試。"
FALLBACK_REPLY = "[Multiagent Service] 已收到您的訊息，但 AI 推論功能尚未啟用。"
SERVICE_NOT_READY_REPLY = "服務啟動中，請稍後再試。"


# OpenAI-format JSON-Schema for the plan_route tool. Keep this in lockstep with
# `PlanRouteInput` in src/mcp_servers/routing_tool.py — the routing tool itself
# uses Pydantic for validation; this schema is what DeepSeek/OpenAI consumes.
PLAN_ROUTE_TOOL = {
    "type": "function",
    "function": {
        "name": "plan_route",
        "description": (
            "Plan up to top_k driving routes between two GPS coordinates in "
            "Taipei. Returns {routes: [...], error: str | null}. Use this "
            "whenever the user wants navigation/route planning."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin_lat": {"type": "number", "description": "Origin latitude (WGS84)"},
                "origin_lng": {"type": "number", "description": "Origin longitude (WGS84)"},
                "dest_lat": {"type": "number", "description": "Destination latitude (WGS84)"},
                "dest_lng": {"type": "number", "description": "Destination longitude (WGS84)"},
                "top_k": {
                    "type": "integer",
                    "description": "Number of route alternatives to return (1-10, default 3)",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["origin_lat", "origin_lng", "dest_lat", "dest_lng"],
        },
    },
}


class ChatAgent:
    """DeepSeek-backed chat agent with optional routing tool-calling."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tool_loops: int = DEFAULT_MAX_TOOL_LOOPS,
        base_url: str = DEEPSEEK_BASE_URL,
    ) -> None:
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._max_tool_loops = max_tool_loops
        if api_key:
            self._client: AsyncOpenAI | None = AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            logger.warning("DEEPSEEK_API_KEY not set — chat agent will return fallback stub")
            self._client = None

    @property
    def is_available(self) -> bool:
        return self._client is not None

    async def _execute_tool(self, name: str, args_json: str, captured: dict[str, Any]) -> str:
        """Run a single tool call, mutate `captured`, return the JSON-serialised result for the model."""
        if name != "plan_route":
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid tool arguments: {exc}"})
        try:
            result = await plan_route(**args)
        except Exception as exc:
            logger.exception("plan_route tool execution failed: %s", exc)
            return json.dumps({"routes": [], "error": str(exc)})
        if not result.get("error"):
            # chat.response.route_payload is a single object, so a second
            # successful call within the same turn overwrites the first —
            # warn so this is visible in logs if it ever happens.
            if captured["payload"] is not None:
                logger.warning(
                    "plan_route called multiple times in one chat turn — "
                    "previous payload (%d routes) will be overwritten",
                    len((captured["payload"].get("routes") or [])),
                )
            captured["payload"] = result
        return json.dumps(result, ensure_ascii=False)

    async def agenerate(self, content: str) -> dict[str, Any]:
        """Generate a reply (and optional route payload) for a chat message.

        Returns:
            `{"reply": str, "route_payload": dict | None}`.
        """
        if self._client is None:
            return {"reply": FALLBACK_REPLY, "route_payload": None}

        # Refuse to call the model with no graph wired up — the routing tool
        # would always return "service not ready".
        from src.kafka import runtime as kafka_runtime

        if kafka_runtime.get_graph() is None or kafka_runtime.get_session_factory() is None:
            logger.warning("Chat agent invoked before runtime ready — returning service-not-ready reply")
            return {"reply": SERVICE_NOT_READY_REPLY, "route_payload": None}

        captured: dict[str, Any] = {"payload": None}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": content},
        ]

        try:
            for _ in range(self._max_tool_loops):
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(
                        model=self._model,
                        messages=messages,
                        tools=[PLAN_ROUTE_TOOL],
                        tool_choice="auto",
                        max_tokens=self._max_tokens,
                        stream=False,
                        # Disable DeepSeek thinking mode — saves output tokens
                        # and avoids needing to round-trip `reasoning_content`.
                        extra_body={"thinking": {"type": "disabled"}},
                    ),
                    timeout=self._timeout,
                )
                choice_msg = response.choices[0].message
                # Append assistant's turn to the history so subsequent calls see it.
                assistant_entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": choice_msg.content or "",
                }
                if choice_msg.tool_calls:
                    assistant_entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in choice_msg.tool_calls
                    ]
                messages.append(assistant_entry)

                if not choice_msg.tool_calls:
                    reply = (choice_msg.content or "").strip() or "（已收到您的訊息）"
                    return {"reply": reply, "route_payload": captured["payload"]}

                # Run every requested tool call and feed results back as `tool` messages.
                for tc in choice_msg.tool_calls:
                    tool_output = await self._execute_tool(tc.function.name, tc.function.arguments, captured)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_output,
                        }
                    )
            # Loop exhausted without a final assistant text reply.
            logger.warning("Chat agent hit max_tool_loops=%d", self._max_tool_loops)
            return {
                "reply": "（已執行工具但回覆超過內部上限）",
                "route_payload": captured["payload"],
            }
        except asyncio.TimeoutError:
            logger.error("DeepSeek agent timed out after %.1fs", self._timeout)
            return {"reply": GENERIC_ERROR_REPLY, "route_payload": None}
        except Exception as exc:
            logger.exception("DeepSeek agent call failed: %s", exc)
            return {"reply": GENERIC_ERROR_REPLY, "route_payload": None}


def build_chat_agent_from_env() -> ChatAgent:
    """Construct a ChatAgent honoring `DEEPSEEK_API_KEY` from env."""
    return ChatAgent(api_key=os.getenv("DEEPSEEK_API_KEY"))

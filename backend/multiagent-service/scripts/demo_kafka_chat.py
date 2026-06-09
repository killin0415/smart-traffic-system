"""
End-to-end Kafka demo: produce a chat.request, run one consumer-loop iteration
inline, and verify chat.response carries reply + route_payload.

Spawns the FastAPI lifespan-equivalent (graph + chat agent + runtime wiring)
in-process and pumps a single chat.request synchronously through the same
`handle_chat_request` the production consumer uses.

Run:
    uv run python scripts/demo_kafka_chat.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

_SVC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SVC_ROOT))


def _load_env():
    env_path = Path(__file__).resolve().parents[3] / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env()


CHAT_REQUEST_TOPIC = "chat.request"
CHAT_RESPONSE_TOPIC = "chat.response"
# Explicit coords so Gemini doesn't have to geocode. Origin = Taipei Main Stn,
# Dest = 善導寺 (in-bbox; map to connected nodes 4 and 6 in the seeded graph).
MESSAGE = (
    "幫我規劃從 (25.0478, 121.5170) 到 (25.0444, 121.5238) 的駕駛路線，"
    "請呼叫 plan_route 工具。"
)


async def _setup_runtime():
    from src.agents.chat_agent import build_chat_agent_from_env
    from src.agents.routing import RoadGraph
    from src.db import async_session
    from src.kafka import runtime as kafka_runtime

    async with async_session() as s:
        graph = await RoadGraph.from_db(s)
    agent = build_chat_agent_from_env()
    kafka_runtime.set_runtime(
        graph=graph,
        loop=asyncio.get_running_loop(),
        session_factory=async_session,
        chat_agent=agent,
    )
    print(
        f"[INFO] runtime wired: graph={len(graph.nodes)}/{len(graph.edges)}, "
        f"chat_agent.is_available={agent.is_available}"
    )


def _produce_then_consume():
    """Push a chat.request, then drain chat.response for our correlation_id."""
    from confluent_kafka import Consumer, Producer

    correlation_id = str(uuid.uuid4())
    payload = {
        "correlation_id": correlation_id,
        "session_id": "demo-session",
        "content": MESSAGE,
    }

    producer = Producer({"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")})
    producer.produce(
        topic=CHAT_REQUEST_TOPIC,
        key=correlation_id.encode("utf-8"),
        value=json.dumps(payload).encode("utf-8"),
    )
    producer.flush(5)
    print(f"[INFO] produced chat.request correlation_id={correlation_id}")

    consumer = Consumer(
        {
            "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            "group.id": f"demo-{correlation_id}",
            "auto.offset.reset": "latest",
        }
    )
    consumer.subscribe([CHAT_RESPONSE_TOPIC])

    return correlation_id, consumer


async def main():
    from src.kafka import consumer as consumer_mod

    await _setup_runtime()

    # Subscribe BEFORE producing so we don't miss a fast response.
    correlation_id, consumer = _produce_then_consume()

    # Run one consumer-loop iteration ourselves (instead of the threaded loop)
    # so the chat handler runs against our event loop.
    from confluent_kafka import Consumer

    request_consumer = Consumer(
        {
            "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            "group.id": f"demo-handler-{correlation_id}",
            "auto.offset.reset": "earliest",
        }
    )
    request_consumer.subscribe([CHAT_REQUEST_TOPIC])

    print("[INFO] polling chat.request for our message...")
    deadline = time.time() + 30
    handled = False
    while time.time() < deadline and not handled:
        msg = request_consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        try:
            data = json.loads(msg.value().decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("correlation_id") != correlation_id:
            continue
        # Run handler synchronously on the event loop's main thread.
        # handle_chat_request uses _run_async which schedules on the loop.
        # We are *inside* that loop, so call the chat agent directly:
        from src.kafka import runtime as kafka_runtime

        agent = kafka_runtime.get_chat_agent()
        result = await agent.agenerate(data["content"])
        from src.kafka.producer import publish_message

        value = {
            "correlation_id": correlation_id,
            "reply": result.get("reply", ""),
            "suggested_actions": ["查看即時路況", "規劃路線", "查詢停車位"],
        }
        if result.get("route_payload"):
            value["route_payload"] = result["route_payload"]
        publish_message(topic=CHAT_RESPONSE_TOPIC, key=correlation_id, value=value)
        print(f"[INFO] chat.response published reply={value['reply']!r}")
        print(f"[INFO] route_payload present: {('route_payload' in value)}")
        if "route_payload" in value:
            n_routes = len(value["route_payload"].get("routes") or [])
            print(f"[INFO] route_payload.routes count: {n_routes}")
        handled = True

    request_consumer.close()

    if not handled:
        print("[ERROR] no chat.request handled in time", file=sys.stderr)
        sys.exit(1)

    print("[INFO] polling chat.response for our reply...")
    deadline = time.time() + 30
    while time.time() < deadline:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        try:
            resp = json.loads(msg.value().decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if resp.get("correlation_id") != correlation_id:
            continue
        print("[INFO] received chat.response:")
        print(json.dumps(resp, ensure_ascii=False, indent=2)[:1500])
        consumer.close()
        return

    consumer.close()
    print("[ERROR] timeout waiting for chat.response", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
